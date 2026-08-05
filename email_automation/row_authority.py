"""Provider-free primitive contracts for B2 stable row authority."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import RFC_4122, UUID, uuid4


SCHEMA_VERSION = 1
MAX_ROW_BINDINGS = 128
MAX_ROW_AUTHORITY_PLANNED_WRITES = 400
ROW_BINDINGS_HASH_DOMAIN = "sitesift.row.bindings.v1"
THREAD_ROW_BINDING_HASH_DOMAIN = "sitesift.thread.row_binding.v1"
ROW_THREAD_EDGE_ID_DOMAIN = "sitesift.row.thread_edge_id.v1"
ROW_THREAD_EDGE_HASH_DOMAIN = "sitesift.row.thread_edge.v1"
CONTACT_ROW_EDGE_ID_DOMAIN = "sitesift.contact.row_edge_id.v1"
CONTACT_ROW_EDGE_HASH_DOMAIN = "sitesift.contact.row_edge.v1"
CONTACT_ROW_EVIDENCE_ID_DOMAIN = "sitesift.contact.row_evidence_id.v1"
CONTACT_ROW_EVIDENCE_HASH_DOMAIN = "sitesift.contact.row_evidence.v1"
CONTACT_ROW_BINDING_HEAD_HASH_DOMAIN = (
    "sitesift.contact.row_binding_head.v1"
)
CONTACT_ALIAS_HASH_DOMAIN = "sitesift.contact.optout_alias.v1"
CONTACT_TRANSITION_ID_DOMAIN = "sitesift.contact.optout_transition_id.v1"
CONTACT_TRANSITION_REQUEST_HASH_DOMAIN = (
    "sitesift.contact.optout_transition_request.v1"
)
CONTACT_SETTLEMENT_HASH_DOMAIN = "sitesift.contact.optout_settlement.v1"
CONTACT_HEAD_HASH_DOMAIN = "sitesift.contact.optout_head.v1"
CONTACT_FANOUT_ID_DOMAIN = "sitesift.contact.optout_fanout_id.v1"
CONTACT_FANOUT_HEAD_HASH_DOMAIN = "sitesift.contact.optout_fanout_head.v1"
CONTACT_FANOUT_OBLIGATION_HASH_DOMAIN = (
    "sitesift.contact.optout_fanout_obligation.v1"
)
CONTACT_FANOUT_RESULT_HASH_DOMAIN = (
    "sitesift.contact.optout_fanout_result.v1"
)
B1_AUTHORITY_LINK_HASH_DOMAIN = "sitesift.row.b1_authority_link.v1"
B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN = (
    "sitesift.row.b1_authority_link.v2"
)
OPERATOR_ACTION_ID_DOMAIN = "sitesift.row.operator_action_id.v1"
OPERATOR_CLIENT_REQUEST_HASH_DOMAIN = (
    "sitesift.row.operator_client_request.v1"
)
OPERATOR_ACTION_HASH_DOMAIN = "sitesift.row.operator_action.v1"
CLAIM_REQUEST_ID_DOMAIN = "sitesift.row.claim_request_id.v1"
CLAIM_SET_HASH_DOMAIN = "sitesift.row.claim_set.v1"
OWNER_GENERATION_HASH_DOMAIN = "sitesift.row.owner_generation.v1"
LOGICAL_OUTCOME_HASH_DOMAIN = "sitesift.row.logical_outcome.v1"
OUTCOME_EVIDENCE_HASH_DOMAIN = "sitesift.row.outcome_evidence.v1"
OWNER_SETTLEMENT_HASH_DOMAIN = "sitesift.row.owner_settlement.v1"
ROW_AUTHORITY_HEAD_HASH_DOMAIN = "sitesift.row.authority_head.v1"
SOURCE_SETTLEMENT_LINK_HASH_DOMAIN = (
    "sitesift.row.source_settlement_link.v1"
)
MAX_OPAQUE_BYTES = 512
MAX_MAILBOX_BYTES = 320
MAX_FIRESTORE_DOCUMENT_ID_BYTES = 1500
CONTACT_NORMALIZATION_VERSION = "sitesift-mailbox-v1"
MAX_JSON_SAFE_INTEGER = 9007199254740991
MAX_CANONICAL_JSON_DEPTH = 64
MAX_CANONICAL_JSON_NODES = 4096
MAX_CANONICAL_JSON_BYTES = 16 * 1024 * 1024

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_PATTERN = re.compile(
    r"^sitesift\.[a-z0-9][a-z0-9_.-]*\.v[1-9][0-9]*$"
)
_ROW_ID_PATTERN = re.compile(
    r"^sr1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
)


class RowAuthorityError(RuntimeError):
    code = "row_authority_error"


class RowAuthorityRetryable(RowAuthorityError):
    code = "row_authority_retryable"


class RowAuthorityAmbiguous(RowAuthorityError):
    code = "row_authority_ambiguous"


class RowAuthorityConflict(RowAuthorityError):
    code = "row_authority_conflict"


class RowAuthorityConfigError(RowAuthorityError):
    code = "row_authority_config_error"


def _utf8_bytes(value, *, field_name):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RowAuthorityConfigError(
            f"{field_name} must contain valid UTF-8 text"
        ) from exc


def _track_canonical_utf8(encoded, *, path, state):
    state["utf8_bytes"] += len(encoded)
    if state["utf8_bytes"] > MAX_CANONICAL_JSON_BYTES:
        raise RowAuthorityConfigError(
            f"{path} exceeds the canonical JSON byte bound"
        )


def _canonical_json_value(value, *, path, seen, depth, state):
    if depth > MAX_CANONICAL_JSON_DEPTH:
        raise RowAuthorityConfigError(
            f"{path} exceeds the canonical JSON depth bound"
        )
    state["nodes"] += 1
    if state["nodes"] > MAX_CANONICAL_JSON_NODES:
        raise RowAuthorityConfigError(
            f"{path} exceeds the canonical JSON node bound"
        )
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        encoded = _utf8_bytes(value, field_name=path)
        _track_canonical_utf8(encoded, path=path, state=state)
        return value
    if type(value) is int:
        if abs(value) > MAX_JSON_SAFE_INTEGER:
            raise RowAuthorityConfigError(
                f"{path} exceeds the JSON safe-integer bound"
            )
        return value
    if type(value) is float:
        raise RowAuthorityConfigError(f"{path} cannot be a float")
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in seen:
            raise RowAuthorityConfigError(f"{path} contains a cycle")
        seen.add(identity)
        try:
            return [
                _canonical_json_value(
                    item,
                    path=f"{path}[{index}]",
                    seen=seen,
                    depth=depth + 1,
                    state=state,
                )
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    if type(value) is dict:
        identity = id(value)
        if identity in seen:
            raise RowAuthorityConfigError(f"{path} contains a cycle")
        seen.add(identity)
        try:
            normalized = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise RowAuthorityConfigError(
                        f"{path} contains a non-string key"
                    )
                encoded_key = _utf8_bytes(key, field_name=f"{path} key")
                _track_canonical_utf8(
                    encoded_key,
                    path=f"{path} key",
                    state=state,
                )
                normalized[key] = _canonical_json_value(
                    item,
                    path=f"{path}.{key}",
                    seen=seen,
                    depth=depth + 1,
                    state=state,
                )
            return normalized
        finally:
            seen.remove(identity)
    raise RowAuthorityConfigError(
        f"{path} contains unsupported type {type(value).__name__}"
    )


def canonical_json_bytes(value):
    state = {"nodes": 0, "utf8_bytes": 0}
    normalized = _canonical_json_value(
        value,
        path="$",
        seen=set(),
        depth=0,
        state=state,
    )
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_CANONICAL_JSON_BYTES:
        raise RowAuthorityConfigError(
            "canonical JSON output exceeds the byte bound"
        )
    return encoded


def _require_sha256(value, *, field_name):
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise RowAuthorityConfigError(
            f"{field_name} must be a complete lowercase SHA-256 hash"
        )
    return value


def _require_domain(domain):
    if type(domain) is not str:
        raise RowAuthorityConfigError(
            "domain must be a bounded versioned sitesift domain"
        )
    encoded = _utf8_bytes(domain, field_name="domain")
    if len(encoded) > 128 or _DOMAIN_PATTERN.fullmatch(domain) is None:
        raise RowAuthorityConfigError(
            "domain must be a bounded versioned sitesift domain"
        )
    return domain


def domain_hash(domain, payload, *, user_scope_hash):
    checked_domain = _require_domain(domain)
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    if type(payload) is not dict:
        raise RowAuthorityConfigError(
            "domain hash payload must be an exact field dictionary"
        )
    reserved_fields = {"schemaVersion", "userScopeHash"} & payload.keys()
    if reserved_fields:
        raise RowAuthorityConfigError(
            "domain hash payload cannot replace canonical envelope fields"
        )
    material = {
        **payload,
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
    }
    return hashlib.sha256(
        _utf8_bytes(checked_domain, field_name="domain")
        + b"\0"
        + canonical_json_bytes(material)
    ).hexdigest()


def _contains_control(value):
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def _require_firestore_document_id(value, *, field_name):
    if type(value) is not str or not value:
        raise RowAuthorityConfigError(
            f"{field_name} must be a nonempty Firestore document ID"
        )
    if (
        value in {".", ".."}
        or "/" in value
        or _contains_control(value)
        or (
            len(value) >= 4
            and value.startswith("__")
            and value.endswith("__")
        )
    ):
        raise RowAuthorityConfigError(
            f"{field_name} must be a safe Firestore document ID"
        )
    encoded = _utf8_bytes(value, field_name=field_name)
    if len(encoded) > MAX_FIRESTORE_DOCUMENT_ID_BYTES:
        raise RowAuthorityConfigError(
            f"{field_name} exceeds the Firestore document ID byte bound"
        )
    return value


def user_scope_hash(verified_user_id):
    if type(verified_user_id) is not str:
        raise RowAuthorityConfigError(
            "verified_user_id must be an exact string"
        )
    encoded = _utf8_bytes(
        verified_user_id,
        field_name="verified_user_id",
    )
    if (
        not encoded
        or len(encoded) > MAX_OPAQUE_BYTES
        or _contains_control(verified_user_id)
    ):
        raise RowAuthorityConfigError(
            "verified_user_id must be nonempty, bounded, and control-free"
        )
    material = {"verifiedUserId": verified_user_id}
    return hashlib.sha256(
        b"sitesift.user.scope.v1\0" + canonical_json_bytes(material)
    ).hexdigest()


def validate_row_id(value):
    if type(value) is not str or _ROW_ID_PATTERN.fullmatch(value) is None:
        raise RowAuthorityConfigError(
            "row_id must be sr1_ followed by RFC4122 UUIDv4 hex"
        )
    return value


def new_row_id(*, uuid_factory=uuid4):
    value = uuid_factory()
    if (
        not isinstance(value, UUID)
        or value.version != 4
        or value.variant != RFC_4122
    ):
        raise RowAuthorityConfigError(
            "row ID factory must return an RFC4122 UUIDv4"
        )
    return validate_row_id(f"sr1_{value.hex}")


def normalize_contact_mailbox(mailbox):
    if type(mailbox) is not str:
        raise RowAuthorityConfigError("mailbox must be a string")
    normalized = unicodedata.normalize("NFC", mailbox).strip().lower()
    normalized = unicodedata.normalize("NFC", normalized)
    encoded = _utf8_bytes(normalized, field_name="mailbox")
    if (
        not encoded
        or len(encoded) > MAX_MAILBOX_BYTES
        or _contains_control(normalized)
        or normalized.count("@") != 1
    ):
        raise RowAuthorityConfigError(
            "mailbox must be bounded, control-free, and contain one @"
        )
    local_part, domain = normalized.split("@", 1)
    if not local_part or not domain:
        raise RowAuthorityConfigError(
            "mailbox local part and domain must be nonempty"
        )
    canonical_local = local_part.split("+", 1)[0]
    if not canonical_local:
        raise RowAuthorityConfigError(
            "mailbox canonical local part must be nonempty"
        )
    return normalized, f"{canonical_local}@{domain}"


def contact_identity_hash(normalized_mailbox, *, user_scope_hash):
    exact, _canonical = normalize_contact_mailbox(normalized_mailbox)
    if exact != normalized_mailbox:
        raise RowAuthorityConfigError(
            "contact identity hash requires a normalized mailbox"
        )
    payload = {
        "normalizationVersion": CONTACT_NORMALIZATION_VERSION,
        "normalizedMailboxIdentity": normalized_mailbox,
    }
    return domain_hash(
        "sitesift.contact.identity.v1",
        payload,
        user_scope_hash=user_scope_hash,
    )


MAX_PROVIDER_VALUES = 256
MAX_PROVIDER_VALUE_BYTES = 8192
MARKER_KEY = "sitesift_row_id_v1"
MARKER_VISIBILITY = "DOCUMENT"
ROW_LIFECYCLES = frozenset(
    {"active", "nonviable", "deleted", "ambiguous"}
)
OWNER_PRIORITIES = {
    "contact_optout": 3,
    "terminal": 2,
    "human_decision": 1,
}
HEAD_STATES = frozenset({"clear", "claimed", "review_pending", "settled"})
_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"\.(?P<microsecond>[0-9]{6})Z$"
)


def _require_exact_dict(value, *, keys, field_name):
    if type(value) is not dict or set(value) != set(keys):
        raise RowAuthorityConfigError(
            f"{field_name} must contain the exact approved fields"
        )
    return value


def _require_uint(value, *, field_name):
    if (
        type(value) is not int
        or value < 0
        or value > MAX_JSON_SAFE_INTEGER
    ):
        raise RowAuthorityConfigError(
            f"{field_name} must be a JSON-safe unsigned integer"
        )
    return value


def _require_pos(value, *, field_name):
    checked = _require_uint(value, field_name=field_name)
    if checked < 1:
        raise RowAuthorityConfigError(f"{field_name} must be positive")
    return checked


def _require_optional_hash(value, *, field_name):
    if value is None:
        return None
    return _require_sha256(value, field_name=field_name)


def _require_opaque(value, *, field_name):
    if type(value) is not str:
        raise RowAuthorityConfigError(f"{field_name} must be an exact string")
    encoded = _utf8_bytes(value, field_name=field_name)
    if (
        not encoded
        or len(encoded) > MAX_OPAQUE_BYTES
        or unicodedata.normalize("NFC", value) != value
        or _contains_control(value)
    ):
        raise RowAuthorityConfigError(
            f"{field_name} must be bounded, NFC, nonempty, and control-free"
        )
    return value


def _require_timestamp(value, *, field_name):
    if type(value) is not str:
        raise RowAuthorityConfigError(
            f"{field_name} must be an exact UTC timestamp"
        )
    match = _TIMESTAMP_PATTERN.fullmatch(value)
    if match is None:
        raise RowAuthorityConfigError(
            f"{field_name} must use exact UTC RFC3339 microseconds"
        )
    parts = {key: int(item) for key, item in match.groupdict().items()}
    year = parts["year"]
    month = parts["month"]
    day = parts["day"]
    if year < 1 or month < 1 or month > 12:
        raise RowAuthorityConfigError(f"{field_name} is not a valid date")
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days_in_month = (
        31,
        29 if leap else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    if day < 1 or day > days_in_month[month - 1]:
        raise RowAuthorityConfigError(f"{field_name} is not a valid date")
    if (
        parts["hour"] > 23
        or parts["minute"] > 59
        or parts["second"] > 59
    ):
        raise RowAuthorityConfigError(f"{field_name} is not a valid time")
    return value


def _defensive_copy(value):
    if type(value) is dict:
        return {key: _defensive_copy(item) for key, item in value.items()}
    if type(value) is list:
        return [_defensive_copy(item) for item in value]
    if type(value) is tuple:
        return tuple(_defensive_copy(item) for item in value)
    return value


def _require_row_authority_planned_writes(value):
    checked = _require_uint(value, field_name="planned_writes")
    if checked > MAX_ROW_AUTHORITY_PLANNED_WRITES:
        raise RowAuthorityConfigError(
            "planned_writes exceeds the row-authority transaction ceiling"
        )
    return checked


def derive_owner_priority(owner_kind):
    if type(owner_kind) is not str or owner_kind not in OWNER_PRIORITIES:
        raise RowAuthorityConfigError("owner_kind is not approved")
    return OWNER_PRIORITIES[owner_kind]


_B1_SOURCE_IDENTITY_KEYS = frozenset(
    {
        "schemaVersion",
        "canonicalSourceId",
        "creationHash",
        "verifiedAliases",
        "threadId",
        "lifecycleState",
        "createdAt",
        "updatedAt",
    }
)
_B1_SOURCE_CLASSIFICATION_KEYS = frozenset(
    {
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
        "retainedTerminalKind",
        "retainedTerminalImmutableHash",
        "retainedTerminalRecordHash",
        "retainedTerminalBindingHash",
        "createdAt",
        "updatedAt",
    }
)
_B1_SOURCE_OWNER_KEYS = frozenset(
    {
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
)
_B1_SOURCE_LEDGER_KEYS = frozenset(
    {
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
)
_B1_SOURCE_ENTRY_KEYS = frozenset(
    {
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
)
_B1_SOURCE_ENTRY_MUTABLE_KEYS = frozenset(
    {"state", "resolutionEvidence", "resolutionEvidenceHash"}
)
_B1_TRANSITION_OWNER_KINDS = frozenset(
    {"none", "contact_optout", "terminal", "human_decision"}
)
_B1_TERMINAL_CANDIDATE_TYPES = frozenset(
    {"property_unavailable", "close_conversation"}
)
_B1_HUMAN_CANDIDATE_TYPES = frozenset(
    {
        "call_requested",
        "actionable_tour_review",
        "needs_user_input",
        "wrong_contact_pause",
        "forwarded_observed",
        "disabled_policy_suppressed",
    }
)
_B1_ORDINARY_CANDIDATE_TYPES = frozenset(
    {
        "confirmed_tour",
        "non_tour",
        "new_property",
        "field_update",
        "generic_reply",
        "informational",
    }
)
_B1_LOCAL_SOURCE_POLICY_EVIDENCE_KINDS = frozenset(
    {"local_ignore_auto_reply", "local_ignore_self_sender"}
)
_B1_HARD_OPTOUT_EVIDENCE_V1_KEYS = frozenset(
    {"schemaVersion", "evidenceKind", "evidenceHash"}
)
_B1_HARD_OPTOUT_EVIDENCE_V2_KEYS = frozenset(
    {
        *_B1_HARD_OPTOUT_EVIDENCE_V1_KEYS,
        "exactIdentityHash",
        "canonicalMailboxIdentityHash",
    }
)
_B1_SOURCE_ALIAS_TYPES = frozenset({"graph", "internet_message_id"})
_B1_COMPLETE_PROPOSAL_KEYS = frozenset(
    {"schemaVersion", "transitionCandidates", "ordinaryObligations"}
)
_B1_COMPLETION_EVIDENCE_KEYS = frozenset(
    {
        "schemaVersion",
        "evidenceKind",
        "canonicalSourceId",
        "ledgerHash",
        "workKey",
        "payloadHash",
        "workKind",
        "resultHash",
    }
)
_B1_DELEGATION_EVIDENCE_KEYS = frozenset(
    {
        "schemaVersion",
        "evidenceKind",
        "canonicalSourceId",
        "ledgerHash",
        "workKey",
        "payloadHash",
        "workKind",
        "deferredBindingHash",
    }
)
_B1_DOMINANCE_EVIDENCE_KEYS = frozenset(
    {
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
)
_B1_LINK_V1_KEYS = frozenset(
    {
        "canonicalSourceId",
        "snapshotImmutableHash",
        "selectionHash",
        "ownerDecisionHash",
        "ledgerHash",
        "ownerKind",
        "ownerKey",
        "workKey",
        "payloadHash",
        "hardOptOutEvidenceHash",
        "authorityLinkHash",
    }
)
_B1_LINK_V2_KEYS = frozenset(
    {
        *_B1_LINK_V1_KEYS,
        "exactIdentityHash",
        "canonicalMailboxIdentityHash",
    }
)
_B1_SOURCE_SETTLEMENT_KEYS = frozenset(
    {
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
)
_B1_SOURCE_SETTLEMENT_ALIAS_KEYS = frozenset(
    {"sourceAliasKey", "aliasType", "normalizedValueHash"}
)
_B1_THREAD_BLOCKER_KEYS = frozenset(
    {
        "canonicalSourceId",
        "ownerKind",
        "ownerKey",
        "generation",
        "threadHeadRevision",
        "headHash",
    }
)
_B1_MAX_SOURCE_ALIASES = 8
_B1_MAX_SOURCE_ENTRIES = 128
_B1_MAX_CLASSIFICATION_BYTES = 614400
_B1_MAX_LEDGER_BYTES = 600 * 1024


def _is_aware_datetime(value):
    try:
        return (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
    except Exception:
        return False


def _validate_b1_json(value, *, active):
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise RowAuthorityConfigError("B1 material must be finite JSON")
        return
    if type(value) is str:
        _utf8_bytes(value, field_name="B1 material")
        return
    if type(value) not in {dict, list}:
        raise RowAuthorityConfigError("B1 material must use exact JSON types")
    identity = id(value)
    if identity in active:
        raise RowAuthorityConfigError("B1 material contains a cycle")
    active.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise RowAuthorityConfigError(
                        "B1 material keys must be exact strings"
                    )
                _utf8_bytes(key, field_name="B1 material key")
                _validate_b1_json(item, active=active)
        else:
            for item in value:
                _validate_b1_json(item, active=active)
    except RecursionError as exc:
        raise RowAuthorityConfigError("B1 material is too deeply nested") from exc
    finally:
        active.remove(identity)


def _b1_canonical_json_bytes(value):
    _validate_b1_json(value, active=set())
    try:
        return json.dumps(
            value,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise RowAuthorityConfigError("B1 material is not canonical JSON") from exc


def _b1_canonical_hash(value):
    return hashlib.sha256(_b1_canonical_json_bytes(value)).hexdigest()


def _b1_sorted_items(value, *, field_name):
    if type(value) is not list:
        raise RowAuthorityConfigError(f"{field_name} must be an exact list")
    result = []
    for item in value:
        if type(item) is not dict:
            raise RowAuthorityConfigError(
                f"{field_name} entries must be exact dictionaries"
            )
        _validate_b1_json(item, active=set())
        result.append(_defensive_copy(item))
    return sorted(result, key=_b1_canonical_json_bytes)


def _b1_candidate_type(candidate):
    value = candidate.get("type")
    if type(value) is not str or not value:
        raise RowAuthorityConfigError("B1 candidate type is malformed")
    return value


def _normalize_b1_complete_proposal(value):
    checked = _require_exact_dict(
        value,
        keys=_B1_COMPLETE_PROPOSAL_KEYS,
        field_name="B1 complete proposal",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != 1:
        raise RowAuthorityConfigError("B1 proposal schemaVersion must be 1")
    transitions = _b1_sorted_items(
        checked["transitionCandidates"],
        field_name="B1 transitionCandidates",
    )
    obligations = _b1_sorted_items(
        checked["ordinaryObligations"],
        field_name="B1 ordinaryObligations",
    )
    legal_transitions = (
        {"contact_optout"}
        | set(_B1_TERMINAL_CANDIDATE_TYPES)
        | set(_B1_HUMAN_CANDIDATE_TYPES)
    )
    if any(_b1_candidate_type(item) not in legal_transitions for item in transitions):
        raise RowAuthorityConfigError("B1 transition candidate lane is invalid")
    if any(
        _b1_candidate_type(item) not in _B1_ORDINARY_CANDIDATE_TYPES
        for item in obligations
    ):
        raise RowAuthorityConfigError("B1 ordinary obligation lane is invalid")
    return {
        "schemaVersion": 1,
        "transitionCandidates": transitions,
        "ordinaryObligations": obligations,
    }


def _derive_b1_selection(*, canonical_source_id, proposal, hard_optout):
    candidates = []
    obligations = _b1_sorted_items(
        proposal["ordinaryObligations"],
        field_name="B1 ordinaryObligations",
    )
    for candidate in _b1_sorted_items(
        proposal["transitionCandidates"],
        field_name="B1 transitionCandidates",
    ):
        candidate_type = _b1_candidate_type(candidate)
        if candidate_type == "contact_optout":
            if hard_optout:
                candidates.append(_defensive_copy(candidate))
            else:
                candidates.append(
                    {
                        "type": "needs_user_input",
                        "reason": "unverified_optout_review",
                        "sourceCandidateHash": _b1_canonical_hash(candidate),
                    }
                )
        elif candidate_type in (
            set(_B1_TERMINAL_CANDIDATE_TYPES)
            | set(_B1_HUMAN_CANDIDATE_TYPES)
        ):
            candidates.append(_defensive_copy(candidate))
        else:
            raise RowAuthorityConfigError("B1 transition candidate is unsupported")
    candidates.sort(key=_b1_canonical_json_bytes)
    obligations.sort(key=_b1_canonical_json_bytes)
    hard = [item for item in candidates if item["type"] == "contact_optout"]
    terminal = [
        item for item in candidates if item["type"] in _B1_TERMINAL_CANDIDATE_TYPES
    ]
    human = [
        item for item in candidates if item["type"] in _B1_HUMAN_CANDIDATE_TYPES
    ]
    if hard:
        owner_kind, selected = "contact_optout", hard
    elif terminal:
        owner_kind, selected = "terminal", terminal
    elif human:
        owner_kind, selected = "human_decision", human
    else:
        owner_kind, selected = "none", []
    owner_key = None
    if owner_kind != "none":
        owner_key = _b1_canonical_hash(
            {
                "hashKind": "source-selection-v1",
                "canonicalSourceId": canonical_source_id,
                "ownerKind": owner_kind,
                "selectedCandidates": selected,
            }
        )
    selected_hashes = {_b1_canonical_hash(item) for item in selected}
    selection = {
        "candidateTaxonomyVersion": "source-candidate-taxonomy-v1",
        "ownerKind": owner_kind,
        "ownerKey": owner_key,
        "selectedCandidates": _defensive_copy(selected),
        "candidateDominance": [
            {
                "candidateHash": _b1_canonical_hash(item),
                "outcome": (
                    "selected"
                    if _b1_canonical_hash(item) in selected_hashes
                    else "dominated"
                ),
            }
            for item in candidates
        ],
        "transitionCandidatesHash": _b1_canonical_hash(candidates),
        "ordinaryObligationsHash": _b1_canonical_hash(obligations),
    }
    return candidates, obligations, selection


def _validate_b1_source_identity(document):
    checked = _require_exact_dict(
        document,
        keys=_B1_SOURCE_IDENTITY_KEYS,
        field_name="B1 source identity",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != 1:
        raise RowAuthorityConfigError("B1 identity schemaVersion must be 1")
    source_id = _require_firestore_document_id(
        checked["canonicalSourceId"],
        field_name="B1 canonicalSourceId",
    )
    _require_opaque(source_id, field_name="B1 canonicalSourceId")
    _require_sha256(checked["creationHash"], field_name="B1 creationHash")
    if checked["lifecycleState"] != "pending" or type(
        checked["lifecycleState"]
    ) is not str:
        raise RowAuthorityConfigError("B1 identity lifecycle is unsupported")
    if not _is_aware_datetime(checked["createdAt"]) or not _is_aware_datetime(
        checked["updatedAt"]
    ):
        raise RowAuthorityConfigError("B1 identity timestamps must be aware")
    thread_id = _require_firestore_document_id(
        checked["threadId"],
        field_name="B1 threadId",
    )
    _require_opaque(thread_id, field_name="B1 threadId")
    aliases = checked["verifiedAliases"]
    if type(aliases) is not list or not 1 <= len(aliases) <= _B1_MAX_SOURCE_ALIASES:
        raise RowAuthorityConfigError("B1 verifiedAliases is malformed")
    validated = []
    seen = set()
    for alias in aliases:
        item = _require_exact_dict(
            alias,
            keys={"sourceAliasKey", "aliasType", "normalizedValueHash"},
            field_name="B1 alias descriptor",
        )
        key = _require_sha256(item["sourceAliasKey"], field_name="B1 sourceAliasKey")
        if key in seen:
            raise RowAuthorityConfigError("B1 alias keys must be unique")
        seen.add(key)
        if type(item["aliasType"]) is not str or item[
            "aliasType"
        ] not in _B1_SOURCE_ALIAS_TYPES:
            raise RowAuthorityConfigError("B1 alias type is unsupported")
        _require_sha256(
            item["normalizedValueHash"],
            field_name="B1 normalizedValueHash",
        )
        validated.append(_defensive_copy(item))
    if validated != sorted(validated, key=lambda item: item["sourceAliasKey"]):
        raise RowAuthorityConfigError("B1 aliases must be canonically sorted")
    return _defensive_copy(checked)


def _validate_b1_classification(document, *, canonical_source_id):
    checked = _require_exact_dict(
        document,
        keys=_B1_SOURCE_CLASSIFICATION_KEYS,
        field_name="B1 source classification",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != 1
        or checked["canonicalSourceId"] != canonical_source_id
        or checked["classificationState"] != "snapshot_ready"
        or type(checked["classificationEpoch"]) is not int
        or checked["classificationEpoch"] < 1
        or type(checked["classificationInputSchemaVersion"]) is not int
        or checked["classificationInputSchemaVersion"] != 1
    ):
        raise RowAuthorityConfigError("B1 ready classification is malformed")
    _require_firestore_document_id(
        checked["classificationClaimId"],
        field_name="B1 classificationClaimId",
    )
    for field in ("leaseExpiresAt", "snapshotPersistedAt", "createdAt", "updatedAt"):
        if not _is_aware_datetime(checked[field]):
            raise RowAuthorityConfigError(f"B1 {field} must be aware")
    _require_sha256(
        checked["classificationInputHash"],
        field_name="B1 classificationInputHash",
    )
    if any(
        checked[field] is not None
        for field in (
            "retainedTerminalKind",
            "retainedTerminalImmutableHash",
            "retainedTerminalRecordHash",
            "retainedTerminalBindingHash",
        )
    ):
        raise RowAuthorityConfigError("B1 classification retains terminal authority")
    model_state = checked["modelRequestState"]
    if model_state == "captured":
        model_key = checked["modelRequestKey"]
        if (
            type(model_key) is not str
            or not model_key
            or len(_utf8_bytes(model_key, field_name="B1 modelRequestKey")) > 1024
            or _contains_control(model_key)
        ):
            raise RowAuthorityConfigError("B1 modelRequestKey is malformed")
        _require_firestore_document_id(
            checked["requestStartFence"],
            field_name="B1 requestStartFence",
        )
        if type(checked["proposalEvidence"]) is not dict:
            raise RowAuthorityConfigError("B1 proposal evidence is malformed")
        _validate_b1_json(checked["proposalEvidence"], active=set())
        if checked["deterministicEvidence"] is not None:
            raise RowAuthorityConfigError("B1 classification evidence lanes conflict")
        proposal_evidence = _defensive_copy(checked["proposalEvidence"])
        deterministic_evidence = None
    elif model_state == "not_applicable":
        if (
            checked["modelRequestKey"] is not None
            or checked["requestStartFence"] is not None
            or checked["proposalEvidence"] is not None
            or type(checked["deterministicEvidence"]) is not dict
        ):
            raise RowAuthorityConfigError("B1 deterministic snapshot is malformed")
        _validate_b1_json(checked["deterministicEvidence"], active=set())
        proposal_evidence = None
        deterministic_evidence = _defensive_copy(checked["deterministicEvidence"])
    else:
        raise RowAuthorityConfigError("B1 modelRequestState is unsupported")
    proposal = _normalize_b1_complete_proposal(checked["completeProposalSnapshot"])
    hard_optout = (
        deterministic_evidence is not None
        and deterministic_evidence.get("evidenceKind")
        not in _B1_LOCAL_SOURCE_POLICY_EVIDENCE_KINDS
    )
    transitions, obligations, selection = _derive_b1_selection(
        canonical_source_id=canonical_source_id,
        proposal=proposal,
        hard_optout=hard_optout,
    )
    complete_hash = _b1_canonical_hash(proposal)
    proposal_hash = (
        _b1_canonical_hash(proposal_evidence)
        if proposal_evidence is not None
        else None
    )
    deterministic_hash = (
        _b1_canonical_hash(deterministic_evidence)
        if deterministic_evidence is not None
        else None
    )
    selection_hash = _b1_canonical_hash(selection)
    immutable = {
        "schemaVersion": 1,
        "hashKind": "source-classification-snapshot-v1",
        "canonicalSourceId": canonical_source_id,
        "classificationInputSchemaVersion": 1,
        "classificationInputHash": checked["classificationInputHash"],
        "modelRequestKey": checked["modelRequestKey"],
        "completeProposalSnapshot": proposal,
        "completeProposalHash": complete_hash,
        "transitionCandidates": transitions,
        "ordinaryObligations": obligations,
        "selectionSnapshot": selection,
        "selectionHash": selection_hash,
        "proposalEvidence": proposal_evidence,
        "proposalEvidenceHash": proposal_hash,
        "deterministicEvidence": deterministic_evidence,
        "deterministicEvidenceHash": deterministic_hash,
    }
    snapshot_hash = _b1_canonical_hash(immutable)
    bounded = {**immutable, "snapshotImmutableHash": snapshot_hash}
    if len(_b1_canonical_json_bytes(bounded)) > _B1_MAX_CLASSIFICATION_BYTES:
        raise RowAuthorityConfigError("B1 classification exceeds its byte bound")
    expected = {
        "completeProposalSnapshot": proposal,
        "completeProposalHash": complete_hash,
        "transitionCandidates": transitions,
        "ordinaryObligations": obligations,
        "selectionSnapshot": selection,
        "selectionHash": selection_hash,
        "snapshotImmutableHash": snapshot_hash,
        "proposalEvidence": proposal_evidence,
        "proposalEvidenceHash": proposal_hash,
        "deterministicEvidence": deterministic_evidence,
        "deterministicEvidenceHash": deterministic_hash,
    }
    if any(checked[field] != value for field, value in expected.items()):
        raise RowAuthorityConfigError("B1 classification hashes conflict")
    return _defensive_copy(checked)


def _b1_owner_material(classification):
    selection = classification["selectionSnapshot"]
    owner_kind = selection["ownerKind"]
    owner_key = selection["ownerKey"]
    if (
        type(owner_kind) is not str
        or owner_kind not in _B1_TRANSITION_OWNER_KINDS
        or (owner_kind == "none") != (owner_key is None)
    ):
        raise RowAuthorityConfigError("B1 selected owner is malformed")
    if owner_key is not None:
        _require_sha256(owner_key, field_name="B1 ownerKey")
    material = {
        "schemaVersion": 1,
        "canonicalSourceId": classification["canonicalSourceId"],
        "snapshotImmutableHash": classification["snapshotImmutableHash"],
        "selectionHash": classification["selectionHash"],
        "ownerKind": owner_kind,
        "ownerKey": owner_key,
    }
    material["ownerDecisionHash"] = _b1_canonical_hash(
        {"hashKind": "source-transition-owner-v1", **material}
    )
    return material


def _validate_b1_owner(document, *, canonical_source_id, classification):
    checked = _require_exact_dict(
        document,
        keys=_B1_SOURCE_OWNER_KEYS,
        field_name="B1 transition owner",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != 1
        or checked["canonicalSourceId"] != canonical_source_id
        or type(checked["revision"]) is not int
        or checked["revision"] != 1
        or not _is_aware_datetime(checked["createdAt"])
        or not _is_aware_datetime(checked["updatedAt"])
    ):
        raise RowAuthorityConfigError("B1 transition owner is malformed")
    expected = _b1_owner_material(classification)
    if any(checked[field] != value for field, value in expected.items()):
        raise RowAuthorityConfigError("B1 transition owner conflicts")
    return _defensive_copy(checked)


def _b1_dominance(*, lane, kind, payload_hash, owner_kind, selected_hashes):
    if lane == "ordinary":
        if kind == "generic_reply":
            if owner_kind == "terminal":
                return "delegate_terminal_policy"
            if owner_kind in {"contact_optout", "human_decision"}:
                return "dominated_no_send"
        return "preserve"
    return "delegate_owner" if payload_hash in selected_hashes else "dominated_by_owner"


def _b1_completion_contract(*, kind, dominance):
    evidence_kind = {
        "delegate_owner": "owner_delegation",
        "delegate_terminal_policy": "terminal_policy_delegation",
        "dominated_by_owner": "selection_dominance",
        "dominated_no_send": "selection_dominance",
        "preserve": "work_completion",
    }[dominance]
    return {"schemaVersion": 1, "evidenceKind": evidence_kind, "workKind": kind}


def _build_b1_entries(*, canonical_source_id, classification, owner):
    selected_hashes = {
        _b1_canonical_hash(item)
        for item in classification["selectionSnapshot"]["selectedCandidates"]
    }
    raw = [
        ("transition", _defensive_copy(item))
        for item in classification["transitionCandidates"]
    ] + [
        ("ordinary", _defensive_copy(item))
        for item in classification["ordinaryObligations"]
    ]
    raw.sort(key=lambda item: _b1_canonical_json_bytes({"lane": item[0], "payload": item[1]}))
    occurrences = {}
    result = []
    for lane, payload in raw:
        payload_hash = _b1_canonical_hash(payload)
        semantic_hash = _b1_canonical_hash({"lane": lane, "payloadHash": payload_hash})
        ordinal = occurrences.get(semantic_hash, 0) + 1
        occurrences[semantic_hash] = ordinal
        kind = _b1_candidate_type(payload)
        dominance = _b1_dominance(
            lane=lane,
            kind=kind,
            payload_hash=payload_hash,
            owner_kind=owner["ownerKind"],
            selected_hashes=selected_hashes,
        )
        work_key = _b1_canonical_hash(
            {
                "hashKind": "source-work-key-v1",
                "canonicalSourceId": canonical_source_id,
                "snapshotImmutableHash": classification["snapshotImmutableHash"],
                "selectionHash": classification["selectionHash"],
                "lane": lane,
                "payloadHash": payload_hash,
                "occurrenceOrdinal": ordinal,
            }
        )
        result.append(
            {
                "workKey": work_key,
                "lane": lane,
                "kind": kind,
                "payload": payload,
                "payloadHash": payload_hash,
                "occurrenceOrdinal": ordinal,
                "selectedOwnerKind": owner["ownerKind"],
                "selectedOwnerKey": owner["ownerKey"],
                "dominanceOutcome": dominance,
                "completionContract": _b1_completion_contract(
                    kind=kind,
                    dominance=dominance,
                ),
                "state": "pending",
                "resolutionEvidence": None,
                "resolutionEvidenceHash": None,
            }
        )
    return result


def _b1_resolution_hash(evidence):
    return _b1_canonical_hash(
        {"hashKind": "source-work-resolution-evidence-v1", "evidence": evidence}
    )


def _validate_b1_entry_resolution(
    entry, *, canonical_source_id, ledger_hash, selection_hash, owner_decision_hash
):
    state = entry["state"]
    evidence = entry["resolutionEvidence"]
    evidence_hash = entry["resolutionEvidenceHash"]
    if state in {"pending", "applying"}:
        if evidence is not None or evidence_hash is not None:
            raise RowAuthorityConfigError("B1 unsettled work contains evidence")
        return
    if type(evidence) is not dict:
        raise RowAuthorityConfigError("B1 settled work lacks exact evidence")
    _require_sha256(evidence_hash, field_name="B1 resolutionEvidenceHash")
    if evidence_hash != _b1_resolution_hash(evidence):
        raise RowAuthorityConfigError("B1 resolution evidence hash conflicts")
    common = (
        type(evidence.get("schemaVersion")) is not int
        or evidence.get("schemaVersion") != 1
        or evidence.get("canonicalSourceId") != canonical_source_id
        or evidence.get("ledgerHash") != ledger_hash
        or evidence.get("workKey") != entry["workKey"]
        or evidence.get("payloadHash") != entry["payloadHash"]
        or evidence.get("workKind") != entry["kind"]
    )
    if common:
        raise RowAuthorityConfigError("B1 resolution evidence conflicts")
    if state == "completed":
        valid = (
            set(evidence) == _B1_COMPLETION_EVIDENCE_KEYS
            and evidence.get("evidenceKind") == "work_completion"
            and entry["completionContract"]["evidenceKind"] == "work_completion"
            and _SHA256_PATTERN.fullmatch(str(evidence.get("resultHash"))) is not None
            and type(evidence.get("resultHash")) is str
        )
    elif state == "delegated":
        valid = (
            set(evidence) == _B1_DELEGATION_EVIDENCE_KEYS
            and evidence.get("evidenceKind") == entry["completionContract"]["evidenceKind"]
            and entry["dominanceOutcome"] in {"delegate_owner", "delegate_terminal_policy"}
            and type(evidence.get("deferredBindingHash")) is str
            and _SHA256_PATTERN.fullmatch(evidence["deferredBindingHash"]) is not None
        )
    else:
        valid = (
            set(evidence) == _B1_DOMINANCE_EVIDENCE_KEYS
            and evidence.get("evidenceKind") == "selection_dominance"
            and entry["completionContract"]["evidenceKind"] == "selection_dominance"
            and entry["dominanceOutcome"] in {"dominated_by_owner", "dominated_no_send"}
            and evidence.get("selectionHash") == selection_hash
            and evidence.get("ownerDecisionHash") == owner_decision_hash
            and evidence.get("dominatingOwnerKind") == entry["selectedOwnerKind"]
            and evidence.get("dominatingOwnerKey") == entry["selectedOwnerKey"]
            and evidence.get("dominanceOutcome") == entry["dominanceOutcome"]
        )
    if not valid:
        raise RowAuthorityConfigError("B1 resolution evidence is unsupported")


def _validate_b1_ledger(document, *, canonical_source_id, classification, owner):
    checked = _require_exact_dict(
        document,
        keys=_B1_SOURCE_LEDGER_KEYS,
        field_name="B1 source work ledger",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != 1
        or checked["canonicalSourceId"] != canonical_source_id
        or type(checked["revision"]) is not int
        or checked["revision"] < 1
        or not _is_aware_datetime(checked["createdAt"])
        or not _is_aware_datetime(checked["updatedAt"])
        or type(checked["entries"]) is not list
        or type(checked["entryCount"]) is not int
        or checked["entryCount"] != len(checked["entries"])
    ):
        raise RowAuthorityConfigError("B1 source work ledger is malformed")
    expected_entries = _build_b1_entries(
        canonical_source_id=canonical_source_id,
        classification=classification,
        owner=owner,
    )
    if len(expected_entries) > _B1_MAX_SOURCE_ENTRIES:
        raise RowAuthorityConfigError("B1 source work ledger is overbound")
    immutable_keys = _B1_SOURCE_ENTRY_KEYS - _B1_SOURCE_ENTRY_MUTABLE_KEYS
    immutable_entries = [
        {field: _defensive_copy(entry[field]) for field in sorted(immutable_keys)}
        for entry in expected_entries
    ]
    ledger_hash = _b1_canonical_hash(
        {
            "hashKind": "source-work-ledger-v1",
            "canonicalSourceId": canonical_source_id,
            "completeProposalHash": classification["completeProposalHash"],
            "snapshotImmutableHash": classification["snapshotImmutableHash"],
            "selectionHash": classification["selectionHash"],
            "ownerDecisionHash": owner["ownerDecisionHash"],
            "entries": immutable_entries,
        }
    )
    expected_fields = {
        "completeProposalHash": classification["completeProposalHash"],
        "snapshotImmutableHash": classification["snapshotImmutableHash"],
        "selectionHash": classification["selectionHash"],
        "ownerDecisionHash": owner["ownerDecisionHash"],
        "entryCount": len(expected_entries),
        "ledgerHash": ledger_hash,
    }
    if any(checked[field] != value for field, value in expected_fields.items()):
        raise RowAuthorityConfigError("B1 source work ledger conflicts")
    if len(checked["entries"]) != len(expected_entries):
        raise RowAuthorityConfigError("B1 source work entries conflict")
    for stored, initial in zip(checked["entries"], expected_entries):
        if (
            type(stored) is not dict
            or set(stored) != _B1_SOURCE_ENTRY_KEYS
            or type(stored.get("state")) is not str
            or stored["state"] not in {"pending", "applying", "completed", "delegated", "dominated"}
            or any(stored.get(field) != initial[field] for field in immutable_keys)
        ):
            raise RowAuthorityConfigError("B1 source work entry conflicts")
        _validate_b1_entry_resolution(
            stored,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
            selection_hash=classification["selectionHash"],
            owner_decision_hash=owner["ownerDecisionHash"],
        )
    material = {
        "schemaVersion": 1,
        "canonicalSourceId": canonical_source_id,
        "completeProposalHash": classification["completeProposalHash"],
        "snapshotImmutableHash": classification["snapshotImmutableHash"],
        "selectionHash": classification["selectionHash"],
        "ownerDecisionHash": owner["ownerDecisionHash"],
        "entries": expected_entries,
        "entryCount": len(expected_entries),
        "ledgerHash": ledger_hash,
        "revision": 1,
    }
    if len(_b1_canonical_json_bytes(material)) > _B1_MAX_LEDGER_BYTES:
        raise RowAuthorityConfigError("B1 source work ledger exceeds its byte bound")
    return _defensive_copy(checked)


def _b1_source_settlement_identity_hash(identity):
    return _b1_canonical_hash(
        {
            "hashKind": "source-settlement-identity-v1",
            "schemaVersion": identity["schemaVersion"],
            "canonicalSourceId": identity["canonicalSourceId"],
            "creationHash": identity["creationHash"],
            "threadId": identity["threadId"],
        }
    )


def _b1_final_ledger_evidence_hash(ledger):
    if any(
        entry["state"] not in {"completed", "delegated", "dominated"}
        for entry in ledger["entries"]
    ):
        raise RowAuthorityConfigError(
            "B1 source settlement retains unfinished ledger work"
        )
    ordered_evidence = [
        {
            "workKey": entry["workKey"],
            "payloadHash": entry["payloadHash"],
            "state": entry["state"],
            "resolutionEvidenceHash": entry["resolutionEvidenceHash"],
        }
        for entry in ledger["entries"]
    ]
    return _b1_canonical_hash(
        {
            "hashKind": "source-final-ledger-evidence-v1",
            "ledgerHash": ledger["ledgerHash"],
            "entries": ordered_evidence,
        }
    )


def _b1_source_settlement_hash_material(document):
    return {
        "hashKind": "source-settlement-v1",
        **{
            field: _defensive_copy(document[field])
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


def _validate_b1_source_settlement(
    document,
    *,
    canonical_source_id,
    identity,
    classification,
    owner,
    ledger,
):
    checked = _require_exact_dict(
        document,
        keys=_B1_SOURCE_SETTLEMENT_KEYS,
        field_name="B1 source settlement",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != 1
        or checked["canonicalSourceId"] != canonical_source_id
        or type(checked["settlementRevision"]) is not int
        or checked["settlementRevision"] != 1
        or not _is_aware_datetime(checked["settledAt"])
    ):
        raise RowAuthorityConfigError("B1 source settlement is malformed")
    for field in (
        "identityHash",
        "snapshotImmutableHash",
        "selectionHash",
        "ownerDecisionHash",
        "ledgerHash",
        "finalLedgerEvidenceHash",
        "aliasSetHash",
        "settlementHash",
    ):
        _require_sha256(checked[field], field_name=f"B1 {field}")

    aliases = checked["aliases"]
    if type(aliases) is not list or not aliases:
        raise RowAuthorityConfigError(
            "B1 source settlement alias set is malformed"
        )
    validated_aliases = []
    seen_aliases = set()
    for descriptor in aliases:
        item = _require_exact_dict(
            descriptor,
            keys=_B1_SOURCE_SETTLEMENT_ALIAS_KEYS,
            field_name="B1 source settlement alias",
        )
        alias_key = _require_sha256(
            item["sourceAliasKey"],
            field_name="B1 source settlement alias key",
        )
        if alias_key in seen_aliases:
            raise RowAuthorityConfigError(
                "B1 source settlement aliases are duplicated"
            )
        seen_aliases.add(alias_key)
        if (
            type(item["aliasType"]) is not str
            or item["aliasType"] not in _B1_SOURCE_ALIAS_TYPES
        ):
            raise RowAuthorityConfigError(
                "B1 source settlement alias type is unsupported"
            )
        _require_sha256(
            item["normalizedValueHash"],
            field_name="B1 source settlement normalized alias hash",
        )
        validated_aliases.append(_defensive_copy(item))
    if validated_aliases != sorted(
        validated_aliases,
        key=lambda item: item["sourceAliasKey"],
    ):
        raise RowAuthorityConfigError(
            "B1 source settlement aliases are unordered"
        )
    current_aliases = {
        descriptor["sourceAliasKey"]: descriptor
        for descriptor in identity["verifiedAliases"]
    }
    if any(
        current_aliases.get(descriptor["sourceAliasKey"]) != descriptor
        for descriptor in validated_aliases
    ):
        raise RowAuthorityConfigError(
            "B1 source settlement aliases conflict with source identity"
        )

    blocker = checked["threadHeadBinding"]
    if owner["ownerKind"] == "none":
        if blocker is not None:
            raise RowAuthorityConfigError(
                "B1 none-owner settlement retains a thread binding"
            )
    else:
        blocker = _require_exact_dict(
            blocker,
            keys=_B1_THREAD_BLOCKER_KEYS,
            field_name="B1 source settlement thread binding",
        )
        if (
            blocker["canonicalSourceId"] != canonical_source_id
            or blocker["ownerKind"] != owner["ownerKind"]
            or blocker["ownerKey"] != owner["ownerKey"]
            or type(blocker["generation"]) is not int
            or blocker["generation"] < 1
            or type(blocker["threadHeadRevision"]) is not int
            or blocker["threadHeadRevision"]
            != (2 * blocker["generation"]) - 1
        ):
            raise RowAuthorityConfigError(
                "B1 source settlement thread binding conflicts"
            )
        _require_sha256(
            blocker["ownerKey"],
            field_name="B1 source settlement thread owner key",
        )
        _require_sha256(
            blocker["headHash"],
            field_name="B1 source settlement thread head hash",
        )
        expected_head_hash = _b1_canonical_hash(
            {
                "hashKind": "thread-transition-head-v1",
                "schemaVersion": 1,
                "threadId": identity["threadId"],
                "threadHeadRevision": blocker["threadHeadRevision"],
                "activeOwnerKey": blocker["ownerKey"],
                "activeOwnerKind": blocker["ownerKind"],
                "activeCanonicalSourceId": blocker[
                    "canonicalSourceId"
                ],
                "activeGeneration": blocker["generation"],
                "activeState": "active",
            }
        )
        if blocker["headHash"] != expected_head_hash:
            raise RowAuthorityConfigError(
                "B1 source settlement thread head hash conflicts"
            )

    final_ledger_evidence_hash = _b1_final_ledger_evidence_hash(ledger)
    expected_bindings = {
        "identityHash": _b1_source_settlement_identity_hash(identity),
        "snapshotImmutableHash": classification["snapshotImmutableHash"],
        "selectionHash": classification["selectionHash"],
        "ownerDecisionHash": owner["ownerDecisionHash"],
        "ledgerHash": ledger["ledgerHash"],
        "finalLedgerEvidenceHash": final_ledger_evidence_hash,
        "aliasSetHash": _b1_canonical_hash(
            {
                "hashKind": "source-settlement-alias-set-v1",
                "aliases": validated_aliases,
            }
        ),
    }
    if any(
        checked[field] != value
        for field, value in expected_bindings.items()
    ):
        raise RowAuthorityConfigError(
            "B1 source settlement conflicts with retained authority"
        )
    if checked["settlementHash"] != _b1_canonical_hash(
        _b1_source_settlement_hash_material(checked)
    ):
        raise RowAuthorityConfigError(
            "B1 source settlement hash does not recompute"
        )
    return _defensive_copy(checked)


def _validate_b1_hard_optout_evidence(evidence):
    if type(evidence) is not dict:
        raise RowAuthorityConfigError(
            "B1 hard opt-out evidence is not verified"
        )
    evidence_keys = set(evidence)
    if evidence_keys == _B1_HARD_OPTOUT_EVIDENCE_V1_KEYS:
        evidence_version = 1
    elif evidence_keys == _B1_HARD_OPTOUT_EVIDENCE_V2_KEYS:
        evidence_version = 2
    else:
        raise RowAuthorityConfigError(
            "B1 hard opt-out evidence is not verified"
        )
    if (
        type(evidence.get("schemaVersion")) is not int
        or evidence["schemaVersion"] != evidence_version
        or type(evidence.get("evidenceKind")) is not str
        or not evidence["evidenceKind"]
        or evidence["evidenceKind"]
        in _B1_LOCAL_SOURCE_POLICY_EVIDENCE_KINDS
    ):
        raise RowAuthorityConfigError(
            "B1 hard opt-out evidence is not verified"
        )
    _require_sha256(
        evidence.get("evidenceHash"),
        field_name="B1 evidenceHash",
    )
    if evidence_version == 2:
        _require_sha256(
            evidence.get("exactIdentityHash"),
            field_name="B1 exactIdentityHash",
        )
        _require_sha256(
            evidence.get("canonicalMailboxIdentityHash"),
            field_name="B1 canonicalMailboxIdentityHash",
        )
    return evidence_version


def build_b1_authority_link(
    *,
    user_scope_hash,
    source_identity_document,
    source_classification_document,
    source_owner_document,
    source_ledger_document,
    work_key,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    selector = _require_sha256(work_key, field_name="work_key")
    identity = _validate_b1_source_identity(source_identity_document)
    source_id = identity["canonicalSourceId"]
    classification = _validate_b1_classification(
        source_classification_document,
        canonical_source_id=source_id,
    )
    owner = _validate_b1_owner(
        source_owner_document,
        canonical_source_id=source_id,
        classification=classification,
    )
    ledger = _validate_b1_ledger(
        source_ledger_document,
        canonical_source_id=source_id,
        classification=classification,
        owner=owner,
    )
    matches = [entry for entry in ledger["entries"] if entry["workKey"] == selector]
    if len(matches) != 1:
        raise RowAuthorityConfigError("B1 work selector is not exact")
    entry = matches[0]
    if (
        entry["lane"] != "transition"
        or entry["dominanceOutcome"] != "delegate_owner"
        or owner["ownerKind"] not in OWNER_PRIORITIES
        or entry["selectedOwnerKind"] != owner["ownerKind"]
        or entry["selectedOwnerKey"] != owner["ownerKey"]
    ):
        raise RowAuthorityConfigError("B1 work is not selected owner authority")
    hard_optout_hash = None
    hard_optout_evidence_version = None
    hard_optout_evidence = None
    if owner["ownerKind"] == "contact_optout":
        evidence = classification["deterministicEvidence"]
        if classification["modelRequestState"] != "not_applicable":
            raise RowAuthorityConfigError(
                "B1 hard opt-out evidence is not verified"
            )
        hard_optout_evidence_version = _validate_b1_hard_optout_evidence(
            evidence
        )
        hard_optout_evidence = evidence
        hard_optout_hash = _b1_canonical_hash(evidence)
        if (
            classification["deterministicEvidenceHash"] != hard_optout_hash
            or entry["kind"] != "contact_optout"
            or entry["payload"].get("type") != "contact_optout"
            or entry["payload"].get("evidenceHash") != hard_optout_hash
        ):
            raise RowAuthorityConfigError("B1 hard opt-out evidence binding conflicts")
    material = {
        "canonicalSourceId": source_id,
        "snapshotImmutableHash": classification["snapshotImmutableHash"],
        "selectionHash": classification["selectionHash"],
        "ownerDecisionHash": owner["ownerDecisionHash"],
        "ledgerHash": ledger["ledgerHash"],
        "ownerKind": owner["ownerKind"],
        "ownerKey": owner["ownerKey"],
        "workKey": entry["workKey"],
        "payloadHash": entry["payloadHash"],
        "hardOptOutEvidenceHash": hard_optout_hash,
    }
    hash_domain = B1_AUTHORITY_LINK_HASH_DOMAIN
    if hard_optout_evidence_version == 2:
        material.update(
            {
                "exactIdentityHash": hard_optout_evidence[
                    "exactIdentityHash"
                ],
                "canonicalMailboxIdentityHash": hard_optout_evidence[
                    "canonicalMailboxIdentityHash"
                ],
            }
        )
        hash_domain = B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN
    result = {
        **material,
        "authorityLinkHash": domain_hash(
            hash_domain,
            material,
            user_scope_hash=scope,
        ),
    }
    return _defensive_copy(result)


def validate_b1_authority_link(*, authority_link, user_scope_hash):
    if type(authority_link) is not dict:
        raise RowAuthorityConfigError(
            "B1 authority link must contain the exact approved fields"
        )
    link_keys = set(authority_link)
    if link_keys == _B1_LINK_V1_KEYS:
        approved_keys = _B1_LINK_V1_KEYS
        hash_domain = B1_AUTHORITY_LINK_HASH_DOMAIN
        contact_link_v2 = False
    elif link_keys == _B1_LINK_V2_KEYS:
        approved_keys = _B1_LINK_V2_KEYS
        hash_domain = B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN
        contact_link_v2 = True
    else:
        raise RowAuthorityConfigError(
            "B1 authority link must contain the exact approved fields"
        )
    checked = _require_exact_dict(
        authority_link,
        keys=approved_keys,
        field_name="B1 authority link",
    )
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    _require_opaque(checked["canonicalSourceId"], field_name="canonicalSourceId")
    for field in (
        "snapshotImmutableHash",
        "selectionHash",
        "ownerDecisionHash",
        "ledgerHash",
        "ownerKey",
        "workKey",
        "payloadHash",
        "authorityLinkHash",
    ):
        _require_sha256(checked[field], field_name=field)
    if type(checked["ownerKind"]) is not str or checked["ownerKind"] not in OWNER_PRIORITIES:
        raise RowAuthorityConfigError("B1 link ownerKind is unsupported")
    hard = _require_optional_hash(
        checked["hardOptOutEvidenceHash"],
        field_name="hardOptOutEvidenceHash",
    )
    if (checked["ownerKind"] == "contact_optout") != (hard is not None):
        raise RowAuthorityConfigError("B1 link hard opt-out evidence is miscorrelated")
    if contact_link_v2:
        if checked["ownerKind"] != "contact_optout":
            raise RowAuthorityConfigError(
                "B1 v2 link requires verified contact opt-out authority"
            )
        _require_sha256(
            checked["exactIdentityHash"],
            field_name="exactIdentityHash",
        )
        _require_sha256(
            checked["canonicalMailboxIdentityHash"],
            field_name="canonicalMailboxIdentityHash",
        )
    material = {
        key: _defensive_copy(value)
        for key, value in checked.items()
        if key != "authorityLinkHash"
    }
    expected = domain_hash(
        hash_domain,
        material,
        user_scope_hash=scope,
    )
    if checked["authorityLinkHash"] != expected:
        raise RowAuthorityConfigError("B1 authorityLinkHash does not recompute")
    return _defensive_copy(checked)


_OPERATOR_ACTION_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "actionId",
        "actionKind",
        "actorScopeHash",
        "rowBindingsHash",
        "clientRequestHash",
        "reasonCode",
        "issuedAt",
        "operatorActionHash",
    }
)


def build_operator_action_document(
    *, user_scope_hash, actor_scope_hash, row_bindings_hash, client_request_id, issued_at
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    actor = _require_sha256(actor_scope_hash, field_name="actor_scope_hash")
    bindings_hash = _require_sha256(row_bindings_hash, field_name="row_bindings_hash")
    request_id = _require_opaque(client_request_id, field_name="client_request_id")
    issued = _require_timestamp(issued_at, field_name="issued_at")
    client_request_hash = domain_hash(
        OPERATOR_CLIENT_REQUEST_HASH_DOMAIN,
        {"clientRequestId": request_id},
        user_scope_hash=scope,
    )
    id_payload = {
        "actorScopeHash": actor,
        "rowBindingsHash": bindings_hash,
        "clientRequestHash": client_request_hash,
        "actionKind": "decline",
        "reasonCode": "decline_property",
        "issuedAt": issued,
    }
    action_id = domain_hash(
        OPERATOR_ACTION_ID_DOMAIN,
        id_payload,
        user_scope_hash=scope,
    )
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        "actionId": action_id,
        "actionKind": "decline",
        "actorScopeHash": actor,
        "rowBindingsHash": bindings_hash,
        "clientRequestHash": client_request_hash,
        "reasonCode": "decline_property",
        "issuedAt": issued,
    }
    document["operatorActionHash"] = domain_hash(
        OPERATOR_ACTION_HASH_DOMAIN,
        {
            key: document[key]
            for key in (
                "actionId",
                "actorScopeHash",
                "rowBindingsHash",
                "clientRequestHash",
                "actionKind",
                "reasonCode",
                "issuedAt",
            )
        },
        user_scope_hash=scope,
    )
    return document


def validate_operator_action_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_OPERATOR_ACTION_KEYS,
        field_name="operator action",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
        or checked["actionKind"] != "decline"
        or checked["reasonCode"] != "decline_property"
    ):
        raise RowAuthorityConfigError("operator action schema is unsupported")
    scope = _require_sha256(checked["userScopeHash"], field_name="userScopeHash")
    for field in (
        "actionId",
        "actorScopeHash",
        "rowBindingsHash",
        "clientRequestHash",
        "operatorActionHash",
    ):
        _require_sha256(checked[field], field_name=field)
    _require_timestamp(checked["issuedAt"], field_name="issuedAt")
    id_payload = {
        key: checked[key]
        for key in (
            "actorScopeHash",
            "rowBindingsHash",
            "clientRequestHash",
            "actionKind",
            "reasonCode",
            "issuedAt",
        )
    }
    if checked["actionId"] != domain_hash(
        OPERATOR_ACTION_ID_DOMAIN,
        id_payload,
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("operator actionId does not recompute")
    action_payload = {"actionId": checked["actionId"], **id_payload}
    if checked["operatorActionHash"] != domain_hash(
        OPERATOR_ACTION_HASH_DOMAIN,
        action_payload,
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("operatorActionHash does not recompute")
    return _defensive_copy(checked)


_ROW_DECISION_KEYS = frozenset(
    {
        "rowId",
        "decision",
        "plannedGeneration",
        "winnerGenerationHash",
        "winnerSettlementHash",
    }
)
_CLAIM_SET_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "requestId",
        "authorityOrigin",
        "authorityLink",
        "authorityLinkHash",
        "operatorActionHash",
        "fanoutId",
        "rowBindings",
        "primaryRowId",
        "bindingCount",
        "rowBindingsHash",
        "ownerKind",
        "ownerKey",
        "workKey",
        "payloadHash",
        "derivedPriority",
        "plannedWrites",
        "outcome",
        "rowDecisions",
        "claimSetHash",
        "createdAt",
    }
)
_OWNER_GENERATION_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "generation",
        "requestId",
        "claimSetHash",
        "predecessorHeadHash",
        "predecessorSettlementHash",
        "ownerKind",
        "ownerKey",
        "priority",
        "leaseEpoch",
        "firstFencingToken",
        "generationHash",
        "createdAt",
    }
)
_OWNER_SETTLEMENT_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "generation",
        "generationHash",
        "fencingToken",
        "outcome",
        "dominantGenerationHash",
        "supersededEffectiveSettlementHash",
        "operatorActionHash",
        "outcomeReasonCode",
        "outcomeEvidenceHash",
        "logicalOutcomeHash",
        "settlementHash",
        "settledAt",
    }
)
_SOURCE_SETTLEMENT_LINK_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "generation",
        "generationHash",
        "authorityLinkHash",
        "b1IdentityHash",
        "b1FinalLedgerEvidenceHash",
        "b1SettlementRevision",
        "b1SettlementHash",
        "b2SettlementHash",
        "sourceSettlementLinkHash",
        "linkedAt",
    }
)
_CLAIM_ORIGINS = frozenset(
    {"b1_source", "authenticated_operator", "contact_fanout"}
)
_CLAIM_OUTCOMES = frozenset({"accepted", "dominated"})
_SETTLEMENT_REASON_BY_OUTCOME = {
    "contact_optout": "verified_optout",
    "terminal": "terminal_source",
    "human_declined": "operator_decline",
    "dominated": "superseded_by_higher_priority",
}


def _canonical_claim_bindings(*, user_scope_hash, row_ids, primary_row_id):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    bindings = normalize_row_bindings(row_ids, primary_row_id)
    primary = validate_row_id(primary_row_id)
    count = len(bindings)
    return (
        bindings,
        primary,
        count,
        _row_bindings_hash(
            user_scope_hash=scope,
            row_bindings=bindings,
            primary_row_id=primary,
            binding_count=count,
        ),
    )


def _validated_claim_decisions(*, row_decisions, row_bindings, outcome):
    if type(row_decisions) not in {list, tuple}:
        raise RowAuthorityConfigError("row_decisions must be a list or tuple")
    if type(outcome) is not str or outcome not in _CLAIM_OUTCOMES:
        raise RowAuthorityConfigError("claim outcome is unsupported")
    expected_rows = [binding["rowId"] for binding in row_bindings]
    if len(row_decisions) != len(expected_rows):
        raise RowAuthorityConfigError("row decisions must cover every bound row")
    canonical = []
    for raw in row_decisions:
        decision = _require_exact_dict(
            raw,
            keys=_ROW_DECISION_KEYS,
            field_name="row decision",
        )
        row_id = validate_row_id(decision["rowId"])
        kind = decision["decision"]
        if type(kind) is not str or kind not in {
            "accepted",
            "dominated",
            "blocked_by_claim_set",
        }:
            raise RowAuthorityConfigError("row decision is unsupported")
        planned = decision["plannedGeneration"]
        winner_generation = decision["winnerGenerationHash"]
        winner_settlement = decision["winnerSettlementHash"]
        if kind == "accepted":
            _require_pos(planned, field_name="plannedGeneration")
            if winner_generation is not None or winner_settlement is not None:
                raise RowAuthorityConfigError(
                    "accepted decisions cannot carry winner hashes"
                )
        elif kind == "dominated":
            if planned is not None:
                raise RowAuthorityConfigError(
                    "dominated decisions cannot allocate a generation"
                )
            _require_sha256(
                winner_generation,
                field_name="winnerGenerationHash",
            )
            _require_optional_hash(
                winner_settlement,
                field_name="winnerSettlementHash",
            )
        else:
            if any(
                value is not None
                for value in (planned, winner_generation, winner_settlement)
            ):
                raise RowAuthorityConfigError(
                    "blocked decisions cannot carry generation or winner fields"
                )
        canonical.append(
            {
                "rowId": row_id,
                "decision": kind,
                "plannedGeneration": planned,
                "winnerGenerationHash": winner_generation,
                "winnerSettlementHash": winner_settlement,
            }
        )
    canonical.sort(key=lambda item: item["rowId"])
    if [item["rowId"] for item in canonical] != expected_rows:
        raise RowAuthorityConfigError("row decisions do not match row bindings")
    if outcome == "accepted":
        if any(item["decision"] != "accepted" for item in canonical):
            raise RowAuthorityConfigError(
                "accepted claim sets require only accepted decisions"
            )
    elif (
        not any(item["decision"] == "dominated" for item in canonical)
        or any(item["decision"] == "accepted" for item in canonical)
    ):
        raise RowAuthorityConfigError(
            "dominated claim sets require dominated and blocked decisions"
        )
    return canonical


def _claim_origin_from_inputs(
    *,
    user_scope_hash,
    authority_origin,
    authority_link,
    operator_action_document,
    fanout_id,
    row_bindings,
    canonical_mailbox_identity_hash,
    contact_settlement_hash,
):
    if type(authority_origin) is not str or authority_origin not in _CLAIM_ORIGINS:
        raise RowAuthorityConfigError("authority_origin is unsupported")
    if authority_origin == "b1_source":
        if (
            operator_action_document is not None
            or fanout_id is not None
            or canonical_mailbox_identity_hash is not None
            or contact_settlement_hash is not None
        ):
            raise RowAuthorityConfigError("b1_source origin fields conflict")
        link = validate_b1_authority_link(
            authority_link=authority_link,
            user_scope_hash=user_scope_hash,
        )
        if link["ownerKind"] not in {"terminal", "human_decision"}:
            raise RowAuthorityConfigError("direct B1 contact opt-out claim is forbidden")
        return {
            "authorityLink": link,
            "authorityLinkHash": link["authorityLinkHash"],
            "operatorActionHash": None,
            "fanoutId": None,
            "ownerKind": link["ownerKind"],
            "ownerKey": link["ownerKey"],
            "workKey": link["workKey"],
            "payloadHash": link["payloadHash"],
        }
    if authority_origin == "authenticated_operator":
        if (
            authority_link is not None
            or fanout_id is not None
            or canonical_mailbox_identity_hash is not None
            or contact_settlement_hash is not None
        ):
            raise RowAuthorityConfigError(
                "authenticated_operator origin fields conflict"
            )
        action = validate_operator_action_document(
            document=operator_action_document
        )
        if action["userScopeHash"] != user_scope_hash:
            raise RowAuthorityConfigError("operator action scope conflicts")
        return {
            "authorityLink": None,
            "authorityLinkHash": None,
            "operatorActionHash": action["operatorActionHash"],
            "fanoutId": None,
            "ownerKind": "human_decision",
            "ownerKey": action["actorScopeHash"],
            "workKey": action["actionId"],
            "payloadHash": action["operatorActionHash"],
        }
    if (
        operator_action_document is not None
        or fanout_id is None
        or canonical_mailbox_identity_hash is None
        or contact_settlement_hash is None
        or len(row_bindings) != 1
    ):
        raise RowAuthorityConfigError("contact_fanout origin fields conflict")
    fanout = _require_sha256(fanout_id, field_name="fanout_id")
    canonical_mailbox = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    contact_settlement = _require_sha256(
        contact_settlement_hash,
        field_name="contact_settlement_hash",
    )
    link = validate_b1_authority_link(
        authority_link=authority_link,
        user_scope_hash=user_scope_hash,
    )
    if link["ownerKind"] != "contact_optout":
        raise RowAuthorityConfigError("contact fan-out requires verified opt-out")
    work_key = _require_opaque(
        f"{fanout}--{row_bindings[0]['rowId']}",
        field_name="contact fan-out work key",
    )
    return {
        "authorityLink": link,
        "authorityLinkHash": link["authorityLinkHash"],
        "operatorActionHash": None,
        "fanoutId": fanout,
        "ownerKind": link["ownerKind"],
        "ownerKey": canonical_mailbox,
        "workKey": work_key,
        "payloadHash": contact_settlement,
    }


def _request_id_payload(document):
    return {
        key: document[key]
        for key in (
            "authorityOrigin",
            "authorityLinkHash",
            "operatorActionHash",
            "fanoutId",
            "rowBindingsHash",
            "ownerKind",
            "ownerKey",
            "workKey",
            "payloadHash",
        )
    }


def _claim_set_hash_payload(document):
    return {
        key: _defensive_copy(document[key])
        for key in (
            "requestId",
            "authorityOrigin",
            "authorityLinkHash",
            "operatorActionHash",
            "fanoutId",
            "rowBindingsHash",
            "ownerKind",
            "ownerKey",
            "derivedPriority",
            "plannedWrites",
            "outcome",
            "rowDecisions",
            "createdAt",
        )
    }


def build_claim_set_document(
    *,
    user_scope_hash,
    authority_origin,
    authority_link,
    operator_action_document,
    fanout_id,
    row_ids,
    primary_row_id,
    planned_writes,
    outcome,
    row_decisions,
    created_at,
    canonical_mailbox_identity_hash=None,
    contact_settlement_hash=None,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    bindings, primary, count, bindings_hash = _canonical_claim_bindings(
        user_scope_hash=scope,
        row_ids=row_ids,
        primary_row_id=primary_row_id,
    )
    origin = _claim_origin_from_inputs(
        user_scope_hash=scope,
        authority_origin=authority_origin,
        authority_link=authority_link,
        operator_action_document=operator_action_document,
        fanout_id=fanout_id,
        row_bindings=bindings,
        canonical_mailbox_identity_hash=canonical_mailbox_identity_hash,
        contact_settlement_hash=contact_settlement_hash,
    )
    if authority_origin == "authenticated_operator":
        action = validate_operator_action_document(
            document=operator_action_document
        )
        if action["rowBindingsHash"] != bindings_hash:
            raise RowAuthorityConfigError("operator action bindings conflict")
    decisions = _validated_claim_decisions(
        row_decisions=row_decisions,
        row_bindings=bindings,
        outcome=outcome,
    )
    writes = _require_row_authority_planned_writes(planned_writes)
    created = _require_timestamp(created_at, field_name="created_at")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        "requestId": None,
        "authorityOrigin": authority_origin,
        **origin,
        "rowBindings": bindings,
        "primaryRowId": primary,
        "bindingCount": count,
        "rowBindingsHash": bindings_hash,
        "derivedPriority": derive_owner_priority(origin["ownerKind"]),
        "plannedWrites": writes,
        "outcome": outcome,
        "rowDecisions": decisions,
        "claimSetHash": None,
        "createdAt": created,
    }
    document["requestId"] = domain_hash(
        CLAIM_REQUEST_ID_DOMAIN,
        _request_id_payload(document),
        user_scope_hash=scope,
    )
    document["claimSetHash"] = domain_hash(
        CLAIM_SET_HASH_DOMAIN,
        _claim_set_hash_payload(document),
        user_scope_hash=scope,
    )
    return _defensive_copy(document)


def _validate_claim_origin_document(document):
    origin = document["authorityOrigin"]
    if type(origin) is not str or origin not in _CLAIM_ORIGINS:
        raise RowAuthorityConfigError("claim authorityOrigin is unsupported")
    scope = document["userScopeHash"]
    link = document["authorityLink"]
    link_hash = document["authorityLinkHash"]
    operator_hash = document["operatorActionHash"]
    fanout_id = document["fanoutId"]
    if origin == "b1_source":
        if operator_hash is not None or fanout_id is not None:
            raise RowAuthorityConfigError("b1_source claim fields conflict")
        validated_link = validate_b1_authority_link(
            authority_link=link,
            user_scope_hash=scope,
        )
        if (
            validated_link["ownerKind"] not in {"terminal", "human_decision"}
            or link_hash != validated_link["authorityLinkHash"]
        ):
            raise RowAuthorityConfigError("b1_source claim link conflicts")
        expected = {
            "ownerKind": validated_link["ownerKind"],
            "ownerKey": validated_link["ownerKey"],
            "workKey": validated_link["workKey"],
            "payloadHash": validated_link["payloadHash"],
        }
    elif origin == "authenticated_operator":
        if link is not None or link_hash is not None or fanout_id is not None:
            raise RowAuthorityConfigError("operator claim fields conflict")
        _require_sha256(operator_hash, field_name="operatorActionHash")
        _require_sha256(document["ownerKey"], field_name="ownerKey")
        _require_sha256(document["workKey"], field_name="workKey")
        expected = {
            "ownerKind": "human_decision",
            "payloadHash": operator_hash,
        }
    else:
        if operator_hash is not None:
            raise RowAuthorityConfigError("contact fan-out claim fields conflict")
        _require_sha256(fanout_id, field_name="fanoutId")
        validated_link = validate_b1_authority_link(
            authority_link=link,
            user_scope_hash=scope,
        )
        if (
            validated_link["ownerKind"] != "contact_optout"
            or link_hash != validated_link["authorityLinkHash"]
            or len(document["rowBindings"]) != 1
        ):
            raise RowAuthorityConfigError("contact fan-out link conflicts")
        _require_sha256(document["ownerKey"], field_name="ownerKey")
        _require_sha256(document["payloadHash"], field_name="payloadHash")
        expected = {
            "ownerKind": validated_link["ownerKind"],
            "workKey": (
                f"{fanout_id}--{document['rowBindings'][0]['rowId']}"
            ),
        }
    if any(document[field] != value for field, value in expected.items()):
        raise RowAuthorityConfigError("claim origin correlations conflict")


def validate_claim_set_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_CLAIM_SET_KEYS,
        field_name="claim set",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("claim set schemaVersion must be 1")
    scope = _require_sha256(checked["userScopeHash"], field_name="userScopeHash")
    _require_sha256(checked["requestId"], field_name="requestId")
    _require_sha256(checked["rowBindingsHash"], field_name="rowBindingsHash")
    _require_opaque(checked["ownerKey"], field_name="ownerKey")
    _require_opaque(checked["workKey"], field_name="workKey")
    _require_sha256(checked["payloadHash"], field_name="payloadHash")
    _require_sha256(checked["claimSetHash"], field_name="claimSetHash")
    if type(checked["rowBindings"]) is not list:
        raise RowAuthorityConfigError("claim rowBindings must be an exact list")
    row_ids = []
    for binding in checked["rowBindings"]:
        item = _require_exact_dict(
            binding,
            keys=_ROW_BINDING_KEYS,
            field_name="claim row binding",
        )
        if type(item["role"]) is not str or item["role"] not in {"primary", "related"}:
            raise RowAuthorityConfigError("claim row binding role is unsupported")
        row_ids.append(validate_row_id(item["rowId"]))
    bindings, primary, count, bindings_hash = _canonical_claim_bindings(
        user_scope_hash=scope,
        row_ids=row_ids,
        primary_row_id=checked["primaryRowId"],
    )
    if (
        checked["rowBindings"] != bindings
        or checked["bindingCount"] != count
        or type(checked["bindingCount"]) is not int
        or checked["rowBindingsHash"] != bindings_hash
        or checked["primaryRowId"] != primary
    ):
        raise RowAuthorityConfigError("claim row binding material conflicts")
    _validate_claim_origin_document(checked)
    priority = derive_owner_priority(checked["ownerKind"])
    if type(checked["derivedPriority"]) is not int or checked["derivedPriority"] != priority:
        raise RowAuthorityConfigError("claim priority is not derived")
    _require_row_authority_planned_writes(checked["plannedWrites"])
    decisions = _validated_claim_decisions(
        row_decisions=checked["rowDecisions"],
        row_bindings=bindings,
        outcome=checked["outcome"],
    )
    if checked["rowDecisions"] != decisions:
        raise RowAuthorityConfigError("claim row decisions are not canonical")
    _require_timestamp(checked["createdAt"], field_name="createdAt")
    if checked["requestId"] != domain_hash(
        CLAIM_REQUEST_ID_DOMAIN,
        _request_id_payload(checked),
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("claim requestId does not recompute")
    if checked["claimSetHash"] != domain_hash(
        CLAIM_SET_HASH_DOMAIN,
        _claim_set_hash_payload(checked),
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("claimSetHash does not recompute")
    return _defensive_copy(checked)


def _generation_hash_payload(document):
    return {
        key: document[key]
        for key in (
            "rowId",
            "generation",
            "requestId",
            "claimSetHash",
            "predecessorHeadHash",
            "predecessorSettlementHash",
            "ownerKind",
            "ownerKey",
            "priority",
            "leaseEpoch",
            "firstFencingToken",
            "createdAt",
        )
    }


def build_owner_generation_document(
    *,
    claim_set_document,
    row_id,
    generation,
    predecessor_head_hash,
    predecessor_settlement_hash,
    lease_epoch,
    first_fencing_token,
    created_at,
):
    claim = validate_claim_set_document(document=claim_set_document)
    checked_row = validate_row_id(row_id)
    checked_generation = _require_pos(generation, field_name="generation")
    if claim["outcome"] != "accepted":
        raise RowAuthorityConfigError("generation requires an accepted claim")
    matches = [item for item in claim["rowDecisions"] if item["rowId"] == checked_row]
    if (
        len(matches) != 1
        or matches[0]["decision"] != "accepted"
        or matches[0]["plannedGeneration"] != checked_generation
    ):
        raise RowAuthorityConfigError("generation conflicts with claim decision")
    predecessor_head = _require_sha256(
        predecessor_head_hash,
        field_name="predecessor_head_hash",
    )
    predecessor_settlement = _require_optional_hash(
        predecessor_settlement_hash,
        field_name="predecessor_settlement_hash",
    )
    if type(lease_epoch) is not int or lease_epoch != 1:
        raise RowAuthorityConfigError("new generations require leaseEpoch 1")
    fence = _require_pos(first_fencing_token, field_name="first_fencing_token")
    created = _require_timestamp(created_at, field_name="created_at")
    if created < claim["createdAt"]:
        raise RowAuthorityConfigError("generation cannot predate its claim")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": claim["userScopeHash"],
        "rowId": checked_row,
        "generation": checked_generation,
        "requestId": claim["requestId"],
        "claimSetHash": claim["claimSetHash"],
        "predecessorHeadHash": predecessor_head,
        "predecessorSettlementHash": predecessor_settlement,
        "ownerKind": claim["ownerKind"],
        "ownerKey": claim["ownerKey"],
        "priority": derive_owner_priority(claim["ownerKind"]),
        "leaseEpoch": 1,
        "firstFencingToken": fence,
        "createdAt": created,
    }
    document["generationHash"] = domain_hash(
        OWNER_GENERATION_HASH_DOMAIN,
        _generation_hash_payload(document),
        user_scope_hash=document["userScopeHash"],
    )
    return document


def validate_owner_generation_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_OWNER_GENERATION_KEYS,
        field_name="owner generation",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("generation schemaVersion must be 1")
    scope = _require_sha256(checked["userScopeHash"], field_name="userScopeHash")
    validate_row_id(checked["rowId"])
    _require_pos(checked["generation"], field_name="generation")
    for field in (
        "requestId",
        "claimSetHash",
        "predecessorHeadHash",
        "generationHash",
    ):
        _require_sha256(checked[field], field_name=field)
    _require_opaque(checked["ownerKey"], field_name="ownerKey")
    _require_optional_hash(
        checked["predecessorSettlementHash"],
        field_name="predecessorSettlementHash",
    )
    priority = derive_owner_priority(checked["ownerKind"])
    if type(checked["priority"]) is not int or checked["priority"] != priority:
        raise RowAuthorityConfigError("generation priority is not derived")
    if type(checked["leaseEpoch"]) is not int or checked["leaseEpoch"] != 1:
        raise RowAuthorityConfigError("generation leaseEpoch must be 1")
    _require_pos(checked["firstFencingToken"], field_name="firstFencingToken")
    _require_timestamp(checked["createdAt"], field_name="createdAt")
    if checked["generationHash"] != domain_hash(
        OWNER_GENERATION_HASH_DOMAIN,
        _generation_hash_payload(checked),
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("generationHash does not recompute")
    return _defensive_copy(checked)


def _outcome_evidence_hash(
    *,
    user_scope_hash,
    authority_link_hash,
    operator_action_hash,
    fanout_id,
    payload_hash,
    outcome_reason_code,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    authority = _require_optional_hash(
        authority_link_hash,
        field_name="authority_link_hash",
    )
    operator = _require_optional_hash(
        operator_action_hash,
        field_name="operator_action_hash",
    )
    fanout = _require_optional_hash(fanout_id, field_name="fanout_id")
    payload = _require_sha256(payload_hash, field_name="payload_hash")
    if type(outcome_reason_code) is not str or outcome_reason_code not in set(
        _SETTLEMENT_REASON_BY_OUTCOME.values()
    ):
        raise RowAuthorityConfigError("outcome reason code is unsupported")
    return domain_hash(
        OUTCOME_EVIDENCE_HASH_DOMAIN,
        {
            "authorityLinkHash": authority,
            "operatorActionHash": operator,
            "fanoutId": fanout,
            "payloadHash": payload,
            "outcomeReasonCode": outcome_reason_code,
        },
        user_scope_hash=scope,
    )


def _logical_outcome_hash(
    *,
    user_scope_hash,
    row_id,
    generation,
    owner_kind,
    owner_key,
    outcome,
    outcome_reason_code,
    outcome_evidence_hash,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    checked_row = validate_row_id(row_id)
    checked_generation = _require_pos(generation, field_name="generation")
    derive_owner_priority(owner_kind)
    checked_owner_key = _require_opaque(owner_key, field_name="owner_key")
    if type(outcome) is not str or outcome not in _SETTLEMENT_REASON_BY_OUTCOME:
        raise RowAuthorityConfigError("logical outcome is unsupported")
    if _SETTLEMENT_REASON_BY_OUTCOME[outcome] != outcome_reason_code:
        raise RowAuthorityConfigError("logical outcome reason conflicts")
    evidence_hash = _require_sha256(
        outcome_evidence_hash,
        field_name="outcome_evidence_hash",
    )
    return domain_hash(
        LOGICAL_OUTCOME_HASH_DOMAIN,
        {
            "rowId": checked_row,
            "generation": checked_generation,
            "ownerKind": owner_kind,
            "ownerKey": checked_owner_key,
            "outcome": outcome,
            "outcomeReasonCode": outcome_reason_code,
            "outcomeEvidenceHash": evidence_hash,
        },
        user_scope_hash=scope,
    )


def _settlement_hash_payload(document):
    return {
        key: document[key]
        for key in (
            "rowId",
            "generation",
            "generationHash",
            "fencingToken",
            "outcome",
            "dominantGenerationHash",
            "supersededEffectiveSettlementHash",
            "operatorActionHash",
            "outcomeReasonCode",
            "outcomeEvidenceHash",
            "logicalOutcomeHash",
            "settledAt",
        )
    }


def build_owner_settlement_document(
    *,
    generation_document,
    claim_set_document,
    fencing_token,
    outcome,
    settled_at,
    dominant_generation_hash=None,
    superseded_effective_settlement_hash=None,
    operator_action_document=None,
):
    generation = validate_owner_generation_document(document=generation_document)
    claim = validate_claim_set_document(document=claim_set_document)
    if (
        generation["userScopeHash"] != claim["userScopeHash"]
        or generation["requestId"] != claim["requestId"]
        or generation["claimSetHash"] != claim["claimSetHash"]
        or generation["ownerKind"] != claim["ownerKind"]
        or generation["ownerKey"] != claim["ownerKey"]
    ):
        raise RowAuthorityConfigError("settlement generation and claim conflict")
    matching_decisions = [
        decision
        for decision in claim["rowDecisions"]
        if decision["rowId"] == generation["rowId"]
    ]
    if (
        claim["outcome"] != "accepted"
        or len(matching_decisions) != 1
        or matching_decisions[0]["decision"] != "accepted"
        or matching_decisions[0]["plannedGeneration"]
        != generation["generation"]
    ):
        raise RowAuthorityConfigError(
            "settlement generation is not accepted by the claim"
        )
    if type(outcome) is not str or outcome not in _SETTLEMENT_REASON_BY_OUTCOME:
        raise RowAuthorityConfigError("settlement outcome is unsupported")
    fence = _require_pos(fencing_token, field_name="fencing_token")
    if fence < generation["firstFencingToken"]:
        raise RowAuthorityConfigError(
            "settlement fence predates the generation's first fence"
        )
    dominant = _require_optional_hash(
        dominant_generation_hash,
        field_name="dominant_generation_hash",
    )
    superseded = _require_optional_hash(
        superseded_effective_settlement_hash,
        field_name="superseded_effective_settlement_hash",
    )
    action_hash = None
    if operator_action_document is not None:
        action = validate_operator_action_document(document=operator_action_document)
        if (
            action["userScopeHash"] != claim["userScopeHash"]
            or action["rowBindingsHash"] != claim["rowBindingsHash"]
        ):
            raise RowAuthorityConfigError("settlement operator action conflicts")
        action_hash = action["operatorActionHash"]
    if outcome == "contact_optout":
        valid = (
            generation["ownerKind"] == "contact_optout"
            and dominant is None
            and superseded == generation["predecessorSettlementHash"]
            and action_hash is None
        )
    elif outcome == "terminal":
        valid = (
            generation["ownerKind"] == "terminal"
            and dominant is None
            and superseded is None
            and action_hash is None
        )
    elif outcome == "human_declined":
        valid = (
            generation["ownerKind"] == "human_decision"
            and dominant is None
            and superseded is None
            and action_hash is not None
            and (
                claim["authorityOrigin"] != "authenticated_operator"
                or claim["operatorActionHash"] == action_hash
            )
        )
    else:
        valid = (
            generation["ownerKind"] in {"human_decision", "terminal"}
            and dominant is not None
            and dominant != generation["generationHash"]
            and superseded is None
            and action_hash is None
        )
    if not valid:
        raise RowAuthorityConfigError("settlement conditional fields conflict")
    settled = _require_timestamp(settled_at, field_name="settled_at")
    if settled < generation["createdAt"]:
        raise RowAuthorityConfigError("settlement cannot predate its generation")
    reason = _SETTLEMENT_REASON_BY_OUTCOME[outcome]
    effective_action_hash = action_hash or claim["operatorActionHash"]
    evidence_hash = _outcome_evidence_hash(
        user_scope_hash=claim["userScopeHash"],
        authority_link_hash=claim["authorityLinkHash"],
        operator_action_hash=effective_action_hash,
        fanout_id=claim["fanoutId"],
        payload_hash=claim["payloadHash"],
        outcome_reason_code=reason,
    )
    logical_hash = _logical_outcome_hash(
        user_scope_hash=claim["userScopeHash"],
        row_id=generation["rowId"],
        generation=generation["generation"],
        owner_kind=generation["ownerKind"],
        owner_key=generation["ownerKey"],
        outcome=outcome,
        outcome_reason_code=reason,
        outcome_evidence_hash=evidence_hash,
    )
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": claim["userScopeHash"],
        "rowId": generation["rowId"],
        "generation": generation["generation"],
        "generationHash": generation["generationHash"],
        "fencingToken": fence,
        "outcome": outcome,
        "dominantGenerationHash": dominant,
        "supersededEffectiveSettlementHash": superseded,
        "operatorActionHash": action_hash,
        "outcomeReasonCode": reason,
        "outcomeEvidenceHash": evidence_hash,
        "logicalOutcomeHash": logical_hash,
        "settledAt": settled,
    }
    document["settlementHash"] = domain_hash(
        OWNER_SETTLEMENT_HASH_DOMAIN,
        _settlement_hash_payload(document),
        user_scope_hash=document["userScopeHash"],
    )
    return document


def validate_owner_settlement_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_OWNER_SETTLEMENT_KEYS,
        field_name="owner settlement",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("settlement schemaVersion must be 1")
    scope = _require_sha256(checked["userScopeHash"], field_name="userScopeHash")
    validate_row_id(checked["rowId"])
    _require_pos(checked["generation"], field_name="generation")
    _require_sha256(checked["generationHash"], field_name="generationHash")
    _require_pos(checked["fencingToken"], field_name="fencingToken")
    outcome = checked["outcome"]
    if type(outcome) is not str or outcome not in _SETTLEMENT_REASON_BY_OUTCOME:
        raise RowAuthorityConfigError("settlement outcome is unsupported")
    dominant = _require_optional_hash(
        checked["dominantGenerationHash"],
        field_name="dominantGenerationHash",
    )
    superseded = _require_optional_hash(
        checked["supersededEffectiveSettlementHash"],
        field_name="supersededEffectiveSettlementHash",
    )
    operator = _require_optional_hash(
        checked["operatorActionHash"],
        field_name="operatorActionHash",
    )
    valid = {
        "contact_optout": dominant is None and operator is None,
        "terminal": dominant is None and superseded is None and operator is None,
        "human_declined": dominant is None and superseded is None and operator is not None,
        "dominated": dominant is not None and superseded is None and operator is None,
    }[outcome]
    if not valid or checked["outcomeReasonCode"] != _SETTLEMENT_REASON_BY_OUTCOME[outcome]:
        raise RowAuthorityConfigError("settlement conditional fields conflict")
    for field in ("outcomeEvidenceHash", "logicalOutcomeHash", "settlementHash"):
        _require_sha256(checked[field], field_name=field)
    _require_timestamp(checked["settledAt"], field_name="settledAt")
    if checked["settlementHash"] != domain_hash(
        OWNER_SETTLEMENT_HASH_DOMAIN,
        _settlement_hash_payload(checked),
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("settlementHash does not recompute")
    return _defensive_copy(checked)


def _source_settlement_link_hash_payload(document):
    return {
        key: document[key]
        for key in (
            "rowId",
            "generation",
            "generationHash",
            "authorityLinkHash",
            "b1IdentityHash",
            "b1FinalLedgerEvidenceHash",
            "b1SettlementRevision",
            "b1SettlementHash",
            "b2SettlementHash",
            "linkedAt",
        )
    }


def build_source_settlement_link_document(
    *,
    user_scope_hash,
    row_id,
    generation,
    generation_hash,
    authority_link_hash,
    b1_identity_hash,
    b1_final_ledger_evidence_hash,
    b1_settlement_revision,
    b1_settlement_hash,
    b2_settlement_hash,
    linked_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        "rowId": validate_row_id(row_id),
        "generation": _require_pos(generation, field_name="generation"),
        "generationHash": _require_sha256(generation_hash, field_name="generation_hash"),
        "authorityLinkHash": _require_sha256(authority_link_hash, field_name="authority_link_hash"),
        "b1IdentityHash": _require_sha256(b1_identity_hash, field_name="b1_identity_hash"),
        "b1FinalLedgerEvidenceHash": _require_sha256(
            b1_final_ledger_evidence_hash,
            field_name="b1_final_ledger_evidence_hash",
        ),
        "b1SettlementRevision": _require_pos(
            b1_settlement_revision,
            field_name="b1_settlement_revision",
        ),
        "b1SettlementHash": _require_sha256(b1_settlement_hash, field_name="b1_settlement_hash"),
        "b2SettlementHash": _require_sha256(b2_settlement_hash, field_name="b2_settlement_hash"),
        "linkedAt": _require_timestamp(linked_at, field_name="linked_at"),
    }
    document["sourceSettlementLinkHash"] = domain_hash(
        SOURCE_SETTLEMENT_LINK_HASH_DOMAIN,
        _source_settlement_link_hash_payload(document),
        user_scope_hash=scope,
    )
    return document


def validate_source_settlement_link_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_SOURCE_SETTLEMENT_LINK_KEYS,
        field_name="source settlement link",
    )
    if type(checked["schemaVersion"]) is not int or checked["schemaVersion"] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("source link schemaVersion must be 1")
    scope = _require_sha256(checked["userScopeHash"], field_name="userScopeHash")
    validate_row_id(checked["rowId"])
    _require_pos(checked["generation"], field_name="generation")
    _require_pos(checked["b1SettlementRevision"], field_name="b1SettlementRevision")
    for field in (
        "generationHash",
        "authorityLinkHash",
        "b1IdentityHash",
        "b1FinalLedgerEvidenceHash",
        "b1SettlementHash",
        "b2SettlementHash",
        "sourceSettlementLinkHash",
    ):
        _require_sha256(checked[field], field_name=field)
    _require_timestamp(checked["linkedAt"], field_name="linkedAt")
    if checked["sourceSettlementLinkHash"] != domain_hash(
        SOURCE_SETTLEMENT_LINK_HASH_DOMAIN,
        _source_settlement_link_hash_payload(checked),
        user_scope_hash=scope,
    ):
        raise RowAuthorityConfigError("sourceSettlementLinkHash does not recompute")
    return _defensive_copy(checked)


_ROW_BINDING_KEYS = frozenset({"rowId", "role"})
_THREAD_ROW_BINDING_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "threadId",
        "clientId",
        "rowBindings",
        "primaryRowId",
        "bindingCount",
        "rowBindingsHash",
        "bindingHash",
        "createdAt",
    }
)
_ROW_THREAD_BINDING_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "edgeId",
        "rowId",
        "threadId",
        "role",
        "threadBindingHash",
        "edgeHash",
        "createdAt",
    }
)


def _require_thread_document_id(value, *, field_name):
    checked = _require_opaque(value, field_name=field_name)
    return _require_firestore_document_id(checked, field_name=field_name)


def normalize_row_bindings(row_ids, primary_row_id):
    checked_primary = validate_row_id(primary_row_id)
    if type(row_ids) not in {list, tuple}:
        raise RowAuthorityConfigError(
            "row_ids must be an ordered list or tuple"
        )
    unique_row_ids = {validate_row_id(row_id) for row_id in row_ids}
    if not unique_row_ids:
        raise RowAuthorityConfigError("row bindings cannot be empty")
    if len(unique_row_ids) > MAX_ROW_BINDINGS:
        raise RowAuthorityConfigError("row bindings exceed the 128-row bound")
    if checked_primary not in unique_row_ids:
        raise RowAuthorityConfigError(
            "primary_row_id must be present in row bindings"
        )
    return [
        {
            "rowId": row_id,
            "role": "primary" if row_id == checked_primary else "related",
        }
        for row_id in sorted(unique_row_ids)
    ]


def _row_bindings_hash(
    *, user_scope_hash, row_bindings, primary_row_id, binding_count
):
    return domain_hash(
        ROW_BINDINGS_HASH_DOMAIN,
        {
            "rowBindings": [dict(binding) for binding in row_bindings],
            "primaryRowId": primary_row_id,
            "bindingCount": binding_count,
        },
        user_scope_hash=user_scope_hash,
    )


def build_thread_row_binding_document(
    *,
    user_scope_hash,
    thread_id,
    client_id,
    row_ids,
    primary_row_id,
    created_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_thread_id = _require_thread_document_id(
        thread_id,
        field_name="thread_id",
    )
    checked_client_id = _require_opaque(client_id, field_name="client_id")
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    canonical_bindings = normalize_row_bindings(row_ids, primary_row_id)
    checked_primary = validate_row_id(primary_row_id)
    binding_count = len(canonical_bindings)
    row_bindings_hash = _row_bindings_hash(
        user_scope_hash=checked_scope,
        row_bindings=canonical_bindings,
        primary_row_id=checked_primary,
        binding_count=binding_count,
    )
    binding_hash = domain_hash(
        THREAD_ROW_BINDING_HASH_DOMAIN,
        {
            "threadId": checked_thread_id,
            "clientId": checked_client_id,
            "rowBindingsHash": row_bindings_hash,
            "primaryRowId": checked_primary,
            "bindingCount": binding_count,
            "createdAt": checked_created_at,
        },
        user_scope_hash=checked_scope,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "threadId": checked_thread_id,
        "clientId": checked_client_id,
        "rowBindings": [dict(binding) for binding in canonical_bindings],
        "primaryRowId": checked_primary,
        "bindingCount": binding_count,
        "rowBindingsHash": row_bindings_hash,
        "bindingHash": binding_hash,
        "createdAt": checked_created_at,
    }


def validate_thread_row_binding_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_THREAD_ROW_BINDING_KEYS,
        field_name="thread row binding document",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(
            "thread row binding schemaVersion must be 1"
        )
    if type(checked["rowBindings"]) is not list:
        raise RowAuthorityConfigError(
            "persisted rowBindings must be a canonical list"
        )
    row_ids = []
    for index, binding in enumerate(checked["rowBindings"]):
        item = _require_exact_dict(
            binding,
            keys=_ROW_BINDING_KEYS,
            field_name=f"rowBindings[{index}]",
        )
        row_ids.append(validate_row_id(item["rowId"]))
        if type(item["role"]) is not str or item["role"] not in {
            "primary",
            "related",
        }:
            raise RowAuthorityConfigError("row binding role is not approved")
    expected = build_thread_row_binding_document(
        user_scope_hash=checked["userScopeHash"],
        thread_id=checked["threadId"],
        client_id=checked["clientId"],
        row_ids=row_ids,
        primary_row_id=checked["primaryRowId"],
        created_at=checked["createdAt"],
    )
    _require_pos(checked["bindingCount"], field_name="bindingCount")
    _require_sha256(
        checked["rowBindingsHash"],
        field_name="rowBindingsHash",
    )
    _require_sha256(checked["bindingHash"], field_name="bindingHash")
    if checked != expected:
        raise RowAuthorityConfigError(
            "thread row binding does not match its canonical fields and hashes"
        )
    return {
        **expected,
        "rowBindings": [dict(binding) for binding in expected["rowBindings"]],
    }


def _build_row_thread_binding_document(
    *,
    user_scope_hash,
    row_id,
    thread_id,
    role,
    thread_binding_hash,
    created_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_row_id = validate_row_id(row_id)
    checked_thread_id = _require_thread_document_id(
        thread_id,
        field_name="thread_id",
    )
    if type(role) is not str or role not in {"primary", "related"}:
        raise RowAuthorityConfigError("row thread binding role is not approved")
    checked_binding_hash = _require_sha256(
        thread_binding_hash,
        field_name="thread_binding_hash",
    )
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    edge_id = domain_hash(
        ROW_THREAD_EDGE_ID_DOMAIN,
        {"rowId": checked_row_id, "threadId": checked_thread_id},
        user_scope_hash=checked_scope,
    )
    edge_hash = domain_hash(
        ROW_THREAD_EDGE_HASH_DOMAIN,
        {
            "edgeId": edge_id,
            "rowId": checked_row_id,
            "threadId": checked_thread_id,
            "role": role,
            "threadBindingHash": checked_binding_hash,
            "createdAt": checked_created_at,
        },
        user_scope_hash=checked_scope,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "edgeId": edge_id,
        "rowId": checked_row_id,
        "threadId": checked_thread_id,
        "role": role,
        "threadBindingHash": checked_binding_hash,
        "edgeHash": edge_hash,
        "createdAt": checked_created_at,
    }


def build_row_thread_binding_documents(*, thread_binding_document):
    binding = validate_thread_row_binding_document(
        document=thread_binding_document
    )
    return [
        _build_row_thread_binding_document(
            user_scope_hash=binding["userScopeHash"],
            row_id=row_binding["rowId"],
            thread_id=binding["threadId"],
            role=row_binding["role"],
            thread_binding_hash=binding["bindingHash"],
            created_at=binding["createdAt"],
        )
        for row_binding in binding["rowBindings"]
    ]


def validate_row_thread_binding_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_ROW_THREAD_BINDING_KEYS,
        field_name="row thread binding document",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(
            "row thread binding schemaVersion must be 1"
        )
    expected = _build_row_thread_binding_document(
        user_scope_hash=checked["userScopeHash"],
        row_id=checked["rowId"],
        thread_id=checked["threadId"],
        role=checked["role"],
        thread_binding_hash=checked["threadBindingHash"],
        created_at=checked["createdAt"],
    )
    _require_sha256(checked["edgeId"], field_name="edgeId")
    _require_sha256(checked["edgeHash"], field_name="edgeHash")
    if checked != expected:
        raise RowAuthorityConfigError(
            "row thread binding does not match its canonical fields and hashes"
        )
    return dict(expected)


_CONTACT_ALIAS_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "exactIdentityHash",
        "canonicalMailboxIdentityHash",
        "contactAliasHash",
        "createdAt",
    }
)
_CONTACT_TRANSITION_REQUEST_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "contactTransitionId",
        "transitionKind",
        "exactIdentityHash",
        "canonicalMailboxIdentityHash",
        "authorityLinkHash",
        "hardOptOutEvidenceHash",
        "actorScopeHash",
        "clientRequestHash",
        "expectedActiveOptOutSettlementHash",
        "reasonCode",
        "outcome",
        "resultingContactGeneration",
        "resultingContactSettlementHash",
        "resultingFanoutId",
        "resultingContactHeadHash",
        "resultingFanoutHeadHash",
        "requestedAt",
        "contactTransitionRequestHash",
    }
)
_CONTACT_SETTLEMENT_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "canonicalMailboxIdentityHash",
        "generation",
        "predecessorSettlementHash",
        "transitionKind",
        "contactTransitionId",
        "exactIdentityHash",
        "authorityLink",
        "authorityLinkHash",
        "hardOptOutEvidenceHash",
        "actorScopeHash",
        "reasonCode",
        "contactSettlementHash",
        "settledAt",
    }
)
_CONTACT_HEAD_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "canonicalMailboxIdentityHash",
        "stateRevision",
        "latestGeneration",
        "latestSettlementHash",
        "activeOptOutSettlementHash",
        "state",
        "activeFanoutId",
        "contactHeadHash",
        "createdAt",
        "updatedAt",
    }
)
_CONTACT_FANOUT_HEAD_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "fanoutId",
        "outcome",
        "expectedContactSettlementHash",
        "stateRevision",
        "state",
        "bindingRevision",
        "bindingHeadHash",
        "bindingAssociationCount",
        "discoveryCursorRowId",
        "obligationCount",
        "resultCount",
        "leaseOwnerHash",
        "leaseUntil",
        "fencingToken",
        "supersedingContactSettlementHash",
        "completionBindingRevision",
        "completionBindingHeadHash",
        "completionBindingAssociationCount",
        "completionObligationCount",
        "completionResultCount",
        "completedAt",
        "contactFanoutHeadHash",
        "createdAt",
        "updatedAt",
    }
)
_CONTACT_FANOUT_OBLIGATION_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "fanoutId",
        "rowId",
        "contactRowEdgeHash",
        "expectedContactSettlementHash",
        "outcome",
        "contactFanoutObligationHash",
        "createdAt",
    }
)
_CONTACT_FANOUT_RESULT_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "fanoutId",
        "rowId",
        "obligationHash",
        "outcome",
        "disposition",
        "reasonCode",
        "observedRowHeadHash",
        "claimRequestId",
        "claimSetHash",
        "rowGeneration",
        "rowSettlementHash",
        "releasedRowGeneration",
        "releasedRowSettlementHash",
        "restoredEffectiveGeneration",
        "restoredEffectiveSettlementHash",
        "contactFanoutResultHash",
        "createdAt",
    }
)


def _require_contact_document(document, *, keys, field_name):
    checked = _require_exact_dict(document, keys=keys, field_name=field_name)
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(f"{field_name} schemaVersion must be 1")
    return checked


def _require_contact_fanout_outcome(value, *, field_name="outcome"):
    if type(value) is not str or value not in {"apply", "release"}:
        raise RowAuthorityConfigError(f"{field_name} must be apply or release")
    return value


def _derive_contact_fanout_id(*, user_scope_hash, settlement_hash, outcome):
    return domain_hash(
        CONTACT_FANOUT_ID_DOMAIN,
        {
            "contactSettlementHash": _require_sha256(
                settlement_hash,
                field_name="contact settlement hash",
            ),
            "outcome": _require_contact_fanout_outcome(outcome),
        },
        user_scope_hash=_require_sha256(
            user_scope_hash,
            field_name="user_scope_hash",
        ),
    )


def build_contact_alias_document(
    *,
    user_scope_hash,
    exact_identity_hash,
    canonical_mailbox_identity_hash,
    created_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    exact_hash = _require_sha256(
        exact_identity_hash,
        field_name="exact_identity_hash",
    )
    canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    created = _require_timestamp(created_at, field_name="created_at")
    payload = {
        "exactIdentityHash": exact_hash,
        "canonicalMailboxIdentityHash": canonical_hash,
        "createdAt": created,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactAliasHash": domain_hash(
            CONTACT_ALIAS_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
    }


def validate_contact_alias_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_ALIAS_KEYS,
        field_name="contact alias document",
    )
    expected = build_contact_alias_document(
        user_scope_hash=checked["userScopeHash"],
        exact_identity_hash=checked["exactIdentityHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        created_at=checked["createdAt"],
    )
    _require_sha256(checked["contactAliasHash"], field_name="contactAliasHash")
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact alias does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


def build_contact_transition_request_document(
    *,
    user_scope_hash,
    transition_kind,
    exact_identity_hash,
    canonical_mailbox_identity_hash,
    authority_link_hash,
    hard_optout_evidence_hash,
    actor_scope_hash,
    client_request_hash,
    expected_active_optout_settlement_hash,
    reason_code,
    outcome,
    resulting_contact_generation,
    resulting_contact_settlement_hash,
    resulting_fanout_id,
    resulting_contact_head_hash,
    resulting_fanout_head_hash,
    requested_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    exact_hash = _require_sha256(
        exact_identity_hash,
        field_name="exact_identity_hash",
    )
    canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    authority_hash = _require_optional_hash(
        authority_link_hash,
        field_name="authority_link_hash",
    )
    hard_hash = _require_optional_hash(
        hard_optout_evidence_hash,
        field_name="hard_optout_evidence_hash",
    )
    actor_hash = _require_optional_hash(
        actor_scope_hash,
        field_name="actor_scope_hash",
    )
    request_hash = _require_optional_hash(
        client_request_hash,
        field_name="client_request_hash",
    )
    expected_active_hash = _require_optional_hash(
        expected_active_optout_settlement_hash,
        field_name="expected_active_optout_settlement_hash",
    )
    if transition_kind == "verified_optout":
        if (
            authority_hash is None
            or hard_hash is None
            or actor_hash is not None
            or request_hash is not None
            or expected_active_hash is not None
            or reason_code is not None
            or outcome not in {"created", "already_active"}
        ):
            raise RowAuthorityConfigError(
                "verified contact transition fields are miscorrelated"
            )
        fanout_outcome = "apply"
    elif transition_kind == "authenticated_release":
        if (
            authority_hash is not None
            or hard_hash is not None
            or actor_hash is None
            or request_hash is None
            or expected_active_hash is None
            or reason_code != "authenticated_release"
            or outcome != "created"
        ):
            raise RowAuthorityConfigError(
                "authenticated release transition fields are miscorrelated"
            )
        fanout_outcome = "release"
    else:
        raise RowAuthorityConfigError("contact transition kind is unsupported")
    generation = _require_pos(
        resulting_contact_generation,
        field_name="resulting_contact_generation",
    )
    if (transition_kind == "verified_optout") != (generation % 2 == 1):
        raise RowAuthorityConfigError(
            "contact transition kind does not match its generation epoch"
        )
    settlement_hash = _require_sha256(
        resulting_contact_settlement_hash,
        field_name="resulting_contact_settlement_hash",
    )
    checked_fanout_id = _require_sha256(
        resulting_fanout_id,
        field_name="resulting_fanout_id",
    )
    expected_fanout_id = _derive_contact_fanout_id(
        user_scope_hash=scope,
        settlement_hash=settlement_hash,
        outcome=fanout_outcome,
    )
    if checked_fanout_id != expected_fanout_id:
        raise RowAuthorityConfigError(
            "contact transition fan-out ID does not recompute"
        )
    contact_head_hash = _require_sha256(
        resulting_contact_head_hash,
        field_name="resulting_contact_head_hash",
    )
    fanout_head_hash = _require_sha256(
        resulting_fanout_head_hash,
        field_name="resulting_fanout_head_hash",
    )
    requested = _require_timestamp(requested_at, field_name="requested_at")
    id_payload = {
        "transitionKind": transition_kind,
        "exactIdentityHash": exact_hash,
        "canonicalMailboxIdentityHash": canonical_hash,
        "authorityLinkHash": authority_hash,
        "hardOptOutEvidenceHash": hard_hash,
        "actorScopeHash": actor_hash,
        "clientRequestHash": request_hash,
        "expectedActiveOptOutSettlementHash": expected_active_hash,
        "reasonCode": reason_code,
    }
    transition_id = domain_hash(
        CONTACT_TRANSITION_ID_DOMAIN,
        id_payload,
        user_scope_hash=scope,
    )
    payload = {
        "contactTransitionId": transition_id,
        **id_payload,
        "outcome": outcome,
        "resultingContactGeneration": generation,
        "resultingContactSettlementHash": settlement_hash,
        "resultingFanoutId": checked_fanout_id,
        "resultingContactHeadHash": contact_head_hash,
        "resultingFanoutHeadHash": fanout_head_hash,
        "requestedAt": requested,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactTransitionRequestHash": domain_hash(
            CONTACT_TRANSITION_REQUEST_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
    }


def validate_contact_transition_request_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_TRANSITION_REQUEST_KEYS,
        field_name="contact transition request document",
    )
    expected = build_contact_transition_request_document(
        user_scope_hash=checked["userScopeHash"],
        transition_kind=checked["transitionKind"],
        exact_identity_hash=checked["exactIdentityHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        authority_link_hash=checked["authorityLinkHash"],
        hard_optout_evidence_hash=checked["hardOptOutEvidenceHash"],
        actor_scope_hash=checked["actorScopeHash"],
        client_request_hash=checked["clientRequestHash"],
        expected_active_optout_settlement_hash=checked[
            "expectedActiveOptOutSettlementHash"
        ],
        reason_code=checked["reasonCode"],
        outcome=checked["outcome"],
        resulting_contact_generation=checked["resultingContactGeneration"],
        resulting_contact_settlement_hash=checked[
            "resultingContactSettlementHash"
        ],
        resulting_fanout_id=checked["resultingFanoutId"],
        resulting_contact_head_hash=checked["resultingContactHeadHash"],
        resulting_fanout_head_hash=checked["resultingFanoutHeadHash"],
        requested_at=checked["requestedAt"],
    )
    for field in ("contactTransitionId", "contactTransitionRequestHash"):
        _require_sha256(checked[field], field_name=field)
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact transition request does not match canonical fields and hashes"
        )
    return _defensive_copy(expected)


def build_contact_settlement_document(
    *,
    user_scope_hash,
    canonical_mailbox_identity_hash,
    generation,
    predecessor_settlement_hash,
    transition_kind,
    contact_transition_id,
    exact_identity_hash,
    authority_link,
    actor_scope_hash,
    reason_code,
    settled_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    checked_generation = _require_pos(generation, field_name="generation")
    predecessor_hash = _require_optional_hash(
        predecessor_settlement_hash,
        field_name="predecessor_settlement_hash",
    )
    if (checked_generation == 1) != (predecessor_hash is None):
        raise RowAuthorityConfigError(
            "contact settlement predecessor does not match generation"
        )
    transition_id = _require_sha256(
        contact_transition_id,
        field_name="contact_transition_id",
    )
    exact_hash = _require_sha256(
        exact_identity_hash,
        field_name="exact_identity_hash",
    )
    actor_hash = _require_optional_hash(
        actor_scope_hash,
        field_name="actor_scope_hash",
    )
    if transition_kind == "verified_optout":
        if type(authority_link) is not dict:
            raise RowAuthorityConfigError(
                "verified contact settlement requires a v2 B1 link"
            )
        checked_link = validate_b1_authority_link(
            authority_link=authority_link,
            user_scope_hash=scope,
        )
        if (
            set(checked_link) != _B1_LINK_V2_KEYS
            or checked_link["ownerKind"] != "contact_optout"
            or checked_link["exactIdentityHash"] != exact_hash
            or checked_link["canonicalMailboxIdentityHash"] != canonical_hash
            or actor_hash is not None
            or reason_code is not None
        ):
            raise RowAuthorityConfigError(
                "verified contact settlement origin is miscorrelated"
            )
        authority_hash = checked_link["authorityLinkHash"]
        hard_hash = checked_link["hardOptOutEvidenceHash"]
        expected_transition_id = domain_hash(
            CONTACT_TRANSITION_ID_DOMAIN,
            {
                "transitionKind": "verified_optout",
                "exactIdentityHash": exact_hash,
                "canonicalMailboxIdentityHash": canonical_hash,
                "authorityLinkHash": authority_hash,
                "hardOptOutEvidenceHash": hard_hash,
                "actorScopeHash": None,
                "clientRequestHash": None,
                "expectedActiveOptOutSettlementHash": None,
                "reasonCode": None,
            },
            user_scope_hash=scope,
        )
        if transition_id != expected_transition_id:
            raise RowAuthorityConfigError(
                "verified contact settlement transition ID does not recompute"
            )
        stored_link = _defensive_copy(checked_link)
    elif transition_kind == "authenticated_release":
        if (
            authority_link is not None
            or actor_hash is None
            or reason_code != "authenticated_release"
            or predecessor_hash is None
        ):
            raise RowAuthorityConfigError(
                "authenticated release settlement origin is miscorrelated"
            )
        stored_link = None
        authority_hash = None
        hard_hash = None
    else:
        raise RowAuthorityConfigError(
            "contact settlement transition kind is unsupported"
        )
    if (transition_kind == "verified_optout") != (
        checked_generation % 2 == 1
    ):
        raise RowAuthorityConfigError(
            "contact settlement kind does not match its generation epoch"
        )
    settled = _require_timestamp(settled_at, field_name="settled_at")
    payload = {
        "canonicalMailboxIdentityHash": canonical_hash,
        "generation": checked_generation,
        "predecessorSettlementHash": predecessor_hash,
        "transitionKind": transition_kind,
        "contactTransitionId": transition_id,
        "exactIdentityHash": exact_hash,
        "authorityLinkHash": authority_hash,
        "hardOptOutEvidenceHash": hard_hash,
        "actorScopeHash": actor_hash,
        "reasonCode": reason_code,
        "settledAt": settled,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        "canonicalMailboxIdentityHash": canonical_hash,
        "generation": checked_generation,
        "predecessorSettlementHash": predecessor_hash,
        "transitionKind": transition_kind,
        "contactTransitionId": transition_id,
        "exactIdentityHash": exact_hash,
        "authorityLink": stored_link,
        "authorityLinkHash": authority_hash,
        "hardOptOutEvidenceHash": hard_hash,
        "actorScopeHash": actor_hash,
        "reasonCode": reason_code,
        "contactSettlementHash": domain_hash(
            CONTACT_SETTLEMENT_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
        "settledAt": settled,
    }


def validate_contact_settlement_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_SETTLEMENT_KEYS,
        field_name="contact settlement document",
    )
    expected = build_contact_settlement_document(
        user_scope_hash=checked["userScopeHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        generation=checked["generation"],
        predecessor_settlement_hash=checked["predecessorSettlementHash"],
        transition_kind=checked["transitionKind"],
        contact_transition_id=checked["contactTransitionId"],
        exact_identity_hash=checked["exactIdentityHash"],
        authority_link=checked["authorityLink"],
        actor_scope_hash=checked["actorScopeHash"],
        reason_code=checked["reasonCode"],
        settled_at=checked["settledAt"],
    )
    for field in (
        "contactTransitionId",
        "authorityLinkHash",
        "hardOptOutEvidenceHash",
        "actorScopeHash",
        "contactSettlementHash",
    ):
        _require_optional_hash(checked[field], field_name=field)
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact settlement does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


def build_contact_head_document(
    *,
    user_scope_hash,
    canonical_mailbox_identity_hash,
    state_revision,
    latest_generation,
    latest_settlement_hash,
    active_optout_settlement_hash,
    state,
    active_fanout_id,
    created_at,
    updated_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    revision = _require_pos(state_revision, field_name="state_revision")
    generation = _require_pos(latest_generation, field_name="latest_generation")
    if revision != generation:
        raise RowAuthorityConfigError(
            "contact head state revision must equal its latest generation"
        )
    settlement_hash = _require_sha256(
        latest_settlement_hash,
        field_name="latest_settlement_hash",
    )
    active_hash = _require_optional_hash(
        active_optout_settlement_hash,
        field_name="active_optout_settlement_hash",
    )
    if state == "active":
        if active_hash != settlement_hash:
            raise RowAuthorityConfigError(
                "active contact head must point at its latest settlement"
            )
        fanout_outcome = "apply"
    elif state == "released":
        if active_hash is not None:
            raise RowAuthorityConfigError(
                "released contact head must clear active opt-out settlement"
            )
        fanout_outcome = "release"
    else:
        raise RowAuthorityConfigError("contact head state is unsupported")
    if (state == "active") != (generation % 2 == 1):
        raise RowAuthorityConfigError(
            "contact head state does not match its generation epoch"
        )
    fanout_id = _require_sha256(active_fanout_id, field_name="active_fanout_id")
    expected_fanout_id = _derive_contact_fanout_id(
        user_scope_hash=scope,
        settlement_hash=settlement_hash,
        outcome=fanout_outcome,
    )
    if fanout_id != expected_fanout_id:
        raise RowAuthorityConfigError("contact head fan-out ID does not recompute")
    created = _require_timestamp(created_at, field_name="created_at")
    updated = _require_timestamp(updated_at, field_name="updated_at")
    if updated < created:
        raise RowAuthorityConfigError("contact head update predates creation")
    payload = {
        "canonicalMailboxIdentityHash": canonical_hash,
        "stateRevision": revision,
        "latestGeneration": generation,
        "latestSettlementHash": settlement_hash,
        "activeOptOutSettlementHash": active_hash,
        "state": state,
        "activeFanoutId": fanout_id,
        "createdAt": created,
        "updatedAt": updated,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactHeadHash": domain_hash(
            CONTACT_HEAD_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
    }


def validate_contact_head_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_HEAD_KEYS,
        field_name="contact head document",
    )
    expected = build_contact_head_document(
        user_scope_hash=checked["userScopeHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        state_revision=checked["stateRevision"],
        latest_generation=checked["latestGeneration"],
        latest_settlement_hash=checked["latestSettlementHash"],
        active_optout_settlement_hash=checked["activeOptOutSettlementHash"],
        state=checked["state"],
        active_fanout_id=checked["activeFanoutId"],
        created_at=checked["createdAt"],
        updated_at=checked["updatedAt"],
    )
    for field in (
        "latestSettlementHash",
        "activeOptOutSettlementHash",
        "activeFanoutId",
        "contactHeadHash",
    ):
        _require_optional_hash(checked[field], field_name=field)
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact head does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


def build_contact_fanout_head_document(
    *,
    user_scope_hash,
    fanout_id,
    outcome,
    expected_contact_settlement_hash,
    state_revision,
    state,
    binding_revision,
    binding_head_hash,
    binding_association_count,
    discovery_cursor_row_id,
    obligation_count,
    result_count,
    lease_owner_hash,
    lease_until,
    fencing_token,
    superseding_contact_settlement_hash,
    completion_binding_revision,
    completion_binding_head_hash,
    completion_binding_association_count,
    completion_obligation_count,
    completion_result_count,
    completed_at,
    created_at,
    updated_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    checked_outcome = _require_contact_fanout_outcome(outcome)
    settlement_hash = _require_sha256(
        expected_contact_settlement_hash,
        field_name="expected_contact_settlement_hash",
    )
    checked_fanout_id = _require_sha256(fanout_id, field_name="fanout_id")
    if checked_fanout_id != _derive_contact_fanout_id(
        user_scope_hash=scope,
        settlement_hash=settlement_hash,
        outcome=checked_outcome,
    ):
        raise RowAuthorityConfigError("contact fan-out ID does not recompute")
    revision = _require_pos(state_revision, field_name="state_revision")
    if type(state) is not str or state not in {
        "discovering",
        "applying",
        "superseding",
        "complete",
        "superseded",
        "ambiguous",
    }:
        raise RowAuthorityConfigError("contact fan-out state is unsupported")
    binding_rev = _require_uint(
        binding_revision,
        field_name="binding_revision",
    )
    binding_hash = _require_optional_hash(
        binding_head_hash,
        field_name="binding_head_hash",
    )
    association_count = _require_uint(
        binding_association_count,
        field_name="binding_association_count",
    )
    if binding_rev == 0:
        if binding_hash is not None or association_count != 0:
            raise RowAuthorityConfigError(
                "zero binding revision requires null hash and zero associations"
            )
    elif binding_hash is None:
        raise RowAuthorityConfigError(
            "positive binding revision requires a binding hash"
        )
    if discovery_cursor_row_id is None:
        cursor = None
    else:
        cursor = validate_row_id(discovery_cursor_row_id)
    obligations = _require_uint(obligation_count, field_name="obligation_count")
    results = _require_uint(result_count, field_name="result_count")
    if results > obligations:
        raise RowAuthorityConfigError(
            "contact fan-out results cannot exceed obligations"
        )
    lease_owner = _require_optional_hash(
        lease_owner_hash,
        field_name="lease_owner_hash",
    )
    lease_deadline = (
        None
        if lease_until is None
        else _require_timestamp(lease_until, field_name="lease_until")
    )
    if (lease_owner is None) != (lease_deadline is None):
        raise RowAuthorityConfigError(
            "contact fan-out lease owner and deadline must be paired"
        )
    fence = _require_pos(fencing_token, field_name="fencing_token")
    if fence > revision:
        raise RowAuthorityConfigError(
            "contact fan-out fence cannot exceed its state revision"
        )
    superseding_hash = _require_optional_hash(
        superseding_contact_settlement_hash,
        field_name="superseding_contact_settlement_hash",
    )
    if state in {"superseding", "superseded"}:
        if superseding_hash is None:
            raise RowAuthorityConfigError(
                "superseding fan-out state requires a newer settlement"
            )
        if superseding_hash == settlement_hash:
            raise RowAuthorityConfigError(
                "superseding fan-out settlement must differ from its origin"
            )
    elif superseding_hash is not None:
        raise RowAuthorityConfigError(
            "non-superseding fan-out state cannot name a newer settlement"
        )
    if state in {"complete", "superseded", "ambiguous"}:
        if lease_owner is not None or cursor is not None:
            raise RowAuthorityConfigError(
                "terminal fan-out state requires null lease and cursor"
            )
    if state == "superseded" and results != obligations:
        raise RowAuthorityConfigError(
            "superseded fan-out requires exact result and obligation counts"
        )
    if state != "ambiguous" and obligations > association_count:
        raise RowAuthorityConfigError(
            "contact fan-out obligations exceed its binding snapshot"
        )

    optional_completion_values = (
        completion_binding_revision,
        completion_binding_head_hash,
        completion_binding_association_count,
        completion_obligation_count,
        completion_result_count,
        completed_at,
    )
    if state != "complete":
        if any(value is not None for value in optional_completion_values):
            raise RowAuthorityConfigError(
                "non-complete fan-out cannot carry a completion certificate"
            )
        completion_rev = None
        completion_hash = None
        completion_associations = None
        completion_obligations = None
        completion_results = None
        completed = None
    else:
        if any(
            value is None
            for value in (
                completion_binding_revision,
                completion_binding_association_count,
                completion_obligation_count,
                completion_result_count,
                completed_at,
            )
        ):
            raise RowAuthorityConfigError(
                "complete fan-out requires a complete certificate"
            )
        completion_rev = _require_uint(
            completion_binding_revision,
            field_name="completion_binding_revision",
        )
        completion_hash = _require_optional_hash(
            completion_binding_head_hash,
            field_name="completion_binding_head_hash",
        )
        if (completion_rev == 0) != (completion_hash is None):
            raise RowAuthorityConfigError(
                "completion binding revision and hash are miscorrelated"
            )
        completion_associations = _require_uint(
            completion_binding_association_count,
            field_name="completion_binding_association_count",
        )
        completion_obligations = _require_uint(
            completion_obligation_count,
            field_name="completion_obligation_count",
        )
        completion_results = _require_uint(
            completion_result_count,
            field_name="completion_result_count",
        )
        completed = _require_timestamp(completed_at, field_name="completed_at")
        if not (
            completion_obligations
            == completion_results
            == completion_associations
        ):
            raise RowAuthorityConfigError(
                "completion certificate counts are crossed"
            )
        if completion_rev == 0 and completion_associations != 0:
            raise RowAuthorityConfigError(
                "zero completion binding revision requires zero associations"
            )
        if completion_rev == binding_rev and completion_hash != binding_hash:
            raise RowAuthorityConfigError(
                "equal binding revisions require equal binding hashes"
            )
        if (
            completion_rev > binding_rev
            or completion_associations > association_count
            or binding_rev - completion_rev
            != association_count - completion_associations
        ):
            raise RowAuthorityConfigError(
                "completion binding revision and count deltas are crossed"
            )
        if checked_outcome == "apply":
            if not (obligations == results == association_count):
                raise RowAuthorityConfigError(
                    "complete apply fan-out counts are inconsistent"
                )
        elif not (
            obligations == results == completion_obligations
        ):
            raise RowAuthorityConfigError(
                "complete release fan-out counts are inconsistent"
            )

    created = _require_timestamp(created_at, field_name="created_at")
    updated = _require_timestamp(updated_at, field_name="updated_at")
    if updated < created:
        raise RowAuthorityConfigError("contact fan-out update predates creation")
    if lease_deadline is not None and lease_deadline <= updated:
        raise RowAuthorityConfigError(
            "contact fan-out lease must remain unexpired at its update"
        )
    if completed is not None and (completed < created or completed > updated):
        raise RowAuthorityConfigError(
            "contact fan-out completion time is outside its lifetime"
        )
    payload = {
        "fanoutId": checked_fanout_id,
        "outcome": checked_outcome,
        "expectedContactSettlementHash": settlement_hash,
        "stateRevision": revision,
        "state": state,
        "bindingRevision": binding_rev,
        "bindingHeadHash": binding_hash,
        "bindingAssociationCount": association_count,
        "discoveryCursorRowId": cursor,
        "obligationCount": obligations,
        "resultCount": results,
        "leaseOwnerHash": lease_owner,
        "leaseUntil": lease_deadline,
        "fencingToken": fence,
        "supersedingContactSettlementHash": superseding_hash,
        "completionBindingRevision": completion_rev,
        "completionBindingHeadHash": completion_hash,
        "completionBindingAssociationCount": completion_associations,
        "completionObligationCount": completion_obligations,
        "completionResultCount": completion_results,
        "completedAt": completed,
        "createdAt": created,
        "updatedAt": updated,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactFanoutHeadHash": domain_hash(
            CONTACT_FANOUT_HEAD_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
    }


def validate_contact_fanout_head_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_FANOUT_HEAD_KEYS,
        field_name="contact fan-out head document",
    )
    expected = build_contact_fanout_head_document(
        user_scope_hash=checked["userScopeHash"],
        fanout_id=checked["fanoutId"],
        outcome=checked["outcome"],
        expected_contact_settlement_hash=checked[
            "expectedContactSettlementHash"
        ],
        state_revision=checked["stateRevision"],
        state=checked["state"],
        binding_revision=checked["bindingRevision"],
        binding_head_hash=checked["bindingHeadHash"],
        binding_association_count=checked["bindingAssociationCount"],
        discovery_cursor_row_id=checked["discoveryCursorRowId"],
        obligation_count=checked["obligationCount"],
        result_count=checked["resultCount"],
        lease_owner_hash=checked["leaseOwnerHash"],
        lease_until=checked["leaseUntil"],
        fencing_token=checked["fencingToken"],
        superseding_contact_settlement_hash=checked[
            "supersedingContactSettlementHash"
        ],
        completion_binding_revision=checked["completionBindingRevision"],
        completion_binding_head_hash=checked["completionBindingHeadHash"],
        completion_binding_association_count=checked[
            "completionBindingAssociationCount"
        ],
        completion_obligation_count=checked["completionObligationCount"],
        completion_result_count=checked["completionResultCount"],
        completed_at=checked["completedAt"],
        created_at=checked["createdAt"],
        updated_at=checked["updatedAt"],
    )
    _require_sha256(
        checked["contactFanoutHeadHash"],
        field_name="contactFanoutHeadHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact fan-out head does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


def build_contact_fanout_obligation_document(
    *,
    user_scope_hash,
    fanout_id,
    row_id,
    contact_row_edge_hash,
    expected_contact_settlement_hash,
    outcome,
    created_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    checked_fanout_id = _require_sha256(fanout_id, field_name="fanout_id")
    checked_row_id = validate_row_id(row_id)
    edge_hash = _require_sha256(
        contact_row_edge_hash,
        field_name="contact_row_edge_hash",
    )
    settlement_hash = _require_sha256(
        expected_contact_settlement_hash,
        field_name="expected_contact_settlement_hash",
    )
    checked_outcome = _require_contact_fanout_outcome(outcome)
    if checked_fanout_id != _derive_contact_fanout_id(
        user_scope_hash=scope,
        settlement_hash=settlement_hash,
        outcome=checked_outcome,
    ):
        raise RowAuthorityConfigError(
            "contact fan-out obligation ID does not recompute"
        )
    created = _require_timestamp(created_at, field_name="created_at")
    payload = {
        "fanoutId": checked_fanout_id,
        "rowId": checked_row_id,
        "contactRowEdgeHash": edge_hash,
        "expectedContactSettlementHash": settlement_hash,
        "outcome": checked_outcome,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactFanoutObligationHash": domain_hash(
            CONTACT_FANOUT_OBLIGATION_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
        "createdAt": created,
    }


def validate_contact_fanout_obligation_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_FANOUT_OBLIGATION_KEYS,
        field_name="contact fan-out obligation document",
    )
    expected = build_contact_fanout_obligation_document(
        user_scope_hash=checked["userScopeHash"],
        fanout_id=checked["fanoutId"],
        row_id=checked["rowId"],
        contact_row_edge_hash=checked["contactRowEdgeHash"],
        expected_contact_settlement_hash=checked[
            "expectedContactSettlementHash"
        ],
        outcome=checked["outcome"],
        created_at=checked["createdAt"],
    )
    _require_sha256(
        checked["contactFanoutObligationHash"],
        field_name="contactFanoutObligationHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact fan-out obligation does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


def _require_optional_contact_generation(value, *, field_name):
    if value is None:
        return None
    return _require_pos(value, field_name=field_name)


def build_contact_fanout_result_document(
    *,
    user_scope_hash,
    fanout_id,
    row_id,
    obligation_hash,
    outcome,
    disposition,
    reason_code,
    observed_row_head_hash,
    claim_request_id,
    claim_set_hash,
    row_generation,
    row_settlement_hash,
    released_row_generation,
    released_row_settlement_hash,
    restored_effective_generation,
    restored_effective_settlement_hash,
    created_at,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    checked_fanout_id = _require_sha256(fanout_id, field_name="fanout_id")
    checked_row_id = validate_row_id(row_id)
    checked_obligation_hash = _require_sha256(
        obligation_hash,
        field_name="obligation_hash",
    )
    checked_outcome = _require_contact_fanout_outcome(outcome)
    observed_head_hash = _require_sha256(
        observed_row_head_hash,
        field_name="observed_row_head_hash",
    )
    claim_request = _require_optional_hash(
        claim_request_id,
        field_name="claim_request_id",
    )
    claim_hash = _require_optional_hash(
        claim_set_hash,
        field_name="claim_set_hash",
    )
    checked_row_generation = _require_optional_contact_generation(
        row_generation,
        field_name="row_generation",
    )
    row_hash = _require_optional_hash(
        row_settlement_hash,
        field_name="row_settlement_hash",
    )
    released_generation = _require_optional_contact_generation(
        released_row_generation,
        field_name="released_row_generation",
    )
    released_hash = _require_optional_hash(
        released_row_settlement_hash,
        field_name="released_row_settlement_hash",
    )
    restored_generation = _require_optional_contact_generation(
        restored_effective_generation,
        field_name="restored_effective_generation",
    )
    restored_hash = _require_optional_hash(
        restored_effective_settlement_hash,
        field_name="restored_effective_settlement_hash",
    )

    evidence_pairs = (
        (claim_request, claim_hash, "claim request and claim set"),
        (
            checked_row_generation,
            row_hash,
            "row generation and settlement",
        ),
        (
            released_generation,
            released_hash,
            "released row generation and settlement",
        ),
        (
            restored_generation,
            restored_hash,
            "restored effective generation and settlement",
        ),
    )
    for address, digest, field_name in evidence_pairs:
        if (address is None) != (digest is None):
            raise RowAuthorityConfigError(
                f"contact fan-out result {field_name} must be paired"
            )

    matrix = {
        ("apply", "applied", "claim_accepted"): (
            True,
            True,
            False,
            False,
        ),
        ("apply", "dominated", "claim_dominated"): (
            True,
            False,
            False,
            False,
        ),
        ("apply", "noop", "row_deleted"): (
            False,
            False,
            False,
            False,
        ),
        ("apply", "superseded", "contact_head_advanced"): (
            False,
            False,
            False,
            False,
        ),
        ("release", "restore", "exact_predecessor"): (
            False,
            False,
            True,
            None,
        ),
        ("release", "noop", "row_optout_not_applied"): (
            False,
            False,
            False,
            False,
        ),
        ("release", "noop", "different_effective_owner"): (
            False,
            False,
            True,
            False,
        ),
        ("release", "superseded", "contact_head_advanced"): (
            False,
            False,
            False,
            False,
        ),
    }
    expected_presence = matrix.get(
        (checked_outcome, disposition, reason_code)
    )
    if expected_presence is None:
        raise RowAuthorityConfigError(
            "contact fan-out result disposition and reason are unsupported"
        )
    actual_presence = tuple(
        address is not None for address, _digest, _name in evidence_pairs
    )
    if any(
        required is not None and present != required
        for present, required in zip(actual_presence, expected_presence)
    ):
        raise RowAuthorityConfigError(
            "contact fan-out result evidence does not match its outcome"
        )
    if (
        restored_generation is not None
        and released_generation is not None
        and restored_generation >= released_generation
    ):
        raise RowAuthorityConfigError(
            "restored row generation must predate the released generation"
        )

    created = _require_timestamp(created_at, field_name="created_at")
    payload = {
        "fanoutId": checked_fanout_id,
        "rowId": checked_row_id,
        "obligationHash": checked_obligation_hash,
        "outcome": checked_outcome,
        "disposition": disposition,
        "reasonCode": reason_code,
        "observedRowHeadHash": observed_head_hash,
        "claimRequestId": claim_request,
        "claimSetHash": claim_hash,
        "rowGeneration": checked_row_generation,
        "rowSettlementHash": row_hash,
        "releasedRowGeneration": released_generation,
        "releasedRowSettlementHash": released_hash,
        "restoredEffectiveGeneration": restored_generation,
        "restoredEffectiveSettlementHash": restored_hash,
        "createdAt": created,
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": scope,
        **payload,
        "contactFanoutResultHash": domain_hash(
            CONTACT_FANOUT_RESULT_HASH_DOMAIN,
            payload,
            user_scope_hash=scope,
        ),
    }


def validate_contact_fanout_result_document(*, document):
    checked = _require_contact_document(
        document,
        keys=_CONTACT_FANOUT_RESULT_KEYS,
        field_name="contact fan-out result document",
    )
    expected = build_contact_fanout_result_document(
        user_scope_hash=checked["userScopeHash"],
        fanout_id=checked["fanoutId"],
        row_id=checked["rowId"],
        obligation_hash=checked["obligationHash"],
        outcome=checked["outcome"],
        disposition=checked["disposition"],
        reason_code=checked["reasonCode"],
        observed_row_head_hash=checked["observedRowHeadHash"],
        claim_request_id=checked["claimRequestId"],
        claim_set_hash=checked["claimSetHash"],
        row_generation=checked["rowGeneration"],
        row_settlement_hash=checked["rowSettlementHash"],
        released_row_generation=checked["releasedRowGeneration"],
        released_row_settlement_hash=checked[
            "releasedRowSettlementHash"
        ],
        restored_effective_generation=checked[
            "restoredEffectiveGeneration"
        ],
        restored_effective_settlement_hash=checked[
            "restoredEffectiveSettlementHash"
        ],
        created_at=checked["createdAt"],
    )
    _require_sha256(
        checked["contactFanoutResultHash"],
        field_name="contactFanoutResultHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact fan-out result does not match canonical fields and hash"
        )
    return _defensive_copy(expected)


_CONTACT_ROW_BINDING_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "edgeId",
        "canonicalMailboxIdentityHash",
        "rowId",
        "contactRowEdgeHash",
        "createdAt",
    }
)
_CONTACT_ROW_BINDING_EVIDENCE_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "evidenceId",
        "edgeId",
        "threadId",
        "threadBindingHash",
        "exactIdentityHash",
        "contactRowEvidenceHash",
        "createdAt",
    }
)
_CONTACT_ROW_BINDING_HEAD_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "canonicalMailboxIdentityHash",
        "stateRevision",
        "associationCount",
        "lastAssociationHash",
        "contactRowBindingHeadHash",
        "createdAt",
        "updatedAt",
    }
)


def build_contact_row_binding_document(
    *,
    user_scope_hash,
    canonical_mailbox_identity_hash,
    row_id,
    created_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    checked_row_id = validate_row_id(row_id)
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    edge_id = domain_hash(
        CONTACT_ROW_EDGE_ID_DOMAIN,
        {
            "canonicalMailboxIdentityHash": checked_canonical_hash,
            "rowId": checked_row_id,
        },
        user_scope_hash=checked_scope,
    )
    edge_hash = domain_hash(
        CONTACT_ROW_EDGE_HASH_DOMAIN,
        {
            "edgeId": edge_id,
            "canonicalMailboxIdentityHash": checked_canonical_hash,
            "rowId": checked_row_id,
            "createdAt": checked_created_at,
        },
        user_scope_hash=checked_scope,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "edgeId": edge_id,
        "canonicalMailboxIdentityHash": checked_canonical_hash,
        "rowId": checked_row_id,
        "contactRowEdgeHash": edge_hash,
        "createdAt": checked_created_at,
    }


def validate_contact_row_binding_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_CONTACT_ROW_BINDING_KEYS,
        field_name="contact row binding document",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(
            "contact row binding schemaVersion must be 1"
        )
    expected = build_contact_row_binding_document(
        user_scope_hash=checked["userScopeHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        row_id=checked["rowId"],
        created_at=checked["createdAt"],
    )
    _require_sha256(checked["edgeId"], field_name="edgeId")
    _require_sha256(
        checked["contactRowEdgeHash"],
        field_name="contactRowEdgeHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact row binding does not match its canonical fields and hashes"
        )
    return dict(expected)


def build_contact_row_binding_evidence_document(
    *,
    user_scope_hash,
    edge_id,
    thread_id,
    thread_binding_hash,
    exact_identity_hash,
    created_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_edge_id = _require_sha256(edge_id, field_name="edge_id")
    checked_thread_id = _require_thread_document_id(
        thread_id,
        field_name="thread_id",
    )
    checked_binding_hash = _require_sha256(
        thread_binding_hash,
        field_name="thread_binding_hash",
    )
    checked_exact_hash = _require_sha256(
        exact_identity_hash,
        field_name="exact_identity_hash",
    )
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    evidence_id = domain_hash(
        CONTACT_ROW_EVIDENCE_ID_DOMAIN,
        {
            "edgeId": checked_edge_id,
            "threadBindingHash": checked_binding_hash,
            "exactIdentityHash": checked_exact_hash,
        },
        user_scope_hash=checked_scope,
    )
    evidence_hash = domain_hash(
        CONTACT_ROW_EVIDENCE_HASH_DOMAIN,
        {
            "evidenceId": evidence_id,
            "edgeId": checked_edge_id,
            "threadId": checked_thread_id,
            "threadBindingHash": checked_binding_hash,
            "exactIdentityHash": checked_exact_hash,
            "createdAt": checked_created_at,
        },
        user_scope_hash=checked_scope,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "evidenceId": evidence_id,
        "edgeId": checked_edge_id,
        "threadId": checked_thread_id,
        "threadBindingHash": checked_binding_hash,
        "exactIdentityHash": checked_exact_hash,
        "contactRowEvidenceHash": evidence_hash,
        "createdAt": checked_created_at,
    }


def validate_contact_row_binding_evidence_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_CONTACT_ROW_BINDING_EVIDENCE_KEYS,
        field_name="contact row binding evidence document",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(
            "contact row binding evidence schemaVersion must be 1"
        )
    expected = build_contact_row_binding_evidence_document(
        user_scope_hash=checked["userScopeHash"],
        edge_id=checked["edgeId"],
        thread_id=checked["threadId"],
        thread_binding_hash=checked["threadBindingHash"],
        exact_identity_hash=checked["exactIdentityHash"],
        created_at=checked["createdAt"],
    )
    _require_sha256(checked["evidenceId"], field_name="evidenceId")
    _require_sha256(
        checked["contactRowEvidenceHash"],
        field_name="contactRowEvidenceHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact row evidence does not match its canonical fields and hashes"
        )
    return dict(expected)


def build_contact_row_binding_head_document(
    *,
    user_scope_hash,
    canonical_mailbox_identity_hash,
    state_revision,
    association_count,
    last_association_hash,
    created_at,
    updated_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_canonical_hash = _require_sha256(
        canonical_mailbox_identity_hash,
        field_name="canonical_mailbox_identity_hash",
    )
    checked_revision = _require_pos(
        state_revision,
        field_name="state_revision",
    )
    checked_count = _require_uint(
        association_count,
        field_name="association_count",
    )
    if checked_count == 0:
        if last_association_hash is not None:
            raise RowAuthorityConfigError(
                "an empty contact binding head requires a null last hash"
            )
        checked_last_hash = None
    else:
        checked_last_hash = _require_sha256(
            last_association_hash,
            field_name="last_association_hash",
        )
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    checked_updated_at = _require_timestamp(
        updated_at,
        field_name="updated_at",
    )
    if checked_updated_at < checked_created_at:
        raise RowAuthorityConfigError(
            "contact binding head updated_at cannot predate created_at"
        )
    head_hash = domain_hash(
        CONTACT_ROW_BINDING_HEAD_HASH_DOMAIN,
        {
            "canonicalMailboxIdentityHash": checked_canonical_hash,
            "stateRevision": checked_revision,
            "associationCount": checked_count,
            "lastAssociationHash": checked_last_hash,
            "createdAt": checked_created_at,
            "updatedAt": checked_updated_at,
        },
        user_scope_hash=checked_scope,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "canonicalMailboxIdentityHash": checked_canonical_hash,
        "stateRevision": checked_revision,
        "associationCount": checked_count,
        "lastAssociationHash": checked_last_hash,
        "contactRowBindingHeadHash": head_hash,
        "createdAt": checked_created_at,
        "updatedAt": checked_updated_at,
    }


def validate_contact_row_binding_head_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_CONTACT_ROW_BINDING_HEAD_KEYS,
        field_name="contact row binding head document",
    )
    if (
        type(checked["schemaVersion"]) is not int
        or checked["schemaVersion"] != SCHEMA_VERSION
    ):
        raise RowAuthorityConfigError(
            "contact row binding head schemaVersion must be 1"
        )
    expected = build_contact_row_binding_head_document(
        user_scope_hash=checked["userScopeHash"],
        canonical_mailbox_identity_hash=checked[
            "canonicalMailboxIdentityHash"
        ],
        state_revision=checked["stateRevision"],
        association_count=checked["associationCount"],
        last_association_hash=checked["lastAssociationHash"],
        created_at=checked["createdAt"],
        updated_at=checked["updatedAt"],
    )
    _require_sha256(
        checked["contactRowBindingHeadHash"],
        field_name="contactRowBindingHeadHash",
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "contact binding head does not match canonical fields and hash"
        )
    return dict(expected)


def _plan_contact_row_association(
    *,
    thread_binding_document,
    reverse_binding_document,
    row_identity_document,
    row_head_document,
    proposed_association_document,
    proposed_evidence_document,
    stored_association_document,
    stored_evidence_document,
    contact_binding_head_document,
):
    """Return a deterministic contact-association plan without executing it."""

    thread_binding = validate_thread_row_binding_document(
        document=thread_binding_document
    )
    reverse_binding = validate_row_thread_binding_document(
        document=reverse_binding_document
    )
    row_identity = validate_row_identity_document(document=row_identity_document)
    row_head = validate_row_authority_head(document=row_head_document)
    proposed_association = validate_contact_row_binding_document(
        document=proposed_association_document
    )
    proposed_evidence = validate_contact_row_binding_evidence_document(
        document=proposed_evidence_document
    )

    scope = proposed_association["userScopeHash"]
    row_id = proposed_association["rowId"]
    matching_rows = [
        row_binding
        for row_binding in thread_binding["rowBindings"]
        if row_binding["rowId"] == row_id
    ]
    if len(matching_rows) != 1:
        raise RowAuthorityConflict(
            "stored thread binding does not authorize the requested row"
        )
    expected_reverse = next(
        document
        for document in build_row_thread_binding_documents(
            thread_binding_document=thread_binding
        )
        if document["rowId"] == row_id
    )
    if (
        thread_binding["userScopeHash"] != scope
        or reverse_binding != expected_reverse
        or row_identity["userScopeHash"] != scope
        or row_identity["rowId"] != row_id
        or row_identity["clientId"] != thread_binding["clientId"]
        or row_head["userScopeHash"] != scope
        or row_head["rowId"] != row_id
        or row_head["createdAt"] != row_identity["createdAt"]
        or proposed_evidence["userScopeHash"] != scope
        or proposed_evidence["edgeId"] != proposed_association["edgeId"]
        or proposed_evidence["threadId"] != thread_binding["threadId"]
        or proposed_evidence["threadBindingHash"]
        != thread_binding["bindingHash"]
    ):
        raise RowAuthorityConflict(
            "contact association prerequisites do not correlate"
        )

    association_exists = stored_association_document is not None
    evidence_exists = stored_evidence_document is not None
    head_exists = contact_binding_head_document is not None
    if evidence_exists and not association_exists:
        raise RowAuthorityAmbiguous(
            "contact association evidence exists without its stable edge"
        )
    if association_exists and not head_exists:
        raise RowAuthorityAmbiguous(
            "contact association edge exists without a binding head"
        )

    if association_exists:
        try:
            association = validate_contact_row_binding_document(
                document=stored_association_document
            )
        except Exception as exc:
            raise RowAuthorityConflict(
                "stored contact association contains immutable drift"
            ) from exc
        if any(
            association[field] != proposed_association[field]
            for field in (
                "schemaVersion",
                "userScopeHash",
                "edgeId",
                "canonicalMailboxIdentityHash",
                "rowId",
            )
        ):
            raise RowAuthorityConflict(
                "stored contact association differs from the proposal"
            )
    else:
        association = proposed_association

    if association["createdAt"] < row_identity["createdAt"]:
        raise RowAuthorityConflict(
            "contact association predates immutable row identity"
        )
    if (
        not association_exists
        and association["createdAt"] < thread_binding["createdAt"]
    ):
        raise RowAuthorityConflict(
            "contact association predates its supporting thread binding"
        )
    if thread_binding["createdAt"] < row_identity["createdAt"]:
        raise RowAuthorityConflict(
            "supporting thread binding predates immutable row identity"
        )
    if (
        proposed_evidence["createdAt"] < thread_binding["createdAt"]
        or proposed_evidence["createdAt"] < association["createdAt"]
    ):
        raise RowAuthorityConflict(
            "contact evidence predates its supporting authority"
        )

    if evidence_exists:
        try:
            evidence = validate_contact_row_binding_evidence_document(
                document=stored_evidence_document
            )
        except Exception as exc:
            raise RowAuthorityConflict(
                "stored contact evidence contains immutable drift"
            ) from exc
        if evidence != proposed_evidence:
            raise RowAuthorityConflict(
                "stored contact evidence differs from the proposal"
            )
    else:
        evidence = proposed_evidence

    binding_head = None
    if head_exists:
        try:
            binding_head = validate_contact_row_binding_head_document(
                document=contact_binding_head_document
            )
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "contact binding head is malformed"
            ) from exc
        if (
            binding_head["userScopeHash"] != scope
            or binding_head["canonicalMailboxIdentityHash"]
            != association["canonicalMailboxIdentityHash"]
        ):
            raise RowAuthorityAmbiguous(
                "contact binding head is not protocol-correlated"
            )

    if association_exists:
        if (
            binding_head["associationCount"] < 1
            or binding_head["createdAt"] > association["createdAt"]
            or binding_head["updatedAt"] < association["createdAt"]
            or (
                binding_head["lastAssociationHash"]
                == association["contactRowEdgeHash"]
                and binding_head["updatedAt"] != association["createdAt"]
            )
            or (
                binding_head["lastAssociationHash"]
                != association["contactRowEdgeHash"]
                and binding_head["associationCount"] < 2
            )
        ):
            raise RowAuthorityAmbiguous(
                "contact binding head does not contain the stable association"
            )
        if evidence_exists:
            disposition = "already_applied"
            mutations = ()
        else:
            disposition = "evidence_created"
            mutations = (
                {
                    "target": "evidence",
                    "operation": "create",
                    "document": dict(evidence),
                },
            )
        result_head = binding_head
    else:
        if (
            binding_head is not None
            and binding_head["lastAssociationHash"]
            == association["contactRowEdgeHash"]
        ):
            raise RowAuthorityAmbiguous(
                "contact binding head points to a missing stable association"
            )
        if binding_head is None:
            result_head = build_contact_row_binding_head_document(
                user_scope_hash=scope,
                canonical_mailbox_identity_hash=association[
                    "canonicalMailboxIdentityHash"
                ],
                state_revision=1,
                association_count=1,
                last_association_hash=association["contactRowEdgeHash"],
                created_at=association["createdAt"],
                updated_at=association["createdAt"],
            )
            head_operation = "create"
        else:
            if association["createdAt"] < binding_head["updatedAt"]:
                raise RowAuthorityConflict(
                    "contact association predates the binding head"
                )
            result_head = build_contact_row_binding_head_document(
                user_scope_hash=scope,
                canonical_mailbox_identity_hash=association[
                    "canonicalMailboxIdentityHash"
                ],
                state_revision=binding_head["stateRevision"] + 1,
                association_count=binding_head["associationCount"] + 1,
                last_association_hash=association["contactRowEdgeHash"],
                created_at=binding_head["createdAt"],
                updated_at=association["createdAt"],
            )
            head_operation = "set"
        disposition = "created"
        mutations = (
            {
                "target": "association",
                "operation": "create",
                "document": dict(association),
            },
            {
                "target": "evidence",
                "operation": "create",
                "document": dict(evidence),
            },
            {
                "target": "binding_head",
                "operation": head_operation,
                "document": dict(result_head),
            },
        )

    return {
        "disposition": disposition,
        "association": dict(association),
        "evidence": dict(evidence),
        "bindingHead": dict(result_head),
        "mutations": tuple(
            {
                "target": mutation["target"],
                "operation": mutation["operation"],
                "document": dict(mutation["document"]),
            }
            for mutation in mutations
        ),
    }


def normalize_provider_text(*, value, field_name):
    if type(field_name) is not str or not field_name:
        raise RowAuthorityConfigError("field_name must be a nonempty string")
    if type(value) is not str:
        raise RowAuthorityConfigError(f"{field_name} must be a string")
    try:
        normalized = unicodedata.normalize(
            "NFC",
            value.replace("\r\n", "\n").replace("\r", "\n"),
        )
        encoded = normalized.encode("utf-8")
    except UnicodeError as exc:
        raise RowAuthorityConfigError(
            f"{field_name} must contain valid Unicode text"
        ) from exc
    if len(encoded) > MAX_PROVIDER_VALUE_BYTES:
        raise RowAuthorityConfigError(
            f"{field_name} exceeds the provider-text byte bound"
        )
    return normalized


def _normalize_provider_values(values, *, field_name):
    if type(values) not in {list, tuple}:
        raise RowAuthorityConfigError(
            f"{field_name} must be an ordered list or tuple"
        )
    if len(values) > MAX_PROVIDER_VALUES:
        raise RowAuthorityConfigError(
            f"{field_name} exceeds the 256-value bound"
        )
    return [
        normalize_provider_text(
            value=value,
            field_name=f"{field_name}[{index}]",
        )
        for index, value in enumerate(values)
    ]


def marker_hash(*, row_id, spreadsheet_id, sheet_id, user_scope_hash):
    checked_row_id = validate_row_id(row_id)
    checked_spreadsheet_id = _require_opaque(
        spreadsheet_id,
        field_name="spreadsheet_id",
    )
    checked_sheet_id = _require_uint(sheet_id, field_name="sheet_id")
    payload = {
        "rowId": checked_row_id,
        "markerKey": MARKER_KEY,
        "markerValue": checked_row_id,
        "visibility": MARKER_VISIBILITY,
        "spreadsheetId": checked_spreadsheet_id,
        "sheetId": checked_sheet_id,
    }
    return domain_hash(
        "sitesift.row.marker.v1",
        payload,
        user_scope_hash=user_scope_hash,
    )


def header_hash(*, ordered_headers, user_scope_hash):
    normalized = _normalize_provider_values(
        ordered_headers,
        field_name="ordered_headers",
    )
    return domain_hash(
        "sitesift.row.header.v1",
        {"orderedHeaders": normalized},
        user_scope_hash=user_scope_hash,
    )


def row_snapshot_hash(
    *,
    spreadsheet_id,
    sheet_id,
    ordered_headers,
    ordered_cell_values,
    user_scope_hash,
):
    checked_spreadsheet_id = _require_opaque(
        spreadsheet_id,
        field_name="spreadsheet_id",
    )
    checked_sheet_id = _require_uint(sheet_id, field_name="sheet_id")
    normalized_headers = _normalize_provider_values(
        ordered_headers,
        field_name="ordered_headers",
    )
    normalized_cells = _normalize_provider_values(
        ordered_cell_values,
        field_name="ordered_cell_values",
    )
    checked_header_hash = domain_hash(
        "sitesift.row.header.v1",
        {"orderedHeaders": normalized_headers},
        user_scope_hash=user_scope_hash,
    )
    return domain_hash(
        "sitesift.row.snapshot.v1",
        {
            "spreadsheetId": checked_spreadsheet_id,
            "sheetId": checked_sheet_id,
            "headerHash": checked_header_hash,
            "orderedCellValues": normalized_cells,
        },
        user_scope_hash=user_scope_hash,
    )


_MARKER_OBSERVATION_KEYS = frozenset(
    {
        "rowId",
        "sheetId",
        "providerRowIndex",
        "displayRowNumber",
        "metadataId",
    }
)
_EVIDENCE_OBSERVATION_KEYS = frozenset(
    {
        "providerRowIndex",
        "displayRowNumber",
        "metadataId",
        "markerHash",
        "rowSnapshotHash",
    }
)


def build_row_observation(
    *,
    spreadsheet_id,
    marker_observation,
    ordered_headers,
    ordered_cell_values,
    user_scope_hash,
):
    marker = _require_exact_dict(
        marker_observation,
        keys=_MARKER_OBSERVATION_KEYS,
        field_name="marker_observation",
    )
    row_id = validate_row_id(marker["rowId"])
    sheet_id = _require_uint(marker["sheetId"], field_name="sheetId")
    provider_index = _require_uint(
        marker["providerRowIndex"],
        field_name="providerRowIndex",
    )
    display_number = _require_pos(
        marker["displayRowNumber"],
        field_name="displayRowNumber",
    )
    if display_number != provider_index + 1:
        raise RowAuthorityConfigError(
            "displayRowNumber must equal providerRowIndex plus one"
        )
    metadata_id = _require_pos(marker["metadataId"], field_name="metadataId")
    checked_spreadsheet_id = _require_opaque(
        spreadsheet_id,
        field_name="spreadsheet_id",
    )
    return {
        "providerRowIndex": provider_index,
        "displayRowNumber": display_number,
        "metadataId": metadata_id,
        "markerHash": marker_hash(
            row_id=row_id,
            spreadsheet_id=checked_spreadsheet_id,
            sheet_id=sheet_id,
            user_scope_hash=user_scope_hash,
        ),
        "rowSnapshotHash": row_snapshot_hash(
            spreadsheet_id=checked_spreadsheet_id,
            sheet_id=sheet_id,
            ordered_headers=ordered_headers,
            ordered_cell_values=ordered_cell_values,
            user_scope_hash=user_scope_hash,
        ),
    }


def _validated_evidence_observations(*, lifecycle, observations):
    if type(lifecycle) is not str or lifecycle not in ROW_LIFECYCLES:
        raise RowAuthorityConfigError("lifecycle is not approved")
    if type(observations) not in {list, tuple}:
        raise RowAuthorityConfigError(
            "observations must be an ordered list or tuple"
        )
    count = len(observations)
    if lifecycle in {"active", "nonviable"} and count != 1:
        raise RowAuthorityConfigError(
            "active and nonviable evidence require exactly one observation"
        )
    if lifecycle == "deleted" and count != 0:
        raise RowAuthorityConfigError("deleted evidence requires no observation")
    if lifecycle == "ambiguous" and not 2 <= count <= MAX_ROW_BINDINGS:
        raise RowAuthorityConfigError(
            "ambiguous evidence requires two through 128 observations"
        )
    validated = []
    for index, observation in enumerate(observations):
        checked = _require_exact_dict(
            observation,
            keys=_EVIDENCE_OBSERVATION_KEYS,
            field_name=f"observations[{index}]",
        )
        provider_index = _require_uint(
            checked["providerRowIndex"],
            field_name=f"observations[{index}].providerRowIndex",
        )
        display_number = _require_pos(
            checked["displayRowNumber"],
            field_name=f"observations[{index}].displayRowNumber",
        )
        if display_number != provider_index + 1:
            raise RowAuthorityConfigError(
                "observation display row must equal provider index plus one"
            )
        item = {
            "providerRowIndex": provider_index,
            "displayRowNumber": display_number,
            "metadataId": _require_uint(
                checked["metadataId"],
                field_name=f"observations[{index}].metadataId",
            ),
            "markerHash": _require_sha256(
                checked["markerHash"],
                field_name=f"observations[{index}].markerHash",
            ),
            "rowSnapshotHash": _require_sha256(
                checked["rowSnapshotHash"],
                field_name=f"observations[{index}].rowSnapshotHash",
            ),
        }
        validated.append(item)
    validated.sort(
        key=lambda item: (
            item["providerRowIndex"],
            item["metadataId"],
            item["rowSnapshotHash"],
        )
    )
    return validated


def observation_evidence_hash(*, lifecycle, observations, user_scope_hash):
    validated = _validated_evidence_observations(
        lifecycle=lifecycle,
        observations=observations,
    )
    return domain_hash(
        "sitesift.row.observation_evidence.v1",
        {
            "observationKind": lifecycle,
            "observations": validated,
        },
        user_scope_hash=user_scope_hash,
    )


_IDENTITY_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "clientId",
        "spreadsheetId",
        "sheetId",
        "markerKey",
        "markerValue",
        "creationKind",
        "creationSourceHash",
        "markerHash",
        "identityHash",
        "createdAt",
    }
)


def _identity_hash(
    *,
    user_scope_hash,
    row_id,
    client_id,
    spreadsheet_id,
    sheet_id,
    marker_hash_value,
    creation_kind,
    creation_source_hash,
):
    return domain_hash(
        "sitesift.row.identity.v1",
        {
            "rowId": row_id,
            "clientId": client_id,
            "spreadsheetId": spreadsheet_id,
            "sheetId": sheet_id,
            "markerHash": marker_hash_value,
            "creationKind": creation_kind,
            "creationSourceHash": creation_source_hash,
        },
        user_scope_hash=user_scope_hash,
    )


def build_row_identity_document(
    *,
    user_scope_hash,
    row_id,
    client_id,
    spreadsheet_id,
    sheet_id,
    creation_kind,
    creation_source_hash,
    created_at,
):
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_row_id = validate_row_id(row_id)
    checked_client_id = _require_opaque(client_id, field_name="client_id")
    checked_spreadsheet_id = _require_opaque(
        spreadsheet_id,
        field_name="spreadsheet_id",
    )
    checked_sheet_id = _require_uint(sheet_id, field_name="sheet_id")
    if type(creation_kind) is not str or creation_kind not in {
        "fresh",
        "migration",
    }:
        raise RowAuthorityConfigError("creation_kind is not approved")
    checked_source_hash = _require_sha256(
        creation_source_hash,
        field_name="creation_source_hash",
    )
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    checked_marker_hash = marker_hash(
        row_id=checked_row_id,
        spreadsheet_id=checked_spreadsheet_id,
        sheet_id=checked_sheet_id,
        user_scope_hash=checked_scope,
    )
    checked_identity_hash = _identity_hash(
        user_scope_hash=checked_scope,
        row_id=checked_row_id,
        client_id=checked_client_id,
        spreadsheet_id=checked_spreadsheet_id,
        sheet_id=checked_sheet_id,
        marker_hash_value=checked_marker_hash,
        creation_kind=creation_kind,
        creation_source_hash=checked_source_hash,
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
        "rowId": checked_row_id,
        "clientId": checked_client_id,
        "spreadsheetId": checked_spreadsheet_id,
        "sheetId": checked_sheet_id,
        "markerKey": MARKER_KEY,
        "markerValue": checked_row_id,
        "creationKind": creation_kind,
        "creationSourceHash": checked_source_hash,
        "markerHash": checked_marker_hash,
        "identityHash": checked_identity_hash,
        "createdAt": checked_created_at,
    }


def validate_row_identity_document(*, document):
    checked = _require_exact_dict(
        document,
        keys=_IDENTITY_KEYS,
        field_name="row identity document",
    )
    if type(checked["schemaVersion"]) is not int or checked[
        "schemaVersion"
    ] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("identity schemaVersion must be 1")
    if checked["markerKey"] != MARKER_KEY:
        raise RowAuthorityConfigError("identity markerKey is not approved")
    if checked["markerValue"] != checked["rowId"]:
        raise RowAuthorityConfigError("identity markerValue must equal rowId")
    expected = build_row_identity_document(
        user_scope_hash=checked["userScopeHash"],
        row_id=checked["rowId"],
        client_id=checked["clientId"],
        spreadsheet_id=checked["spreadsheetId"],
        sheet_id=checked["sheetId"],
        creation_kind=checked["creationKind"],
        creation_source_hash=checked["creationSourceHash"],
        created_at=checked["createdAt"],
    )
    if checked != expected:
        raise RowAuthorityConfigError(
            "row identity document does not match its canonical hashes"
        )
    return dict(expected)


_REVISION_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "revision",
        "spreadsheetId",
        "sheetId",
        "providerRowIndex",
        "displayRowNumber",
        "metadataId",
        "markerHash",
        "rowSnapshotHash",
        "lifecycle",
        "observationEvidenceHash",
        "previousRevisionHash",
        "revisionHash",
        "observedAt",
    }
)


def _revision_hash_payload(document):
    return {
        key: document[key]
        for key in (
            "rowId",
            "revision",
            "providerRowIndex",
            "displayRowNumber",
            "metadataId",
            "rowSnapshotHash",
            "markerHash",
            "lifecycle",
            "observationEvidenceHash",
            "previousRevisionHash",
            "observedAt",
        )
    }


def build_row_location_revision_document(
    *,
    identity_document,
    revision,
    lifecycle,
    observations,
    previous_revision_hash,
    observed_at,
):
    identity = validate_row_identity_document(document=identity_document)
    checked_revision = _require_pos(revision, field_name="revision")
    if checked_revision == 1:
        if previous_revision_hash is not None:
            raise RowAuthorityConfigError(
                "revision 1 must have a null previous revision hash"
            )
        checked_previous = None
    else:
        checked_previous = _require_sha256(
            previous_revision_hash,
            field_name="previous_revision_hash",
        )
    checked_observed_at = _require_timestamp(
        observed_at,
        field_name="observed_at",
    )
    canonical_observations = _validated_evidence_observations(
        lifecycle=lifecycle,
        observations=observations,
    )
    evidence_hash = domain_hash(
        "sitesift.row.observation_evidence.v1",
        {
            "observationKind": lifecycle,
            "observations": canonical_observations,
        },
        user_scope_hash=identity["userScopeHash"],
    )
    if lifecycle in {"active", "nonviable"}:
        observation = canonical_observations[0]
        if observation["markerHash"] != identity["markerHash"]:
            raise RowAuthorityConfigError(
                "active observation marker must match immutable identity"
            )
        provider_index = observation["providerRowIndex"]
        display_number = observation["displayRowNumber"]
        metadata_id = observation["metadataId"]
        snapshot_hash = observation["rowSnapshotHash"]
    else:
        provider_index = None
        display_number = None
        metadata_id = None
        snapshot_hash = None
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": identity["userScopeHash"],
        "rowId": identity["rowId"],
        "revision": checked_revision,
        "spreadsheetId": identity["spreadsheetId"],
        "sheetId": identity["sheetId"],
        "providerRowIndex": provider_index,
        "displayRowNumber": display_number,
        "metadataId": metadata_id,
        "markerHash": identity["markerHash"],
        "rowSnapshotHash": snapshot_hash,
        "lifecycle": lifecycle,
        "observationEvidenceHash": evidence_hash,
        "previousRevisionHash": checked_previous,
        "observedAt": checked_observed_at,
    }
    document["revisionHash"] = domain_hash(
        "sitesift.row.location.v1",
        _revision_hash_payload(document),
        user_scope_hash=identity["userScopeHash"],
    )
    return document


def _validate_row_location_revision(document, *, identity_document=None):
    checked = _require_exact_dict(
        document,
        keys=_REVISION_KEYS,
        field_name="row location revision document",
    )
    if type(checked["schemaVersion"]) is not int or checked[
        "schemaVersion"
    ] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("revision schemaVersion must be 1")
    scope = _require_sha256(
        checked["userScopeHash"],
        field_name="userScopeHash",
    )
    validate_row_id(checked["rowId"])
    revision = _require_pos(checked["revision"], field_name="revision")
    _require_opaque(checked["spreadsheetId"], field_name="spreadsheetId")
    _require_uint(checked["sheetId"], field_name="sheetId")
    lifecycle = checked["lifecycle"]
    if type(lifecycle) is not str or lifecycle not in ROW_LIFECYCLES:
        raise RowAuthorityConfigError("revision lifecycle is not approved")
    if lifecycle in {"active", "nonviable"}:
        provider_index = _require_uint(
            checked["providerRowIndex"],
            field_name="providerRowIndex",
        )
        display_number = _require_pos(
            checked["displayRowNumber"],
            field_name="displayRowNumber",
        )
        if display_number != provider_index + 1:
            raise RowAuthorityConfigError(
                "revision display row must equal provider index plus one"
            )
        _require_uint(checked["metadataId"], field_name="metadataId")
        _require_sha256(
            checked["rowSnapshotHash"],
            field_name="rowSnapshotHash",
        )
    elif any(
        checked[field] is not None
        for field in (
            "providerRowIndex",
            "displayRowNumber",
            "metadataId",
            "rowSnapshotHash",
        )
    ):
        raise RowAuthorityConfigError(
            "deleted and ambiguous revisions require null geometry"
        )
    _require_sha256(checked["markerHash"], field_name="markerHash")
    _require_sha256(
        checked["observationEvidenceHash"],
        field_name="observationEvidenceHash",
    )
    if revision == 1:
        if checked["previousRevisionHash"] is not None:
            raise RowAuthorityConfigError(
                "revision 1 must have a null previous revision hash"
            )
    else:
        _require_sha256(
            checked["previousRevisionHash"],
            field_name="previousRevisionHash",
        )
    _require_timestamp(checked["observedAt"], field_name="observedAt")
    expected_revision_hash = domain_hash(
        "sitesift.row.location.v1",
        _revision_hash_payload(checked),
        user_scope_hash=scope,
    )
    if checked["revisionHash"] != expected_revision_hash:
        raise RowAuthorityConfigError("revisionHash does not recompute")
    if identity_document is not None:
        identity = validate_row_identity_document(document=identity_document)
        for revision_field, identity_field in (
            ("userScopeHash", "userScopeHash"),
            ("rowId", "rowId"),
            ("spreadsheetId", "spreadsheetId"),
            ("sheetId", "sheetId"),
            ("markerHash", "markerHash"),
        ):
            if checked[revision_field] != identity[identity_field]:
                raise RowAuthorityConfigError(
                    f"revision {revision_field} does not match identity"
                )
    return dict(checked)


def validate_row_location_revision_document(*, document, identity_document):
    return _validate_row_location_revision(
        document,
        identity_document=identity_document,
    )


_HEAD_KEYS = frozenset(
    {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "stateRevision",
        "currentLocationRevision",
        "currentLocationHash",
        "currentLocationLifecycle",
        "effectiveOwnerGeneration",
        "effectiveOwnerGenerationHash",
        "effectiveOwnerKind",
        "effectivePriority",
        "state",
        "leaseOwnerHash",
        "leaseUntil",
        "fencingToken",
        "latestSettlementHash",
        "effectiveSettlementHash",
        "latestSourceSettlementLinkHash",
        "latestOptOutReleaseResultHash",
        "projectionBacklogCount",
        "headHash",
        "createdAt",
        "updatedAt",
    }
)


def _head_hash_payload(document):
    return {
        key: value
        for key, value in document.items()
        if key not in {"schemaVersion", "userScopeHash", "headHash"}
    }


def _with_head_hash(document):
    result = dict(document)
    result["headHash"] = domain_hash(
        ROW_AUTHORITY_HEAD_HASH_DOMAIN,
        _head_hash_payload(result),
        user_scope_hash=result["userScopeHash"],
    )
    return result


def build_initial_row_authority_head(
    *,
    identity_document,
    location_revision_document,
    created_at,
):
    identity = validate_row_identity_document(document=identity_document)
    revision = validate_row_location_revision_document(
        document=location_revision_document,
        identity_document=identity,
    )
    checked_created_at = _require_timestamp(
        created_at,
        field_name="created_at",
    )
    if revision["revision"] != 1 or revision["lifecycle"] not in {
        "active",
        "nonviable",
    }:
        raise RowAuthorityConfigError(
            "initial head requires active or nonviable revision 1"
        )
    if (
        checked_created_at != identity["createdAt"]
        or checked_created_at != revision["observedAt"]
    ):
        raise RowAuthorityConfigError(
            "initial identity, revision, and head timestamps must match"
        )
    return _with_head_hash(
        {
            "schemaVersion": SCHEMA_VERSION,
            "userScopeHash": identity["userScopeHash"],
            "rowId": identity["rowId"],
            "stateRevision": 1,
            "currentLocationRevision": 1,
            "currentLocationHash": revision["revisionHash"],
            "currentLocationLifecycle": revision["lifecycle"],
            "effectiveOwnerGeneration": None,
            "effectiveOwnerGenerationHash": None,
            "effectiveOwnerKind": None,
            "effectivePriority": None,
            "state": "clear",
            "leaseOwnerHash": None,
            "leaseUntil": None,
            "fencingToken": None,
            "latestSettlementHash": None,
            "effectiveSettlementHash": None,
            "latestSourceSettlementLinkHash": None,
            "latestOptOutReleaseResultHash": None,
            "projectionBacklogCount": 0,
            "createdAt": checked_created_at,
            "updatedAt": checked_created_at,
        }
    )


def validate_row_authority_head(*, document):
    checked = _require_exact_dict(
        document,
        keys=_HEAD_KEYS,
        field_name="row authority head",
    )
    if type(checked["schemaVersion"]) is not int or checked[
        "schemaVersion"
    ] != SCHEMA_VERSION:
        raise RowAuthorityConfigError("head schemaVersion must be 1")
    scope = _require_sha256(
        checked["userScopeHash"],
        field_name="userScopeHash",
    )
    validate_row_id(checked["rowId"])
    _require_pos(checked["stateRevision"], field_name="stateRevision")
    _require_pos(
        checked["currentLocationRevision"],
        field_name="currentLocationRevision",
    )
    _require_sha256(
        checked["currentLocationHash"],
        field_name="currentLocationHash",
    )
    if (
        type(checked["currentLocationLifecycle"]) is not str
        or checked["currentLocationLifecycle"] not in ROW_LIFECYCLES
    ):
        raise RowAuthorityConfigError(
            "currentLocationLifecycle is not approved"
        )
    state = checked["state"]
    if type(state) is not str or state not in HEAD_STATES:
        raise RowAuthorityConfigError("head state is not approved")

    generation = checked["effectiveOwnerGeneration"]
    generation_hash = checked["effectiveOwnerGenerationHash"]
    owner_kind = checked["effectiveOwnerKind"]
    priority = checked["effectivePriority"]
    owner_values = (generation, generation_hash, owner_kind, priority)
    if all(value is None for value in owner_values):
        has_owner = False
    elif all(value is not None for value in owner_values):
        has_owner = True
        _require_pos(generation, field_name="effectiveOwnerGeneration")
        _require_sha256(
            generation_hash,
            field_name="effectiveOwnerGenerationHash",
        )
        if type(owner_kind) is not str or owner_kind not in OWNER_PRIORITIES:
            raise RowAuthorityConfigError("effectiveOwnerKind is not approved")
        checked_priority = _require_pos(
            priority,
            field_name="effectivePriority",
        )
        if checked_priority != OWNER_PRIORITIES[owner_kind]:
            raise RowAuthorityConfigError(
                "effectivePriority does not match effectiveOwnerKind"
            )
    else:
        raise RowAuthorityConfigError(
            "effective owner fields must be all null or all populated"
        )

    lease_owner = checked["leaseOwnerHash"]
    lease_until = checked["leaseUntil"]
    if lease_owner is None and lease_until is None:
        has_lease = False
    elif lease_owner is not None and lease_until is not None:
        has_lease = True
        _require_sha256(lease_owner, field_name="leaseOwnerHash")
        _require_timestamp(lease_until, field_name="leaseUntil")
    else:
        raise RowAuthorityConfigError(
            "leaseOwnerHash and leaseUntil must be correlated"
        )

    fencing_token = checked["fencingToken"]
    if fencing_token is not None:
        _require_pos(fencing_token, field_name="fencingToken")
    latest_settlement = _require_optional_hash(
        checked["latestSettlementHash"],
        field_name="latestSettlementHash",
    )
    effective_settlement = _require_optional_hash(
        checked["effectiveSettlementHash"],
        field_name="effectiveSettlementHash",
    )
    _require_optional_hash(
        checked["latestSourceSettlementLinkHash"],
        field_name="latestSourceSettlementLinkHash",
    )
    _require_optional_hash(
        checked["latestOptOutReleaseResultHash"],
        field_name="latestOptOutReleaseResultHash",
    )

    if state == "clear":
        if has_owner or has_lease or fencing_token is not None:
            raise RowAuthorityConfigError(
                "clear heads cannot carry owner, lease, or fence fields"
            )
        if effective_settlement is not None:
            raise RowAuthorityConfigError(
                "clear heads cannot carry an effective settlement"
            )
    elif state in {"claimed", "review_pending"}:
        if not has_owner or not has_lease or fencing_token is None:
            raise RowAuthorityConfigError(
                "claimed and review-pending heads require owner, lease, and fence"
            )
        if (state == "review_pending") != (
            owner_kind == "human_decision"
        ):
            raise RowAuthorityConfigError(
                "head state does not match its effective owner kind"
            )
    elif state == "settled":
        if (
            not has_owner
            or has_lease
            or fencing_token is None
            or latest_settlement is None
            or effective_settlement is None
        ):
            raise RowAuthorityConfigError(
                "settled heads require owner and settlements with no lease"
            )

    _require_uint(
        checked["projectionBacklogCount"],
        field_name="projectionBacklogCount",
    )
    created_at = _require_timestamp(checked["createdAt"], field_name="createdAt")
    updated_at = _require_timestamp(checked["updatedAt"], field_name="updatedAt")
    if updated_at < created_at:
        raise RowAuthorityConfigError("head updatedAt cannot predate createdAt")
    expected_hash = domain_hash(
        ROW_AUTHORITY_HEAD_HASH_DOMAIN,
        _head_hash_payload(checked),
        user_scope_hash=scope,
    )
    if checked["headHash"] != expected_hash:
        raise RowAuthorityConfigError("headHash does not recompute")
    return dict(checked)


def build_location_advanced_head(*, expected_head, location_revision_document):
    head = validate_row_authority_head(document=expected_head)
    revision = _validate_row_location_revision(location_revision_document)
    if revision["userScopeHash"] != head["userScopeHash"]:
        raise RowAuthorityConfigError(
            "location revision scope does not match expected head"
        )
    if revision["rowId"] != head["rowId"]:
        raise RowAuthorityConfigError(
            "location revision row does not match expected head"
        )
    if revision["revision"] != head["currentLocationRevision"] + 1:
        raise RowAuthorityConfigError(
            "location revision must immediately follow the expected head"
        )
    if revision["previousRevisionHash"] != head["currentLocationHash"]:
        raise RowAuthorityConfigError(
            "location revision predecessor does not match expected head"
        )
    if revision["observedAt"] < head["updatedAt"]:
        raise RowAuthorityConfigError(
            "location observation cannot predate the expected head"
        )
    result = {
        key: value
        for key, value in head.items()
        if key != "headHash"
    }
    result.update(
        {
            "stateRevision": head["stateRevision"] + 1,
            "currentLocationRevision": revision["revision"],
            "currentLocationHash": revision["revisionHash"],
            "currentLocationLifecycle": revision["lifecycle"],
            "updatedAt": revision["observedAt"],
        }
    )
    advanced = _with_head_hash(result)
    return validate_row_authority_head(document=advanced)


def _build_claim_advanced_head(
    *,
    expected_head,
    generation_document,
    lease_owner_hash,
    lease_until,
    dominated_predecessor_settlement_hash,
    claimed_at,
    expected_generation=None,
    expected_first_fencing_token=None,
):
    head = validate_row_authority_head(document=expected_head)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    lease_owner = _require_sha256(
        lease_owner_hash,
        field_name="lease_owner_hash",
    )
    deadline = _require_timestamp(lease_until, field_name="lease_until")
    event_time = _require_timestamp(claimed_at, field_name="claimed_at")
    dominated_hash = _require_optional_hash(
        dominated_predecessor_settlement_hash,
        field_name="dominated_predecessor_settlement_hash",
    )
    if (
        head["userScopeHash"] != generation["userScopeHash"]
        or head["rowId"] != generation["rowId"]
        or generation["predecessorHeadHash"] != head["headHash"]
        or generation["predecessorSettlementHash"]
        != head["effectiveSettlementHash"]
    ):
        raise RowAuthorityConfigError(
            "claim generation conflicts with the expected head"
        )
    if head["currentLocationLifecycle"] not in {"active", "nonviable"}:
        raise RowAuthorityConfigError(
            "claims require active or nonviable row identity"
        )
    if event_time < head["updatedAt"] or event_time < generation["createdAt"]:
        raise RowAuthorityConfigError(
            "claim event cannot predate head or generation readiness"
        )
    if deadline <= event_time:
        raise RowAuthorityConfigError("claim lease must end after claimed_at")
    current_generation = head["effectiveOwnerGeneration"]
    prior_fence = head["fencingToken"]
    if expected_generation is None:
        checked_expected_generation = (
            1 if current_generation is None else current_generation + 1
        )
    else:
        checked_expected_generation = _require_pos(
            expected_generation,
            field_name="expected_generation",
        )
    if expected_first_fencing_token is None:
        if current_generation is None:
            expected_first_fence = 1
        else:
            if prior_fence is None:
                raise RowAuthorityConfigError(
                    "owned head is missing its retained fencing token"
                )
            expected_first_fence = prior_fence + 1
    else:
        expected_first_fence = _require_pos(
            expected_first_fencing_token,
            field_name="expected_first_fencing_token",
        )
        if prior_fence is not None and expected_first_fence <= prior_fence:
            raise RowAuthorityConfigError(
                "bounded generation fence must advance the current head"
            )
    if generation["generation"] != checked_expected_generation:
        raise RowAuthorityConfigError(
            "generation does not match the bounded allocation floor"
        )
    if generation["firstFencingToken"] != expected_first_fence:
        raise RowAuthorityConfigError(
            "generation first fence does not match the bounded allocation floor"
        )
    if current_generation is None:
        if (
            head["state"] != "clear"
            or head["effectiveSettlementHash"] is not None
            or dominated_hash is not None
        ):
            raise RowAuthorityConfigError(
                "first claim requires a clear ownerless head"
            )
    else:
        if (
            generation["generation"] <= current_generation
            or generation["priority"] <= head["effectivePriority"]
        ):
            raise RowAuthorityConfigError(
                "superseding claim must allocate a higher-priority generation"
            )
        if head["state"] in {"claimed", "review_pending"}:
            if dominated_hash is None:
                raise RowAuthorityConfigError(
                    "unsettled predecessor requires dominated settlement"
                )
        elif dominated_hash is not None:
            raise RowAuthorityConfigError(
                "settled predecessor cannot be dominated again"
            )
    result = {
        key: value for key, value in head.items() if key != "headHash"
    }
    result.update(
        {
            "stateRevision": head["stateRevision"] + 1,
            "effectiveOwnerGeneration": generation["generation"],
            "effectiveOwnerGenerationHash": generation["generationHash"],
            "effectiveOwnerKind": generation["ownerKind"],
            "effectivePriority": generation["priority"],
            "state": (
                "review_pending"
                if generation["ownerKind"] == "human_decision"
                else "claimed"
            ),
            "leaseOwnerHash": lease_owner,
            "leaseUntil": deadline,
            "fencingToken": generation["firstFencingToken"],
            "latestSettlementHash": (
                dominated_hash
                if dominated_hash is not None
                else head["latestSettlementHash"]
            ),
            "updatedAt": event_time,
        }
    )
    return validate_row_authority_head(document=_with_head_hash(result))


def _build_lease_takeover_head(
    *,
    expected_head,
    generation_document,
    new_lease_owner_hash,
    new_lease_until,
    taken_at,
):
    head = validate_row_authority_head(document=expected_head)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    new_owner = _require_sha256(
        new_lease_owner_hash,
        field_name="new_lease_owner_hash",
    )
    deadline = _require_timestamp(
        new_lease_until,
        field_name="new_lease_until",
    )
    event_time = _require_timestamp(taken_at, field_name="taken_at")
    if head["state"] not in {"claimed", "review_pending"}:
        raise RowAuthorityConfigError("lease takeover requires an active claim")
    if (
        head["userScopeHash"] != generation["userScopeHash"]
        or head["rowId"] != generation["rowId"]
        or head["effectiveOwnerGeneration"] != generation["generation"]
        or head["effectiveOwnerGenerationHash"] != generation["generationHash"]
        or head["effectiveOwnerKind"] != generation["ownerKind"]
        or head["effectivePriority"] != generation["priority"]
        or head["fencingToken"] < generation["firstFencingToken"]
    ):
        raise RowAuthorityConfigError(
            "lease takeover generation conflicts with the expected head"
        )
    if event_time < head["updatedAt"]:
        raise RowAuthorityConfigError("takeover cannot predate the expected head")
    if head["leaseUntil"] >= event_time:
        raise RowAuthorityConfigError("lease must expire before takeover")
    if deadline <= event_time:
        raise RowAuthorityConfigError("new lease must end after takeover")
    result = {
        key: value for key, value in head.items() if key != "headHash"
    }
    result.update(
        {
            "stateRevision": head["stateRevision"] + 1,
            "leaseOwnerHash": new_owner,
            "leaseUntil": deadline,
            "fencingToken": head["fencingToken"] + 1,
            "updatedAt": event_time,
        }
    )
    return validate_row_authority_head(document=_with_head_hash(result))


_LEASE_TAKEOVER_LOCATION_FIELDS = frozenset(
    {
        "headHash",
        "stateRevision",
        "currentLocationRevision",
        "currentLocationHash",
        "currentLocationLifecycle",
        "updatedAt",
    }
)


def _lease_takeover_head_is_location_only_forward(
    *, takeover_head, current_head
):
    takeover = validate_row_authority_head(document=takeover_head)
    current = validate_row_authority_head(document=current_head)
    if current == takeover:
        return True
    for field in _HEAD_KEYS - _LEASE_TAKEOVER_LOCATION_FIELDS:
        if current[field] != takeover[field]:
            return False
    location_delta = (
        current["currentLocationRevision"]
        - takeover["currentLocationRevision"]
    )
    state_delta = current["stateRevision"] - takeover["stateRevision"]
    return (
        location_delta > 0
        and state_delta == location_delta
        and current["updatedAt"] >= takeover["updatedAt"]
    )


def _build_settlement_advanced_head(
    *, expected_head, generation_document, settlement_document
):
    head = validate_row_authority_head(document=expected_head)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    settlement = validate_owner_settlement_document(
        document=settlement_document
    )
    if head["state"] not in {"claimed", "review_pending"}:
        raise RowAuthorityConfigError("settlement requires an active claim")
    if (
        head["userScopeHash"] != generation["userScopeHash"]
        or settlement["userScopeHash"] != generation["userScopeHash"]
        or head["rowId"] != generation["rowId"]
        or settlement["rowId"] != generation["rowId"]
        or head["effectiveOwnerGeneration"] != generation["generation"]
        or settlement["generation"] != generation["generation"]
        or head["effectiveOwnerGenerationHash"] != generation["generationHash"]
        or settlement["generationHash"] != generation["generationHash"]
        or generation["predecessorSettlementHash"]
        != head["effectiveSettlementHash"]
        or head["effectiveOwnerKind"] != generation["ownerKind"]
        or head["effectivePriority"] != generation["priority"]
        or settlement["fencingToken"] != head["fencingToken"]
        or head["fencingToken"] < generation["firstFencingToken"]
    ):
        raise RowAuthorityConfigError(
            "settlement conflicts with generation or expected head"
        )
    allowed_outcome = {
        "contact_optout": "contact_optout",
        "terminal": "terminal",
        "human_decision": "human_declined",
    }[generation["ownerKind"]]
    if settlement["outcome"] != allowed_outcome:
        raise RowAuthorityConfigError(
            "settlement outcome conflicts with owner kind"
        )
    if settlement["settledAt"] < head["updatedAt"]:
        raise RowAuthorityConfigError(
            "settlement cannot predate the expected head"
        )
    result = {
        key: value for key, value in head.items() if key != "headHash"
    }
    result.update(
        {
            "stateRevision": head["stateRevision"] + 1,
            "state": "settled",
            "leaseOwnerHash": None,
            "leaseUntil": None,
            "latestSettlementHash": settlement["settlementHash"],
            "effectiveSettlementHash": settlement["settlementHash"],
            "updatedAt": settlement["settledAt"],
        }
    )
    return validate_row_authority_head(document=_with_head_hash(result))


def _settlement_head_is_forward(
    *,
    settled_head,
    current_head,
    generation_document,
    settlement_document,
    higher_generation_proven=False,
):
    settled = validate_row_authority_head(document=settled_head)
    current = validate_row_authority_head(document=current_head)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    settlement = validate_owner_settlement_document(
        document=settlement_document
    )
    if current == settled:
        return True
    if _lease_takeover_head_is_location_only_forward(
        takeover_head=settled,
        current_head=current,
    ):
        return True
    if not higher_generation_proven:
        return False
    if (
        current["userScopeHash"] != settled["userScopeHash"]
        or current["rowId"] != settled["rowId"]
        or current["createdAt"] != settled["createdAt"]
        or current["effectiveOwnerGeneration"] is None
        or current["effectiveOwnerGeneration"]
        <= generation["generation"]
        or current["effectiveOwnerGeneration"] > 3
        or current["effectivePriority"] <= generation["priority"]
        or current["fencingToken"] <= settlement["fencingToken"]
        or current["updatedAt"] < settlement["settledAt"]
        or current["currentLocationRevision"]
        < settled["currentLocationRevision"]
    ):
        return False
    location_delta = (
        current["currentLocationRevision"]
        - settled["currentLocationRevision"]
    )
    generation_delta = (
        current["effectiveOwnerGeneration"] - generation["generation"]
    )
    if current["stateRevision"] < (
        settled["stateRevision"] + location_delta + generation_delta
    ):
        return False
    if location_delta == 0 and (
        current["currentLocationHash"] != settled["currentLocationHash"]
        or current["currentLocationLifecycle"]
        != settled["currentLocationLifecycle"]
    ):
        return False
    if current["state"] in {"claimed", "review_pending"} and (
        current["latestSettlementHash"] != settlement["settlementHash"]
        or current["effectiveSettlementHash"]
        != settlement["settlementHash"]
    ):
        return False
    return True


def _build_source_link_advanced_head(
    *, expected_head, source_link_document
):
    head = validate_row_authority_head(document=expected_head)
    source_link = validate_source_settlement_link_document(
        document=source_link_document
    )
    if (
        head["userScopeHash"] != source_link["userScopeHash"]
        or head["rowId"] != source_link["rowId"]
    ):
        raise RowAuthorityConfigError(
            "source settlement link conflicts with the expected head"
        )
    if source_link["linkedAt"] < head["updatedAt"]:
        raise RowAuthorityConfigError(
            "source link cannot predate the expected head"
        )
    result = {
        key: value for key, value in head.items() if key != "headHash"
    }
    result.update(
        {
            "stateRevision": head["stateRevision"] + 1,
            "latestSourceSettlementLinkHash": source_link[
                "sourceSettlementLinkHash"
            ],
            "updatedAt": source_link["linkedAt"],
        }
    )
    return validate_row_authority_head(document=_with_head_hash(result))


def _source_link_head_reflects_b2_settlement(
    *,
    head_document,
    generation_document,
    settlement_document,
    historical_generation_proven=False,
):
    head = validate_row_authority_head(document=head_document)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    settlement = validate_owner_settlement_document(
        document=settlement_document
    )
    if (
        head["userScopeHash"] != generation["userScopeHash"]
        or settlement["userScopeHash"] != generation["userScopeHash"]
        or head["rowId"] != generation["rowId"]
        or settlement["rowId"] != generation["rowId"]
        or settlement["generation"] != generation["generation"]
        or settlement["generationHash"] != generation["generationHash"]
        or head["updatedAt"] < settlement["settledAt"]
    ):
        return False
    current_generation = head["effectiveOwnerGeneration"]
    same_generation = (
        current_generation == generation["generation"]
        and head["effectiveOwnerGenerationHash"]
        == generation["generationHash"]
        and head["effectiveOwnerKind"] == generation["ownerKind"]
        and head["effectivePriority"] == generation["priority"]
        and head["state"] == "settled"
        and head["fencingToken"] == settlement["fencingToken"]
        and (
            head["latestSettlementHash"] == settlement["settlementHash"]
            or head["latestOptOutReleaseResultHash"] is not None
        )
        and head["effectiveSettlementHash"]
        == settlement["settlementHash"]
    )
    higher_generation = (
        current_generation is not None
        and current_generation > generation["generation"]
        and (
            head["effectivePriority"] > generation["priority"]
            or historical_generation_proven is True
        )
        and head["fencingToken"] > settlement["fencingToken"]
        and head["latestSettlementHash"] is not None
    )
    release_restored = (
        head["latestOptOutReleaseResultHash"] is not None
        and head["latestSettlementHash"] is not None
        and (
            current_generation is None
            or current_generation < generation["generation"]
        )
    )
    return same_generation or higher_generation or release_restored


def _timestamp_as_datetime(value, *, field_name):
    checked = _require_timestamp(value, field_name=field_name)
    return datetime.strptime(
        checked,
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ).replace(tzinfo=timezone.utc)


def _generation_document_id(*, row_id, generation):
    checked_row = validate_row_id(row_id)
    checked_generation = _require_pos(generation, field_name="generation")
    return _require_firestore_document_id(
        f"{checked_row}--{checked_generation}",
        field_name="owner generation document ID",
    )


def _bounded_predecessor_release_lookup_hash(
    *, latest_settlements, latest_authorities
):
    """Return the exact older settlement hash when a bounded pair skips it."""
    if (
        type(latest_settlements) not in {list, tuple}
        or type(latest_authorities) not in {list, tuple}
        or len(latest_settlements) != 2
        or len(latest_authorities) != 2
    ):
        return None
    try:
        newer_generation = validate_owner_generation_document(
            document=latest_authorities[0]["generation"]
        )
        older_settlement = validate_owner_settlement_document(
            document=latest_settlements[1]["document"]
        )
    except Exception:
        return None
    if (
        older_settlement["outcome"] == "dominated"
        or newer_generation["predecessorSettlementHash"]
        == older_settlement["settlementHash"]
    ):
        return None
    return older_settlement["settlementHash"]


def _read_bounded_release_restored_authority(
    *, user_ref, row_id, release_document, read
):
    """Read the exact authority restored by one already-bounded release."""
    try:
        release_result = validate_contact_fanout_result_document(
            document=release_document
        )
        restored_number = release_result["restoredEffectiveGeneration"]
        if restored_number is None:
            return None
        restored_id = _generation_document_id(
            row_id=row_id,
            generation=restored_number,
        )
        restored_generation_ref = user_ref.collection(
            "rowOwnerGenerations"
        ).document(restored_id)
        restored_generation_exists, restored_generation = read(
            restored_generation_ref
        )
        restored_claim = None
        if restored_generation_exists:
            checked_generation = validate_owner_generation_document(
                document=restored_generation
            )
            restored_claim_ref = user_ref.collection("rowClaimSets").document(
                checked_generation["requestId"]
            )
            restored_claim_exists, restored_claim_payload = read(
                restored_claim_ref
            )
            if restored_claim_exists:
                restored_claim = restored_claim_payload
        restored_settlement_ref = user_ref.collection(
            "rowOwnerSettlements"
        ).document(restored_id)
        restored_settlement_exists, restored_settlement = read(
            restored_settlement_ref
        )
        return {
            "generation": (
                restored_generation if restored_generation_exists else None
            ),
            "claimSet": restored_claim,
            "settlement": (
                restored_settlement if restored_settlement_exists else None
            ),
        }
    except RowAuthorityRetryable:
        raise
    except Exception:
        return {"malformed": True}


def _read_bounded_replay_winner_successor(
    *,
    user_ref,
    row_id,
    winner_generation,
    winner_settlement,
    claim_created_at,
    read,
    transaction,
    query_readbacks,
):
    """Read the exact N+1/N+2 bracket for one dominated winner."""
    successor_id = _generation_document_id(
        row_id=row_id,
        generation=winner_generation["generation"] + 1,
    )
    successor_generation_ref = user_ref.collection(
        "rowOwnerGenerations"
    ).document(successor_id)
    successor_generation_exists, successor_generation = read(
        successor_generation_ref
    )
    successor_claim = None
    checked_successor = None
    if successor_generation_exists:
        try:
            checked_successor = validate_owner_generation_document(
                document=successor_generation
            )
            successor_claim_ref = user_ref.collection(
                "rowClaimSets"
            ).document(checked_successor["requestId"])
            successor_claim_exists, successor_claim_payload = read(
                successor_claim_ref
            )
            if successor_claim_exists:
                successor_claim = successor_claim_payload
        except RowAuthorityRetryable:
            raise
        except Exception:
            checked_successor = None

    successor_settlement_ref = user_ref.collection(
        "rowOwnerSettlements"
    ).document(successor_id)
    successor_settlement_exists, successor_settlement = read(
        successor_settlement_ref
    )
    try:
        checked_successor_settlement = (
            validate_owner_settlement_document(
                document=successor_settlement
            )
            if successor_settlement_exists
            else None
        )
    except Exception:
        checked_successor_settlement = None

    def read_release_proof(settlement_hash):
        matches = []
        try:
            release_query = (
                user_ref.collection("contactOptOutFanoutResults")
                .where("rowId", "==", row_id)
                .where(
                    "releasedRowSettlementHash",
                    "==",
                    settlement_hash,
                )
                .order_by("__name__")
                .limit(2)
            )
            release_snapshots = tuple(transaction.get(release_query))
        except Exception as exc:
            raise RowAuthorityRetryable(
                "dominated replay winner-successor release query failed before writes"
            ) from exc
        for release_snapshot in release_snapshots:
            release_exists, release_payload = read(
                release_snapshot.reference
            )
            if not release_exists:
                raise RowAuthorityAmbiguous(
                    "dominated replay winner-successor release query returned a missing document"
                )
            matches.append(
                {
                    "path": release_snapshot.reference.path,
                    "document": release_payload,
                }
            )
        query_readbacks.append(
            {
                "kind": "stable",
                "query": release_query,
                "matches": tuple(
                    (
                        entry["path"],
                        _defensive_copy(entry["document"]),
                    )
                    for entry in matches
                ),
            }
        )
        restored = None
        if len(matches) == 1:
            restored = _read_bounded_release_restored_authority(
                user_ref=user_ref,
                row_id=row_id,
                release_document=matches[0]["document"],
                read=read,
            )
        return matches, restored

    link_release_matches = []
    link_restored_authority = None
    try:
        checked_winner_settlement = validate_owner_settlement_document(
            document=winner_settlement
        )
    except Exception:
        checked_winner_settlement = None
    release_skipped = (
        checked_successor is not None
        and checked_winner_settlement is not None
        and checked_winner_settlement["outcome"] == "contact_optout"
        and checked_successor["predecessorSettlementHash"]
        != checked_winner_settlement["settlementHash"]
    )
    if release_skipped:
        try:
            (
                link_release_matches,
                link_restored_authority,
            ) = read_release_proof(
                checked_winner_settlement["settlementHash"]
            )
        except RowAuthorityError:
            raise

    restoration_release_matches = []
    restored_winner_authority = None
    restoration_exit_generation = None
    restoration_exit_claim = None
    restoration_exit_settlement = None
    restoration_may_be_needed = (
        checked_successor is not None
        and checked_successor_settlement is not None
        and checked_successor_settlement["outcome"] == "contact_optout"
        and checked_successor["createdAt"] <= claim_created_at
    )
    if restoration_may_be_needed:
        try:
            (
                restoration_release_matches,
                restored_winner_authority,
            ) = read_release_proof(
                checked_successor_settlement["settlementHash"]
            )
        except RowAuthorityError:
            raise
        restoration_exit_id = _generation_document_id(
            row_id=row_id,
            generation=winner_generation["generation"] + 2,
        )
        restoration_exit_ref = user_ref.collection(
            "rowOwnerGenerations"
        ).document(restoration_exit_id)
        (
            restoration_exit_exists,
            restoration_exit_payload,
        ) = read(restoration_exit_ref)
        if restoration_exit_exists:
            restoration_exit_generation = restoration_exit_payload
            try:
                checked_restoration_exit = (
                    validate_owner_generation_document(
                        document=restoration_exit_payload
                    )
                )
                restoration_exit_claim_ref = user_ref.collection(
                    "rowClaimSets"
                ).document(checked_restoration_exit["requestId"])
                (
                    restoration_exit_claim_exists,
                    restoration_exit_claim_payload,
                ) = read(restoration_exit_claim_ref)
                if restoration_exit_claim_exists:
                    restoration_exit_claim = (
                        restoration_exit_claim_payload
                    )
            except RowAuthorityRetryable:
                raise
            except Exception:
                pass
        restoration_exit_settlement_ref = user_ref.collection(
            "rowOwnerSettlements"
        ).document(restoration_exit_id)
        (
            restoration_exit_settlement_exists,
            restoration_exit_settlement_payload,
        ) = read(restoration_exit_settlement_ref)
        if restoration_exit_settlement_exists:
            restoration_exit_settlement = (
                restoration_exit_settlement_payload
            )
    return {
        "generation": (
            successor_generation if successor_generation_exists else None
        ),
        "claimSet": successor_claim,
        "settlement": (
            successor_settlement if successor_settlement_exists else None
        ),
        "linkReleaseMatches": link_release_matches,
        "linkRestoredAuthority": link_restored_authority,
        "restorationReleaseMatches": restoration_release_matches,
        "restoredWinnerAuthority": restored_winner_authority,
        "restorationExitGeneration": restoration_exit_generation,
        "restorationExitClaimSet": restoration_exit_claim,
        "restorationExitSettlement": restoration_exit_settlement,
    }


def _validate_bounded_release_restored_authority(
    *,
    scope,
    row_id,
    release_result,
    released_generation,
    raw_restored_authority,
    successor_generation,
):
    """Validate the exact owner restored before a later successor."""
    restored_number = release_result["restoredEffectiveGeneration"]
    restored_hash = release_result["restoredEffectiveSettlementHash"]
    if restored_number is None:
        if raw_restored_authority is not None or restored_hash is not None:
            raise RowAuthorityAmbiguous(
                "clear predecessor release carries restored authority"
            )
        return None
    if (
        type(raw_restored_authority) is not dict
        or set(raw_restored_authority)
        != {"generation", "claimSet", "settlement"}
    ):
        raise RowAuthorityAmbiguous(
            "predecessor release lacks exact restored authority"
        )
    try:
        restored_generation = validate_owner_generation_document(
            document=raw_restored_authority["generation"]
        )
        restored_claim = validate_claim_set_document(
            document=raw_restored_authority["claimSet"]
        )
        restored_settlement = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=row_id,
            generation=restored_generation,
            claim=restored_claim,
            settlement_document=raw_restored_authority["settlement"],
        )
    except RowAuthorityError:
        raise
    except Exception as exc:
        raise RowAuthorityAmbiguous(
            "predecessor release restored authority is malformed"
        ) from exc
    decisions = [
        item
        for item in restored_claim["rowDecisions"]
        if item["rowId"] == row_id
    ]
    if (
        restored_generation["userScopeHash"] != scope
        or restored_claim["userScopeHash"] != scope
        or restored_generation["rowId"] != row_id
        or restored_generation["generation"] != restored_number
        or restored_generation["requestId"] != restored_claim["requestId"]
        or restored_generation["claimSetHash"]
        != restored_claim["claimSetHash"]
        or restored_generation["ownerKind"]
        not in {"terminal", "human_decision"}
        or restored_generation["ownerKind"] != restored_claim["ownerKind"]
        or restored_generation["ownerKey"] != restored_claim["ownerKey"]
        or restored_generation["priority"] != restored_claim["derivedPriority"]
        or restored_claim["outcome"] != "accepted"
        or restored_generation["createdAt"] < restored_claim["createdAt"]
        or len(decisions) != 1
        or decisions[0]["decision"] != "accepted"
        or decisions[0]["plannedGeneration"]
        != restored_generation["generation"]
        or restored_settlement["outcome"]
        != _expected_owner_settlement_outcome(
            restored_generation["ownerKind"]
        )
        or restored_settlement["settlementHash"] != restored_hash
        or restored_settlement["settledAt"] > released_generation["createdAt"]
        or restored_generation["generation"]
        >= released_generation["generation"]
        or successor_generation["priority"] <= restored_generation["priority"]
    ):
        raise RowAuthorityConflict(
            "restored authority does not permit the bounded successor"
        )
    return {
        "generation": restored_generation,
        "claimSet": restored_claim,
        "settlement": restored_settlement,
    }


def _validate_bounded_row_history(*, scope, row_id, head, row_state):
    """Validate the bounded current/history proof used by B2-C allocation."""
    checked_scope = _require_sha256(scope, field_name="scope")
    checked_row_id = validate_row_id(row_id)
    checked_head = validate_row_authority_head(document=head)
    if (
        checked_head["userScopeHash"] != checked_scope
        or checked_head["rowId"] != checked_row_id
    ):
        raise RowAuthorityConflict("bounded row history head does not correlate")

    raw_latest = row_state.get("latestSettlements")
    if type(raw_latest) not in {list, tuple} or len(raw_latest) > 2:
        raise RowAuthorityAmbiguous(
            "bounded row history requires at most two latest settlements"
        )
    latest = []
    for raw_entry in raw_latest:
        if type(raw_entry) is not dict or set(raw_entry) != {"path", "document"}:
            raise RowAuthorityAmbiguous(
                "bounded row history settlement entry is malformed"
            )
        try:
            settlement = validate_owner_settlement_document(
                document=raw_entry["document"]
            )
            expected_id = _generation_document_id(
                row_id=checked_row_id,
                generation=settlement["generation"],
            )
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "bounded row history settlement is malformed"
            ) from exc
        path = raw_entry["path"]
        if (
            type(path) is not str
            or path.split("/")[-2:] != ["rowOwnerSettlements", expected_id]
            or settlement["userScopeHash"] != checked_scope
            or settlement["rowId"] != checked_row_id
        ):
            raise RowAuthorityAmbiguous(
                "bounded row history settlement occupies the wrong path"
            )
        latest.append(settlement)
    raw_authorities = row_state.get("latestSettlementAuthorities")
    if (
        type(raw_authorities) not in {list, tuple}
        or len(raw_authorities) != len(latest)
    ):
        raise RowAuthorityAmbiguous(
            "bounded row history lacks exact settlement authority"
        )
    latest_authorities = []
    for settlement, raw_authority in zip(latest, raw_authorities):
        if type(raw_authority) is not dict or set(raw_authority) != {
            "generation",
            "claimSet",
        }:
            raise RowAuthorityAmbiguous(
                "bounded settlement authority entry is malformed"
            )
        try:
            authority_generation = validate_owner_generation_document(
                document=raw_authority["generation"]
            )
            authority_claim = validate_claim_set_document(
                document=raw_authority["claimSet"]
            )
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "bounded settlement authority is missing or malformed"
            ) from exc
        correlated_settlement = _validate_correlated_owner_settlement(
            scope=checked_scope,
            row_id=checked_row_id,
            generation=authority_generation,
            claim=authority_claim,
            settlement_document=settlement,
        )
        matching_decisions = [
            decision
            for decision in authority_claim["rowDecisions"]
            if decision["rowId"] == checked_row_id
        ]
        if (
            authority_generation["userScopeHash"] != checked_scope
            or authority_claim["userScopeHash"] != checked_scope
            or authority_generation["rowId"] != checked_row_id
            or authority_generation["generation"]
            != settlement["generation"]
            or authority_generation["requestId"]
            != authority_claim["requestId"]
            or authority_generation["claimSetHash"]
            != authority_claim["claimSetHash"]
            or authority_generation["ownerKind"] != authority_claim["ownerKind"]
            or authority_generation["ownerKey"] != authority_claim["ownerKey"]
            or authority_generation["priority"]
            != authority_claim["derivedPriority"]
            or authority_claim["outcome"] != "accepted"
            or authority_generation["createdAt"]
            < authority_claim["createdAt"]
            or len(matching_decisions) != 1
            or matching_decisions[0]["decision"] != "accepted"
            or matching_decisions[0]["plannedGeneration"]
            != authority_generation["generation"]
            or correlated_settlement["outcome"]
            not in {
                "dominated",
                _expected_owner_settlement_outcome(
                    authority_generation["ownerKind"]
                ),
            }
        ):
            raise RowAuthorityConflict(
                "bounded settlement does not match its exact owner authority"
            )
        latest_authorities.append(
            {
                "generation": authority_generation,
                "claimSet": authority_claim,
                "settlement": correlated_settlement,
            }
        )
    for authority in latest_authorities:
        generation = authority["generation"]
        if generation["generation"] == 1 and (
            generation["predecessorSettlementHash"] is not None
            or generation["firstFencingToken"] != 1
        ):
            raise RowAuthorityAmbiguous(
                "first bounded row generation has predecessor state"
            )
    if len(latest) == 1 and latest[0]["generation"] != 1:
        raise RowAuthorityAmbiguous(
            "single bounded row settlement must be generation one"
        )
    validated_predecessor_release_matches = ()
    validated_predecessor_restored_authority = None
    if len(latest) == 2:
        newer_generation = latest_authorities[0]["generation"]
        older_generation = latest_authorities[1]["generation"]
        if (
            latest[0]["generation"] != latest[1]["generation"] + 1
            or latest[0]["fencingToken"] <= latest[1]["fencingToken"]
            or newer_generation["firstFencingToken"]
            != latest[1]["fencingToken"] + 1
            or newer_generation["createdAt"] < latest[1]["settledAt"]
        ):
            raise RowAuthorityAmbiguous(
                "latest row settlements have a gap or fencing regression"
            )
        if latest[1]["outcome"] == "dominated":
            if (
                latest[1]["dominantGenerationHash"]
                != newer_generation["generationHash"]
                or latest[1]["settledAt"] != newer_generation["createdAt"]
                or newer_generation["predecessorSettlementHash"]
                != older_generation["predecessorSettlementHash"]
                or newer_generation["priority"]
                <= older_generation["priority"]
            ):
                raise RowAuthorityConflict(
                    "latest dominated settlement does not link to its successor"
                )
        elif (
            newer_generation["predecessorSettlementHash"]
            == latest[1]["settlementHash"]
        ):
            if newer_generation["priority"] <= older_generation["priority"]:
                raise RowAuthorityConflict(
                    "direct settled successor does not have higher priority"
                )
        else:
            raw_release_matches = row_state.get(
                "latestPredecessorReleaseMatches"
            )
            if type(raw_release_matches) not in {list, tuple}:
                raise RowAuthorityAmbiguous(
                    "settled successor lacks bounded predecessor release proof"
                )
            if len(raw_release_matches) != 1:
                raise RowAuthorityAmbiguous(
                    "settled successor predecessor release is missing or duplicated"
                )
            raw_release = raw_release_matches[0]
            if type(raw_release) is not dict or set(raw_release) != {
                "path",
                "document",
            }:
                raise RowAuthorityAmbiguous(
                    "settled successor predecessor release proof is malformed"
                )
            try:
                predecessor_release = validate_contact_fanout_result_document(
                    document=raw_release["document"]
                )
            except Exception as exc:
                raise RowAuthorityAmbiguous(
                    "settled successor predecessor release proof is malformed"
                ) from exc
            if (
                type(raw_release["path"]) is not str
                or raw_release["path"].split("/")[-2:]
                != [
                    "contactOptOutFanoutResults",
                    f"{predecessor_release['fanoutId']}--{checked_row_id}",
                ]
                or older_generation["ownerKind"] != "contact_optout"
                or latest[1]["outcome"] != "contact_optout"
                or predecessor_release["userScopeHash"] != checked_scope
                or predecessor_release["rowId"] != checked_row_id
                or predecessor_release["outcome"] != "release"
                or predecessor_release["disposition"] != "restore"
                or predecessor_release["reasonCode"] != "exact_predecessor"
                or predecessor_release["releasedRowGeneration"]
                != older_generation["generation"]
                or predecessor_release["releasedRowSettlementHash"]
                != latest[1]["settlementHash"]
                or predecessor_release["restoredEffectiveSettlementHash"]
                != newer_generation["predecessorSettlementHash"]
                or latest[1]["supersededEffectiveSettlementHash"]
                != newer_generation["predecessorSettlementHash"]
                or predecessor_release["createdAt"] < latest[1]["settledAt"]
                or predecessor_release["createdAt"]
                > newer_generation["createdAt"]
            ):
                raise RowAuthorityAmbiguous(
                    "settled successor predecessor release does not correlate"
                )
            validated_predecessor_restored_authority = (
                _validate_bounded_release_restored_authority(
                    scope=checked_scope,
                    row_id=checked_row_id,
                    release_result=predecessor_release,
                    released_generation=older_generation,
                    raw_restored_authority=row_state.get(
                        "latestPredecessorRestoredAuthority"
                    ),
                    successor_generation=newer_generation,
                )
            )
            validated_predecessor_release_matches = tuple(
                raw_release_matches
            )
    latest_hash = latest[0]["settlementHash"] if latest else None
    if checked_head["latestSettlementHash"] != latest_hash:
        if not latest and checked_head["latestSettlementHash"] is not None:
            raise RowAuthorityConflict(
                "row head claims settlement history that does not exist"
            )
        raise RowAuthorityAmbiguous(
            "row head latest settlement differs from bounded history"
        )

    current_number = checked_head["effectiveOwnerGeneration"]
    current_generation_document = row_state.get("currentGeneration")
    current_claim_document = row_state.get("currentClaimSet")
    current_settlement_document = row_state.get("currentSettlement")
    if current_number is None:
        if any(
            value is not None
            for value in (
                current_generation_document,
                current_claim_document,
                current_settlement_document,
            )
        ):
            raise RowAuthorityAmbiguous(
                "ownerless row has unexpected current ownership artifacts"
            )
        if (
            checked_head["state"] != "clear"
            or checked_head["effectiveSettlementHash"] is not None
        ):
            raise RowAuthorityConflict(
                "ownerless bounded row history does not match a clear head"
            )
        generation = None
        claim = None
        settlement = None
    else:
        try:
            generation = validate_owner_generation_document(
                document=current_generation_document
            )
            claim = validate_claim_set_document(document=current_claim_document)
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "bounded row history is missing current generation authority"
            ) from exc
        matching_decisions = [
            decision
            for decision in claim["rowDecisions"]
            if decision["rowId"] == checked_row_id
        ]
        if claim["authorityOrigin"] == "authenticated_operator":
            minimum_writes = 2 + (3 * claim["bindingCount"])
            maximum_writes = minimum_writes
        else:
            minimum_writes = 1 + (2 * claim["bindingCount"])
            maximum_writes = minimum_writes + sum(
                decision["plannedGeneration"] is not None
                and decision["plannedGeneration"] > 1
                for decision in claim["rowDecisions"]
            )
        if (
            generation["userScopeHash"] != checked_scope
            or claim["userScopeHash"] != checked_scope
            or generation["rowId"] != checked_row_id
            or generation["generation"] != current_number
            or generation["generationHash"]
            != checked_head["effectiveOwnerGenerationHash"]
            or generation["requestId"] != claim["requestId"]
            or generation["claimSetHash"] != claim["claimSetHash"]
            or generation["ownerKind"] != claim["ownerKind"]
            or generation["ownerKey"] != claim["ownerKey"]
            or generation["priority"] != claim["derivedPriority"]
            or generation["ownerKind"] != checked_head["effectiveOwnerKind"]
            or generation["priority"] != checked_head["effectivePriority"]
            or claim["outcome"] != "accepted"
            or len(matching_decisions) != 1
            or matching_decisions[0]["decision"] != "accepted"
            or matching_decisions[0]["plannedGeneration"] != current_number
            or not (minimum_writes <= claim["plannedWrites"] <= maximum_writes)
            or generation["createdAt"] < claim["createdAt"]
            or generation["createdAt"] < checked_head["createdAt"]
            or checked_head["updatedAt"] < generation["createdAt"]
            or checked_head["stateRevision"] < generation["generation"] + 1
            or checked_head["stateRevision"] <= checked_head["fencingToken"]
        ):
            raise RowAuthorityConflict(
                "bounded current generation and claim do not correlate"
            )
        if checked_head["fencingToken"] < generation["firstFencingToken"]:
            raise RowAuthorityAmbiguous(
                "bounded current head fence regressed below its generation"
            )
        if checked_head["state"] in {"claimed", "review_pending"}:
            expected_state = (
                "review_pending"
                if generation["ownerKind"] == "human_decision"
                else "claimed"
            )
            if current_settlement_document is not None:
                raise RowAuthorityAmbiguous(
                    "unsettled bounded row already has a current settlement"
                )
            if (
                checked_head["state"] != expected_state
                or checked_head["leaseUntil"] <= generation["createdAt"]
                or generation["predecessorSettlementHash"]
                != checked_head["effectiveSettlementHash"]
            ):
                raise RowAuthorityConflict(
                    "bounded active owner does not match its row head"
                )
            settlement = None
            if not latest:
                adjacent = (
                    generation["generation"] == 1
                    and generation["firstFencingToken"] == 1
                )
            else:
                adjacent = (
                    generation["generation"] == latest[0]["generation"] + 1
                    and generation["firstFencingToken"]
                    == latest[0]["fencingToken"] + 1
                )
            if not adjacent:
                raise RowAuthorityAmbiguous(
                    "current unsettled generation is not adjacent to history"
                )
        elif checked_head["state"] == "settled":
            if current_settlement_document is None:
                raise RowAuthorityAmbiguous(
                    "settled bounded row is missing its current settlement"
                )
            settlement = _validate_correlated_owner_settlement(
                scope=checked_scope,
                row_id=checked_row_id,
                generation=generation,
                claim=claim,
                settlement_document=current_settlement_document,
            )
            if (
                settlement["outcome"]
                != _expected_owner_settlement_outcome(generation["ownerKind"])
                or settlement["settlementHash"]
                != checked_head["effectiveSettlementHash"]
                or settlement["fencingToken"] != checked_head["fencingToken"]
                or settlement["settledAt"] > checked_head["updatedAt"]
            ):
                raise RowAuthorityConflict(
                    "settled bounded row does not match its effective owner"
                )
        else:
            raise RowAuthorityConflict(
                "bounded owned row has an unsupported head state"
            )

    release_result_document = row_state.get("releaseResult")
    release_result_path = row_state.get("releaseResultPath")
    historical_divergence = (
        checked_head["latestSettlementHash"]
        != checked_head["effectiveSettlementHash"]
    )
    current_pending = (
        generation is not None
        and settlement is None
        and checked_head["state"] in {"claimed", "review_pending"}
    )
    active_bridge = False
    if current_pending and latest and latest[0]["outcome"] == "dominated":
        bridge_generation = latest_authorities[0]["generation"]
        active_bridge = (
            bridge_generation["generation"] + 1 == generation["generation"]
            and latest[0]["dominantGenerationHash"]
            == generation["generationHash"]
            and latest[0]["settledAt"] == generation["createdAt"]
            and generation["predecessorSettlementHash"]
            == bridge_generation["predecessorSettlementHash"]
            and generation["priority"] > bridge_generation["priority"]
        )
        if not active_bridge:
            raise RowAuthorityConflict(
                "active dominated settlement does not bridge to the current owner"
            )
    if current_pending and not historical_divergence and latest:
        direct_generation = latest_authorities[0]["generation"]
        if (
            generation["priority"] <= direct_generation["priority"]
            or (
                latest[0]["outcome"] == "dominated"
                and not active_bridge
            )
            or (
                latest[0]["outcome"] != "dominated"
                and generation["predecessorSettlementHash"]
                != latest[0]["settlementHash"]
            )
        ):
            raise RowAuthorityConflict(
                "current owner does not advance its direct predecessor authority"
            )
    release_result = None
    released_authority = None
    if historical_divergence and checked_head[
        "latestOptOutReleaseResultHash"
    ] is not None:
        if not latest or release_result_document is None:
            raise RowAuthorityAmbiguous(
                "release-restored history is missing its exact result bridge"
            )
        try:
            release_result = validate_contact_fanout_result_document(
                document=release_result_document
            )
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "release-restored result bridge is malformed"
            ) from exc
        raw_released = row_state.get("releasedAuthority")
        if type(raw_released) is not dict or set(raw_released) != {
            "path",
            "generation",
            "claimSet",
            "settlement",
        }:
            raise RowAuthorityAmbiguous(
                "release-restored history lacks exact released authority"
            )
        try:
            released_owner_generation = validate_owner_generation_document(
                document=raw_released["generation"]
            )
            released_owner_claim = validate_claim_set_document(
                document=raw_released["claimSet"]
            )
            released_settlement = _validate_correlated_owner_settlement(
                scope=checked_scope,
                row_id=checked_row_id,
                generation=released_owner_generation,
                claim=released_owner_claim,
                settlement_document=raw_released["settlement"],
            )
            released_id = _generation_document_id(
                row_id=checked_row_id,
                generation=released_owner_generation["generation"],
            )
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "release-restored released authority is malformed"
            ) from exc
        released_decisions = [
            decision
            for decision in released_owner_claim["rowDecisions"]
            if decision["rowId"] == checked_row_id
        ]
        if (
            type(raw_released["path"]) is not str
            or raw_released["path"].split("/")[-2:]
            != ["rowOwnerSettlements", released_id]
            or released_owner_generation["userScopeHash"] != checked_scope
            or released_owner_claim["userScopeHash"] != checked_scope
            or released_owner_generation["rowId"] != checked_row_id
            or released_owner_generation["requestId"]
            != released_owner_claim["requestId"]
            or released_owner_generation["claimSetHash"]
            != released_owner_claim["claimSetHash"]
            or released_owner_generation["ownerKind"] != "contact_optout"
            or released_owner_generation["ownerKind"]
            != released_owner_claim["ownerKind"]
            or released_owner_generation["ownerKey"]
            != released_owner_claim["ownerKey"]
            or released_owner_generation["priority"]
            != released_owner_claim["derivedPriority"]
            or released_owner_claim["outcome"] != "accepted"
            or released_owner_generation["createdAt"]
            < released_owner_claim["createdAt"]
            or len(released_decisions) != 1
            or released_decisions[0]["decision"] != "accepted"
            or released_decisions[0]["plannedGeneration"]
            != released_owner_generation["generation"]
            or released_settlement["outcome"] != "contact_optout"
        ):
            raise RowAuthorityAmbiguous(
                "release-restored released authority does not correlate"
            )
        matching_latest = [
            index
            for index, settlement in enumerate(latest)
            if settlement["generation"]
            == released_owner_generation["generation"]
        ]
        if len(matching_latest) > 1:
            raise RowAuthorityAmbiguous(
                "release-restored released authority is duplicated"
            )
        if matching_latest:
            index = matching_latest[0]
            if (
                latest[index] != released_settlement
                or latest_authorities[index]["generation"]
                != released_owner_generation
                or latest_authorities[index]["claimSet"]
                != released_owner_claim
            ):
                raise RowAuthorityAmbiguous(
                    "release-restored bounded and exact authority differ"
                )
        elif (
            not latest
            or released_owner_generation["generation"]
            >= latest[0]["generation"]
        ):
            raise RowAuthorityAmbiguous(
                "release-restored released authority is not historical"
            )
        released_authority = {
            "generation": released_owner_generation,
            "claimSet": released_owner_claim,
            "settlement": released_settlement,
        }
        restored_generation = release_result["restoredEffectiveGeneration"]
        restored_settlement_hash = release_result[
            "restoredEffectiveSettlementHash"
        ]
        if (
            release_result["userScopeHash"] != checked_scope
            or release_result["rowId"] != checked_row_id
            or type(release_result_path) is not str
            or release_result_path.split("/")[-2:]
            != [
                "contactOptOutFanoutResults",
                f"{release_result['fanoutId']}--{checked_row_id}",
            ]
            or release_result["contactFanoutResultHash"]
            != checked_head["latestOptOutReleaseResultHash"]
            or release_result["outcome"] != "release"
            or release_result["disposition"] != "restore"
            or release_result["reasonCode"] != "exact_predecessor"
            or release_result["releasedRowGeneration"]
            != released_owner_generation["generation"]
            or release_result["releasedRowSettlementHash"]
            != released_settlement["settlementHash"]
            or released_settlement["outcome"] != "contact_optout"
            or released_settlement[
                "supersededEffectiveSettlementHash"
            ]
            != released_owner_generation["predecessorSettlementHash"]
            or restored_settlement_hash
            != released_owner_generation["predecessorSettlementHash"]
            or release_result["createdAt"] < released_settlement["settledAt"]
            or release_result["createdAt"] > checked_head["updatedAt"]
            or restored_settlement_hash
            != checked_head["effectiveSettlementHash"]
        ):
            raise RowAuthorityAmbiguous(
                "release-restored result bridge does not correlate"
            )
        if not active_bridge:
            if not matching_latest or matching_latest[0] != 0:
                raise RowAuthorityAmbiguous(
                    "release result does not bridge the latest settlement"
                )
        elif matching_latest:
            if matching_latest[0] != len(latest) - 1:
                raise RowAuthorityAmbiguous(
                    "combined release bridge targets the wrong settlement"
                )
            if (
                matching_latest[0] > 0
                and release_result["createdAt"]
                > latest_authorities[matching_latest[0] - 1]["generation"][
                    "createdAt"
                ]
            ):
                raise RowAuthorityAmbiguous(
                    "combined release bridge postdates its successor"
                )
        else:
            if (
                len(latest) != 2
                or any(
                    settlement["outcome"] != "dominated"
                    for settlement in latest
                )
                or released_owner_generation["generation"] + 1
                != latest_authorities[-1]["generation"]["generation"]
                or latest_authorities[-1]["generation"][
                    "predecessorSettlementHash"
                ]
                != released_settlement[
                    "supersededEffectiveSettlementHash"
                ]
                or release_result["createdAt"]
                > latest_authorities[-1]["generation"]["createdAt"]
            ):
                raise RowAuthorityAmbiguous(
                    "combined row history lacks its exact release foundation"
                )
        raw_restored = row_state.get("restoredAuthority")
        restored_authority = None
        if restored_generation is None:
            if raw_restored is not None or restored_settlement_hash is not None:
                raise RowAuthorityAmbiguous(
                    "clear release restoration carries owner authority"
                )
        else:
            if type(raw_restored) is not dict or set(raw_restored) != {
                "generation",
                "claimSet",
                "settlement",
            }:
                raise RowAuthorityAmbiguous(
                    "release restoration lacks exact owner authority"
                )
            try:
                restored_owner_generation = validate_owner_generation_document(
                    document=raw_restored["generation"]
                )
                restored_owner_claim = validate_claim_set_document(
                    document=raw_restored["claimSet"]
                )
                restored_owner_settlement = (
                    _validate_correlated_owner_settlement(
                        scope=checked_scope,
                        row_id=checked_row_id,
                        generation=restored_owner_generation,
                        claim=restored_owner_claim,
                        settlement_document=raw_restored["settlement"],
                    )
                )
            except Exception as exc:
                raise RowAuthorityAmbiguous(
                    "release restored owner authority is malformed"
                ) from exc
            restored_decisions = [
                decision
                for decision in restored_owner_claim["rowDecisions"]
                if decision["rowId"] == checked_row_id
            ]
            if (
                restored_owner_generation["generation"] != restored_generation
                or restored_owner_generation["userScopeHash"] != checked_scope
                or restored_owner_claim["userScopeHash"] != checked_scope
                or restored_owner_generation["rowId"] != checked_row_id
                or restored_owner_generation["requestId"]
                != restored_owner_claim["requestId"]
                or restored_owner_generation["claimSetHash"]
                != restored_owner_claim["claimSetHash"]
                or restored_owner_generation["ownerKind"]
                not in {"terminal", "human_decision"}
                or restored_owner_generation["ownerKind"]
                != restored_owner_claim["ownerKind"]
                or restored_owner_generation["ownerKey"]
                != restored_owner_claim["ownerKey"]
                or restored_owner_generation["priority"]
                != restored_owner_claim["derivedPriority"]
                or restored_owner_claim["outcome"] != "accepted"
                or restored_owner_generation["createdAt"]
                < restored_owner_claim["createdAt"]
                or len(restored_decisions) != 1
                or restored_decisions[0]["decision"] != "accepted"
                or restored_decisions[0]["plannedGeneration"]
                != restored_owner_generation["generation"]
                or restored_owner_settlement["outcome"]
                != _expected_owner_settlement_outcome(
                    restored_owner_generation["ownerKind"]
                )
                or restored_owner_settlement["settlementHash"]
                != restored_settlement_hash
                or restored_owner_settlement["settledAt"]
                > released_owner_generation["createdAt"]
                or restored_owner_generation["generation"]
                >= released_settlement["generation"]
            ):
                raise RowAuthorityAmbiguous(
                    "release restored owner authority does not correlate"
                )
            restored_authority = {
                "generation": restored_owner_generation,
                "claimSet": restored_owner_claim,
                "settlement": restored_owner_settlement,
            }
        if settlement is not None:
            restored_owner_matches = (
                restored_generation == current_number
                and restored_settlement_hash == settlement["settlementHash"]
            )
        elif generation is None:
            restored_owner_matches = (
                restored_generation is None
                and restored_settlement_hash is None
            )
        else:
            restored_owner_matches = (
                generation["generation"] > latest[0]["generation"]
                and generation["predecessorSettlementHash"]
                == restored_settlement_hash
                and (
                    restored_authority is None
                    or generation["priority"]
                    > restored_authority["generation"]["priority"]
                )
                and (
                    restored_generation is None
                    or restored_generation < generation["generation"]
                )
            )
        if not restored_owner_matches:
            raise RowAuthorityAmbiguous(
                "release-restored result bridge does not match the effective owner"
            )
    elif historical_divergence:
        if (
            generation is None
            or checked_head["state"] not in {"claimed", "review_pending"}
            or not latest
            or not active_bridge
            or generation["predecessorSettlementHash"]
            != checked_head["effectiveSettlementHash"]
        ):
            raise RowAuthorityAmbiguous(
                "row settlement divergence lacks a bounded bridge"
            )

    next_generation = max(
        [
            current_number or 0,
            *(settlement["generation"] for settlement in latest),
        ]
    ) + 1
    next_fence = max(
        [
            checked_head["fencingToken"] or 0,
            *(settlement["fencingToken"] for settlement in latest),
        ]
    ) + 1
    return {
        "currentGeneration": generation,
        "currentClaimSet": claim,
        "currentSettlement": settlement,
        "latestSettlements": tuple(latest),
        "latestSettlementAuthorities": tuple(latest_authorities),
        "latestPredecessorReleaseMatches": (
            validated_predecessor_release_matches
        ),
        "latestPredecessorRestoredAuthority": (
            validated_predecessor_restored_authority
        ),
        "releaseResult": release_result,
        "releasedAuthority": released_authority,
        "restoredAuthority": (
            restored_authority
            if release_result is not None
            else None
        ),
        "nextGeneration": next_generation,
        "nextFirstFencingToken": next_fence,
    }


def _derive_claim_request_context(
    *,
    user_scope_hash,
    authority_origin,
    authority_link,
    operator_action_document,
    fanout_id,
    thread_binding_document,
    canonical_mailbox_identity_hash=None,
    contact_settlement_hash=None,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    binding = validate_thread_row_binding_document(
        document=thread_binding_document
    )
    if binding["userScopeHash"] != scope:
        raise RowAuthorityConfigError("claim thread binding scope conflicts")
    origin = _claim_origin_from_inputs(
        user_scope_hash=scope,
        authority_origin=authority_origin,
        authority_link=authority_link,
        operator_action_document=operator_action_document,
        fanout_id=fanout_id,
        row_bindings=binding["rowBindings"],
        canonical_mailbox_identity_hash=canonical_mailbox_identity_hash,
        contact_settlement_hash=contact_settlement_hash,
    )
    material = {
        "authorityOrigin": authority_origin,
        "authorityLinkHash": origin["authorityLinkHash"],
        "operatorActionHash": origin["operatorActionHash"],
        "fanoutId": origin["fanoutId"],
        "rowBindingsHash": binding["rowBindingsHash"],
        "ownerKind": origin["ownerKind"],
        "ownerKey": origin["ownerKey"],
        "workKey": origin["workKey"],
        "payloadHash": origin["payloadHash"],
    }
    return {
        "requestId": domain_hash(
            CLAIM_REQUEST_ID_DOMAIN,
            material,
            user_scope_hash=scope,
        ),
        "binding": binding,
        "origin": origin,
    }


def _expected_owner_settlement_outcome(owner_kind):
    return {
        "contact_optout": "contact_optout",
        "terminal": "terminal",
        "human_decision": "human_declined",
    }[owner_kind]


def _validate_correlated_owner_settlement(
    *, scope, row_id, generation, claim, settlement_document
):
    try:
        settlement = validate_owner_settlement_document(
            document=settlement_document
        )
    except Exception as exc:
        raise RowAuthorityConflict(
            "owner settlement contains immutable drift"
        ) from exc
    if (
        settlement["userScopeHash"] != scope
        or settlement["rowId"] != row_id
        or settlement["generation"] != generation["generation"]
        or settlement["generationHash"] != generation["generationHash"]
        or settlement["fencingToken"] < generation["firstFencingToken"]
        or settlement["settledAt"] < generation["createdAt"]
    ):
        raise RowAuthorityConflict(
            "owner settlement does not correlate to its generation"
        )
    expected_evidence_hash = _outcome_evidence_hash(
        user_scope_hash=scope,
        authority_link_hash=claim["authorityLinkHash"],
        operator_action_hash=(
            settlement["operatorActionHash"] or claim["operatorActionHash"]
        ),
        fanout_id=claim["fanoutId"],
        payload_hash=claim["payloadHash"],
        outcome_reason_code=settlement["outcomeReasonCode"],
    )
    expected_logical_hash = _logical_outcome_hash(
        user_scope_hash=scope,
        row_id=row_id,
        generation=generation["generation"],
        owner_kind=generation["ownerKind"],
        owner_key=generation["ownerKey"],
        outcome=settlement["outcome"],
        outcome_reason_code=settlement["outcomeReasonCode"],
        outcome_evidence_hash=expected_evidence_hash,
    )
    if (
        settlement["outcomeEvidenceHash"] != expected_evidence_hash
        or settlement["logicalOutcomeHash"] != expected_logical_hash
        or (
            claim["authorityOrigin"] == "authenticated_operator"
            and settlement["outcome"] == "human_declined"
            and settlement["operatorActionHash"]
            != claim["operatorActionHash"]
        )
        or (
            settlement["outcome"] == "contact_optout"
            and settlement["supersededEffectiveSettlementHash"]
            != generation["predecessorSettlementHash"]
        )
    ):
        raise RowAuthorityConflict(
            "owner settlement derived provenance does not correlate"
        )
    return settlement


def _validate_current_owner_state(*, scope, row_id, head, row_state):
    lineage_documents = row_state.get("ownerLineage")
    current_number = head["effectiveOwnerGeneration"]
    if current_number is None:
        if lineage_documents not in (None, [], ()) or any(
            row_state.get(field) is not None
            for field in (
                "currentGeneration",
                "currentClaimSet",
                "currentSettlement",
                "currentPredecessorGeneration",
                "currentPredecessorClaimSet",
                "currentPredecessorSettlement",
            )
        ):
            raise RowAuthorityAmbiguous(
                "ownerless row has unexpected immutable ownership records"
            )
        return None, None, None, ()
    if (
        type(lineage_documents) not in {list, tuple}
        or current_number > 3
        or len(lineage_documents) != current_number
    ):
        raise RowAuthorityConflict(
            "owned row lacks its complete bounded ownership lineage"
        )

    lineage = []
    for expected_number, raw_entry in enumerate(lineage_documents, start=1):
        if type(raw_entry) is not dict or set(raw_entry) != {
            "generation",
            "claimSet",
            "settlement",
        }:
            raise RowAuthorityConflict("owner lineage entry is malformed")
        generation_document = raw_entry["generation"]
        claim_document = raw_entry["claimSet"]
        if generation_document is None or claim_document is None:
            raise RowAuthorityAmbiguous(
                "owner lineage is missing a generation or claim set"
            )
        try:
            generation = validate_owner_generation_document(
                document=generation_document
            )
            claim = validate_claim_set_document(document=claim_document)
        except Exception as exc:
            raise RowAuthorityConflict(
                "owner lineage generation or claim set contains immutable drift"
            ) from exc
        if claim["authorityOrigin"] == "authenticated_operator":
            minimum_planned_writes = 2 + (3 * claim["bindingCount"])
            maximum_planned_writes = minimum_planned_writes
        else:
            minimum_planned_writes = 1 + (2 * claim["bindingCount"])
            maximum_planned_writes = minimum_planned_writes + sum(
                decision["plannedGeneration"] > 1
                for decision in claim["rowDecisions"]
            )
        if (
            generation["userScopeHash"] != scope
            or claim["userScopeHash"] != scope
            or generation["rowId"] != row_id
            or generation["generation"] != expected_number
            or generation["generation"] > generation["priority"]
            or generation["requestId"] != claim["requestId"]
            or generation["claimSetHash"] != claim["claimSetHash"]
            or generation["ownerKind"] != claim["ownerKind"]
            or generation["ownerKey"] != claim["ownerKey"]
            or generation["priority"] != claim["derivedPriority"]
            or generation["createdAt"] < claim["createdAt"]
            or claim["createdAt"] < head["createdAt"]
            or generation["createdAt"] < head["createdAt"]
            or not (
                minimum_planned_writes
                <= claim["plannedWrites"]
                <= maximum_planned_writes
            )
            or claim["outcome"] != "accepted"
            or not any(
                decision["rowId"] == row_id
                and decision["decision"] == "accepted"
                and decision["plannedGeneration"] == expected_number
                for decision in claim["rowDecisions"]
            )
        ):
            raise RowAuthorityConflict(
                "owner lineage generation and claim do not correlate"
            )
        settlement = None
        if raw_entry["settlement"] is not None:
            settlement = _validate_correlated_owner_settlement(
                scope=scope,
                row_id=row_id,
                generation=generation,
                claim=claim,
                settlement_document=raw_entry["settlement"],
            )
        if expected_number == 1:
            if (
                generation["predecessorSettlementHash"] is not None
                or generation["firstFencingToken"] != 1
            ):
                raise RowAuthorityConflict(
                    "first owner generation has predecessor ownership state"
                )
        else:
            predecessor = lineage[-1]
            predecessor_generation = predecessor["generation"]
            predecessor_settlement = predecessor["settlement"]
            if predecessor_settlement is None:
                raise RowAuthorityConflict(
                    "owner lineage is missing a predecessor settlement"
                )
            if (
                generation["priority"] <= predecessor_generation["priority"]
                or generation["firstFencingToken"]
                != predecessor_settlement["fencingToken"] + 1
                or predecessor_settlement["settledAt"]
                > generation["createdAt"]
            ):
                raise RowAuthorityConflict(
                    "owner lineage priority, fence, or time regressed"
                )
            if predecessor_settlement["outcome"] == "dominated":
                valid_link = (
                    predecessor_generation["ownerKind"]
                    in {"human_decision", "terminal"}
                    and predecessor_settlement["dominantGenerationHash"]
                    == generation["generationHash"]
                    and predecessor_settlement["settledAt"]
                    == generation["createdAt"]
                    and generation["predecessorSettlementHash"]
                    == predecessor_generation["predecessorSettlementHash"]
                )
            else:
                valid_link = (
                    predecessor_settlement["outcome"]
                    == _expected_owner_settlement_outcome(
                        predecessor_generation["ownerKind"]
                    )
                    and predecessor_settlement["settlementHash"]
                    == generation["predecessorSettlementHash"]
                )
            if not valid_link:
                raise RowAuthorityConflict(
                    "owner lineage predecessor does not link forward"
                )
        lineage.append(
            {
                "generation": generation,
                "claimSet": claim,
                "settlement": settlement,
            }
        )

    current = lineage[-1]
    generation = current["generation"]
    claim = current["claimSet"]
    settlement = current["settlement"]
    predecessor = lineage[-2] if len(lineage) > 1 else None
    if (
        row_state.get("currentGeneration") != generation
        or row_state.get("currentClaimSet") != claim
        or row_state.get("currentSettlement") != settlement
        or row_state.get("currentPredecessorGeneration")
        != (predecessor["generation"] if predecessor else None)
        or row_state.get("currentPredecessorClaimSet")
        != (predecessor["claimSet"] if predecessor else None)
        or row_state.get("currentPredecessorSettlement")
        != (predecessor["settlement"] if predecessor else None)
    ):
        raise RowAuthorityConflict(
            "current owner summary differs from its complete lineage"
        )
    if (
        generation["generation"] != current_number
        or generation["generationHash"]
        != head["effectiveOwnerGenerationHash"]
        or generation["ownerKind"] != head["effectiveOwnerKind"]
        or generation["priority"] != head["effectivePriority"]
        or head["updatedAt"] < generation["createdAt"]
        or head["stateRevision"] < generation["generation"] + 1
        or head["fencingToken"] < generation["firstFencingToken"]
        or head["stateRevision"] <= head["fencingToken"]
    ):
        raise RowAuthorityConflict(
            "current owner generation does not correlate to the row head"
        )
    if head["state"] in {"claimed", "review_pending"}:
        expected_state = (
            "review_pending"
            if generation["ownerKind"] == "human_decision"
            else "claimed"
        )
        if settlement is not None:
            raise RowAuthorityAmbiguous(
                "unsettled row already contains a generation settlement"
            )
        if (
            head["state"] != expected_state
            or head["leaseUntil"] <= generation["createdAt"]
            or generation["predecessorSettlementHash"]
            != head["effectiveSettlementHash"]
            or head["latestSettlementHash"]
            != (predecessor["settlement"]["settlementHash"] if predecessor else None)
        ):
            raise RowAuthorityConflict(
                "active owner head does not match its validated lineage"
            )
    elif head["state"] == "settled":
        if (
            settlement is None
            or settlement["outcome"]
            != _expected_owner_settlement_outcome(generation["ownerKind"])
            or settlement["settlementHash"] != head["latestSettlementHash"]
            or settlement["settlementHash"] != head["effectiveSettlementHash"]
            or settlement["fencingToken"] != head["fencingToken"]
            or settlement["settledAt"] > head["updatedAt"]
        ):
            raise RowAuthorityConflict(
                "settled row does not match its exact effective settlement"
            )
    else:
        raise RowAuthorityConflict("owned row has an unsupported head state")
    return generation, claim, settlement, tuple(lineage)


def _validate_complete_accepted_claim_cohorts(*, row_states):
    states_by_row = {state["rowId"]: state for state in row_states}
    claims_by_request = {}
    for state in row_states:
        for entry in state["ownerLineage"]:
            claim = entry["claimSet"]
            existing = claims_by_request.setdefault(claim["requestId"], claim)
            if existing != claim:
                raise RowAuthorityConflict(
                    "accepted claim cohort contains divergent claim copies"
                )
    for claim in claims_by_request.values():
        claim_row_ids = {
            binding["rowId"] for binding in claim["rowBindings"]
        }
        if not claim_row_ids.issubset(states_by_row):
            continue
        dominated_predecessors = 0
        for decision in claim["rowDecisions"]:
            state = states_by_row.get(decision["rowId"])
            generation_number = decision["plannedGeneration"]
            if (
                state is None
                or generation_number is None
                or len(state["ownerLineage"]) < generation_number
            ):
                raise RowAuthorityConflict(
                    "accepted claim cohort is missing a bound row generation"
                )
            entry = state["ownerLineage"][generation_number - 1]
            if entry["claimSet"] != claim:
                raise RowAuthorityConflict(
                    "accepted claim cohort does not own every planned generation"
                )
            if generation_number > 1:
                predecessor_settlement = state["ownerLineage"][
                    generation_number - 2
                ]["settlement"]
                if predecessor_settlement is None:
                    raise RowAuthorityConflict(
                        "accepted claim cohort lacks predecessor settlement proof"
                    )
                dominated_predecessors += (
                    predecessor_settlement["outcome"] == "dominated"
                )
        expected_writes = (
            2 + (3 * claim["bindingCount"])
            if claim["authorityOrigin"] == "authenticated_operator"
            else 1
            + (2 * claim["bindingCount"])
            + dominated_predecessors
        )
        if claim["plannedWrites"] != expected_writes:
            raise RowAuthorityConflict(
                "accepted claim cohort has a nonsemantic write count"
            )


def _validate_bounded_current_claim_cohorts(*, row_states):
    """Fail closed on a partial cohort using bounded direct/inductive proof."""
    states_by_row = {state["rowId"]: state for state in row_states}
    claims_by_request = {}
    for state in row_states:
        claim = state.get("currentClaimSet")
        if claim is None:
            continue
        existing = claims_by_request.setdefault(claim["requestId"], claim)
        if existing != claim:
            raise RowAuthorityConflict(
                "bounded current claim cohort contains divergent copies"
            )
    for claim in claims_by_request.values():
        claim_row_ids = {
            binding["rowId"] for binding in claim["rowBindings"]
        }
        if not claim_row_ids.issubset(states_by_row):
            continue
        for decision in claim["rowDecisions"]:
            state = states_by_row[decision["rowId"]]
            planned = decision["plannedGeneration"]
            current = state.get("currentGeneration")
            if decision["decision"] != "accepted" or planned is None:
                raise RowAuthorityConflict(
                    "accepted current claim cohort has a nonaccepted decision"
                )
            history = state.get("boundedHistory")
            if history is None:
                raise RowAuthorityAmbiguous(
                    "accepted current claim cohort lacks bounded history"
                )
            known_max = history["nextGeneration"] - 1
            if known_max < planned:
                raise RowAuthorityConflict(
                    "accepted current claim cohort is missing a bound generation"
                )
            direct_authorities = []
            if current is not None:
                direct_authorities.append(
                    {
                        "generation": current,
                        "claimSet": state.get("currentClaimSet"),
                    }
                )
            direct_authorities.extend(
                history["latestSettlementAuthorities"]
            )
            for field in (
                "releasedAuthority",
                "restoredAuthority",
                "latestPredecessorRestoredAuthority",
            ):
                authority = history.get(field)
                if authority is not None:
                    direct_authorities.append(authority)
            planned_authorities = [
                authority
                for authority in direct_authorities
                if authority["generation"]["generation"] == planned
            ]
            for authority in planned_authorities:
                generation = authority["generation"]
                if (
                    authority["claimSet"] != claim
                    or generation["requestId"] != claim["requestId"]
                    or generation["claimSetHash"] != claim["claimSetHash"]
                    or generation["ownerKind"] != claim["ownerKind"]
                    or generation["ownerKey"] != claim["ownerKey"]
                    or generation["priority"] != claim["derivedPriority"]
                    or generation["createdAt"] < claim["createdAt"]
                ):
                    raise RowAuthorityConflict(
                        "accepted current claim cohort has foreign direct authority"
                    )
            if known_max == planned and not planned_authorities:
                raise RowAuthorityAmbiguous(
                    "accepted current claim cohort lacks its direct boundary authority"
                )


def _claim_head_is_forward(
    *,
    head,
    generation,
    requested_lease_owner_hash,
    requested_lease_until,
    higher_generation_proven=False,
    release_restoration_proven=False,
):
    if head["updatedAt"] < generation["createdAt"]:
        return False
    current_generation = head["effectiveOwnerGeneration"]
    if current_generation is None or current_generation < generation["generation"]:
        return bool(release_restoration_proven)
    if current_generation > generation["generation"]:
        return bool(higher_generation_proven)
    if (
        head["effectiveOwnerGenerationHash"] != generation["generationHash"]
        or head["effectiveOwnerKind"] != generation["ownerKind"]
        or head["effectivePriority"] != generation["priority"]
        or head["fencingToken"] < generation["firstFencingToken"]
    ):
        return False
    if (
        head["state"] in {"claimed", "review_pending"}
        and head["fencingToken"] == generation["firstFencingToken"]
        and (
            head["leaseOwnerHash"] != requested_lease_owner_hash
            or head["leaseUntil"] != requested_lease_until
        )
    ):
        return False
    return True


def _validate_bounded_replay_release(
    *,
    scope,
    row_id,
    matches,
    released_generation,
    released_settlement,
    restored_generation,
    restored_settlement_hash,
    not_after,
):
    """Validate one exact release result used by a replay-time bracket."""
    if type(matches) not in {list, tuple}:
        raise RowAuthorityAmbiguous(
            "dominated replay release proof is not bounded"
        )
    if len(matches) != 1:
        raise RowAuthorityAmbiguous(
            "dominated replay release proof is missing or duplicated"
        )
    entry = matches[0]
    if type(entry) is not dict or set(entry) != {"path", "document"}:
        raise RowAuthorityAmbiguous(
            "dominated replay release proof is malformed"
        )
    try:
        release = validate_contact_fanout_result_document(
            document=entry["document"]
        )
    except Exception as exc:
        raise RowAuthorityAmbiguous(
            "dominated replay release proof is malformed"
        ) from exc
    if (
        type(entry["path"]) is not str
        or entry["path"].split("/")[-2:]
        != [
            "contactOptOutFanoutResults",
            f"{release['fanoutId']}--{row_id}",
        ]
        or release["userScopeHash"] != scope
        or release["rowId"] != row_id
        or release["outcome"] != "release"
        or release["disposition"] != "restore"
        or release["reasonCode"] != "exact_predecessor"
        or release["releasedRowGeneration"]
        != released_generation["generation"]
        or release["releasedRowSettlementHash"]
        != released_settlement["settlementHash"]
        or release["restoredEffectiveGeneration"] != restored_generation
        or release["restoredEffectiveSettlementHash"]
        != restored_settlement_hash
        or release["createdAt"] < released_settlement["settledAt"]
        or release["createdAt"] > not_after
    ):
        raise RowAuthorityAmbiguous(
            "dominated replay release proof does not correlate"
        )
    return release


def _bounded_blocked_owner_at_time(*, state, stored_claim):
    """Return the owner at a blocked replay's claim time from a causal suffix."""
    history = state["boundedHistory"]
    claim_at = stored_claim["createdAt"]
    current = state["currentGeneration"]
    known_max = history["nextGeneration"] - 1
    authorities = {}

    def add_authority(authority):
        if authority is None:
            return
        generation = authority["generation"]
        generation_hash = generation["generationHash"]
        retained = authorities.get(generation_hash)
        if retained is None or (
            retained.get("settlement") is None
            and authority.get("settlement") is not None
        ):
            authorities[generation_hash] = authority

    for authority in history["latestSettlementAuthorities"]:
        add_authority(authority)
    add_authority(history.get("releasedAuthority"))
    if current is not None and current["generation"] == known_max:
        add_authority(
            {
                "generation": current,
                "claimSet": state.get("currentClaimSet"),
                "settlement": state.get("currentSettlement"),
            }
        )

    restored_authorities = tuple(
        authority
        for authority in (
            history.get("restoredAuthority"),
            history.get("latestPredecessorRestoredAuthority"),
        )
        if authority is not None
    )
    for restored in restored_authorities:
        restored_generation = restored["generation"]
        restored_settlement = restored["settlement"]
        if any(
            authority["generation"]["generation"]
            == restored_generation["generation"] + 1
            and authority["generation"]["predecessorSettlementHash"]
            == restored_settlement["settlementHash"]
            for authority in authorities.values()
        ):
            add_authority(restored)

    events = []
    for authority in authorities.values():
        generation = authority["generation"]
        events.append(
            {
                "kind": "generation",
                "time": generation["createdAt"],
                "position": 2 * generation["generation"],
                "owner": generation,
                "authority": authority,
            }
        )

    release_events = {}

    def add_release(release, released, restored):
        if release is None or released is None:
            return
        release_hash = release["contactFanoutResultHash"]
        event = {
            "kind": "release",
            "time": release["createdAt"],
            "position": 2 * released["generation"]["generation"] + 1,
            "owner": (
                None if restored is None else restored["generation"]
            ),
            "authority": restored,
            "release": release,
            "released": released,
        }
        retained = release_events.get(release_hash)
        if retained is not None and retained != event:
            raise RowAuthorityAmbiguous(
                "blocked claim replay release proof is inconsistent"
            )
        release_events[release_hash] = event

    add_release(
        history.get("releaseResult"),
        history.get("releasedAuthority"),
        history.get("restoredAuthority"),
    )
    predecessor_matches = history["latestPredecessorReleaseMatches"]
    if predecessor_matches:
        predecessor_release = predecessor_matches[0]["document"]
        released_generation_number = predecessor_release[
            "releasedRowGeneration"
        ]
        predecessor_released = next(
            (
                authority
                for authority in history["latestSettlementAuthorities"]
                if authority["generation"]["generation"]
                == released_generation_number
            ),
            None,
        )
        if predecessor_released is None:
            raise RowAuthorityAmbiguous(
                "blocked claim replay predecessor release lacks its authority"
            )
        add_release(
            predecessor_release,
            predecessor_released,
            history.get("latestPredecessorRestoredAuthority"),
        )
    events.extend(release_events.values())

    if any(event["time"] == claim_at for event in events):
        raise RowAuthorityAmbiguous(
            "blocked claim replay transition order is ambiguous"
        )
    if not events:
        if current is None and not history["latestSettlements"]:
            return None
        raise RowAuthorityAmbiguous(
            "blocked claim replay lacks a bounded owner-at-time proof"
        )
    events.sort(
        key=lambda event: (
            event["time"],
            event["position"],
            event["kind"],
        )
    )
    before = [event for event in events if event["time"] < claim_at]
    after = [event for event in events if event["time"] > claim_at]
    if not before:
        first = after[0]
        if (
            first["kind"] == "generation"
            and first["owner"]["generation"] == 1
            and first["owner"]["firstFencingToken"] == 1
            and first["owner"]["predecessorSettlementHash"] is None
        ):
            return None
        raise RowAuthorityAmbiguous(
            "blocked claim replay predates its bounded transition window"
        )

    selected = before[-1]
    if after:
        following = after[0]
        if not _blocked_replay_events_are_adjacent(
            earlier=selected,
            later=following,
        ):
            raise RowAuthorityAmbiguous(
                "blocked claim replay transition window has a gap"
            )
    else:
        selected_owner = selected["owner"]
        if (
            (selected_owner is None and current is not None)
            or (
                selected_owner is not None
                and (
                    current is None
                    or current["generationHash"]
                    != selected_owner["generationHash"]
                )
            )
        ):
            raise RowAuthorityAmbiguous(
                "blocked claim replay transition window does not reach current state"
            )
    return selected["owner"]


def _blocked_replay_events_are_adjacent(*, earlier, later):
    """Return whether two validated events directly bound one owner interval."""
    if earlier["kind"] == "generation":
        authority = earlier["authority"]
        generation = authority["generation"]
        settlement = authority.get("settlement")
        if later["kind"] == "release":
            return (
                later["released"]["generation"]["generationHash"]
                == generation["generationHash"]
            )
        successor = later["authority"]["generation"]
        if (
            settlement is None
            or successor["generation"] != generation["generation"] + 1
            or successor["priority"] <= generation["priority"]
        ):
            return False
        if settlement["outcome"] == "dominated":
            return (
                settlement["dominantGenerationHash"]
                == successor["generationHash"]
                and settlement["settledAt"] == successor["createdAt"]
                and successor["predecessorSettlementHash"]
                == generation["predecessorSettlementHash"]
            )
        return (
            successor["predecessorSettlementHash"]
            == settlement["settlementHash"]
        )

    if later["kind"] != "generation":
        return False
    released_generation = earlier["released"]["generation"]
    successor = later["authority"]["generation"]
    restored = earlier["authority"]
    restored_hash = (
        None
        if restored is None
        else restored["settlement"]["settlementHash"]
    )
    restored_priority = (
        None if restored is None else restored["generation"]["priority"]
    )
    return (
        successor["generation"] == released_generation["generation"] + 1
        and successor["predecessorSettlementHash"] == restored_hash
        and (
            restored_priority is None
            or successor["priority"] > restored_priority
        )
    )


def _validate_bounded_dominated_replay(
    *, scope, state, decision, stored_claim
):
    """Validate one immutable dominated decision without replaying 1..N."""
    if decision["decision"] == "blocked_by_claim_set":
        owner_at_time = _bounded_blocked_owner_at_time(
            state=state,
            stored_claim=stored_claim,
        )
        if (
            owner_at_time is not None
            and stored_claim["derivedPriority"] <= owner_at_time["priority"]
        ):
            raise RowAuthorityConflict(
                "blocked claim replay row should have been dominated"
            )
        return
    if decision["decision"] != "dominated":
        raise RowAuthorityConflict(
            "dominated claim replay contains an unsupported decision"
        )
    matches = state.get("replayWinnerMatches")
    if type(matches) not in {list, tuple}:
        raise RowAuthorityConflict(
            "dominated claim replay lacks a bounded winner proof"
        )
    if len(matches) > 1:
        raise RowAuthorityAmbiguous(
            "dominated claim replay winner is duplicated"
        )
    if len(matches) != 1:
        raise RowAuthorityConflict(
            "dominated claim replay lacks its immutable winner"
        )
    entry = matches[0]
    if type(entry) is not dict or set(entry) != {"path", "document"}:
        raise RowAuthorityConflict(
            "dominated claim replay winner proof is malformed"
        )
    try:
        winner = validate_owner_generation_document(document=entry["document"])
        winner_claim = validate_claim_set_document(
            document=state.get("replayWinnerClaimSet")
        )
        expected_id = _generation_document_id(
            row_id=state["rowId"],
            generation=winner["generation"],
        )
    except Exception as exc:
        raise RowAuthorityConflict(
            "dominated claim replay winner authority is malformed"
        ) from exc
    matching_decisions = [
        item
        for item in winner_claim["rowDecisions"]
        if item["rowId"] == state["rowId"]
    ]
    if winner_claim["authorityOrigin"] == "authenticated_operator":
        minimum_writes = 2 + (3 * winner_claim["bindingCount"])
        maximum_writes = minimum_writes
    else:
        minimum_writes = 1 + (2 * winner_claim["bindingCount"])
        maximum_writes = minimum_writes + sum(
            item["plannedGeneration"] is not None
            and item["plannedGeneration"] > 1
            for item in winner_claim["rowDecisions"]
        )
    if (
        type(entry["path"]) is not str
        or entry["path"].split("/")[-2:]
        != ["rowOwnerGenerations", expected_id]
        or winner["userScopeHash"] != scope
        or winner_claim["userScopeHash"] != scope
        or winner["rowId"] != state["rowId"]
        or winner["generationHash"] != decision["winnerGenerationHash"]
        or winner["requestId"] != winner_claim["requestId"]
        or winner["claimSetHash"] != winner_claim["claimSetHash"]
        or winner["ownerKind"] != winner_claim["ownerKind"]
        or winner["ownerKey"] != winner_claim["ownerKey"]
        or winner["priority"] != winner_claim["derivedPriority"]
        or winner_claim["outcome"] != "accepted"
        or len(matching_decisions) != 1
        or matching_decisions[0]["decision"] != "accepted"
        or matching_decisions[0]["plannedGeneration"]
        != winner["generation"]
        or winner["createdAt"] < winner_claim["createdAt"]
        or not (
            minimum_writes
            <= winner_claim["plannedWrites"]
            <= maximum_writes
        )
        or stored_claim["derivedPriority"] > winner["priority"]
        or winner["createdAt"] > stored_claim["createdAt"]
    ):
        raise RowAuthorityConflict(
            "dominated claim replay winner does not correlate"
        )
    winner_settlement = None
    if state.get("replayWinnerSettlement") is not None:
        winner_settlement = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=state["rowId"],
            generation=winner,
            claim=winner_claim,
            settlement_document=state["replayWinnerSettlement"],
        )
        if winner_settlement["outcome"] not in {
            "dominated",
            _expected_owner_settlement_outcome(winner["ownerKind"]),
        }:
            raise RowAuthorityConflict(
                "dominated replay winner settlement has an invalid outcome"
            )
        if (
            winner_settlement["outcome"] == "dominated"
            and winner_settlement["settledAt"]
            <= stored_claim["createdAt"]
        ):
            if (
                winner_settlement["settledAt"]
                == stored_claim["createdAt"]
            ):
                raise RowAuthorityAmbiguous(
                    "dominated replay winner transition order is ambiguous"
                )
            raise RowAuthorityConflict(
                "dominated replay winner was superseded before the claim"
            )
    possible_settlement_hashes = {winner["predecessorSettlementHash"]}
    if winner_settlement is not None and winner_settlement[
        "outcome"
    ] != "dominated":
        if winner_settlement["settledAt"] < stored_claim["createdAt"]:
            possible_settlement_hashes = {
                winner_settlement["settlementHash"]
            }
        elif winner_settlement["settledAt"] == stored_claim["createdAt"]:
            possible_settlement_hashes.add(
                winner_settlement["settlementHash"]
            )
    if decision["winnerSettlementHash"] not in possible_settlement_hashes:
        raise RowAuthorityConflict(
            "dominated claim replay winner settlement drifted"
        )

    history = state["boundedHistory"]
    latest = history["latestSettlements"]
    latest_authorities = history["latestSettlementAuthorities"]
    current = state.get("currentGeneration")
    known_max = max(
        current["generation"] if current is not None else 0,
        latest[0]["generation"] if latest else 0,
    )
    if known_max < winner["generation"]:
        raise RowAuthorityConflict(
            "dominated replay winner is ahead of bounded row history"
        )
    winner_boundary_matches = 0
    if current is not None and current["generation"] == winner["generation"]:
        winner_boundary_matches += 1
        if (
            current != winner
            or state.get("currentClaimSet") != winner_claim
            or state.get("currentSettlement") != winner_settlement
        ):
            raise RowAuthorityConflict(
                "dominated replay winner differs from current authority"
            )
    for authority in latest_authorities:
        if authority["generation"]["generation"] != winner["generation"]:
            continue
        winner_boundary_matches += 1
        if (
            authority["generation"] != winner
            or authority["claimSet"] != winner_claim
            or authority["settlement"] != winner_settlement
        ):
            raise RowAuthorityConflict(
                "dominated replay winner differs from bounded history"
            )
    if known_max == winner["generation"] and winner_boundary_matches == 0:
        raise RowAuthorityAmbiguous(
            "dominated replay winner lacks a bounded row boundary"
        )

    successor_document = state.get("replayWinnerSuccessorGeneration")
    successor_required = known_max > winner["generation"]
    if successor_required and successor_document is None:
        raise RowAuthorityAmbiguous(
            "dominated replay winner lacks its exact successor"
        )
    if not successor_required and successor_document is not None:
        raise RowAuthorityAmbiguous(
            "dominated replay winner has orphan successor state"
        )
    if successor_document is None:
        current_is_winner = (
            current is not None
            and current["generation"] == winner["generation"]
        )
        if not current_is_winner:
            release = history.get("releaseResult")
            released = history.get("releasedAuthority")
            if (
                release is None
                or released is None
                or released.get("generation") != winner
                or released.get("claimSet") != winner_claim
                or released.get("settlement") != winner_settlement
            ):
                raise RowAuthorityAmbiguous(
                    "dominated replay winner exit is not bounded"
                )
            if release["createdAt"] <= stored_claim["createdAt"]:
                if release["createdAt"] == stored_claim["createdAt"]:
                    raise RowAuthorityAmbiguous(
                        "dominated replay winner release order is ambiguous"
                    )
                raise RowAuthorityConflict(
                    "dominated replay winner was released before the claim"
                )
        return
    try:
        successor = validate_owner_generation_document(
            document=successor_document
        )
        successor_claim = validate_claim_set_document(
            document=state.get("replayWinnerSuccessorClaimSet")
        )
    except Exception as exc:
        raise RowAuthorityConflict(
            "dominated claim replay winner successor is malformed"
        ) from exc
    successor_decisions = [
        item
        for item in successor_claim["rowDecisions"]
        if item["rowId"] == state["rowId"]
    ]
    if successor_claim["authorityOrigin"] == "authenticated_operator":
        successor_minimum_writes = 2 + (
            3 * successor_claim["bindingCount"]
        )
        successor_maximum_writes = successor_minimum_writes
    else:
        successor_minimum_writes = 1 + (
            2 * successor_claim["bindingCount"]
        )
        successor_maximum_writes = successor_minimum_writes + sum(
            item["plannedGeneration"] is not None
            and item["plannedGeneration"] > 1
            for item in successor_claim["rowDecisions"]
        )
    if (
        successor["userScopeHash"] != scope
        or successor_claim["userScopeHash"] != scope
        or successor["rowId"] != state["rowId"]
        or successor["generation"] != winner["generation"] + 1
        or successor["requestId"] != successor_claim["requestId"]
        or successor["claimSetHash"] != successor_claim["claimSetHash"]
        or successor["ownerKind"] != successor_claim["ownerKind"]
        or successor["ownerKey"] != successor_claim["ownerKey"]
        or successor["priority"] != successor_claim["derivedPriority"]
        or successor_claim["outcome"] != "accepted"
        or successor["createdAt"] < successor_claim["createdAt"]
        or len(successor_decisions) != 1
        or successor_decisions[0]["decision"] != "accepted"
        or successor_decisions[0]["plannedGeneration"]
        != successor["generation"]
        or not (
            successor_minimum_writes
            <= successor_claim["plannedWrites"]
            <= successor_maximum_writes
        )
    ):
        raise RowAuthorityConflict(
            "dominated replay winner successor does not correlate"
        )
    successor_settlement = None
    if state.get("replayWinnerSuccessorSettlement") is not None:
        successor_settlement = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=state["rowId"],
            generation=successor,
            claim=successor_claim,
            settlement_document=state[
                "replayWinnerSuccessorSettlement"
            ],
        )
        if successor_settlement["outcome"] not in {
            "dominated",
            _expected_owner_settlement_outcome(successor["ownerKind"]),
        }:
            raise RowAuthorityConflict(
                "dominated replay winner successor settlement is invalid"
            )
    successor_boundary_matches = 0
    if current is not None and current["generation"] == successor["generation"]:
        successor_boundary_matches += 1
        if (
            current != successor
            or state.get("currentClaimSet") != successor_claim
            or state.get("currentSettlement") != successor_settlement
        ):
            raise RowAuthorityConflict(
                "dominated replay successor differs from current authority"
            )
    for authority in latest_authorities:
        if authority["generation"]["generation"] != successor["generation"]:
            continue
        successor_boundary_matches += 1
        if (
            authority["generation"] != successor
            or authority["claimSet"] != successor_claim
            or authority["settlement"] != successor_settlement
        ):
            raise RowAuthorityConflict(
                "dominated replay successor differs from bounded history"
            )
    if known_max == successor["generation"] and successor_boundary_matches == 0:
        raise RowAuthorityAmbiguous(
            "dominated replay successor lacks a bounded row boundary"
        )
    if known_max > successor["generation"] and successor_settlement is None:
        raise RowAuthorityAmbiguous(
            "historical dominated replay successor lacks a settlement"
        )
    if winner_settlement is None:
        raise RowAuthorityAmbiguous(
            "dominated replay winner lacks its successor settlement bridge"
        )

    forward_release = None
    if winner_settlement["outcome"] == "dominated":
        if (
            winner_settlement["dominantGenerationHash"]
            != successor["generationHash"]
            or winner_settlement["settledAt"] != successor["createdAt"]
            or successor["predecessorSettlementHash"]
            != winner["predecessorSettlementHash"]
            or successor["priority"] <= winner["priority"]
        ):
            raise RowAuthorityConflict(
                "dominated replay successor does not link its dominated winner"
            )
    elif (
        successor["predecessorSettlementHash"]
        == winner_settlement["settlementHash"]
    ):
        if (
            successor["priority"] <= winner["priority"]
            or winner_settlement["settledAt"] > successor["createdAt"]
        ):
            raise RowAuthorityConflict(
                "dominated replay direct successor regresses authority"
            )
    else:
        if (
            winner["ownerKind"] != "contact_optout"
            or winner_settlement["outcome"] != "contact_optout"
            or successor["predecessorSettlementHash"]
            != winner["predecessorSettlementHash"]
            or winner_settlement["supersededEffectiveSettlementHash"]
            != winner["predecessorSettlementHash"]
        ):
            raise RowAuthorityConflict(
                "dominated replay successor does not link forward"
            )
        raw_link_restored = state.get(
            "replayWinnerSuccessorRestoredAuthority"
        )
        raw_link_generation = (
            raw_link_restored.get("generation")
            if type(raw_link_restored) is dict
            else None
        )
        restored_generation_number = (
            raw_link_generation.get("generation")
            if type(raw_link_generation) is dict
            else None
        )
        forward_release = _validate_bounded_replay_release(
            scope=scope,
            row_id=state["rowId"],
            matches=state.get(
                "replayWinnerSuccessorReleaseMatches"
            ),
            released_generation=winner,
            released_settlement=winner_settlement,
            restored_generation=restored_generation_number,
            restored_settlement_hash=successor[
                "predecessorSettlementHash"
            ],
            not_after=successor["createdAt"],
        )
        _validate_bounded_release_restored_authority(
            scope=scope,
            row_id=state["rowId"],
            release_result=forward_release,
            released_generation=winner,
            raw_restored_authority=raw_link_restored,
            successor_generation=successor,
        )

    if successor["createdAt"] > stored_claim["createdAt"]:
        if (
            forward_release is not None
            and forward_release["createdAt"]
            <= stored_claim["createdAt"]
        ):
            if (
                forward_release["createdAt"]
                == stored_claim["createdAt"]
            ):
                raise RowAuthorityAmbiguous(
                    "dominated replay winner release order is ambiguous"
                )
            raise RowAuthorityConflict(
                "dominated replay winner was released before the claim"
            )
        return
    if successor["createdAt"] == stored_claim["createdAt"]:
        raise RowAuthorityAmbiguous(
            "dominated replay winner successor order is ambiguous"
        )
    if (
        successor["ownerKind"] != "contact_optout"
        or successor_settlement is None
        or successor_settlement["outcome"] != "contact_optout"
        or successor["predecessorSettlementHash"]
        != winner_settlement["settlementHash"]
        or winner["ownerKind"] not in {"terminal", "human_decision"}
        or winner_settlement["outcome"]
        != _expected_owner_settlement_outcome(winner["ownerKind"])
    ):
        raise RowAuthorityConflict(
            "dominated replay winner was not restored by its successor"
        )

    exact_release_not_after = stored_claim["createdAt"]
    raw_exact_release_matches = state.get(
        "replayWinnerRestorationReleaseMatches"
    )
    if (
        type(raw_exact_release_matches) in {list, tuple}
        and len(raw_exact_release_matches) == 1
        and type(raw_exact_release_matches[0]) is dict
    ):
        try:
            raw_exact_release = validate_contact_fanout_result_document(
                document=raw_exact_release_matches[0].get("document")
            )
            if raw_exact_release["createdAt"] > exact_release_not_after:
                exact_release_not_after = raw_exact_release["createdAt"]
        except Exception:
            pass
    exact_restoration_release = _validate_bounded_replay_release(
        scope=scope,
        row_id=state["rowId"],
        matches=raw_exact_release_matches,
        released_generation=successor,
        released_settlement=successor_settlement,
        restored_generation=winner["generation"],
        restored_settlement_hash=winner_settlement["settlementHash"],
        not_after=exact_release_not_after,
    )
    if exact_restoration_release["createdAt"] == stored_claim["createdAt"]:
        raise RowAuthorityAmbiguous(
            "dominated replay winner restoration order is ambiguous"
        )
    exact_restored_winner = _validate_bounded_release_restored_authority(
        scope=scope,
        row_id=state["rowId"],
        release_result=exact_restoration_release,
        released_generation=successor,
        raw_restored_authority=state.get(
            "replayWinnerRestoredAuthority"
        ),
        successor_generation=successor,
    )
    if (
        exact_restored_winner is None
        or exact_restored_winner["generation"] != winner
        or exact_restored_winner["claimSet"] != winner_claim
        or exact_restored_winner["settlement"] != winner_settlement
    ):
        raise RowAuthorityConflict(
            "dominated replay exact successor restored another winner"
        )

    restoration_exit_document = state.get(
        "replayWinnerRestorationExitGeneration"
    )
    restoration_exit_claim_document = state.get(
        "replayWinnerRestorationExitClaimSet"
    )
    restoration_exit_settlement_document = state.get(
        "replayWinnerRestorationExitSettlement"
    )
    restoration_exit_authority = None
    if restoration_exit_document is None:
        if (
            restoration_exit_claim_document is not None
            or restoration_exit_settlement_document is not None
        ):
            raise RowAuthorityAmbiguous(
                "dominated replay restoration exit is orphaned"
            )
        if known_max > successor["generation"]:
            raise RowAuthorityAmbiguous(
                "dominated replay restored winner lacks its exact exit"
            )
    else:
        try:
            restoration_exit = validate_owner_generation_document(
                document=restoration_exit_document
            )
            restoration_exit_claim = validate_claim_set_document(
                document=restoration_exit_claim_document
            )
        except Exception as exc:
            raise RowAuthorityConflict(
                "dominated replay restoration exit is malformed"
            ) from exc
        restoration_exit_decisions = [
            item
            for item in restoration_exit_claim["rowDecisions"]
            if item["rowId"] == state["rowId"]
        ]
        if restoration_exit_claim["authorityOrigin"] == (
            "authenticated_operator"
        ):
            restoration_exit_minimum_writes = 2 + (
                3 * restoration_exit_claim["bindingCount"]
            )
            restoration_exit_maximum_writes = (
                restoration_exit_minimum_writes
            )
        else:
            restoration_exit_minimum_writes = 1 + (
                2 * restoration_exit_claim["bindingCount"]
            )
            restoration_exit_maximum_writes = (
                restoration_exit_minimum_writes
                + sum(
                    item["plannedGeneration"] is not None
                    and item["plannedGeneration"] > 1
                    for item in restoration_exit_claim["rowDecisions"]
                )
            )
        if (
            restoration_exit["userScopeHash"] != scope
            or restoration_exit_claim["userScopeHash"] != scope
            or restoration_exit["rowId"] != state["rowId"]
            or restoration_exit["generation"]
            != successor["generation"] + 1
            or restoration_exit["requestId"]
            != restoration_exit_claim["requestId"]
            or restoration_exit["claimSetHash"]
            != restoration_exit_claim["claimSetHash"]
            or restoration_exit["ownerKind"]
            != restoration_exit_claim["ownerKind"]
            or restoration_exit["ownerKey"]
            != restoration_exit_claim["ownerKey"]
            or restoration_exit["priority"]
            != restoration_exit_claim["derivedPriority"]
            or restoration_exit_claim["outcome"] != "accepted"
            or restoration_exit["createdAt"]
            < restoration_exit_claim["createdAt"]
            or len(restoration_exit_decisions) != 1
            or restoration_exit_decisions[0]["decision"] != "accepted"
            or restoration_exit_decisions[0]["plannedGeneration"]
            != restoration_exit["generation"]
            or not (
                restoration_exit_minimum_writes
                <= restoration_exit_claim["plannedWrites"]
                <= restoration_exit_maximum_writes
            )
            or restoration_exit["predecessorSettlementHash"]
            != winner_settlement["settlementHash"]
            or restoration_exit["priority"] <= winner["priority"]
            or restoration_exit["createdAt"]
            < exact_restoration_release["createdAt"]
        ):
            raise RowAuthorityConflict(
                "dominated replay restoration exit does not correlate"
            )
        restoration_exit_settlement = None
        if restoration_exit_settlement_document is not None:
            restoration_exit_settlement = (
                _validate_correlated_owner_settlement(
                    scope=scope,
                    row_id=state["rowId"],
                    generation=restoration_exit,
                    claim=restoration_exit_claim,
                    settlement_document=(
                        restoration_exit_settlement_document
                    ),
                )
            )
            if restoration_exit_settlement["outcome"] not in {
                "dominated",
                _expected_owner_settlement_outcome(
                    restoration_exit["ownerKind"]
                ),
            }:
                raise RowAuthorityConflict(
                    "dominated replay restoration exit settlement is invalid"
                )
        if known_max < restoration_exit["generation"]:
            raise RowAuthorityAmbiguous(
                "dominated replay restoration exit is ahead of history"
            )
        restoration_exit_boundary_matches = 0
        if (
            current is not None
            and current["generation"] == restoration_exit["generation"]
        ):
            restoration_exit_boundary_matches += 1
            if (
                current != restoration_exit
                or state.get("currentClaimSet")
                != restoration_exit_claim
                or state.get("currentSettlement")
                != restoration_exit_settlement
            ):
                raise RowAuthorityConflict(
                    "dominated replay restoration exit differs from current authority"
                )
        for authority in latest_authorities:
            if authority["generation"]["generation"] != (
                restoration_exit["generation"]
            ):
                continue
            restoration_exit_boundary_matches += 1
            if (
                authority["generation"] != restoration_exit
                or authority["claimSet"] != restoration_exit_claim
                or authority["settlement"]
                != restoration_exit_settlement
            ):
                raise RowAuthorityConflict(
                    "dominated replay restoration exit differs from bounded history"
                )
        if (
            known_max == restoration_exit["generation"]
            and restoration_exit_boundary_matches == 0
        ):
            raise RowAuthorityAmbiguous(
                "dominated replay restoration exit lacks a bounded boundary"
            )
        if (
            known_max > restoration_exit["generation"]
            and restoration_exit_settlement is None
        ):
            raise RowAuthorityAmbiguous(
                "historical dominated replay restoration exit lacks a settlement"
            )
        restoration_exit_authority = {
            "generation": restoration_exit,
            "claimSet": restoration_exit_claim,
            "settlement": restoration_exit_settlement,
        }

    restoration_candidates = []
    restoration_candidates.append(
        {
            "release": exact_restoration_release,
            "released": {
                "generation": successor,
                "claimSet": successor_claim,
                "settlement": successor_settlement,
            },
            "restored": exact_restored_winner,
        }
    )
    if history.get("releaseResult") is not None:
        restoration_candidates.append(
            {
                "release": history["releaseResult"],
                "released": history.get("releasedAuthority"),
                "restored": history.get("restoredAuthority"),
            }
        )
    predecessor_releases = history.get(
        "latestPredecessorReleaseMatches"
    )
    if (
        type(predecessor_releases) in {list, tuple}
        and len(predecessor_releases) == 1
        and len(latest_authorities) == 2
    ):
        restoration_candidates.append(
            {
                "release": predecessor_releases[0]["document"],
                "released": latest_authorities[1],
                "restored": history.get(
                    "latestPredecessorRestoredAuthority"
                ),
            }
        )
    valid_restorations = []
    seen_release_hashes = set()
    for candidate in restoration_candidates:
        release = candidate["release"]
        released = candidate["released"]
        restored = candidate["restored"]
        if (
            type(released) is not dict
            or type(restored) is not dict
            or restored.get("generation") != winner
            or restored.get("claimSet") != winner_claim
            or restored.get("settlement") != winner_settlement
            or release["restoredEffectiveGeneration"]
            != winner["generation"]
            or release["restoredEffectiveSettlementHash"]
            != winner_settlement["settlementHash"]
            or released["generation"]["ownerKind"]
            != "contact_optout"
            or released["generation"]["predecessorSettlementHash"]
            != winner_settlement["settlementHash"]
            or released["settlement"]["outcome"] != "contact_optout"
            or released["settlement"][
                "supersededEffectiveSettlementHash"
            ]
            != winner_settlement["settlementHash"]
            or released["generation"]["priority"] <= winner["priority"]
            or released["generation"]["generation"]
            < successor["generation"]
        ):
            continue
        release_hash = release["contactFanoutResultHash"]
        if release_hash not in seen_release_hashes:
            seen_release_hashes.add(release_hash)
            valid_restorations.append(candidate)
    if any(
        candidate["release"]["createdAt"]
        == stored_claim["createdAt"]
        for candidate in valid_restorations
    ):
        raise RowAuthorityAmbiguous(
            "dominated replay winner restoration order is ambiguous"
        )
    prior_restorations = [
        candidate
        for candidate in valid_restorations
        if candidate["release"]["createdAt"]
        < stored_claim["createdAt"]
    ]
    if not prior_restorations:
        raise RowAuthorityConflict(
            "dominated replay winner lacks a prior restoration"
        )
    prior_restorations.sort(
        key=lambda candidate: (
            candidate["release"]["createdAt"],
            candidate["released"]["generation"]["generation"],
            candidate["release"]["contactFanoutResultHash"],
        ),
        reverse=True,
    )
    restoration = prior_restorations[0]
    restoration_release = restoration["release"]
    released_restoration = restoration["released"]
    restoration_generation = released_restoration["generation"][
        "generation"
    ]
    if known_max < restoration_generation:
        raise RowAuthorityConflict(
            "dominated replay restoration is ahead of bounded history"
        )
    if known_max == restoration_generation:
        if (
            current != winner
            or state.get("currentClaimSet") != winner_claim
            or state.get("currentSettlement") != winner_settlement
        ):
            raise RowAuthorityConflict(
                "dominated replay restored winner is not current"
            )
        return
    exit_generation_number = restoration_generation + 1
    exit_authorities = []
    if (
        restoration_generation == successor["generation"]
        and restoration_exit_authority is not None
    ):
        exit_authorities.append(restoration_exit_authority)
    if (
        current is not None
        and current["generation"] == exit_generation_number
    ):
        exit_authorities.append(
            {
                "generation": current,
                "claimSet": state.get("currentClaimSet"),
                "settlement": state.get("currentSettlement"),
            }
        )
    exit_authorities.extend(
        authority
        for authority in latest_authorities
        if authority["generation"]["generation"]
        == exit_generation_number
    )
    if not exit_authorities:
        raise RowAuthorityAmbiguous(
            "dominated replay restored-winner exit is not bounded"
        )
    for authority in exit_authorities:
        exit_generation = authority["generation"]
        if exit_generation["createdAt"] <= stored_claim["createdAt"]:
            if exit_generation["createdAt"] == stored_claim["createdAt"]:
                raise RowAuthorityAmbiguous(
                    "dominated replay restored-winner exit order is ambiguous"
                )
            raise RowAuthorityConflict(
                "dominated replay restored winner exited before the claim"
            )
        if (
            exit_generation["predecessorSettlementHash"]
            != winner_settlement["settlementHash"]
            or exit_generation["priority"] <= winner["priority"]
            or exit_generation["createdAt"]
            < restoration_release["createdAt"]
        ):
            raise RowAuthorityConflict(
                "dominated replay restored-winner exit does not correlate"
            )


def _validate_bounded_accepted_replay(
    *, scope, state, decision, stored_claim, lease_owner, deadline
):
    """Validate one accepted generation and its direct historical bracket."""
    candidate = state.get("candidateGeneration")
    if candidate is None:
        raise RowAuthorityAmbiguous(
            "accepted claim replay is missing its generation"
        )
    try:
        generation = validate_owner_generation_document(document=candidate)
    except Exception as exc:
        raise RowAuthorityConflict(
            "accepted claim generation contains immutable drift"
        ) from exc
    if (
        generation["userScopeHash"] != scope
        or generation["rowId"] != decision["rowId"]
        or generation["generation"] != decision["plannedGeneration"]
        or generation["requestId"] != stored_claim["requestId"]
        or generation["claimSetHash"] != stored_claim["claimSetHash"]
        or generation["ownerKind"] != stored_claim["ownerKind"]
        or generation["ownerKey"] != stored_claim["ownerKey"]
        or generation["priority"] != stored_claim["derivedPriority"]
        or generation["createdAt"] < stored_claim["createdAt"]
    ):
        raise RowAuthorityConflict(
            "accepted claim generation does not correlate"
        )
    candidate_settlement = state.get("candidateSettlement")
    checked_candidate_settlement = None
    if candidate_settlement is not None:
        checked_candidate_settlement = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=state["rowId"],
            generation=generation,
            claim=stored_claim,
            settlement_document=candidate_settlement,
        )
        if checked_candidate_settlement["settledAt"] > state["head"][
            "updatedAt"
        ]:
            raise RowAuthorityConflict(
                "accepted claim settlement postdates the current row head"
            )
    current_number = state["head"]["effectiveOwnerGeneration"]
    bounded_history = state["boundedHistory"]
    latest_settlements = bounded_history["latestSettlements"]
    historical_settlement_proven = (
        checked_candidate_settlement is not None
        and bool(latest_settlements)
        and generation["generation"]
        <= latest_settlements[0]["generation"]
    )
    if not _claim_head_is_forward(
        head=state["head"],
        generation=generation,
        requested_lease_owner_hash=lease_owner,
        requested_lease_until=deadline,
        higher_generation_proven=(
            current_number is not None
            and current_number > generation["generation"]
        ),
        release_restoration_proven=historical_settlement_proven,
    ):
        raise RowAuthorityConflict(
            "accepted claim replay found a regressed row head"
        )

    if generation["generation"] == 1:
        if (
            generation["predecessorSettlementHash"] is not None
            or generation["firstFencingToken"] != 1
            or any(
                state.get(field) is not None
                for field in (
                    "candidatePredecessorGeneration",
                    "candidatePredecessorClaimSet",
                    "candidatePredecessorSettlement",
                )
            )
        ):
            raise RowAuthorityConflict(
                "first accepted replay has predecessor ownership state"
            )
        return generation, None

    try:
        predecessor_generation = validate_owner_generation_document(
            document=state.get("candidatePredecessorGeneration")
        )
        predecessor_claim = validate_claim_set_document(
            document=state.get("candidatePredecessorClaimSet")
        )
        predecessor_settlement = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=state["rowId"],
            generation=predecessor_generation,
            claim=predecessor_claim,
            settlement_document=state.get("candidatePredecessorSettlement"),
        )
    except RowAuthorityError:
        raise
    except Exception as exc:
        raise RowAuthorityConflict(
            "accepted replay lacks its exact predecessor proof"
        ) from exc
    predecessor_decisions = [
        item
        for item in predecessor_claim["rowDecisions"]
        if item["rowId"] == state["rowId"]
    ]
    if (
        predecessor_generation["userScopeHash"] != scope
        or predecessor_claim["userScopeHash"] != scope
        or predecessor_generation["rowId"] != state["rowId"]
        or predecessor_generation["generation"]
        != generation["generation"] - 1
        or predecessor_generation["requestId"]
        != predecessor_claim["requestId"]
        or predecessor_generation["claimSetHash"]
        != predecessor_claim["claimSetHash"]
        or predecessor_generation["ownerKind"]
        != predecessor_claim["ownerKind"]
        or predecessor_generation["ownerKey"]
        != predecessor_claim["ownerKey"]
        or predecessor_generation["priority"]
        != predecessor_claim["derivedPriority"]
        or predecessor_claim["outcome"] != "accepted"
        or predecessor_generation["createdAt"]
        < predecessor_claim["createdAt"]
        or len(predecessor_decisions) != 1
        or predecessor_decisions[0]["decision"] != "accepted"
        or predecessor_decisions[0]["plannedGeneration"]
        != predecessor_generation["generation"]
        or predecessor_settlement["outcome"]
        not in {
            "dominated",
            _expected_owner_settlement_outcome(
                predecessor_generation["ownerKind"]
            ),
        }
        or generation["firstFencingToken"]
        != predecessor_settlement["fencingToken"] + 1
        or predecessor_settlement["settledAt"] > generation["createdAt"]
    ):
        raise RowAuthorityConflict(
            "accepted replay predecessor bracket does not correlate"
        )
    if predecessor_settlement["outcome"] == "dominated":
        if (
            predecessor_settlement["dominantGenerationHash"]
            != generation["generationHash"]
            or predecessor_settlement["settledAt"] != generation["createdAt"]
            or generation["predecessorSettlementHash"]
            != predecessor_generation["predecessorSettlementHash"]
            or generation["priority"] <= predecessor_generation["priority"]
        ):
            raise RowAuthorityConflict(
                "accepted replay dominated predecessor does not link forward"
            )
        return generation, predecessor_settlement
    if (
        generation["predecessorSettlementHash"]
        == predecessor_settlement["settlementHash"]
    ):
        if generation["priority"] <= predecessor_generation["priority"]:
            raise RowAuthorityConflict(
                "accepted replay direct predecessor has equal or higher priority"
            )
        return generation, None
    if (
        predecessor_generation["ownerKind"] != "contact_optout"
        or predecessor_settlement["outcome"] != "contact_optout"
        or generation["predecessorSettlementHash"]
        != predecessor_settlement["supersededEffectiveSettlementHash"]
        or predecessor_generation["predecessorSettlementHash"]
        != predecessor_settlement["supersededEffectiveSettlementHash"]
    ):
        raise RowAuthorityConflict(
            "accepted replay predecessor settlement does not link forward"
        )
    release_matches = state.get("candidatePredecessorReleaseMatches")
    if type(release_matches) not in {list, tuple}:
        raise RowAuthorityAmbiguous(
            "accepted replay lacks a bounded predecessor release proof"
        )
    if len(release_matches) > 1:
        raise RowAuthorityAmbiguous(
            "accepted replay predecessor release proof is duplicated"
        )
    if len(release_matches) != 1:
        raise RowAuthorityAmbiguous(
            "accepted replay predecessor release proof is missing"
        )
    release_entry = release_matches[0]
    if type(release_entry) is not dict or set(release_entry) != {
        "path",
        "document",
    }:
        raise RowAuthorityAmbiguous(
            "accepted replay predecessor release proof is malformed"
        )
    try:
        predecessor_release = validate_contact_fanout_result_document(
            document=release_entry["document"]
        )
    except Exception as exc:
        raise RowAuthorityAmbiguous(
            "accepted replay predecessor release proof is malformed"
        ) from exc
    if (
        type(release_entry["path"]) is not str
        or release_entry["path"].split("/")[-2:]
        != [
            "contactOptOutFanoutResults",
            f"{predecessor_release['fanoutId']}--{state['rowId']}",
        ]
        or predecessor_release["userScopeHash"] != scope
        or predecessor_release["rowId"] != state["rowId"]
        or predecessor_release["outcome"] != "release"
        or predecessor_release["disposition"] != "restore"
        or predecessor_release["reasonCode"] != "exact_predecessor"
        or predecessor_release["releasedRowGeneration"]
        != predecessor_generation["generation"]
        or predecessor_release["releasedRowSettlementHash"]
        != predecessor_settlement["settlementHash"]
        or predecessor_release["restoredEffectiveSettlementHash"]
        != generation["predecessorSettlementHash"]
        or predecessor_release["createdAt"]
        < predecessor_settlement["settledAt"]
        or predecessor_release["createdAt"] > generation["createdAt"]
    ):
        raise RowAuthorityAmbiguous(
            "accepted replay predecessor release proof does not correlate"
        )
    _validate_bounded_release_restored_authority(
        scope=scope,
        row_id=state["rowId"],
        release_result=predecessor_release,
        released_generation=predecessor_generation,
        raw_restored_authority=state.get(
            "candidatePredecessorRestoredAuthority"
        ),
        successor_generation=generation,
    )
    return generation, None


def _plan_row_claim_set(
    *,
    user_scope_hash,
    authority_origin,
    authority_link,
    operator_action_document,
    fanout_id,
    canonical_mailbox_identity_hash,
    contact_settlement_hash,
    thread_binding_document,
    row_states,
    stored_claim_set_document,
    created_at,
    lease_owner_hash,
    lease_until,
    combined_operator_decline=False,
):
    scope = _require_sha256(user_scope_hash, field_name="user_scope_hash")
    created = _require_timestamp(created_at, field_name="created_at")
    lease_owner = _require_sha256(
        lease_owner_hash,
        field_name="lease_owner_hash",
    )
    deadline = _require_timestamp(lease_until, field_name="lease_until")
    if deadline <= created:
        raise RowAuthorityConfigError("claim lease must end after created_at")
    context = _derive_claim_request_context(
        user_scope_hash=scope,
        authority_origin=authority_origin,
        authority_link=authority_link,
        operator_action_document=operator_action_document,
        fanout_id=fanout_id,
        thread_binding_document=thread_binding_document,
        canonical_mailbox_identity_hash=canonical_mailbox_identity_hash,
        contact_settlement_hash=contact_settlement_hash,
    )
    binding = context["binding"]
    if type(row_states) not in {list, tuple} or len(row_states) != binding[
        "bindingCount"
    ]:
        raise RowAuthorityConfigError(
            "claim row state must cover every bound row"
        )
    states_by_row = {}
    for state in row_states:
        if type(state) is not dict or "rowId" not in state:
            raise RowAuthorityConfigError("claim row state is malformed")
        row_id = validate_row_id(state["rowId"])
        if row_id in states_by_row:
            raise RowAuthorityConfigError("claim row state is duplicated")
        states_by_row[row_id] = state
    if list(states_by_row) != [
        item["rowId"] for item in binding["rowBindings"]
    ]:
        raise RowAuthorityConfigError(
            "claim row state is not in canonical binding order"
        )

    validated_states = []
    for row_binding in binding["rowBindings"]:
        row_id = row_binding["rowId"]
        state = states_by_row[row_id]
        try:
            identity = validate_row_identity_document(
                document=state.get("identity")
            )
            head = validate_row_authority_head(document=state.get("head"))
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "claim row identity or head is missing or malformed"
            ) from exc
        if (
            identity["userScopeHash"] != scope
            or identity["rowId"] != row_id
            or identity["clientId"] != binding["clientId"]
            or head["userScopeHash"] != scope
            or head["rowId"] != row_id
            or head["createdAt"] != identity["createdAt"]
        ):
            raise RowAuthorityConflict(
                "claim row identity, binding, and head do not correlate"
            )
        if (
            binding["createdAt"] < identity["createdAt"]
            or created < binding["createdAt"]
            or created < identity["createdAt"]
            or (
                stored_claim_set_document is None
                and created < head["updatedAt"]
            )
        ):
            raise RowAuthorityConflict(
                "claim event predates binding or row authority readiness"
            )
        bounded_history = None
        if "latestSettlements" in state:
            bounded_history = _validate_bounded_row_history(
                scope=scope,
                row_id=row_id,
                head=head,
                row_state=state,
            )
            current_generation = bounded_history["currentGeneration"]
            current_claim = bounded_history["currentClaimSet"]
            current_settlement = bounded_history["currentSettlement"]
            owner_lineage = None
        else:
            (
                current_generation,
                current_claim,
                current_settlement,
                owner_lineage,
            ) = _validate_current_owner_state(
                scope=scope,
                row_id=row_id,
                head=head,
                row_state=state,
            )
        validated_states.append(
            {
                **state,
                "identity": identity,
                "head": head,
                "currentGeneration": current_generation,
                "currentClaimSet": current_claim,
                "currentSettlement": current_settlement,
                "ownerLineage": owner_lineage,
                "boundedHistory": bounded_history,
            }
        )

    if all(state["boundedHistory"] is None for state in validated_states):
        _validate_complete_accepted_claim_cohorts(row_states=validated_states)
    elif all(
        state["boundedHistory"] is not None for state in validated_states
    ):
        _validate_bounded_current_claim_cohorts(row_states=validated_states)

    if stored_claim_set_document is not None:
        try:
            stored_claim = validate_claim_set_document(
                document=stored_claim_set_document
            )
        except Exception as exc:
            raise RowAuthorityConflict(
                "stored claim set contains immutable drift"
            ) from exc
        expected_static = {
            "userScopeHash": scope,
            "requestId": context["requestId"],
            "authorityOrigin": authority_origin,
            "authorityLink": context["origin"]["authorityLink"],
            "authorityLinkHash": context["origin"]["authorityLinkHash"],
            "operatorActionHash": context["origin"]["operatorActionHash"],
            "fanoutId": context["origin"]["fanoutId"],
            "rowBindings": binding["rowBindings"],
            "primaryRowId": binding["primaryRowId"],
            "bindingCount": binding["bindingCount"],
            "rowBindingsHash": binding["rowBindingsHash"],
            "ownerKind": context["origin"]["ownerKind"],
            "ownerKey": context["origin"]["ownerKey"],
            "workKey": context["origin"]["workKey"],
            "payloadHash": context["origin"]["payloadHash"],
            "createdAt": created,
        }
        if any(
            stored_claim[field] != value
            for field, value in expected_static.items()
        ):
            raise RowAuthorityConflict(
                "stored claim set differs from the exact request"
            )
        if stored_claim["outcome"] == "dominated":
            expected_dominated_writes = (
                2 if combined_operator_decline else 1
            )
            if (
                stored_claim["plannedWrites"]
                != expected_dominated_writes
            ):
                raise RowAuthorityConflict(
                    "dominated claim replay has a nonsemantic write count"
                )
            if any(
                state.get("candidateGeneration") is not None
                or state.get("candidateSettlement") is not None
                for state in validated_states
            ):
                raise RowAuthorityAmbiguous(
                    "dominated claim replay found partial future ownership state"
                )
            if all(
                state["boundedHistory"] is not None
                for state in validated_states
            ):
                for state, decision in zip(
                    validated_states,
                    stored_claim["rowDecisions"],
                ):
                    if decision["rowId"] != state["rowId"]:
                        raise RowAuthorityConflict(
                            "dominated claim replay decisions are not canonical"
                        )
                    _validate_bounded_dominated_replay(
                        scope=scope,
                        state=state,
                        decision=decision,
                        stored_claim=stored_claim,
                    )
                return {
                    "disposition": "already_applied",
                    "claimSet": stored_claim,
                    "generations": (),
                    "heads": tuple(
                        state["head"] for state in validated_states
                    ),
                    "predecessorSettlements": (),
                    "mutations": (),
                }
            for state, decision in zip(
                validated_states,
                stored_claim["rowDecisions"],
            ):
                if decision["rowId"] != state["rowId"]:
                    raise RowAuthorityConflict(
                        "dominated claim replay decisions are not canonical"
                    )
                if decision["decision"] == "blocked_by_claim_set":
                    strictly_prior = [
                        entry
                        for entry in state["ownerLineage"]
                        if entry["generation"]["createdAt"]
                        < stored_claim["createdAt"]
                    ]
                    equal_time = [
                        entry
                        for entry in state["ownerLineage"]
                        if entry["generation"]["createdAt"]
                        == stored_claim["createdAt"]
                    ]
                    possible_owners = [
                        strictly_prior[-1] if strictly_prior else None,
                        *equal_time,
                    ]
                    if not any(
                        owner is None
                        or stored_claim["derivedPriority"]
                        > owner["generation"]["priority"]
                        for owner in possible_owners
                    ):
                        raise RowAuthorityConflict(
                            "blocked claim replay row should have been dominated"
                        )
                    continue
                matching_winners = [
                    (index, entry)
                    for index, entry in enumerate(state["ownerLineage"])
                    if entry["generation"]["generationHash"]
                    == decision["winnerGenerationHash"]
                ]
                if len(matching_winners) != 1:
                    raise RowAuthorityConflict(
                        "dominated claim replay lacks its immutable winner"
                    )
                winner_index, winner = matching_winners[0]
                winner_generation = winner["generation"]
                if (
                    stored_claim["derivedPriority"]
                    > winner_generation["priority"]
                    or winner_generation["createdAt"]
                    > stored_claim["createdAt"]
                    or (
                        winner_index + 1 < len(state["ownerLineage"])
                        and state["ownerLineage"][winner_index + 1][
                            "generation"
                        ]["createdAt"]
                        < stored_claim["createdAt"]
                    )
                ):
                    raise RowAuthorityConflict(
                        "dominated claim replay winner was not effective"
                    )
                winner_settlement = winner["settlement"]
                possible_settlement_hashes = {
                    winner_generation["predecessorSettlementHash"]
                }
                if (
                    winner_settlement is not None
                    and winner_settlement["outcome"] != "dominated"
                ):
                    if (
                        winner_settlement["settledAt"]
                        < stored_claim["createdAt"]
                    ):
                        possible_settlement_hashes = {
                            winner_settlement["settlementHash"]
                        }
                    elif (
                        winner_settlement["settledAt"]
                        == stored_claim["createdAt"]
                    ):
                        possible_settlement_hashes.add(
                            winner_settlement["settlementHash"]
                        )
                if (
                    decision["winnerSettlementHash"]
                    not in possible_settlement_hashes
                ):
                    raise RowAuthorityConflict(
                        "dominated claim replay winner settlement drifted"
                    )
            return {
                "disposition": "already_applied",
                "claimSet": stored_claim,
                "generations": (),
                "heads": tuple(state["head"] for state in validated_states),
                "predecessorSettlements": (),
                "mutations": (),
            }
        if all(
            state["boundedHistory"] is not None
            for state in validated_states
        ):
            generations = []
            predecessor_settlements = []
            for state, decision in zip(
                validated_states,
                stored_claim["rowDecisions"],
            ):
                if (
                    decision["rowId"] != state["rowId"]
                    or decision["decision"] != "accepted"
                ):
                    raise RowAuthorityConflict(
                        "accepted claim replay decisions are not canonical"
                    )
                generation, predecessor = (
                    _validate_bounded_accepted_replay(
                        scope=scope,
                        state=state,
                        decision=decision,
                        stored_claim=stored_claim,
                        lease_owner=lease_owner,
                        deadline=deadline,
                    )
                )
                generations.append(generation)
                if predecessor is not None:
                    predecessor_settlements.append(predecessor)
            expected_replay_writes = (
                2 + (3 * binding["bindingCount"])
                if combined_operator_decline
                else 1
                + (2 * binding["bindingCount"])
                + len(predecessor_settlements)
            )
            if stored_claim["plannedWrites"] != expected_replay_writes:
                raise RowAuthorityConflict(
                    "accepted claim replay has a nonsemantic write count"
                )
            return {
                "disposition": "already_applied",
                "claimSet": stored_claim,
                "generations": tuple(generations),
                "heads": tuple(state["head"] for state in validated_states),
                "predecessorSettlements": tuple(predecessor_settlements),
                "mutations": (),
            }
        generations = []
        predecessor_settlements = []
        for state, decision in zip(
            validated_states,
            stored_claim["rowDecisions"],
        ):
            candidate = state.get("candidateGeneration")
            if candidate is None:
                raise RowAuthorityAmbiguous(
                    "accepted claim replay is missing its generation"
                )
            try:
                generation = validate_owner_generation_document(
                    document=candidate
                )
            except Exception as exc:
                raise RowAuthorityConflict(
                    "accepted claim generation contains immutable drift"
                ) from exc
            if (
                generation["userScopeHash"] != scope
                or generation["rowId"] != decision["rowId"]
                or generation["generation"]
                != decision["plannedGeneration"]
                or generation["requestId"] != stored_claim["requestId"]
                or generation["claimSetHash"] != stored_claim["claimSetHash"]
                or generation["ownerKind"] != stored_claim["ownerKind"]
                or generation["ownerKey"] != stored_claim["ownerKey"]
                or generation["priority"]
                != stored_claim["derivedPriority"]
                or generation["createdAt"] < stored_claim["createdAt"]
            ):
                raise RowAuthorityConflict(
                    "accepted claim generation does not correlate"
                )
            current_number = state["head"]["effectiveOwnerGeneration"]
            if (
                current_number is None
                or current_number < generation["generation"]
                or len(state["ownerLineage"]) < generation["generation"]
            ):
                raise RowAuthorityConflict(
                    "accepted claim replay found a regressed row head"
                )
            lineage_entry = state["ownerLineage"][
                generation["generation"] - 1
            ]
            if (
                lineage_entry["generation"] != generation
                or lineage_entry["claimSet"] != stored_claim
            ):
                raise RowAuthorityConflict(
                    "accepted claim replay differs from validated ownership lineage"
                )
            if not _claim_head_is_forward(
                head=state["head"],
                generation=generation,
                requested_lease_owner_hash=lease_owner,
                requested_lease_until=deadline,
                higher_generation_proven=(
                    current_number > generation["generation"]
                ),
            ):
                raise RowAuthorityConflict(
                    "accepted claim replay found a regressed row head"
                )
            if generation["generation"] > 1:
                predecessor = state["ownerLineage"][
                    generation["generation"] - 2
                ]["settlement"]
                if predecessor["outcome"] == "dominated":
                    predecessor_settlements.append(predecessor)
            generations.append(generation)
        expected_replay_writes = (
            2 + (3 * binding["bindingCount"])
            if combined_operator_decline
            else 1
            + (2 * binding["bindingCount"])
            + len(predecessor_settlements)
        )
        if stored_claim["plannedWrites"] != expected_replay_writes:
            raise RowAuthorityConflict(
                "accepted claim replay has a nonsemantic write count"
            )
        return {
            "disposition": "already_applied",
            "claimSet": stored_claim,
            "generations": tuple(generations),
            "heads": tuple(state["head"] for state in validated_states),
            "predecessorSettlements": tuple(predecessor_settlements),
            "mutations": (),
        }

    for state in validated_states:
        if state["head"]["currentLocationLifecycle"] not in {
            "active",
            "nonviable",
        }:
            raise RowAuthorityConflict(
                "new claims require an active or nonviable row"
            )
        if state["head"]["effectiveOwnerGeneration"] is None and (
            state["head"]["state"] != "clear"
            or state["head"]["effectiveSettlementHash"] is not None
            or (
                state["boundedHistory"] is None
                and state["head"]["latestSettlementHash"] is not None
            )
        ):
            raise RowAuthorityConflict(
                "ownerless historical row cannot allocate a generation"
            )

    incoming_priority = derive_owner_priority(context["origin"]["ownerKind"])
    if any(
        state.get("candidateGeneration") is not None
        or state.get("candidateSettlement") is not None
        for state in validated_states
    ):
        raise RowAuthorityAmbiguous(
            "new claim found partial future generation or settlement state"
        )
    losing_rows = {
        state["rowId"]
        for state in validated_states
        if state["head"]["effectivePriority"] is not None
        and incoming_priority <= state["head"]["effectivePriority"]
    }
    if losing_rows:
        decisions = []
        for state in validated_states:
            if state["rowId"] in losing_rows:
                head = state["head"]
                decisions.append(
                    {
                        "rowId": state["rowId"],
                        "decision": "dominated",
                        "plannedGeneration": None,
                        "winnerGenerationHash": head[
                            "effectiveOwnerGenerationHash"
                        ],
                        "winnerSettlementHash": head[
                            "effectiveSettlementHash"
                        ],
                    }
                )
            else:
                decisions.append(
                    {
                        "rowId": state["rowId"],
                        "decision": "blocked_by_claim_set",
                        "plannedGeneration": None,
                        "winnerGenerationHash": None,
                        "winnerSettlementHash": None,
                    }
                )
        claim = build_claim_set_document(
            user_scope_hash=scope,
            authority_origin=authority_origin,
            authority_link=authority_link,
            operator_action_document=operator_action_document,
            fanout_id=fanout_id,
            row_ids=[item["rowId"] for item in binding["rowBindings"]],
            primary_row_id=binding["primaryRowId"],
            planned_writes=(2 if combined_operator_decline else 1),
            outcome="dominated",
            row_decisions=decisions,
            created_at=created,
            canonical_mailbox_identity_hash=canonical_mailbox_identity_hash,
            contact_settlement_hash=contact_settlement_hash,
        )
        if claim["requestId"] != context["requestId"]:
            raise RowAuthorityConfigError("claim request derivation drifted")
        return {
            "disposition": "dominated",
            "claimSet": claim,
            "generations": (),
            "heads": tuple(state["head"] for state in validated_states),
            "predecessorSettlements": (),
            "mutations": (
                {
                    "target": "claim_set",
                    "operation": "create",
                    "document": claim,
                },
            ),
        }

    dominated_count = sum(
        state["head"]["state"] in {"claimed", "review_pending"}
        for state in validated_states
        if state["head"]["effectiveOwnerGeneration"] is not None
    )
    planned_writes = (
        2 + (3 * len(validated_states))
        if combined_operator_decline
        else 1 + (2 * len(validated_states)) + dominated_count
    )
    _require_row_authority_planned_writes(planned_writes)
    decisions = []
    for state in validated_states:
        head = state["head"]
        history = state["boundedHistory"]
        generation_number = (
            history["nextGeneration"]
            if history is not None
            else (
                1
                if head["effectiveOwnerGeneration"] is None
                else head["effectiveOwnerGeneration"] + 1
            )
        )
        decisions.append(
            {
                "rowId": state["rowId"],
                "decision": "accepted",
                "plannedGeneration": generation_number,
                "winnerGenerationHash": None,
                "winnerSettlementHash": None,
            }
        )
    claim = build_claim_set_document(
        user_scope_hash=scope,
        authority_origin=authority_origin,
        authority_link=authority_link,
        operator_action_document=operator_action_document,
        fanout_id=fanout_id,
        row_ids=[item["rowId"] for item in binding["rowBindings"]],
        primary_row_id=binding["primaryRowId"],
        planned_writes=planned_writes,
        outcome="accepted",
        row_decisions=decisions,
        created_at=created,
        canonical_mailbox_identity_hash=canonical_mailbox_identity_hash,
        contact_settlement_hash=contact_settlement_hash,
    )
    if claim["requestId"] != context["requestId"]:
        raise RowAuthorityConfigError("claim request derivation drifted")
    generations = []
    predecessor_settlements = []
    heads = []
    mutations = [
        {
            "target": "claim_set",
            "operation": "create",
            "document": claim,
        }
    ]
    for state, decision in zip(validated_states, decisions):
        predecessor_head = state["head"]
        generation = build_owner_generation_document(
            claim_set_document=claim,
            row_id=state["rowId"],
            generation=decision["plannedGeneration"],
            predecessor_head_hash=predecessor_head["headHash"],
            predecessor_settlement_hash=predecessor_head[
                "effectiveSettlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=(
                state["boundedHistory"]["nextFirstFencingToken"]
                if state["boundedHistory"] is not None
                else (
                    1
                    if predecessor_head["fencingToken"] is None
                    else predecessor_head["fencingToken"] + 1
                )
            ),
            created_at=created,
        )
        generations.append(generation)
        mutations.append(
            {
                "target": f"generation:{state['rowId']}",
                "operation": "create",
                "document": generation,
            }
        )
        dominated_settlement = None
        if predecessor_head["state"] in {"claimed", "review_pending"}:
            dominated_settlement = build_owner_settlement_document(
                generation_document=state["currentGeneration"],
                claim_set_document=state["currentClaimSet"],
                fencing_token=predecessor_head["fencingToken"],
                outcome="dominated",
                settled_at=created,
                dominant_generation_hash=generation["generationHash"],
            )
            predecessor_settlements.append(dominated_settlement)
            mutations.append(
                {
                    "target": f"predecessor_settlement:{state['rowId']}",
                    "operation": "create",
                    "document": dominated_settlement,
                }
            )
        head = _build_claim_advanced_head(
            expected_head=predecessor_head,
            generation_document=generation,
            lease_owner_hash=lease_owner,
            lease_until=deadline,
            dominated_predecessor_settlement_hash=(
                dominated_settlement["settlementHash"]
                if dominated_settlement is not None
                else None
            ),
            claimed_at=created,
            expected_generation=(
                state["boundedHistory"]["nextGeneration"]
                if state["boundedHistory"] is not None
                else None
            ),
            expected_first_fencing_token=(
                state["boundedHistory"]["nextFirstFencingToken"]
                if state["boundedHistory"] is not None
                else None
            ),
        )
        heads.append(head)
        mutations.append(
            {
                "target": f"head:{state['rowId']}",
                "operation": "set",
                "document": head,
            }
        )
    expected_planner_mutations = (
        planned_writes - 1 - len(validated_states)
        if combined_operator_decline
        else planned_writes
    )
    if len(mutations) != expected_planner_mutations:
        raise RowAuthorityConfigError("claim planned-write count drifted")
    return {
        "disposition": "created",
        "claimSet": claim,
        "generations": tuple(generations),
        "heads": tuple(heads),
        "predecessorSettlements": tuple(predecessor_settlements),
        "mutations": tuple(mutations),
    }


def _plan_operator_row_claim(**arguments):
    return _plan_row_claim_set(
        authority_origin="authenticated_operator",
        authority_link=None,
        fanout_id=None,
        canonical_mailbox_identity_hash=None,
        contact_settlement_hash=None,
        combined_operator_decline=True,
        **arguments,
    )


def _plan_contact_fanout_row_claim(**arguments):
    link = validate_b1_authority_link(
        authority_link=arguments.get("authority_link"),
        user_scope_hash=arguments.get("user_scope_hash"),
    )
    if set(link) != _B1_LINK_V2_KEYS:
        raise RowAuthorityConfigError(
            "contact fan-out requires a v2 verified-contact authority link"
        )
    return _plan_row_claim_set(
        authority_origin="contact_fanout",
        operator_action_document=None,
        **arguments,
    )


def _plan_owner_generation_settlement(
    *,
    user_scope_hash,
    row_id,
    expected_head,
    actual_head_document,
    identity_document,
    generation_document,
    claim_set_document,
    stored_settlement_document,
    prior_effective_settlement_document,
    settled_at,
    operator_action_document,
    actual_owner_lineage=None,
    actual_bounded_history=None,
):
    scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    checked_row_id = validate_row_id(row_id)
    expected = validate_row_authority_head(document=expected_head)
    actual = validate_row_authority_head(document=actual_head_document)
    identity = validate_row_identity_document(document=identity_document)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    claim = validate_claim_set_document(document=claim_set_document)
    settled = _require_timestamp(settled_at, field_name="settled_at")
    if (
        expected["userScopeHash"] != scope
        or actual["userScopeHash"] != scope
        or identity["userScopeHash"] != scope
        or generation["userScopeHash"] != scope
        or claim["userScopeHash"] != scope
        or expected["rowId"] != checked_row_id
        or actual["rowId"] != checked_row_id
        or identity["rowId"] != checked_row_id
        or generation["rowId"] != checked_row_id
        or expected["createdAt"] != identity["createdAt"]
        or actual["createdAt"] != identity["createdAt"]
        or generation["createdAt"] < identity["createdAt"]
        or generation["createdAt"] > expected["updatedAt"]
        or generation["createdAt"] < claim["createdAt"]
    ):
        raise RowAuthorityConfigError(
            "settlement identity, head, generation, and claim do not correlate"
        )
    expected_state = {
        "contact_optout": "claimed",
        "terminal": "claimed",
        "human_decision": "review_pending",
    }[generation["ownerKind"]]
    if (
        expected["state"] != expected_state
        or expected["effectiveOwnerGeneration"]
        != generation["generation"]
        or expected["effectiveOwnerGenerationHash"]
        != generation["generationHash"]
        or expected["effectiveOwnerKind"] != generation["ownerKind"]
        or expected["effectivePriority"] != generation["priority"]
        or expected["fencingToken"] < generation["firstFencingToken"]
        or generation["requestId"] != claim["requestId"]
        or generation["claimSetHash"] != claim["claimSetHash"]
        or generation["ownerKind"] != claim["ownerKind"]
        or generation["ownerKey"] != claim["ownerKey"]
        or generation["predecessorSettlementHash"]
        != expected["effectiveSettlementHash"]
    ):
        raise RowAuthorityConfigError(
            "settlement generation is not the expected active owner"
        )
    if (
        settled < claim["createdAt"]
        or settled < generation["createdAt"]
        or settled < expected["updatedAt"]
    ):
        raise RowAuthorityConfigError(
            "settlement cannot predate generation or current head"
        )
    predecessor_hash = generation["predecessorSettlementHash"]
    if predecessor_hash is None:
        if prior_effective_settlement_document is not None:
            raise RowAuthorityConfigError(
                "ownerless predecessor cannot carry effective settlement proof"
            )
        prior_settlement = None
    else:
        if prior_effective_settlement_document is None:
            raise RowAuthorityConfigError(
                "effective predecessor settlement proof is required"
            )
        prior_settlement = validate_owner_settlement_document(
            document=prior_effective_settlement_document
        )
        if (
            prior_settlement["userScopeHash"] != scope
            or prior_settlement["rowId"] != checked_row_id
            or prior_settlement["settlementHash"] != predecessor_hash
            or prior_settlement["generation"] >= generation["generation"]
            or prior_settlement["fencingToken"]
            >= generation["firstFencingToken"]
            or prior_settlement["outcome"] == "dominated"
            or prior_settlement["settledAt"] > generation["createdAt"]
        ):
            raise RowAuthorityConfigError(
                "effective predecessor settlement proof does not correlate"
            )
    outcome = _expected_owner_settlement_outcome(generation["ownerKind"])
    candidate = build_owner_settlement_document(
        generation_document=generation,
        claim_set_document=claim,
        fencing_token=expected["fencingToken"],
        outcome=outcome,
        settled_at=settled,
        superseded_effective_settlement_hash=(
            predecessor_hash
            if generation["ownerKind"] == "contact_optout"
            else None
        ),
        operator_action_document=operator_action_document,
    )
    immediate_head = _build_settlement_advanced_head(
        expected_head=expected,
        generation_document=generation,
        settlement_document=candidate,
    )
    if stored_settlement_document is not None:
        stored = _validate_correlated_owner_settlement(
            scope=scope,
            row_id=checked_row_id,
            generation=generation,
            claim=claim,
            settlement_document=stored_settlement_document,
        )
        if stored != candidate:
            raise RowAuthorityConflict(
                "stored settlement differs from the requested logical outcome"
            )
        if actual == expected:
            raise RowAuthorityAmbiguous(
                "settlement exists without its settled head"
            )
        if actual_bounded_history is not None:
            bounded = actual_bounded_history
            historical_matches = [
                item
                for item in bounded["latestSettlements"]
                if item["generation"] == generation["generation"]
                and item["settlementHash"] == stored["settlementHash"]
            ]
            restored = bounded.get("restoredAuthority")
            restored_matches = (
                restored is not None
                and restored["generation"] == generation
                and restored["claimSet"] == claim
                and restored["settlement"] == stored
            )
            historical_max_proven = (
                bool(bounded["latestSettlements"])
                and generation["generation"]
                <= bounded["latestSettlements"][0]["generation"]
            )
            if (
                len(historical_matches) != 1
                and not restored_matches
                and not historical_max_proven
            ):
                raise RowAuthorityConflict(
                    "settlement replay target lacks bounded historical proof"
                )
            if actual["updatedAt"] < stored["settledAt"]:
                raise RowAuthorityConflict(
                    "settlement replay current head predates its target"
                )
            return {
                "disposition": "already_applied",
                "generation": generation,
                "settlement": stored,
                "head": actual,
                "mutations": (),
                "higherGenerationProven": (
                    actual["effectiveOwnerGeneration"] is not None
                    and actual["effectiveOwnerGeneration"]
                    > generation["generation"]
                ),
                "releaseRestorationProven": restored_matches,
            }
        higher_generation_proven = False
        if (
            actual["effectiveOwnerGeneration"] is not None
            and actual["effectiveOwnerGeneration"]
            > generation["generation"]
        ):
            if type(actual_owner_lineage) not in {list, tuple}:
                raise RowAuthorityAmbiguous(
                    "settlement replay lacks complete later owner lineage"
                )
            lineage_documents = tuple(actual_owner_lineage)
            if len(lineage_documents) != actual[
                "effectiveOwnerGeneration"
            ]:
                raise RowAuthorityAmbiguous(
                    "settlement replay later owner lineage is incomplete"
                )
            last = lineage_documents[-1]
            predecessor = (
                lineage_documents[-2]
                if len(lineage_documents) > 1
                else None
            )
            try:
                (
                    _current_generation,
                    _current_claim,
                    _current_settlement,
                    validated_lineage,
                ) = _validate_current_owner_state(
                    scope=scope,
                    row_id=checked_row_id,
                    head=actual,
                    row_state={
                        "ownerLineage": lineage_documents,
                        "currentGeneration": last.get("generation"),
                        "currentClaimSet": last.get("claimSet"),
                        "currentSettlement": last.get("settlement"),
                        "currentPredecessorGeneration": (
                            predecessor.get("generation")
                            if predecessor is not None
                            else None
                        ),
                        "currentPredecessorClaimSet": (
                            predecessor.get("claimSet")
                            if predecessor is not None
                            else None
                        ),
                        "currentPredecessorSettlement": (
                            predecessor.get("settlement")
                            if predecessor is not None
                            else None
                        ),
                    },
                )
            except (RowAuthorityConflict, RowAuthorityAmbiguous):
                raise
            except Exception as exc:
                raise RowAuthorityConflict(
                    "settlement replay later owner lineage is malformed"
                ) from exc
            target_entry = validated_lineage[generation["generation"] - 1]
            if (
                target_entry["generation"] != generation
                or target_entry["claimSet"] != claim
                or target_entry["settlement"] != stored
            ):
                raise RowAuthorityConflict(
                    "settlement replay target differs from later owner lineage"
                )
            higher_generation_proven = True
        if not _settlement_head_is_forward(
            settled_head=immediate_head,
            current_head=actual,
            generation_document=generation,
            settlement_document=stored,
            higher_generation_proven=higher_generation_proven,
        ):
            raise RowAuthorityConflict(
                "settlement replay cannot prove a forward row head"
            )
        return {
            "disposition": "already_applied",
            "generation": generation,
            "settlement": stored,
            "head": actual,
            "mutations": (),
            "higherGenerationProven": higher_generation_proven,
            "releaseRestorationProven": False,
        }
    if actual != expected:
        raise RowAuthorityConflict(
            "settlement expected head is stale or drifted"
        )
    return {
        "disposition": "settled",
        "generation": generation,
        "settlement": candidate,
        "head": immediate_head,
        "higherGenerationProven": False,
        "releaseRestorationProven": False,
        "mutations": (
            {
                "target": "settlement",
                "operation": "create",
                "document": candidate,
            },
            {
                "target": "head",
                "operation": "set",
                "document": immediate_head,
            },
        ),
    }


def _operator_decline_lease_until(issued_at):
    issued = _timestamp_as_datetime(
        issued_at,
        field_name="operator action issuedAt",
    )
    return (issued + timedelta(microseconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _build_immediate_operator_decline_head(
    *,
    expected_head,
    generation_document,
    settlement_document,
    operator_action_document,
    expected_generation=None,
    expected_first_fencing_token=None,
):
    expected = validate_row_authority_head(document=expected_head)
    generation = validate_owner_generation_document(
        document=generation_document
    )
    settlement = validate_owner_settlement_document(
        document=settlement_document
    )
    action = validate_operator_action_document(
        document=operator_action_document
    )
    if (
        expected["state"] != "clear"
        or expected["effectiveOwnerGeneration"] is not None
        or expected["effectiveOwnerGenerationHash"] is not None
        or expected["effectiveOwnerKind"] is not None
        or expected["effectivePriority"] is not None
        or expected["fencingToken"] is not None
        or expected["effectiveSettlementHash"] is not None
        or generation["predecessorHeadHash"] != expected["headHash"]
        or generation["predecessorSettlementHash"] is not None
        or generation["ownerKind"] != "human_decision"
        or settlement["generationHash"] != generation["generationHash"]
        or settlement["fencingToken"]
        != generation["firstFencingToken"]
        or settlement["outcome"] != "human_declined"
        or settlement["operatorActionHash"]
        != action["operatorActionHash"]
        or settlement["settledAt"] != action["issuedAt"]
    ):
        raise RowAuthorityConfigError(
            "immediate operator decline does not correlate to a clear row"
        )
    checked_generation = (
        1
        if expected_generation is None
        else _require_pos(
            expected_generation,
            field_name="expected_generation",
        )
    )
    checked_fence = (
        1
        if expected_first_fencing_token is None
        else _require_pos(
            expected_first_fencing_token,
            field_name="expected_first_fencing_token",
        )
    )
    if (
        generation["generation"] != checked_generation
        or generation["firstFencingToken"] != checked_fence
    ):
        raise RowAuthorityConfigError(
            "immediate operator decline does not use bounded generation floors"
        )
    transient_claimed = _build_claim_advanced_head(
        expected_head=expected,
        generation_document=generation,
        lease_owner_hash=action["actorScopeHash"],
        lease_until=_operator_decline_lease_until(action["issuedAt"]),
        dominated_predecessor_settlement_hash=None,
        claimed_at=action["issuedAt"],
        expected_generation=checked_generation,
        expected_first_fencing_token=checked_fence,
    )
    settled = _build_settlement_advanced_head(
        expected_head=transient_claimed,
        generation_document=generation,
        settlement_document=settlement,
    )
    material = {
        key: value for key, value in settled.items() if key != "headHash"
    }
    material["stateRevision"] = expected["stateRevision"] + 1
    return validate_row_authority_head(document=_with_head_hash(material))


def _validate_bounded_operator_action_matches(
    *, scope, row_id, action, row_state
):
    """Validate the exact settlement matched by one immutable action."""
    raw_matches = row_state.get("actionSettlementMatches")
    if type(raw_matches) not in {list, tuple} or len(raw_matches) > 2:
        raise RowAuthorityAmbiguous(
            "operator decline action settlement proof is not bounded"
        )
    if len(raw_matches) > 1:
        raise RowAuthorityAmbiguous(
            "operator decline action settlement is duplicated"
        )
    matches = []
    for raw in raw_matches:
        if type(raw) is not dict or set(raw) != {
            "path",
            "generation",
            "claimSet",
            "settlement",
        }:
            raise RowAuthorityAmbiguous(
                "operator decline action settlement proof is malformed"
            )
        try:
            generation = validate_owner_generation_document(
                document=raw["generation"]
            )
            claim = validate_claim_set_document(document=raw["claimSet"])
            settlement = _validate_correlated_owner_settlement(
                scope=scope,
                row_id=row_id,
                generation=generation,
                claim=claim,
                settlement_document=raw["settlement"],
            )
            expected_id = _generation_document_id(
                row_id=row_id,
                generation=generation["generation"],
            )
        except RowAuthorityError:
            raise
        except Exception as exc:
            raise RowAuthorityConflict(
                "operator decline action settlement authority is malformed"
            ) from exc
        decisions = [
            decision
            for decision in claim["rowDecisions"]
            if decision["rowId"] == row_id
        ]
        if (
            type(raw["path"]) is not str
            or raw["path"].split("/")[-2:]
            != ["rowOwnerSettlements", expected_id]
            or generation["userScopeHash"] != scope
            or generation["rowId"] != row_id
            or generation["requestId"] != claim["requestId"]
            or generation["claimSetHash"] != claim["claimSetHash"]
            or generation["ownerKind"] != "human_decision"
            or generation["ownerKind"] != claim["ownerKind"]
            or generation["ownerKey"] != claim["ownerKey"]
            or generation["priority"] != claim["derivedPriority"]
            or claim["outcome"] != "accepted"
            or len(decisions) != 1
            or decisions[0]["decision"] != "accepted"
            or decisions[0]["plannedGeneration"]
            != generation["generation"]
            or settlement["operatorActionHash"]
            != action["operatorActionHash"]
            or settlement["outcome"] != "human_declined"
        ):
            raise RowAuthorityConflict(
                "operator decline action settlement does not correlate"
            )
        matches.append(
            {
                "generation": generation,
                "claimSet": claim,
                "settlement": settlement,
            }
        )
    return tuple(matches)


def _plan_operator_decline(
    *,
    user_scope_hash,
    thread_binding_document,
    operator_action_document,
    stored_operator_action_document,
    row_states,
    stored_claim_set_document,
):
    scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    binding = validate_thread_row_binding_document(
        document=thread_binding_document
    )
    action = validate_operator_action_document(
        document=operator_action_document
    )
    if (
        binding["userScopeHash"] != scope
        or action["userScopeHash"] != scope
        or action["rowBindingsHash"] != binding["rowBindingsHash"]
        or action["issuedAt"] < binding["createdAt"]
    ):
        raise RowAuthorityConflict(
            "operator action does not correlate to its stable thread binding"
        )
    stored_action = None
    if stored_operator_action_document is not None:
        try:
            stored_action = validate_operator_action_document(
                document=stored_operator_action_document
            )
        except Exception as exc:
            raise RowAuthorityConflict(
                "stored operator action contains immutable drift"
            ) from exc
        if stored_action != action:
            raise RowAuthorityConflict(
                "stored operator action differs from the exact request"
            )
    if type(row_states) not in {list, tuple} or len(row_states) != binding[
        "bindingCount"
    ]:
        raise RowAuthorityConfigError(
            "operator decline row state must cover every bound row"
        )
    states_by_row = {}
    for state in row_states:
        if type(state) is not dict or "rowId" not in state:
            raise RowAuthorityConfigError(
                "operator decline row state is malformed"
            )
        row_id = validate_row_id(state["rowId"])
        if row_id in states_by_row:
            raise RowAuthorityConfigError(
                "operator decline row state is duplicated"
            )
        states_by_row[row_id] = state
    if list(states_by_row) != [
        item["rowId"] for item in binding["rowBindings"]
    ]:
        raise RowAuthorityConfigError(
            "operator decline row state is not canonical"
        )

    validated_states = []
    for row_binding in binding["rowBindings"]:
        row_id = row_binding["rowId"]
        state = states_by_row[row_id]
        try:
            identity = validate_row_identity_document(
                document=state.get("identity")
            )
            head = validate_row_authority_head(document=state.get("head"))
        except Exception as exc:
            raise RowAuthorityAmbiguous(
                "operator decline row identity or head is missing or malformed"
            ) from exc
        if (
            identity["userScopeHash"] != scope
            or identity["rowId"] != row_id
            or identity["clientId"] != binding["clientId"]
            or head["userScopeHash"] != scope
            or head["rowId"] != row_id
            or head["createdAt"] != identity["createdAt"]
            or binding["createdAt"] < identity["createdAt"]
        ):
            raise RowAuthorityConflict(
                "operator decline identity, binding, and head do not correlate"
            )
        if stored_action is None and action["issuedAt"] < head["updatedAt"]:
            raise RowAuthorityConflict(
                "operator action predates a current row head"
            )
        bounded_history = None
        if "latestSettlements" in state:
            bounded_history = _validate_bounded_row_history(
                scope=scope,
                row_id=row_id,
                head=head,
                row_state=state,
            )
            current_generation = bounded_history["currentGeneration"]
            current_claim = bounded_history["currentClaimSet"]
            current_settlement = bounded_history["currentSettlement"]
            owner_lineage = None
            action_matches = _validate_bounded_operator_action_matches(
                scope=scope,
                row_id=row_id,
                action=action,
                row_state=state,
            )
        else:
            (
                current_generation,
                current_claim,
                current_settlement,
                owner_lineage,
            ) = _validate_current_owner_state(
                scope=scope,
                row_id=row_id,
                head=head,
                row_state=state,
            )
            action_matches = tuple(
                entry
                for entry in owner_lineage
                if entry["settlement"] is not None
                and entry["settlement"]["operatorActionHash"]
                == action["operatorActionHash"]
            )
        validated_states.append(
            {
                **state,
                "identity": identity,
                "head": head,
                "currentGeneration": current_generation,
                "currentClaimSet": current_claim,
                "currentSettlement": current_settlement,
                "ownerLineage": owner_lineage,
                "boundedHistory": bounded_history,
                "actionSettlementMatches": action_matches,
            }
        )
    if all(state["boundedHistory"] is None for state in validated_states):
        _validate_complete_accepted_claim_cohorts(
            row_states=validated_states
        )
    elif all(
        state["boundedHistory"] is not None for state in validated_states
    ):
        _validate_bounded_current_claim_cohorts(
            row_states=validated_states
        )
    else:
        raise RowAuthorityAmbiguous(
            "operator decline mixes bounded and legacy row history"
        )

    matching_action_settlements = [
        state["actionSettlementMatches"] for state in validated_states
    ]
    if stored_action is None and any(matching_action_settlements):
        raise RowAuthorityAmbiguous(
            "operator decline artifacts exist without their immutable action"
        )
    if stored_action is None and stored_claim_set_document is not None:
        raise RowAuthorityAmbiguous(
            "operator decline claim exists without its immutable action"
        )

    lease_until = _operator_decline_lease_until(action["issuedAt"])
    if stored_action is not None:
        if stored_claim_set_document is not None:
            claim_plan = _plan_operator_row_claim(
                user_scope_hash=scope,
                operator_action_document=action,
                thread_binding_document=binding,
                row_states=validated_states,
                stored_claim_set_document=stored_claim_set_document,
                created_at=action["issuedAt"],
                lease_owner_hash=action["actorScopeHash"],
                lease_until=lease_until,
            )
            if claim_plan["disposition"] != "already_applied":
                raise RowAuthorityAmbiguous(
                    "stored operator claim is not a complete replay"
                )
            settlements = []
            if claim_plan["claimSet"]["outcome"] == "accepted":
                for state, generation in zip(
                    validated_states,
                    claim_plan["generations"],
                ):
                    if state["boundedHistory"] is None:
                        entry = state["ownerLineage"][
                            generation["generation"] - 1
                        ]
                        settlement = entry["settlement"]
                    else:
                        settlement_document = state.get(
                            "candidateSettlement"
                        )
                        if settlement_document is None:
                            raise RowAuthorityAmbiguous(
                                "accepted operator decline lacks its settlement"
                            )
                        settlement = _validate_correlated_owner_settlement(
                            scope=scope,
                            row_id=state["rowId"],
                            generation=generation,
                            claim=claim_plan["claimSet"],
                            settlement_document=settlement_document,
                        )
                    if settlement is None:
                        raise RowAuthorityAmbiguous(
                            "accepted operator decline lacks its settlement"
                        )
                    if (
                        settlement["outcome"] != "human_declined"
                        or settlement["operatorActionHash"]
                        != action["operatorActionHash"]
                        or settlement["settledAt"] != action["issuedAt"]
                    ):
                        raise RowAuthorityConflict(
                            "operator decline settlement differs from its action"
                        )
                    settlements.append(settlement)
            return {
                "disposition": "already_applied",
                "action": action,
                "claimSet": claim_plan["claimSet"],
                "generations": claim_plan["generations"],
                "settlements": tuple(settlements),
                "heads": tuple(
                    state["head"] for state in validated_states
                ),
                "mutations": (),
            }

        replay_entries = []
        for matches in matching_action_settlements:
            if len(matches) != 1:
                raise RowAuthorityAmbiguous(
                    "pending operator decline replay is incomplete"
                )
            replay_entries.append(matches[0])
        claims = [entry["claimSet"] for entry in replay_entries]
        if (
            any(claim != claims[0] for claim in claims[1:])
            or claims[0]["rowBindingsHash"]
            != binding["rowBindingsHash"]
        ):
            raise RowAuthorityConflict(
                "pending operator decline spans different claim cohorts"
            )
        generations = []
        settlements = []
        for entry in replay_entries:
            generation = entry["generation"]
            settlement = entry["settlement"]
            if (
                generation["ownerKind"] != "human_decision"
                or settlement["outcome"] != "human_declined"
                or settlement["settledAt"] != action["issuedAt"]
            ):
                raise RowAuthorityConflict(
                    "pending operator decline replay differs from its action"
                )
            generations.append(generation)
            settlements.append(settlement)
        return {
            "disposition": "already_applied",
            "action": action,
            "claimSet": claims[0],
            "generations": tuple(generations),
            "settlements": tuple(settlements),
            "heads": tuple(state["head"] for state in validated_states),
            "mutations": (),
        }

    pending = [
        state["head"]["state"] == "review_pending"
        and state["head"]["effectiveOwnerKind"] == "human_decision"
        for state in validated_states
    ]
    if any(pending) and not all(pending):
        raise RowAuthorityConflict(
            "operator decline cannot mix pending and nonpending rows"
        )
    if all(pending):
        claims = [state["currentClaimSet"] for state in validated_states]
        if (
            any(claim != claims[0] for claim in claims[1:])
            or claims[0]["rowBindingsHash"] != binding["rowBindingsHash"]
        ):
            raise RowAuthorityConflict(
                "pending operator decline does not match one bound claim cohort"
            )
        planned_writes = 1 + (2 * len(validated_states))
        _require_row_authority_planned_writes(planned_writes)
        generations = []
        settlements = []
        heads = []
        mutations = [
            {
                "target": "action",
                "operation": "create",
                "document": action,
            }
        ]
        for state in validated_states:
            prior_effective_settlement = None
            predecessor_hash = state["currentGeneration"][
                "predecessorSettlementHash"
            ]
            if predecessor_hash is not None:
                if state["boundedHistory"] is None:
                    candidates = [
                        entry["settlement"]
                        for entry in state["ownerLineage"]
                        if entry["settlement"] is not None
                        and entry["settlement"]["settlementHash"]
                        == predecessor_hash
                    ]
                else:
                    candidates = [
                        entry["settlement"]
                        for entry in state["boundedHistory"][
                            "latestSettlementAuthorities"
                        ]
                        if entry["settlement"]["settlementHash"]
                        == predecessor_hash
                    ]
                    restored = state["boundedHistory"].get(
                        "restoredAuthority"
                    )
                    if (
                        restored is not None
                        and restored["settlement"]["settlementHash"]
                        == predecessor_hash
                    ):
                        candidates.append(restored["settlement"])
                unique_candidates = {}
                for candidate in candidates:
                    candidate_hash = candidate["settlementHash"]
                    existing = unique_candidates.get(candidate_hash)
                    if existing is not None and existing != candidate:
                        raise RowAuthorityConflict(
                            "pending operator predecessor settlement copies diverge"
                        )
                    unique_candidates[candidate_hash] = candidate
                candidates = list(unique_candidates.values())
                if len(candidates) != 1:
                    raise RowAuthorityAmbiguous(
                        "pending operator decline lacks its exact predecessor settlement"
                    )
                prior_effective_settlement = candidates[0]
            settlement_plan = _plan_owner_generation_settlement(
                user_scope_hash=scope,
                row_id=state["rowId"],
                expected_head=state["head"],
                actual_head_document=state["head"],
                identity_document=state["identity"],
                generation_document=state["currentGeneration"],
                claim_set_document=state["currentClaimSet"],
                stored_settlement_document=None,
                prior_effective_settlement_document=(
                    prior_effective_settlement
                ),
                settled_at=action["issuedAt"],
                operator_action_document=action,
                actual_owner_lineage=state["ownerLineage"],
                actual_bounded_history=state["boundedHistory"],
            )
            generations.append(settlement_plan["generation"])
            settlements.append(settlement_plan["settlement"])
            heads.append(settlement_plan["head"])
            mutations.extend(
                (
                    {
                        "target": f"settlement:{state['rowId']}",
                        "operation": "create",
                        "document": settlement_plan["settlement"],
                    },
                    {
                        "target": f"head:{state['rowId']}",
                        "operation": "set",
                        "document": settlement_plan["head"],
                    },
                )
            )
        if len(mutations) != planned_writes:
            raise RowAuthorityConfigError(
                "pending decline planned-write count drifted"
            )
        return {
            "disposition": "declined",
            "action": action,
            "claimSet": claims[0],
            "generations": tuple(generations),
            "settlements": tuple(settlements),
            "heads": tuple(heads),
            "mutations": tuple(mutations),
        }

    claim_plan = _plan_operator_row_claim(
        user_scope_hash=scope,
        operator_action_document=action,
        thread_binding_document=binding,
        row_states=validated_states,
        stored_claim_set_document=None,
        created_at=action["issuedAt"],
        lease_owner_hash=action["actorScopeHash"],
        lease_until=lease_until,
    )
    if claim_plan["disposition"] == "dominated":
        mutations = (
            {
                "target": "action",
                "operation": "create",
                "document": action,
            },
            *claim_plan["mutations"],
        )
        if len(mutations) != claim_plan["claimSet"]["plannedWrites"]:
            raise RowAuthorityConfigError(
                "dominated decline planned-write count drifted"
            )
        return {
            "disposition": "dominated",
            "action": action,
            "claimSet": claim_plan["claimSet"],
            "generations": (),
            "settlements": (),
            "heads": tuple(state["head"] for state in validated_states),
            "mutations": mutations,
        }
    if claim_plan["disposition"] != "created":
        raise RowAuthorityConfigError(
            "new operator decline claim has an unsupported disposition"
        )
    mutations = [
        {
            "target": "action",
            "operation": "create",
            "document": action,
        },
        {
            "target": "claim_set",
            "operation": "create",
            "document": claim_plan["claimSet"],
        },
    ]
    settlements = []
    heads = []
    for state, generation in zip(
        validated_states,
        claim_plan["generations"],
    ):
        settlement = build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim_plan["claimSet"],
            fencing_token=generation["firstFencingToken"],
            outcome="human_declined",
            settled_at=action["issuedAt"],
            operator_action_document=action,
        )
        head = _build_immediate_operator_decline_head(
            expected_head=state["head"],
            generation_document=generation,
            settlement_document=settlement,
            operator_action_document=action,
            expected_generation=(
                state["boundedHistory"]["nextGeneration"]
                if state["boundedHistory"] is not None
                else None
            ),
            expected_first_fencing_token=(
                state["boundedHistory"]["nextFirstFencingToken"]
                if state["boundedHistory"] is not None
                else None
            ),
        )
        settlements.append(settlement)
        heads.append(head)
        mutations.extend(
            (
                {
                    "target": f"generation:{state['rowId']}",
                    "operation": "create",
                    "document": generation,
                },
                {
                    "target": f"settlement:{state['rowId']}",
                    "operation": "create",
                    "document": settlement,
                },
                {
                    "target": f"head:{state['rowId']}",
                    "operation": "set",
                    "document": head,
                },
            )
        )
    if len(mutations) != claim_plan["claimSet"]["plannedWrites"]:
        raise RowAuthorityConfigError(
            "accepted decline planned-write count drifted"
        )
    return {
        "disposition": "declined",
        "action": action,
        "claimSet": claim_plan["claimSet"],
        "generations": claim_plan["generations"],
        "settlements": tuple(settlements),
        "heads": tuple(heads),
        "mutations": tuple(mutations),
    }


def _initialization_result(*, disposition, identity, revision, head):
    if disposition not in {"created", "existing"}:
        raise RowAuthorityConfigError(
            "initialization disposition is not approved"
        )
    return {
        "disposition": disposition,
        "identity": dict(identity),
        "locationRevision": dict(revision),
        "authorityHead": dict(head),
    }


def _location_result(*, disposition, identity, revision, head):
    if disposition not in {"advanced", "unchanged", "already_applied"}:
        raise RowAuthorityConfigError(
            "location disposition is not approved"
        )
    return {
        "disposition": disposition,
        "identity": dict(identity),
        "locationRevision": dict(revision),
        "authorityHead": dict(head),
    }


def _thread_binding_result(*, disposition, thread_binding, reverse_bindings):
    if disposition not in {"created", "already_applied"}:
        raise RowAuthorityConfigError(
            "thread binding disposition is not approved"
        )
    binding = validate_thread_row_binding_document(document=thread_binding)
    reverse = [
        validate_row_thread_binding_document(document=document)
        for document in reverse_bindings
    ]
    return {
        "disposition": disposition,
        "threadBinding": binding,
        "reverseBindings": reverse,
    }


def _contact_association_result(
    *,
    disposition,
    association,
    evidence,
    binding_head,
):
    if disposition not in {
        "created",
        "evidence_created",
        "already_applied",
    }:
        raise RowAuthorityConfigError(
            "contact association disposition is not approved"
        )
    return {
        "disposition": disposition,
        "association": validate_contact_row_binding_document(
            document=association
        ),
        "evidence": validate_contact_row_binding_evidence_document(
            document=evidence
        ),
        "bindingHead": validate_contact_row_binding_head_document(
            document=binding_head
        ),
    }


def _claim_result(
    *,
    disposition,
    claim_set,
    generations,
    heads,
    predecessor_settlements,
):
    if disposition not in {"created", "dominated", "already_applied"}:
        raise RowAuthorityConfigError("claim disposition is not approved")
    claim = validate_claim_set_document(document=claim_set)
    checked_generations = [
        validate_owner_generation_document(document=document)
        for document in generations
    ]
    checked_heads = [
        validate_row_authority_head(document=document) for document in heads
    ]
    checked_settlements = [
        validate_owner_settlement_document(document=document)
        for document in predecessor_settlements
    ]
    if [head["rowId"] for head in checked_heads] != [
        binding["rowId"] for binding in claim["rowBindings"]
    ]:
        raise RowAuthorityConfigError("claim result heads are not canonical")
    if disposition == "dominated" and (
        claim["outcome"] != "dominated"
        or checked_generations
        or checked_settlements
    ):
        raise RowAuthorityConfigError("dominated claim result is malformed")
    if disposition == "created" and (
        claim["outcome"] != "accepted"
        or len(checked_generations) != claim["bindingCount"]
    ):
        raise RowAuthorityConfigError("created claim result is malformed")
    return {
        "disposition": disposition,
        "claimSet": claim,
        "generations": checked_generations,
        "heads": checked_heads,
        "predecessorSettlements": checked_settlements,
    }


def _operator_decline_result(
    *,
    disposition,
    action,
    claim_set,
    generations,
    settlements,
    heads,
):
    if disposition not in {
        "declined",
        "dominated",
        "already_applied",
    }:
        raise RowAuthorityConfigError(
            "operator decline disposition is not approved"
        )
    checked_action = validate_operator_action_document(document=action)
    checked_claim = validate_claim_set_document(document=claim_set)
    checked_generations = [
        validate_owner_generation_document(document=document)
        for document in generations
    ]
    checked_settlements = [
        validate_owner_settlement_document(document=document)
        for document in settlements
    ]
    checked_heads = [
        validate_row_authority_head(document=document) for document in heads
    ]
    if [head["rowId"] for head in checked_heads] != [
        binding["rowId"] for binding in checked_claim["rowBindings"]
    ]:
        raise RowAuthorityConfigError(
            "operator decline result heads are not canonical"
        )
    if disposition == "dominated" and (
        checked_claim["outcome"] != "dominated"
        or checked_generations
        or checked_settlements
    ):
        raise RowAuthorityConfigError(
            "dominated operator decline result is malformed"
        )
    if checked_claim["authorityOrigin"] == "authenticated_operator" and (
        checked_claim["operatorActionHash"]
        != checked_action["operatorActionHash"]
    ):
        raise RowAuthorityConfigError(
            "operator decline claim differs from its action"
        )
    if checked_settlements:
        if (
            len(checked_generations) != len(checked_settlements)
            or len(checked_heads) != len(checked_settlements)
        ):
            raise RowAuthorityConfigError(
                "operator decline result cohorts are incomplete"
            )
        for generation, settlement in zip(
            checked_generations,
            checked_settlements,
        ):
            if (
                settlement["generationHash"]
                != generation["generationHash"]
                or settlement["outcome"] != "human_declined"
                or settlement["operatorActionHash"]
                != checked_action["operatorActionHash"]
            ):
                raise RowAuthorityConfigError(
                    "operator decline settlement differs from its generation or action"
                )
    return {
        "disposition": disposition,
        "action": checked_action,
        "claimSet": checked_claim,
        "generations": checked_generations,
        "settlements": checked_settlements,
        "heads": checked_heads,
    }


def _lease_takeover_result(*, disposition, generation, head):
    if disposition not in {"taken_over", "already_applied"}:
        raise RowAuthorityConfigError(
            "lease takeover disposition is not approved"
        )
    checked_generation = validate_owner_generation_document(
        document=generation
    )
    checked_head = validate_row_authority_head(document=head)
    if (
        checked_head["userScopeHash"]
        != checked_generation["userScopeHash"]
        or checked_head["rowId"] != checked_generation["rowId"]
        or checked_head["effectiveOwnerGeneration"]
        != checked_generation["generation"]
        or checked_head["effectiveOwnerGenerationHash"]
        != checked_generation["generationHash"]
        or checked_head["effectiveOwnerKind"]
        != checked_generation["ownerKind"]
        or checked_head["effectivePriority"]
        != checked_generation["priority"]
        or checked_head["state"] not in {"claimed", "review_pending"}
        or checked_head["fencingToken"]
        < checked_generation["firstFencingToken"]
    ):
        raise RowAuthorityConfigError(
            "lease takeover result does not match its generation"
        )
    return {
        "disposition": disposition,
        "generation": checked_generation,
        "head": checked_head,
    }


def _owner_settlement_result(
    *,
    disposition,
    generation,
    settlement,
    head,
    higher_generation_proven=False,
    release_restoration_proven=False,
):
    if disposition not in {"settled", "already_applied"}:
        raise RowAuthorityConfigError(
            "owner settlement disposition is not approved"
        )
    checked_generation = validate_owner_generation_document(
        document=generation
    )
    checked_settlement = validate_owner_settlement_document(
        document=settlement
    )
    checked_head = validate_row_authority_head(document=head)
    base_correlates = (
        checked_settlement["userScopeHash"]
        == checked_generation["userScopeHash"]
        and checked_settlement["rowId"] == checked_generation["rowId"]
        and checked_settlement["generation"]
        == checked_generation["generation"]
        and checked_settlement["generationHash"]
        == checked_generation["generationHash"]
        and checked_head["userScopeHash"]
        == checked_generation["userScopeHash"]
        and checked_head["rowId"] == checked_generation["rowId"]
        and checked_head["updatedAt"] >= checked_settlement["settledAt"]
    )
    same_generation = (
        checked_head["effectiveOwnerGeneration"]
        == checked_generation["generation"]
        and checked_head["effectiveOwnerGenerationHash"]
        == checked_generation["generationHash"]
        and checked_head["state"] == "settled"
        and checked_head["fencingToken"]
        == checked_settlement["fencingToken"]
        and checked_head["latestSettlementHash"]
        == checked_settlement["settlementHash"]
        and checked_head["effectiveSettlementHash"]
        == checked_settlement["settlementHash"]
    )
    higher_generation = (
        higher_generation_proven is True
        and
        checked_head["effectiveOwnerGeneration"] is not None
        and checked_head["effectiveOwnerGeneration"]
        > checked_generation["generation"]
        and checked_head["effectivePriority"] > checked_generation["priority"]
        and checked_head["fencingToken"]
        > checked_settlement["fencingToken"]
    )
    release_restored = (
        release_restoration_proven is True
        and checked_head["effectiveOwnerGeneration"]
        == checked_generation["generation"]
        and checked_head["effectiveOwnerGenerationHash"]
        == checked_generation["generationHash"]
        and checked_head["state"] == "settled"
        and checked_head["fencingToken"]
        == checked_settlement["fencingToken"]
        and checked_head["effectiveSettlementHash"]
        == checked_settlement["settlementHash"]
        and checked_head["latestSettlementHash"]
        != checked_settlement["settlementHash"]
        and checked_head["latestOptOutReleaseResultHash"] is not None
    )
    if not base_correlates or not (
        same_generation or higher_generation or release_restored
    ):
        raise RowAuthorityConfigError(
            "owner settlement result does not correlate"
        )
    return {
        "disposition": disposition,
        "generation": checked_generation,
        "settlement": checked_settlement,
        "head": checked_head,
    }


def _source_settlement_link_result(
    *,
    disposition,
    source_settlement_link,
    head,
    later_link_proven=False,
):
    if disposition not in {"linked", "already_applied"}:
        raise RowAuthorityConfigError(
            "source settlement link disposition is not approved"
        )
    link = validate_source_settlement_link_document(
        document=source_settlement_link
    )
    checked_head = validate_row_authority_head(document=head)
    pointer_matches = (
        checked_head["latestSourceSettlementLinkHash"]
        == link["sourceSettlementLinkHash"]
    )
    if (
        type(later_link_proven) is not bool
        or checked_head["userScopeHash"] != link["userScopeHash"]
        or checked_head["rowId"] != link["rowId"]
        or checked_head["latestSourceSettlementLinkHash"] is None
        or checked_head["updatedAt"] < link["linkedAt"]
        or (
            disposition == "linked"
            and not pointer_matches
        )
        or (
            disposition == "already_applied"
            and not pointer_matches
            and later_link_proven is not True
        )
    ):
        raise RowAuthorityConfigError(
            "source settlement link result does not correlate"
        )
    return {
        "disposition": disposition,
        "sourceSettlementLink": link,
        "head": checked_head,
    }


def _location_semantics(revision):
    return tuple(
        revision[field]
        for field in (
            "lifecycle",
            "spreadsheetId",
            "sheetId",
            "providerRowIndex",
            "displayRowNumber",
            "metadataId",
            "markerHash",
            "rowSnapshotHash",
            "observationEvidenceHash",
        )
    )


class RowAuthorityStore:
    def __init__(self, firestore, *, transaction_executor):
        if firestore is None:
            raise RowAuthorityConfigError("firestore dependency is required")
        if not callable(getattr(firestore, "collection", None)):
            raise RowAuthorityConfigError(
                "firestore dependency must expose collection"
            )
        if not callable(getattr(firestore, "transaction", None)):
            raise RowAuthorityConfigError(
                "firestore dependency must expose transaction"
            )
        if not callable(transaction_executor):
            raise RowAuthorityConfigError(
                "transaction_executor dependency must be callable"
            )
        self._firestore = firestore
        self._transaction_executor = transaction_executor

    def _initialization_references(self, *, verified_user_id, row_id):
        try:
            user_ref = self._firestore.collection("users").document(
                verified_user_id
            )
            return (
                user_ref.collection("rowIdentities").document(row_id),
                user_ref.collection("rowLocationRevisions").document(
                    f"{row_id}--1"
                ),
                user_ref.collection("rowAuthorityHeads").document(row_id),
            )
        except Exception as exc:
            raise RowAuthorityConfigError(
                "verified user ID or row ID cannot form exact document paths"
            ) from exc

    def _location_references(
        self,
        *,
        verified_user_id,
        row_id,
        previous_revision,
        candidate_revision,
    ):
        try:
            user_ref = self._firestore.collection("users").document(
                verified_user_id
            )
            revisions = user_ref.collection("rowLocationRevisions")
            return (
                user_ref.collection("rowIdentities").document(row_id),
                user_ref.collection("rowAuthorityHeads").document(row_id),
                revisions.document(f"{row_id}--{previous_revision}"),
                revisions.document(f"{row_id}--{candidate_revision}"),
            )
        except Exception as exc:
            raise RowAuthorityConfigError(
                "verified user ID or row ID cannot form exact document paths"
            ) from exc

    def _lease_takeover_references(
        self,
        *,
        verified_user_id,
        row_id,
        generation,
    ):
        try:
            user_ref = self._firestore.collection("users").document(
                verified_user_id
            )
            generation_id = _generation_document_id(
                row_id=row_id,
                generation=generation,
            )
            return (
                user_ref.collection("rowIdentities").document(row_id),
                user_ref.collection("rowAuthorityHeads").document(row_id),
                user_ref.collection("rowOwnerGenerations").document(
                    generation_id
                ),
                user_ref.collection("rowOwnerSettlements").document(
                    generation_id
                ),
            )
        except Exception as exc:
            raise RowAuthorityConfigError(
                "lease authority cannot form exact document paths"
            ) from exc

    def _thread_binding_references(
        self,
        *,
        verified_user_id,
        thread_binding,
        reverse_bindings,
    ):
        try:
            user_ref = self._firestore.collection("users").document(
                verified_user_id
            )
            binding_ref = user_ref.collection("threadRowBindings").document(
                thread_binding["threadId"]
            )
            row_references = tuple(
                (
                    user_ref.collection("rowIdentities").document(
                        row_binding["rowId"]
                    ),
                    user_ref.collection("rowAuthorityHeads").document(
                        row_binding["rowId"]
                    ),
                )
                for row_binding in thread_binding["rowBindings"]
            )
            edge_references = tuple(
                user_ref.collection("rowThreadBindings").document(
                    reverse_binding["edgeId"]
                )
                for reverse_binding in reverse_bindings
            )
            return binding_ref, row_references, edge_references
        except Exception as exc:
            raise RowAuthorityConfigError(
                "thread and row bindings cannot form exact document paths"
            ) from exc

    @staticmethod
    def _read_reference_payloads(references, *, transaction=None):
        snapshots = []
        for reference in references:
            if transaction is None:
                snapshots.append(reference.get())
            else:
                snapshots.append(reference.get(transaction=transaction))
        return tuple(
            (
                bool(snapshot.exists),
                snapshot.to_dict() if snapshot.exists else None,
            )
            for snapshot in snapshots
        )

    def initialize_row_identity(
        self,
        *,
        verified_user_id,
        client_id,
        spreadsheet_id,
        marker_observation,
        headers,
        cells,
        lifecycle,
        creation_kind,
        creation_source_hash,
        created_at,
    ):
        planned_writes = 3
        if planned_writes > MAX_ROW_AUTHORITY_PLANNED_WRITES:
            raise RowAuthorityConfigError(
                "row initialization exceeds the planned-write ceiling"
            )
        if type(lifecycle) is not str or lifecycle not in {
            "active",
            "nonviable",
        }:
            raise RowAuthorityConfigError(
                "initial row lifecycle must be active or nonviable"
            )
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        marker = _require_exact_dict(
            marker_observation,
            keys=_MARKER_OBSERVATION_KEYS,
            field_name="marker_observation",
        )
        checked_row_id = validate_row_id(marker["rowId"])
        checked_sheet_id = _require_uint(
            marker["sheetId"],
            field_name="marker_observation.sheetId",
        )
        identity = build_row_identity_document(
            user_scope_hash=checked_scope,
            row_id=checked_row_id,
            client_id=client_id,
            spreadsheet_id=spreadsheet_id,
            sheet_id=checked_sheet_id,
            creation_kind=creation_kind,
            creation_source_hash=creation_source_hash,
            created_at=created_at,
        )
        observation = build_row_observation(
            spreadsheet_id=spreadsheet_id,
            marker_observation=marker,
            ordered_headers=headers,
            ordered_cell_values=cells,
            user_scope_hash=checked_scope,
        )
        revision = build_row_location_revision_document(
            identity_document=identity,
            revision=1,
            lifecycle=lifecycle,
            observations=(observation,),
            previous_revision_hash=None,
            observed_at=created_at,
        )
        head = build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=created_at,
        )
        references = self._initialization_references(
            verified_user_id=checked_user_id,
            row_id=checked_row_id,
        )
        expected_documents = (identity, revision, head)
        callback_state = {
            "entered": False,
            "prepared": False,
            "existing": False,
            "rejected": False,
        }

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "existing": False,
                    "rejected": False,
                }
            )
            observed = self._read_reference_payloads(
                references,
                transaction=transaction,
            )
            if all(not exists for exists, _payload in observed):
                callback_state["prepared"] = True
                for reference, document in zip(
                    references,
                    expected_documents,
                ):
                    transaction.create(reference, document)
                return "created"
            if all(exists for exists, _payload in observed) and tuple(
                payload for _exists, payload in observed
            ) == expected_documents:
                callback_state["existing"] = True
                return "existing"
            callback_state["rejected"] = True
            raise RowAuthorityAmbiguous(
                "row identity initialization found partial or drifted state"
            )

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "row identity transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "row identity transaction could not start"
                ) from exc
            try:
                readback = self._read_reference_payloads(references)
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "row identity commit outcome cannot be read back"
                ) from readback_exc
            if all(exists for exists, _payload in readback) and tuple(
                payload for _exists, payload in readback
            ) == expected_documents:
                disposition = (
                    "created" if callback_state["prepared"] else "existing"
                )
            elif all(not exists for exists, _payload in readback):
                raise RowAuthorityRetryable(
                    "row identity commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "row identity commit readback is partial or drifted"
                ) from exc
        if disposition not in {"created", "existing"}:
            raise RowAuthorityRetryable(
                "row identity transaction returned no approved disposition"
            )
        if disposition == "created" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "row identity transaction reported an unprepared create"
            )
        if disposition == "existing" and not callback_state["existing"]:
            raise RowAuthorityRetryable(
                "row identity transaction reported an unobserved existing state"
            )
        return _initialization_result(
            disposition=disposition,
            identity=identity,
            revision=revision,
            head=head,
        )

    def bind_thread_rows(
        self,
        *,
        verified_user_id,
        thread_id,
        client_id,
        row_ids,
        primary_row_id,
        created_at,
    ):
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        thread_binding = build_thread_row_binding_document(
            user_scope_hash=checked_scope,
            thread_id=thread_id,
            client_id=client_id,
            row_ids=row_ids,
            primary_row_id=primary_row_id,
            created_at=created_at,
        )
        reverse_bindings = build_row_thread_binding_documents(
            thread_binding_document=thread_binding
        )
        planned_writes = 1 + len(reverse_bindings)
        if planned_writes > MAX_ROW_AUTHORITY_PLANNED_WRITES:
            raise RowAuthorityConfigError(
                "thread binding exceeds the planned-write ceiling"
            )
        binding_ref, row_references, edge_references = (
            self._thread_binding_references(
                verified_user_id=checked_user_id,
                thread_binding=thread_binding,
                reverse_bindings=reverse_bindings,
            )
        )
        references = (
            binding_ref,
            *(
                reference
                for identity_and_head in row_references
                for reference in identity_and_head
            ),
            *edge_references,
        )
        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "observed": None,
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "observed": None,
                }
            )
            try:
                observed = self._read_reference_payloads(
                    references,
                    transaction=transaction,
                )
            except Exception as exc:
                callback_state["read_failed"] = True
                raise RowAuthorityRetryable(
                    "thread binding transaction read failed before writes"
                ) from exc
            callback_state["observed"] = observed

            row_observed_end = 1 + (2 * len(row_references))
            prerequisite_observed = observed[1:row_observed_end]
            edge_observed = observed[row_observed_end:]
            validated_prerequisites = []
            for index, row_binding in enumerate(
                thread_binding["rowBindings"]
            ):
                identity_exists, identity_payload = prerequisite_observed[
                    2 * index
                ]
                head_exists, head_payload = prerequisite_observed[
                    (2 * index) + 1
                ]
                if not identity_exists or not head_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "thread binding is missing row identity or head"
                        )
                    )
                try:
                    identity = validate_row_identity_document(
                        document=identity_payload
                    )
                    head = validate_row_authority_head(document=head_payload)
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "thread binding row identity or head is malformed"
                        )
                    )
                if (
                    identity["userScopeHash"] != checked_scope
                    or identity["rowId"] != row_binding["rowId"]
                    or identity["clientId"] != thread_binding["clientId"]
                    or head["userScopeHash"] != checked_scope
                    or head["rowId"] != row_binding["rowId"]
                    or head["createdAt"] != identity["createdAt"]
                ):
                    reject(
                        RowAuthorityConflict(
                            "thread binding row authority does not correlate"
                        )
                    )
                validated_prerequisites.append((identity, head))

            if any(
                thread_binding["createdAt"] < identity["createdAt"]
                for identity, _head in validated_prerequisites
            ):
                reject(
                    RowAuthorityConflict(
                        "thread binding predates immutable row identity"
                    )
                )

            target_presence = (observed[0][0],) + tuple(
                exists for exists, _payload in edge_observed
            )
            if all(target_presence):
                try:
                    stored_binding = validate_thread_row_binding_document(
                        document=observed[0][1]
                    )
                    stored_edges = tuple(
                        validate_row_thread_binding_document(document=payload)
                        for _exists, payload in edge_observed
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityConflict(
                            "stored thread binding contains immutable drift"
                        )
                    )
                if stored_binding != thread_binding or stored_edges != tuple(
                    reverse_bindings
                ):
                    reject(
                        RowAuthorityConflict(
                            "stored thread binding differs from the proposal"
                        )
                    )
                callback_state["disposition"] = "already_applied"
                return "already_applied"

            if any(target_presence):
                if observed[0][0]:
                    try:
                        stored_binding = validate_thread_row_binding_document(
                            document=observed[0][1]
                        )
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "stored thread binding contains immutable drift"
                            )
                        )
                    if stored_binding != thread_binding:
                        reject(
                            RowAuthorityConflict(
                                "stored thread binding differs from the proposal"
                            )
                        )
                for expected_edge, (exists, payload) in zip(
                    reverse_bindings,
                    edge_observed,
                ):
                    if not exists:
                        continue
                    try:
                        stored_edge = validate_row_thread_binding_document(
                            document=payload
                        )
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "stored reverse binding contains immutable drift"
                            )
                        )
                    if stored_edge != expected_edge:
                        reject(
                            RowAuthorityConflict(
                                "stored reverse binding differs from the proposal"
                            )
                        )
                reject(
                    RowAuthorityAmbiguous(
                        "thread binding is only partially present"
                    )
                )

            for _identity, head in validated_prerequisites:
                if thread_binding["createdAt"] < head["updatedAt"]:
                    reject(
                        RowAuthorityConflict(
                            "thread binding predates row authority"
                        )
                    )
            callback_state["prepared"] = True
            callback_state["disposition"] = "created"
            transaction.create(binding_ref, thread_binding)
            for reference, document in zip(
                edge_references,
                reverse_bindings,
            ):
                transaction.create(reference, document)
            return "created"

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "thread binding transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "thread binding transaction could not start"
                ) from exc
            try:
                readback = self._read_reference_payloads(references)
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "thread binding commit outcome cannot be read back"
                ) from readback_exc
            observed = callback_state["observed"]
            if observed is None:
                raise RowAuthorityAmbiguous(
                    "thread binding commit has no complete before-image"
                ) from exc
            row_observed_end = 1 + (2 * len(row_references))
            expected_after = (
                (True, thread_binding),
                *observed[1:row_observed_end],
                *((True, document) for document in reverse_bindings),
            )
            exact_before = readback == observed
            exact_after = readback == expected_after
            if (
                exact_after
                and callback_state["disposition"] == "already_applied"
            ):
                disposition = "already_applied"
            elif exact_after and callback_state["prepared"]:
                disposition = "created"
            elif exact_before and callback_state["prepared"]:
                raise RowAuthorityRetryable(
                    "thread binding commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "thread binding commit readback is partial or drifted"
                ) from exc
        if disposition not in {"created", "already_applied"}:
            raise RowAuthorityRetryable(
                "thread binding transaction returned no approved disposition"
            )
        if disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "thread binding transaction returned a mismatched disposition"
            )
        if disposition == "created" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "thread binding transaction reported an unprepared create"
            )
        return _thread_binding_result(
            disposition=disposition,
            thread_binding=thread_binding,
            reverse_bindings=reverse_bindings,
        )

    def claim_row_set(
        self,
        *,
        verified_user_id,
        canonical_source_id,
        work_key,
        created_at,
        lease_owner_hash,
        lease_until,
    ):
        _require_row_authority_planned_writes(
            1 + (3 * MAX_ROW_BINDINGS)
        )
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_source_id = _require_firestore_document_id(
            canonical_source_id,
            field_name="canonical_source_id",
        )
        _require_opaque(
            checked_source_id,
            field_name="canonical_source_id",
        )
        checked_work_key = _require_sha256(work_key, field_name="work_key")
        checked_created_at = _require_timestamp(
            created_at,
            field_name="created_at",
        )
        created_datetime = _timestamp_as_datetime(
            checked_created_at,
            field_name="created_at",
        )
        checked_lease_owner = _require_sha256(
            lease_owner_hash,
            field_name="lease_owner_hash",
        )
        checked_lease_until = _require_timestamp(
            lease_until,
            field_name="lease_until",
        )
        if checked_lease_until <= checked_created_at:
            raise RowAuthorityConfigError(
                "claim lease must end after created_at"
            )
        checked_scope = user_scope_hash(checked_user_id)
        try:
            user_ref = self._firestore.collection("users").document(
                checked_user_id
            )
            b1_references = (
                user_ref.collection("sourceIdentities").document(
                    checked_source_id
                ),
                user_ref.collection("sourceClassifications").document(
                    checked_source_id
                ),
                user_ref.collection("sourceTransitionOwners").document(
                    checked_source_id
                ),
                user_ref.collection("sourceWorkLedgers").document(
                    checked_source_id
                ),
            )
        except Exception as exc:
            raise RowAuthorityConfigError(
                "B1 authority cannot form exact document paths"
            ) from exc

        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "plan": None,
            "references": {},
            "before": {},
            "ordered_paths": [],
            "mutation_references": {},
            "query_readbacks": [],
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "plan": None,
                    "references": {},
                    "before": {},
                    "ordered_paths": [],
                    "mutation_references": {},
                    "query_readbacks": [],
                }
            )

            def read(reference):
                path = reference.path
                if path in callback_state["before"]:
                    return callback_state["before"][path]
                try:
                    snapshot = reference.get(transaction=transaction)
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "claim transaction read failed before writes"
                    ) from exc
                observed = (
                    bool(snapshot.exists),
                    snapshot.to_dict() if snapshot.exists else None,
                )
                callback_state["references"][path] = reference
                callback_state["before"][path] = observed
                callback_state["ordered_paths"].append(path)
                return observed

            b1_observed = tuple(read(reference) for reference in b1_references)
            if not all(exists for exists, _payload in b1_observed):
                reject(
                    RowAuthorityAmbiguous(
                        "B1 claim authority bundle is incomplete"
                    )
                )
            (
                identity_document,
                classification_document,
                owner_document,
                ledger_document,
            ) = tuple(payload for _exists, payload in b1_observed)
            try:
                b1_identity = _validate_b1_source_identity(identity_document)
                if b1_identity["canonicalSourceId"] != checked_source_id:
                    raise RowAuthorityConfigError(
                        "B1 source identity does not match its document ID"
                    )
                b1_classification = _validate_b1_classification(
                    classification_document,
                    canonical_source_id=checked_source_id,
                )
                b1_owner = _validate_b1_owner(
                    owner_document,
                    canonical_source_id=checked_source_id,
                    classification=b1_classification,
                )
                b1_ledger = _validate_b1_ledger(
                    ledger_document,
                    canonical_source_id=checked_source_id,
                    classification=b1_classification,
                    owner=b1_owner,
                )
                authority_link = build_b1_authority_link(
                    user_scope_hash=checked_scope,
                    source_identity_document=b1_identity,
                    source_classification_document=b1_classification,
                    source_owner_document=b1_owner,
                    source_ledger_document=b1_ledger,
                    work_key=checked_work_key,
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "B1 claim authority bundle is malformed or drifted"
                    )
                )
            if authority_link["ownerKind"] == "contact_optout":
                reject(
                    RowAuthorityConflict(
                        "direct B1 contact opt-out claim is blocked until B2-C"
                    )
                )
            readiness = (
                b1_identity["createdAt"],
                b1_classification["snapshotPersistedAt"],
                b1_owner["createdAt"],
                b1_ledger["createdAt"],
            )
            if created_datetime < max(
                value.astimezone(timezone.utc) for value in readiness
            ):
                reject(
                    RowAuthorityConflict(
                        "claim predates immutable B1 authority readiness"
                    )
                )
            try:
                thread_id = _require_firestore_document_id(
                    b1_identity["threadId"],
                    field_name="B1 threadId",
                )
                thread_ref = user_ref.collection(
                    "threadRowBindings"
                ).document(thread_id)
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "B1 thread authority cannot form an exact path"
                    )
                )
            binding_exists, binding_payload = read(thread_ref)
            if not binding_exists:
                reject(
                    RowAuthorityAmbiguous(
                        "B1 claim is missing its stable thread binding"
                    )
                )
            try:
                thread_binding = validate_thread_row_binding_document(
                    document=binding_payload
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "B1 stable thread binding is malformed or drifted"
                    )
                )
            if (
                thread_binding["userScopeHash"] != checked_scope
                or thread_binding["threadId"] != thread_id
            ):
                reject(
                    RowAuthorityConflict(
                        "B1 source and stable thread binding do not correlate"
                    )
                )
            try:
                request_context = _derive_claim_request_context(
                    user_scope_hash=checked_scope,
                    authority_origin="b1_source",
                    authority_link=authority_link,
                    operator_action_document=None,
                    fanout_id=None,
                    thread_binding_document=thread_binding,
                )
                claim_ref = user_ref.collection("rowClaimSets").document(
                    request_context["requestId"]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "claim request identity cannot form an exact path"
                    )
                )
            claim_exists, claim_payload = read(claim_ref)
            stored_claim = None
            if claim_exists:
                try:
                    stored_claim = validate_claim_set_document(
                        document=claim_payload
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityConflict(
                            "stored claim set contains immutable drift"
                        )
                    )
                if stored_claim["requestId"] != request_context["requestId"]:
                    reject(
                        RowAuthorityConflict(
                            "stored claim set occupies the wrong request path"
                        )
                    )

            basic_states = []
            row_references = {}
            for row_binding in thread_binding["rowBindings"]:
                row_id = row_binding["rowId"]
                identity_ref = user_ref.collection("rowIdentities").document(
                    row_id
                )
                head_ref = user_ref.collection("rowAuthorityHeads").document(
                    row_id
                )
                identity_exists, row_identity = read(identity_ref)
                head_exists, row_head = read(head_ref)
                if not identity_exists or not head_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "claim is missing a bound row identity or head"
                        )
                    )
                basic_states.append(
                    {
                        "rowId": row_id,
                        "identity": row_identity,
                        "head": row_head,
                    }
                )
                row_references[row_id] = {
                    "identity": identity_ref,
                    "head": head_ref,
                }

            row_states = []
            for basic in basic_states:
                row_id = basic["rowId"]
                try:
                    preliminary_head = validate_row_authority_head(
                        document=basic["head"]
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "claim row head is malformed"
                        )
                    )
                try:
                    settlements_query = (
                        user_ref.collection("rowOwnerSettlements")
                        .where("rowId", "==", row_id)
                        .order_by("generation", direction="DESCENDING")
                        .limit(2)
                    )
                    settlement_snapshots = tuple(
                        transaction.get(settlements_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "claim bounded settlement query failed before writes"
                    ) from exc
                latest_settlements = []
                latest_settlement_authorities = []
                for settlement_snapshot in settlement_snapshots:
                    settlement_exists, settlement_payload = read(
                        settlement_snapshot.reference
                    )
                    if not settlement_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "claim bounded settlement query returned a missing document"
                            )
                        )
                    latest_settlements.append(
                        {
                            "path": settlement_snapshot.reference.path,
                            "document": settlement_payload,
                        }
                    )
                    settlement_generation = None
                    settlement_claim = None
                    try:
                        checked_settlement = (
                            validate_owner_settlement_document(
                                document=settlement_payload
                            )
                        )
                        settlement_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(
                            _generation_document_id(
                                row_id=row_id,
                                generation=checked_settlement["generation"],
                            )
                        )
                        (
                            settlement_generation_exists,
                            settlement_generation_payload,
                        ) = read(settlement_generation_ref)
                        if settlement_generation_exists:
                            settlement_generation = (
                                settlement_generation_payload
                            )
                            checked_settlement_generation = (
                                validate_owner_generation_document(
                                    document=settlement_generation_payload
                                )
                            )
                            settlement_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_settlement_generation["requestId"]
                            )
                            settlement_claim_exists, settlement_claim_payload = read(
                                settlement_claim_ref
                            )
                            if settlement_claim_exists:
                                settlement_claim = settlement_claim_payload
                    except RowAuthorityRetryable:
                        raise
                    except Exception:
                        pass
                    latest_settlement_authorities.append(
                        {
                            "generation": settlement_generation,
                            "claimSet": settlement_claim,
                        }
                    )
                callback_state["query_readbacks"].append(
                    {
                        "kind": "settlement_history",
                        "rowId": row_id,
                        "query": settlements_query,
                        "matches": tuple(
                            (
                                entry["path"],
                                _defensive_copy(entry["document"]),
                            )
                            for entry in latest_settlements
                        ),
                    }
                )
                latest_predecessor_release_matches = []
                latest_predecessor_restored_authority = None
                predecessor_release_hash = (
                    _bounded_predecessor_release_lookup_hash(
                        latest_settlements=latest_settlements,
                        latest_authorities=latest_settlement_authorities,
                    )
                )
                if predecessor_release_hash is not None:
                    try:
                        history_release_query = (
                            user_ref.collection(
                                "contactOptOutFanoutResults"
                            )
                            .where("rowId", "==", row_id)
                            .where(
                                "releasedRowSettlementHash",
                                "==",
                                predecessor_release_hash,
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        history_release_snapshots = tuple(
                            transaction.get(history_release_query)
                        )
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "claim predecessor-history release query failed before writes"
                        ) from exc
                    for release_snapshot in history_release_snapshots:
                        release_exists, release_payload = read(
                            release_snapshot.reference
                        )
                        if not release_exists:
                            reject(
                                RowAuthorityAmbiguous(
                                    "claim predecessor-history release query returned a missing document"
                                )
                            )
                        latest_predecessor_release_matches.append(
                            {
                                "path": release_snapshot.reference.path,
                                "document": release_payload,
                            }
                        )
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": history_release_query,
                            "matches": tuple(
                                (
                                    entry["path"],
                                    _defensive_copy(entry["document"]),
                                )
                                for entry in (
                                    latest_predecessor_release_matches
                                )
                            ),
                        }
                    )
                    if len(latest_predecessor_release_matches) == 1:
                        latest_predecessor_restored_authority = (
                            _read_bounded_release_restored_authority(
                                user_ref=user_ref,
                                row_id=row_id,
                                release_document=(
                                    latest_predecessor_release_matches[0][
                                        "document"
                                    ]
                                ),
                                read=read,
                            )
                        )

                current_generation = None
                current_claim = None
                current_settlement = None
                current_number = preliminary_head[
                    "effectiveOwnerGeneration"
                ]
                if current_number is not None:
                    current_id = _generation_document_id(
                        row_id=row_id,
                        generation=current_number,
                    )
                    current_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(current_id)
                    current_generation_exists, current_generation_payload = read(
                        current_generation_ref
                    )
                    if current_generation_exists:
                        current_generation = current_generation_payload
                        try:
                            checked_current_generation = (
                                validate_owner_generation_document(
                                    document=current_generation
                                )
                            )
                            current_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(checked_current_generation["requestId"])
                        except Exception as exc:
                            reject(
                                RowAuthorityAmbiguous(
                                    "current bounded owner generation is malformed"
                                )
                            )
                        current_claim_exists, current_claim_payload = read(
                            current_claim_ref
                        )
                        if current_claim_exists:
                            current_claim = current_claim_payload
                    current_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(current_id)
                    current_settlement_exists, current_settlement_payload = read(
                        current_settlement_ref
                    )
                    if current_settlement_exists:
                        current_settlement = current_settlement_payload
                else:
                    current_settlement_ref = None

                release_result = None
                release_result_path = None
                released_authority = None
                restored_authority = None
                if (
                    preliminary_head["latestSettlementHash"]
                    != preliminary_head["effectiveSettlementHash"]
                    and preliminary_head["latestOptOutReleaseResultHash"]
                    is not None
                ):
                    try:
                        release_query = (
                            user_ref.collection("contactOptOutFanoutResults")
                            .where("rowId", "==", row_id)
                            .where(
                                "contactFanoutResultHash",
                                "==",
                                preliminary_head[
                                    "latestOptOutReleaseResultHash"
                                ],
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        release_snapshots = tuple(transaction.get(release_query))
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "claim release-result query failed before writes"
                        ) from exc
                    if len(release_snapshots) != 1:
                        reject(
                            RowAuthorityAmbiguous(
                                "claim release-result bridge is missing or duplicated"
                            )
                        )
                    release_snapshot = release_snapshots[0]
                    release_exists, release_payload = read(
                        release_snapshot.reference
                    )
                    if not release_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "claim release-result query returned a missing document"
                            )
                        )
                    release_result = release_payload
                    release_result_path = release_snapshot.reference.path
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": release_query,
                            "matches": (
                                (
                                    release_result_path,
                                    _defensive_copy(release_payload),
                                ),
                            ),
                        }
                    )
                    try:
                        checked_release_result = (
                            validate_contact_fanout_result_document(
                                document=release_payload
                            )
                        )
                        released_number = checked_release_result[
                            "releasedRowGeneration"
                        ]
                        released_id = _generation_document_id(
                            row_id=row_id,
                            generation=released_number,
                        )
                        released_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(released_id)
                        (
                            released_generation_exists,
                            released_generation_payload,
                        ) = read(released_generation_ref)
                        released_claim_payload = None
                        if released_generation_exists:
                            checked_released_generation = (
                                validate_owner_generation_document(
                                    document=released_generation_payload
                                )
                            )
                            released_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_released_generation["requestId"]
                            )
                            (
                                released_claim_exists,
                                observed_released_claim,
                            ) = read(released_claim_ref)
                            if released_claim_exists:
                                released_claim_payload = observed_released_claim
                        released_settlement_ref = user_ref.collection(
                            "rowOwnerSettlements"
                        ).document(released_id)
                        (
                            released_settlement_exists,
                            released_settlement_payload,
                        ) = read(released_settlement_ref)
                        released_authority = {
                            "path": released_settlement_ref.path,
                            "generation": (
                                released_generation_payload
                                if released_generation_exists
                                else None
                            ),
                            "claimSet": released_claim_payload,
                            "settlement": (
                                released_settlement_payload
                                if released_settlement_exists
                                else None
                            ),
                        }
                        restored_number = checked_release_result[
                            "restoredEffectiveGeneration"
                        ]
                        if restored_number is not None:
                            restored_id = _generation_document_id(
                                row_id=row_id,
                                generation=restored_number,
                            )
                            restored_generation_ref = user_ref.collection(
                                "rowOwnerGenerations"
                            ).document(restored_id)
                            restored_generation_exists, restored_generation_payload = read(
                                restored_generation_ref
                            )
                            restored_claim_payload = None
                            if restored_generation_exists:
                                checked_restored_generation = (
                                    validate_owner_generation_document(
                                        document=restored_generation_payload
                                    )
                                )
                                restored_claim_ref = user_ref.collection(
                                    "rowClaimSets"
                                ).document(
                                    checked_restored_generation["requestId"]
                                )
                                restored_claim_exists, observed_restored_claim = read(
                                    restored_claim_ref
                                )
                                if restored_claim_exists:
                                    restored_claim_payload = observed_restored_claim
                            restored_settlement_ref = user_ref.collection(
                                "rowOwnerSettlements"
                            ).document(restored_id)
                            (
                                restored_settlement_exists,
                                restored_settlement_payload,
                            ) = read(restored_settlement_ref)
                            restored_authority = {
                                "generation": (
                                    restored_generation_payload
                                    if restored_generation_exists
                                    else None
                                ),
                                "claimSet": restored_claim_payload,
                                "settlement": (
                                    restored_settlement_payload
                                    if restored_settlement_exists
                                    else None
                                ),
                            }
                    except RowAuthorityRetryable:
                        raise
                    except Exception:
                        released_authority = {"malformed": True}
                        restored_authority = {"malformed": True}

                provisional_state = {
                    **basic,
                    "currentGeneration": current_generation,
                    "currentClaimSet": current_claim,
                    "currentSettlement": current_settlement,
                    "latestSettlements": latest_settlements,
                    "latestSettlementAuthorities": (
                        latest_settlement_authorities
                    ),
                    "latestPredecessorReleaseMatches": (
                        latest_predecessor_release_matches
                    ),
                    "latestPredecessorRestoredAuthority": (
                        latest_predecessor_restored_authority
                    ),
                    "releaseResult": release_result,
                    "releaseResultPath": release_result_path,
                    "releasedAuthority": released_authority,
                    "restoredAuthority": restored_authority,
                }
                try:
                    bounded_history = _validate_bounded_row_history(
                        scope=checked_scope,
                        row_id=row_id,
                        head=preliminary_head,
                        row_state=provisional_state,
                    )
                except RowAuthorityError as exc:
                    reject(exc)
                stored_decision = None
                if stored_claim is not None:
                    matching = [
                        decision
                        for decision in stored_claim["rowDecisions"]
                        if decision["rowId"] == row_id
                    ]
                    if len(matching) != 1:
                        reject(
                            RowAuthorityConflict(
                                "stored claim decisions do not cover the bound row"
                            )
                        )
                    stored_decision = matching[0]
                if (
                    stored_claim is not None
                    and stored_claim["outcome"] == "accepted"
                ):
                    candidate_number = stored_decision["plannedGeneration"]
                else:
                    candidate_number = bounded_history["nextGeneration"]
                candidate_id = _generation_document_id(
                    row_id=row_id,
                    generation=candidate_number,
                )
                candidate_generation_ref = user_ref.collection(
                    "rowOwnerGenerations"
                ).document(candidate_id)
                candidate_exists, candidate_payload = read(
                    candidate_generation_ref
                )
                candidate_settlement_ref = user_ref.collection(
                    "rowOwnerSettlements"
                ).document(candidate_id)
                candidate_settlement_exists, candidate_settlement_payload = (
                    read(candidate_settlement_ref)
                )
                candidate_predecessor_generation = None
                candidate_predecessor_claim = None
                candidate_predecessor_settlement = None
                candidate_predecessor_release_matches = []
                candidate_predecessor_restored_authority = None
                if (
                    stored_claim is not None
                    and stored_claim["outcome"] == "accepted"
                    and candidate_number > 1
                ):
                    predecessor_id = _generation_document_id(
                        row_id=row_id,
                        generation=candidate_number - 1,
                    )
                    predecessor_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(predecessor_id)
                    (
                        predecessor_generation_exists,
                        predecessor_generation_payload,
                    ) = read(predecessor_generation_ref)
                    if predecessor_generation_exists:
                        candidate_predecessor_generation = (
                            predecessor_generation_payload
                        )
                        try:
                            checked_predecessor_generation = (
                                validate_owner_generation_document(
                                    document=predecessor_generation_payload
                                )
                            )
                            predecessor_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_predecessor_generation["requestId"]
                            )
                        except Exception as exc:
                            reject(
                                RowAuthorityConflict(
                                    "claim replay predecessor generation is malformed"
                                )
                            )
                        predecessor_claim_exists, predecessor_claim_payload = read(
                            predecessor_claim_ref
                        )
                        if predecessor_claim_exists:
                            candidate_predecessor_claim = predecessor_claim_payload
                    predecessor_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(predecessor_id)
                    (
                        predecessor_settlement_exists,
                        predecessor_settlement_payload,
                    ) = read(predecessor_settlement_ref)
                    if predecessor_settlement_exists:
                        candidate_predecessor_settlement = (
                            predecessor_settlement_payload
                        )
                    try:
                        checked_candidate_generation = (
                            validate_owner_generation_document(
                                document=(
                                    candidate_payload
                                    if candidate_exists
                                    else None
                                )
                            )
                        )
                        checked_predecessor_settlement = (
                            validate_owner_settlement_document(
                                document=candidate_predecessor_settlement
                            )
                        )
                    except Exception:
                        checked_candidate_generation = None
                        checked_predecessor_settlement = None
                    if (
                        checked_candidate_generation is not None
                        and checked_candidate_generation["rowId"] == row_id
                        and checked_candidate_generation["generation"]
                        == candidate_number
                        and checked_predecessor_settlement["rowId"] == row_id
                        and checked_predecessor_settlement["generation"]
                        == candidate_number - 1
                        and checked_predecessor_settlement["outcome"]
                        == "contact_optout"
                        and checked_candidate_generation[
                            "predecessorSettlementHash"
                        ]
                        != checked_predecessor_settlement["settlementHash"]
                    ):
                        try:
                            predecessor_release_query = (
                                user_ref.collection(
                                    "contactOptOutFanoutResults"
                                )
                                .where("rowId", "==", row_id)
                                .where(
                                    "releasedRowSettlementHash",
                                    "==",
                                    checked_predecessor_settlement[
                                        "settlementHash"
                                    ],
                                )
                                .order_by("__name__")
                                .limit(2)
                            )
                            predecessor_release_snapshots = tuple(
                                transaction.get(predecessor_release_query)
                            )
                        except Exception as exc:
                            callback_state["read_failed"] = True
                            raise RowAuthorityRetryable(
                                "claim predecessor-release query failed before writes"
                            ) from exc
                        for release_snapshot in predecessor_release_snapshots:
                            release_exists, release_payload = read(
                                release_snapshot.reference
                            )
                            if not release_exists:
                                reject(
                                    RowAuthorityAmbiguous(
                                        "claim predecessor-release query returned a missing document"
                                    )
                                )
                            candidate_predecessor_release_matches.append(
                                {
                                    "path": release_snapshot.reference.path,
                                    "document": release_payload,
                                }
                            )
                        callback_state["query_readbacks"].append(
                            {
                                "kind": "stable",
                                "query": predecessor_release_query,
                                "matches": tuple(
                                    (
                                        entry["path"],
                                        _defensive_copy(entry["document"]),
                                    )
                                    for entry in (
                                        candidate_predecessor_release_matches
                                    )
                                ),
                            }
                        )
                        if len(candidate_predecessor_release_matches) == 1:
                            candidate_predecessor_restored_authority = (
                                _read_bounded_release_restored_authority(
                                    user_ref=user_ref,
                                    row_id=row_id,
                                    release_document=(
                                        candidate_predecessor_release_matches[
                                            0
                                        ]["document"]
                                    ),
                                    read=read,
                                )
                            )

                replay_winner_matches = []
                replay_winner_claim = None
                replay_winner_settlement = None
                replay_winner_successor = {
                    "generation": None,
                    "claimSet": None,
                    "settlement": None,
                    "linkReleaseMatches": [],
                    "linkRestoredAuthority": None,
                    "restorationReleaseMatches": [],
                    "restoredWinnerAuthority": None,
                    "restorationExitGeneration": None,
                    "restorationExitClaimSet": None,
                    "restorationExitSettlement": None,
                }
                if (
                    stored_claim is not None
                    and stored_claim["outcome"] == "dominated"
                    and stored_decision["decision"] == "dominated"
                ):
                    try:
                        winner_query = (
                            user_ref.collection("rowOwnerGenerations")
                            .where("rowId", "==", row_id)
                            .where(
                                "generationHash",
                                "==",
                                stored_decision["winnerGenerationHash"],
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        winner_snapshots = tuple(transaction.get(winner_query))
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "claim replay winner query failed before writes"
                        ) from exc
                    for winner_snapshot in winner_snapshots:
                        winner_exists, winner_payload = read(
                            winner_snapshot.reference
                        )
                        if winner_exists:
                            replay_winner_matches.append(
                                {
                                    "path": winner_snapshot.reference.path,
                                    "document": winner_payload,
                                }
                            )
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": winner_query,
                            "matches": tuple(
                                (
                                    entry["path"],
                                    _defensive_copy(entry["document"]),
                                )
                                for entry in replay_winner_matches
                            ),
                        }
                    )
                    if len(replay_winner_matches) == 1:
                        try:
                            winner_generation = (
                                validate_owner_generation_document(
                                    document=replay_winner_matches[0]["document"]
                                )
                            )
                            winner_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(winner_generation["requestId"])
                            winner_id = _generation_document_id(
                                row_id=row_id,
                                generation=winner_generation["generation"],
                            )
                            winner_settlement_ref = user_ref.collection(
                                "rowOwnerSettlements"
                            ).document(winner_id)
                        except Exception as exc:
                            reject(
                                RowAuthorityConflict(
                                    "claim replay winner generation is malformed"
                                )
                            )
                        winner_claim_exists, winner_claim_payload = read(
                            winner_claim_ref
                        )
                        if winner_claim_exists:
                            replay_winner_claim = winner_claim_payload
                        winner_settlement_exists, winner_settlement_payload = read(
                            winner_settlement_ref
                        )
                        if winner_settlement_exists:
                            replay_winner_settlement = winner_settlement_payload
                        try:
                            replay_winner_successor = (
                                _read_bounded_replay_winner_successor(
                                    user_ref=user_ref,
                                    row_id=row_id,
                                    winner_generation=winner_generation,
                                    winner_settlement=(
                                        replay_winner_settlement
                                    ),
                                    claim_created_at=stored_claim["createdAt"],
                                    read=read,
                                    transaction=transaction,
                                    query_readbacks=callback_state[
                                        "query_readbacks"
                                    ],
                                )
                            )
                        except RowAuthorityError as exc:
                            reject(exc)
                row_references[row_id].update(
                    {
                        "candidate_generation": candidate_generation_ref,
                        "candidate_settlement": candidate_settlement_ref,
                        "current_settlement": current_settlement_ref,
                    }
                )
                row_states.append(
                    {
                        **provisional_state,
                        "candidateGeneration": (
                            candidate_payload if candidate_exists else None
                        ),
                        "candidateSettlement": (
                            candidate_settlement_payload
                            if candidate_settlement_exists
                            else None
                        ),
                        "candidatePredecessorGeneration": (
                            candidate_predecessor_generation
                        ),
                        "candidatePredecessorClaimSet": candidate_predecessor_claim,
                        "candidatePredecessorSettlement": (
                            candidate_predecessor_settlement
                        ),
                        "candidatePredecessorReleaseMatches": (
                            candidate_predecessor_release_matches
                        ),
                        "candidatePredecessorRestoredAuthority": (
                            candidate_predecessor_restored_authority
                        ),
                        "replayWinnerMatches": replay_winner_matches,
                        "replayWinnerClaimSet": replay_winner_claim,
                        "replayWinnerSettlement": replay_winner_settlement,
                        "replayWinnerSuccessorGeneration": (
                            replay_winner_successor["generation"]
                        ),
                        "replayWinnerSuccessorClaimSet": (
                            replay_winner_successor["claimSet"]
                        ),
                        "replayWinnerSuccessorSettlement": (
                            replay_winner_successor["settlement"]
                        ),
                        "replayWinnerSuccessorReleaseMatches": (
                            replay_winner_successor[
                                "linkReleaseMatches"
                            ]
                        ),
                        "replayWinnerSuccessorRestoredAuthority": (
                            replay_winner_successor[
                                "linkRestoredAuthority"
                            ]
                        ),
                        "replayWinnerRestorationReleaseMatches": (
                            replay_winner_successor[
                                "restorationReleaseMatches"
                            ]
                        ),
                        "replayWinnerRestoredAuthority": (
                            replay_winner_successor[
                                "restoredWinnerAuthority"
                            ]
                        ),
                        "replayWinnerRestorationExitGeneration": (
                            replay_winner_successor[
                                "restorationExitGeneration"
                            ]
                        ),
                        "replayWinnerRestorationExitClaimSet": (
                            replay_winner_successor[
                                "restorationExitClaimSet"
                            ]
                        ),
                        "replayWinnerRestorationExitSettlement": (
                            replay_winner_successor[
                                "restorationExitSettlement"
                            ]
                        ),
                    }
                )

            try:
                plan = _plan_row_claim_set(
                    user_scope_hash=checked_scope,
                    authority_origin="b1_source",
                    authority_link=authority_link,
                    operator_action_document=None,
                    fanout_id=None,
                    canonical_mailbox_identity_hash=None,
                    contact_settlement_hash=None,
                    thread_binding_document=thread_binding,
                    row_states=row_states,
                    stored_claim_set_document=stored_claim,
                    created_at=checked_created_at,
                    lease_owner_hash=checked_lease_owner,
                    lease_until=checked_lease_until,
                )
            except RowAuthorityError as exc:
                reject(exc)
            mutations = plan["mutations"]
            exact_count = len(mutations)
            _require_row_authority_planned_writes(exact_count)
            if exact_count != (
                0
                if plan["disposition"] == "already_applied"
                else plan["claimSet"]["plannedWrites"]
            ):
                reject(
                    RowAuthorityConfigError(
                        "claim callback write plan is not exact"
                    )
                )
            mutation_references = {"claim_set": claim_ref}
            for generation in plan["generations"]:
                mutation_references[f"generation:{generation['rowId']}"] = (
                    row_references[generation["rowId"]][
                        "candidate_generation"
                    ]
                )
            for settlement in plan["predecessorSettlements"]:
                mutation_references[
                    f"predecessor_settlement:{settlement['rowId']}"
                ] = row_references[settlement["rowId"]][
                    "current_settlement"
                ]
            for head in plan["heads"]:
                mutation_references[f"head:{head['rowId']}"] = row_references[
                    head["rowId"]
                ]["head"]
            callback_state["mutation_references"] = mutation_references
            for mutation in mutations:
                reference = mutation_references.get(mutation["target"])
                if reference is None:
                    reject(
                        RowAuthorityConfigError(
                            "claim mutation has no exact reference"
                        )
                    )
                if mutation["operation"] == "create":
                    transaction.create(reference, mutation["document"])
                elif mutation["operation"] == "set":
                    transaction.set(
                        reference,
                        mutation["document"],
                        merge=False,
                    )
                else:
                    reject(
                        RowAuthorityConfigError(
                            "claim mutation operation is unsupported"
                        )
                    )
            callback_state["prepared"] = bool(mutations)
            callback_state["disposition"] = plan["disposition"]
            callback_state["plan"] = plan
            return plan["disposition"]

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "claim transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "claim transaction could not start"
                ) from exc
            plan = callback_state["plan"]
            if plan is None:
                raise RowAuthorityAmbiguous(
                    "claim commit has no complete prepared plan"
                ) from exc
            try:
                readback = {}
                for path in callback_state["ordered_paths"]:
                    reference = callback_state["references"][path]
                    snapshot = reference.get()
                    readback[path] = (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    )
                query_observed = []
                for query_readback in callback_state["query_readbacks"]:
                    query = query_readback["query"]
                    stream = getattr(query, "stream", None)
                    if callable(stream):
                        query_snapshots = tuple(stream())
                    else:
                        get = getattr(query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "claim bounded query cannot be read back"
                            )
                        query_snapshots = tuple(get())
                    query_observed.append(
                        tuple(
                            (
                                snapshot.reference.path,
                                snapshot.to_dict(),
                            )
                            for snapshot in query_snapshots
                            if snapshot.exists
                        )
                    )
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "claim commit outcome cannot be read back"
                ) from readback_exc
            expected_after = dict(callback_state["before"])
            for mutation in plan["mutations"]:
                reference = callback_state["mutation_references"][
                    mutation["target"]
                ]
                expected_after[reference.path] = (
                    True,
                    mutation["document"],
                )
            exact_before = readback == callback_state["before"]
            exact_after = readback == expected_after
            query_before_ok = all(
                observed == query_readback["matches"]
                for observed, query_readback in zip(
                    query_observed,
                    callback_state["query_readbacks"],
                    strict=True,
                )
            )
            query_after_expectations = []
            for query_readback in callback_state["query_readbacks"]:
                expected_matches = query_readback["matches"]
                if query_readback["kind"] == "settlement_history":
                    by_path = {
                        path: _defensive_copy(document)
                        for path, document in expected_matches
                    }
                    for mutation in plan["mutations"]:
                        if not mutation["target"].startswith(
                            "predecessor_settlement:"
                        ):
                            continue
                        document = mutation["document"]
                        if document["rowId"] != query_readback["rowId"]:
                            continue
                        reference = callback_state[
                            "mutation_references"
                        ][mutation["target"]]
                        by_path[reference.path] = _defensive_copy(document)
                    expected_matches = tuple(
                        sorted(
                            by_path.items(),
                            key=lambda item: (
                                item[1]["generation"],
                                item[0],
                            ),
                            reverse=True,
                        )[:2]
                    )
                query_after_expectations.append(expected_matches)
            query_after_ok = all(
                observed == expected_matches
                for observed, expected_matches in zip(
                    query_observed,
                    query_after_expectations,
                    strict=True,
                )
            )
            if exact_after and query_after_ok:
                disposition = plan["disposition"]
            elif (
                exact_before
                and query_before_ok
                and not plan["mutations"]
            ):
                disposition = plan["disposition"]
            elif exact_before and query_before_ok:
                raise RowAuthorityRetryable(
                    "claim commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "claim commit readback is partial or drifted"
                ) from exc
        plan = callback_state["plan"]
        if plan is None or disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "claim transaction returned a mismatched disposition"
            )
        if plan["mutations"] and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "claim transaction reported an unprepared mutation"
            )
        return _claim_result(
            disposition=disposition,
            claim_set=plan["claimSet"],
            generations=plan["generations"],
            heads=plan["heads"],
            predecessor_settlements=plan["predecessorSettlements"],
        )

    def take_over_expired_lease(
        self,
        *,
        verified_user_id,
        row_id,
        expected_head,
        new_lease_owner_hash,
        new_lease_until,
        taken_at,
    ):
        _require_row_authority_planned_writes(1)
        expected = validate_row_authority_head(document=expected_head)
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        checked_row_id = validate_row_id(row_id)
        checked_new_owner = _require_sha256(
            new_lease_owner_hash,
            field_name="new_lease_owner_hash",
        )
        checked_new_deadline = _require_timestamp(
            new_lease_until,
            field_name="new_lease_until",
        )
        checked_taken_at = _require_timestamp(
            taken_at,
            field_name="taken_at",
        )
        if (
            expected["userScopeHash"] != checked_scope
            or expected["rowId"] != checked_row_id
        ):
            raise RowAuthorityConfigError(
                "expected lease head does not belong to the requested row"
            )
        if expected["state"] not in {"claimed", "review_pending"}:
            raise RowAuthorityConfigError(
                "lease takeover requires an active claim"
            )
        if checked_taken_at < expected["updatedAt"]:
            raise RowAuthorityConfigError(
                "takeover cannot predate the expected head"
            )
        if expected["leaseUntil"] >= checked_taken_at:
            raise RowAuthorityConfigError(
                "lease must expire before takeover"
            )
        if checked_new_deadline <= checked_taken_at:
            raise RowAuthorityConfigError(
                "new lease must end after takeover"
            )
        generation_number = expected["effectiveOwnerGeneration"]
        user_ref = self._firestore.collection("users").document(
            checked_user_id
        )
        references = self._lease_takeover_references(
            verified_user_id=checked_user_id,
            row_id=checked_row_id,
            generation=generation_number,
        )
        _identity_ref, head_ref, _generation_ref, _settlement_ref = references
        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "before": None,
            "identity": None,
            "generation": None,
            "takeover_head": None,
            "result_head": None,
            "extra_references": {},
            "extra_before": {},
            "query_readbacks": [],
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "before": None,
                    "identity": None,
                    "generation": None,
                    "takeover_head": None,
                    "result_head": None,
                    "extra_references": {},
                    "extra_before": {},
                    "query_readbacks": [],
                }
            )

            def read_extra(reference):
                path = reference.path
                if path in callback_state["extra_before"]:
                    return callback_state["extra_before"][path]
                try:
                    snapshot = reference.get(transaction=transaction)
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "lease takeover bounded read failed before writes"
                    ) from exc
                observed_extra = (
                    bool(snapshot.exists),
                    snapshot.to_dict() if snapshot.exists else None,
                )
                callback_state["extra_references"][path] = reference
                callback_state["extra_before"][path] = observed_extra
                return observed_extra
            try:
                observed = self._read_reference_payloads(
                    references,
                    transaction=transaction,
                )
            except Exception as exc:
                callback_state["read_failed"] = True
                raise RowAuthorityRetryable(
                    "lease takeover transaction read failed before writes"
                ) from exc
            callback_state["before"] = observed
            if any(not exists for exists, _payload in observed[:3]):
                reject(
                    RowAuthorityAmbiguous(
                        "lease takeover is missing identity, head, or generation"
                    )
                )
            if observed[3][0]:
                reject(
                    RowAuthorityConflict(
                        "a settled generation cannot receive a lease takeover"
                    )
                )
            try:
                identity = validate_row_identity_document(
                    document=observed[0][1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "lease takeover row identity is malformed or drifted"
                    )
                )
            try:
                actual_head = validate_row_authority_head(
                    document=observed[1][1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "lease takeover row head is malformed"
                    )
                )
            try:
                generation = validate_owner_generation_document(
                    document=observed[2][1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "lease takeover generation is malformed or drifted"
                    )
                )
            if (
                identity["userScopeHash"] != checked_scope
                or identity["rowId"] != checked_row_id
                or identity["createdAt"] != expected["createdAt"]
                or actual_head["userScopeHash"] != checked_scope
                or actual_head["rowId"] != checked_row_id
                or actual_head["createdAt"] != identity["createdAt"]
                or generation["rowId"] != checked_row_id
                or generation["generation"] != generation_number
                or generation["createdAt"] < identity["createdAt"]
                or generation["createdAt"] > expected["updatedAt"]
            ):
                reject(
                    RowAuthorityConflict(
                        "lease takeover authority does not correlate"
                    )
                )
            try:
                current_claim_ref = user_ref.collection(
                    "rowClaimSets"
                ).document(generation["requestId"])
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "lease takeover claim cannot form an exact path"
                    )
                )
            current_claim_exists, current_claim_payload = read_extra(
                current_claim_ref
            )
            if not current_claim_exists:
                reject(
                    RowAuthorityAmbiguous(
                        "lease takeover is missing its current claim"
                    )
                )
            try:
                history_query = (
                    user_ref.collection("rowOwnerSettlements")
                    .where("rowId", "==", checked_row_id)
                    .order_by("generation", direction="DESCENDING")
                    .limit(2)
                )
                history_snapshots = tuple(transaction.get(history_query))
            except Exception as exc:
                callback_state["read_failed"] = True
                raise RowAuthorityRetryable(
                    "lease takeover bounded history query failed before writes"
                ) from exc
            latest_settlements = []
            latest_authorities = []
            history_matches = []
            for history_snapshot in history_snapshots:
                history_exists, history_payload = read_extra(
                    history_snapshot.reference
                )
                if not history_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "lease takeover history query returned a missing document"
                        )
                    )
                history_matches.append(
                    (
                        history_snapshot.reference.path,
                        _defensive_copy(history_payload),
                    )
                )
                try:
                    checked_history_settlement = (
                        validate_owner_settlement_document(
                            document=history_payload
                        )
                    )
                    history_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=checked_history_settlement["generation"],
                    )
                    history_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(history_id)
                    (
                        history_generation_exists,
                        history_generation_payload,
                    ) = read_extra(history_generation_ref)
                    if not history_generation_exists:
                        raise RowAuthorityAmbiguous(
                            "lease takeover history lacks its generation"
                        )
                    checked_history_generation = (
                        validate_owner_generation_document(
                            document=history_generation_payload
                        )
                    )
                    history_claim_ref = user_ref.collection(
                        "rowClaimSets"
                    ).document(checked_history_generation["requestId"])
                    history_claim_exists, history_claim_payload = read_extra(
                        history_claim_ref
                    )
                    if not history_claim_exists:
                        raise RowAuthorityAmbiguous(
                            "lease takeover history lacks its claim"
                        )
                except RowAuthorityError as exc:
                    reject(exc)
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "lease takeover history authority is malformed"
                        )
                    )
                latest_settlements.append(
                    {
                        "path": history_snapshot.reference.path,
                        "document": history_payload,
                    }
                )
                latest_authorities.append(
                    {
                        "generation": history_generation_payload,
                        "claimSet": history_claim_payload,
                    }
                )
            callback_state["query_readbacks"].append(
                {
                    "query": history_query,
                    "matches": tuple(history_matches),
                }
            )
            latest_predecessor_release_matches = []
            latest_predecessor_restored_authority = None
            predecessor_release_hash = (
                _bounded_predecessor_release_lookup_hash(
                    latest_settlements=latest_settlements,
                    latest_authorities=latest_authorities,
                )
            )
            if predecessor_release_hash is not None:
                try:
                    history_release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "releasedRowSettlementHash",
                            "==",
                            predecessor_release_hash,
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    history_release_snapshots = tuple(
                        transaction.get(history_release_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "lease takeover predecessor-history release query failed before writes"
                    ) from exc
                history_release_matches = []
                for release_snapshot in history_release_snapshots:
                    release_exists, release_payload = read_extra(
                        release_snapshot.reference
                    )
                    if not release_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "lease takeover predecessor-history release query returned a missing document"
                            )
                        )
                    latest_predecessor_release_matches.append(
                        {
                            "path": release_snapshot.reference.path,
                            "document": release_payload,
                        }
                    )
                    history_release_matches.append(
                        (
                            release_snapshot.reference.path,
                            _defensive_copy(release_payload),
                        )
                    )
                callback_state["query_readbacks"].append(
                    {
                        "query": history_release_query,
                        "matches": tuple(history_release_matches),
                    }
                )
                if len(latest_predecessor_release_matches) == 1:
                    latest_predecessor_restored_authority = (
                        _read_bounded_release_restored_authority(
                            user_ref=user_ref,
                            row_id=checked_row_id,
                            release_document=(
                                latest_predecessor_release_matches[0][
                                    "document"
                                ]
                            ),
                            read=read_extra,
                        )
                    )
            release_result = None
            release_result_path = None
            released_authority = None
            restored_authority = None
            if (
                actual_head["latestSettlementHash"]
                != actual_head["effectiveSettlementHash"]
                and actual_head["latestOptOutReleaseResultHash"] is not None
            ):
                try:
                    release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "contactFanoutResultHash",
                            "==",
                            actual_head["latestOptOutReleaseResultHash"],
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    release_snapshots = tuple(transaction.get(release_query))
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "lease takeover release-result query failed before writes"
                    ) from exc
                if len(release_snapshots) != 1:
                    reject(
                        RowAuthorityAmbiguous(
                            "lease takeover release-result bridge is missing or duplicated"
                        )
                    )
                release_snapshot = release_snapshots[0]
                release_exists, release_payload = read_extra(
                    release_snapshot.reference
                )
                if not release_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "lease takeover release-result bridge is missing"
                        )
                    )
                release_result = release_payload
                release_result_path = release_snapshot.reference.path
                callback_state["query_readbacks"].append(
                    {
                        "query": release_query,
                        "matches": (
                            (
                                release_result_path,
                                _defensive_copy(release_payload),
                            ),
                        ),
                    }
                )
                try:
                    checked_release = validate_contact_fanout_result_document(
                        document=release_payload
                    )
                    released_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=checked_release["releasedRowGeneration"],
                    )
                    released_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(released_id)
                    (
                        released_generation_exists,
                        released_generation_payload,
                    ) = read_extra(released_generation_ref)
                    released_claim_payload = None
                    if released_generation_exists:
                        checked_released_generation = (
                            validate_owner_generation_document(
                                document=released_generation_payload
                            )
                        )
                        released_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_released_generation["requestId"])
                        (
                            released_claim_exists,
                            observed_released_claim,
                        ) = read_extra(released_claim_ref)
                        if released_claim_exists:
                            released_claim_payload = observed_released_claim
                    released_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(released_id)
                    (
                        released_settlement_exists,
                        released_settlement_payload,
                    ) = read_extra(released_settlement_ref)
                    released_authority = {
                        "path": released_settlement_ref.path,
                        "generation": (
                            released_generation_payload
                            if released_generation_exists
                            else None
                        ),
                        "claimSet": released_claim_payload,
                        "settlement": (
                            released_settlement_payload
                            if released_settlement_exists
                            else None
                        ),
                    }
                    restored_number = checked_release[
                        "restoredEffectiveGeneration"
                    ]
                    if restored_number is not None:
                        restored_id = _generation_document_id(
                            row_id=checked_row_id,
                            generation=restored_number,
                        )
                        restored_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(restored_id)
                        (
                            restored_generation_exists,
                            restored_generation_payload,
                        ) = read_extra(restored_generation_ref)
                        restored_claim_payload = None
                        if restored_generation_exists:
                            checked_restored_generation = (
                                validate_owner_generation_document(
                                    document=restored_generation_payload
                                )
                            )
                            restored_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_restored_generation["requestId"]
                            )
                            (
                                restored_claim_exists,
                                observed_restored_claim,
                            ) = read_extra(restored_claim_ref)
                            if restored_claim_exists:
                                restored_claim_payload = observed_restored_claim
                        restored_settlement_ref = user_ref.collection(
                            "rowOwnerSettlements"
                        ).document(restored_id)
                        (
                            restored_settlement_exists,
                            restored_settlement_payload,
                        ) = read_extra(restored_settlement_ref)
                        restored_authority = {
                            "generation": (
                                restored_generation_payload
                                if restored_generation_exists
                                else None
                            ),
                            "claimSet": restored_claim_payload,
                            "settlement": (
                                restored_settlement_payload
                                if restored_settlement_exists
                                else None
                            ),
                        }
                except RowAuthorityRetryable:
                    raise
                except Exception:
                    released_authority = {"malformed": True}
                    restored_authority = {"malformed": True}
            try:
                _validate_bounded_row_history(
                    scope=checked_scope,
                    row_id=checked_row_id,
                    head=actual_head,
                    row_state={
                        "currentGeneration": generation,
                        "currentClaimSet": current_claim_payload,
                        "currentSettlement": None,
                        "latestSettlements": latest_settlements,
                        "latestSettlementAuthorities": latest_authorities,
                        "latestPredecessorReleaseMatches": (
                            latest_predecessor_release_matches
                        ),
                        "latestPredecessorRestoredAuthority": (
                            latest_predecessor_restored_authority
                        ),
                        "releaseResult": release_result,
                        "releaseResultPath": release_result_path,
                        "releasedAuthority": released_authority,
                        "restoredAuthority": restored_authority,
                    },
                )
            except RowAuthorityError as exc:
                reject(exc)
            try:
                takeover_head = _build_lease_takeover_head(
                    expected_head=expected,
                    generation_document=generation,
                    new_lease_owner_hash=checked_new_owner,
                    new_lease_until=checked_new_deadline,
                    taken_at=checked_taken_at,
                )
            except RowAuthorityError as exc:
                reject(
                    RowAuthorityConflict(
                        "lease takeover generation conflicts with its head"
                    )
                )
            callback_state["identity"] = identity
            callback_state["generation"] = generation
            callback_state["takeover_head"] = takeover_head
            if _lease_takeover_head_is_location_only_forward(
                takeover_head=takeover_head,
                current_head=actual_head,
            ):
                callback_state["disposition"] = "already_applied"
                callback_state["result_head"] = actual_head
                return "already_applied"
            if actual_head != expected:
                reject(
                    RowAuthorityConflict(
                        "lease takeover expected head is stale or drifted"
                    )
                )
            callback_state["prepared"] = True
            callback_state["disposition"] = "taken_over"
            callback_state["result_head"] = takeover_head
            transaction.set(head_ref, takeover_head, merge=False)
            return "taken_over"

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "lease takeover transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "lease takeover transaction could not start"
                ) from exc
            before = callback_state["before"]
            takeover_head = callback_state["takeover_head"]
            if before is None or takeover_head is None:
                raise RowAuthorityAmbiguous(
                    "lease takeover commit has no complete prepared state"
                ) from exc
            try:
                readback = self._read_reference_payloads(references)
                extra_readback = {}
                for path, reference in callback_state[
                    "extra_references"
                ].items():
                    snapshot = reference.get()
                    extra_readback[path] = (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    )
                query_observed = []
                for query_readback in callback_state["query_readbacks"]:
                    query = query_readback["query"]
                    stream = getattr(query, "stream", None)
                    if callable(stream):
                        query_snapshots = tuple(stream())
                    else:
                        get = getattr(query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "lease takeover bounded query cannot be read back"
                            )
                        query_snapshots = tuple(get())
                    query_observed.append(
                        tuple(
                            (
                                snapshot.reference.path,
                                snapshot.to_dict(),
                            )
                            for snapshot in query_snapshots
                            if snapshot.exists
                        )
                    )
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "lease takeover commit outcome cannot be read back"
                ) from readback_exc
            expected_after = list(before)
            expected_after[1] = (True, takeover_head)
            exact_before = readback == before
            exact_after = readback == tuple(expected_after)
            extra_unchanged = extra_readback == callback_state[
                "extra_before"
            ]
            queries_unchanged = all(
                observed == query_readback["matches"]
                for observed, query_readback in zip(
                    query_observed,
                    callback_state["query_readbacks"],
                    strict=True,
                )
            )
            if (
                exact_after
                and callback_state["prepared"]
                and extra_unchanged
                and queries_unchanged
            ):
                disposition = "taken_over"
            elif (
                exact_before
                and callback_state["disposition"] == "already_applied"
                and extra_unchanged
                and queries_unchanged
            ):
                disposition = "already_applied"
            elif exact_before and extra_unchanged and queries_unchanged:
                raise RowAuthorityRetryable(
                    "lease takeover commit failed before apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "lease takeover commit readback is partial or drifted"
                ) from exc
        if disposition not in {"taken_over", "already_applied"}:
            raise RowAuthorityRetryable(
                "lease takeover returned no approved disposition"
            )
        if disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "lease takeover returned a mismatched disposition"
            )
        if disposition == "taken_over" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "lease takeover reported an unprepared head write"
            )
        generation = callback_state["generation"]
        result_head = callback_state["result_head"]
        if generation is None or result_head is None:
            raise RowAuthorityRetryable(
                "lease takeover returned an incomplete result"
            )
        return _lease_takeover_result(
            disposition=disposition,
            generation=generation,
            head=result_head,
        )

    def settle_owner_generation(
        self,
        *,
        verified_user_id,
        row_id,
        expected_head,
        settled_at,
    ):
        _require_row_authority_planned_writes(2)
        expected = validate_row_authority_head(document=expected_head)
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        checked_row_id = validate_row_id(row_id)
        checked_settled_at = _require_timestamp(
            settled_at,
            field_name="settled_at",
        )
        if (
            expected["userScopeHash"] != checked_scope
            or expected["rowId"] != checked_row_id
        ):
            raise RowAuthorityConfigError(
                "expected settlement head does not belong to the requested row"
            )
        if (
            expected["state"] != "claimed"
            or expected["effectiveOwnerKind"] != "terminal"
        ):
            raise RowAuthorityConflict(
                "public settlement accepts only a claimed terminal generation"
            )
        if checked_settled_at < expected["updatedAt"]:
            raise RowAuthorityConfigError(
                "settlement cannot predate the expected head"
            )
        generation_number = expected["effectiveOwnerGeneration"]
        try:
            user_ref = self._firestore.collection("users").document(
                checked_user_id
            )
            generation_id = _generation_document_id(
                row_id=checked_row_id,
                generation=generation_number,
            )
            identity_ref = user_ref.collection("rowIdentities").document(
                checked_row_id
            )
            head_ref = user_ref.collection("rowAuthorityHeads").document(
                checked_row_id
            )
            generation_ref = user_ref.collection(
                "rowOwnerGenerations"
            ).document(generation_id)
            settlement_ref = user_ref.collection(
                "rowOwnerSettlements"
            ).document(generation_id)
        except Exception as exc:
            raise RowAuthorityConfigError(
                "settlement authority cannot form exact document paths"
            ) from exc
        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "plan": None,
            "references": {},
            "before": {},
            "ordered_paths": [],
            "mutation_references": {},
            "query_readbacks": [],
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "plan": None,
                    "references": {},
                    "before": {},
                    "ordered_paths": [],
                    "mutation_references": {},
                    "query_readbacks": [],
                }
            )

            def read(reference):
                path = reference.path
                if path in callback_state["before"]:
                    return callback_state["before"][path]
                try:
                    snapshot = reference.get(transaction=transaction)
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "settlement transaction read failed before writes"
                    ) from exc
                observed = (
                    bool(snapshot.exists),
                    snapshot.to_dict() if snapshot.exists else None,
                )
                callback_state["references"][path] = reference
                callback_state["before"][path] = observed
                callback_state["ordered_paths"].append(path)
                return observed

            identity_observed = read(identity_ref)
            head_observed = read(head_ref)
            generation_observed = read(generation_ref)
            if not all(
                observed[0]
                for observed in (
                    identity_observed,
                    head_observed,
                    generation_observed,
                )
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "settlement is missing identity, head, or generation"
                    )
                )
            try:
                preliminary_generation = validate_owner_generation_document(
                    document=generation_observed[1]
                )
                if (
                    preliminary_generation["userScopeHash"] != checked_scope
                    or preliminary_generation["rowId"] != checked_row_id
                    or preliminary_generation["generation"]
                    != generation_number
                ):
                    raise RowAuthorityConfigError(
                        "generation does not occupy its expected path"
                    )
                claim_ref = user_ref.collection("rowClaimSets").document(
                    preliminary_generation["requestId"]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "settlement generation is malformed or drifted"
                    )
                )
            claim_observed = read(claim_ref)
            if not claim_observed[0]:
                reject(
                    RowAuthorityAmbiguous(
                        "settlement is missing its immutable claim set"
                    )
                )
            settlement_observed = read(settlement_ref)
            try:
                preliminary_actual_head = validate_row_authority_head(
                    document=head_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "settlement current row head is malformed"
                    )
                )
            try:
                history_query = (
                    user_ref.collection("rowOwnerSettlements")
                    .where("rowId", "==", checked_row_id)
                    .order_by("generation", direction="DESCENDING")
                    .limit(2)
                )
                history_snapshots = tuple(transaction.get(history_query))
            except Exception as exc:
                callback_state["read_failed"] = True
                raise RowAuthorityRetryable(
                    "settlement bounded history query failed before writes"
                ) from exc
            latest_settlements = []
            latest_authorities = []
            for history_snapshot in history_snapshots:
                history_exists, history_payload = read(
                    history_snapshot.reference
                )
                if not history_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "settlement history query returned a missing document"
                        )
                    )
                latest_settlements.append(
                    {
                        "path": history_snapshot.reference.path,
                        "document": history_payload,
                    }
                )
                history_generation = None
                history_claim = None
                try:
                    checked_history_settlement = (
                        validate_owner_settlement_document(
                            document=history_payload
                        )
                    )
                    history_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(
                        _generation_document_id(
                            row_id=checked_row_id,
                            generation=checked_history_settlement[
                                "generation"
                            ],
                        )
                    )
                    history_generation_exists, history_generation_payload = read(
                        history_generation_ref
                    )
                    if history_generation_exists:
                        history_generation = history_generation_payload
                        checked_history_generation = (
                            validate_owner_generation_document(
                                document=history_generation_payload
                            )
                        )
                        history_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_history_generation["requestId"])
                        history_claim_exists, history_claim_payload = read(
                            history_claim_ref
                        )
                        if history_claim_exists:
                            history_claim = history_claim_payload
                except RowAuthorityRetryable:
                    raise
                except Exception:
                    pass
                latest_authorities.append(
                    {
                        "generation": history_generation,
                        "claimSet": history_claim,
                    }
                )
            callback_state["query_readbacks"].append(
                {
                    "kind": "settlement_history",
                    "rowId": checked_row_id,
                    "query": history_query,
                    "matches": tuple(
                        (
                            entry["path"],
                            _defensive_copy(entry["document"]),
                        )
                        for entry in latest_settlements
                    ),
                }
            )
            latest_predecessor_release_matches = []
            latest_predecessor_restored_authority = None
            predecessor_release_hash = (
                _bounded_predecessor_release_lookup_hash(
                    latest_settlements=latest_settlements,
                    latest_authorities=latest_authorities,
                )
            )
            if predecessor_release_hash is not None:
                try:
                    history_release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "releasedRowSettlementHash",
                            "==",
                            predecessor_release_hash,
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    history_release_snapshots = tuple(
                        transaction.get(history_release_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "settlement predecessor-history release query failed before writes"
                    ) from exc
                for release_snapshot in history_release_snapshots:
                    release_exists, release_payload = read(
                        release_snapshot.reference
                    )
                    if not release_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "settlement predecessor-history release query returned a missing document"
                            )
                        )
                    latest_predecessor_release_matches.append(
                        {
                            "path": release_snapshot.reference.path,
                            "document": release_payload,
                        }
                    )
                callback_state["query_readbacks"].append(
                    {
                        "kind": "stable",
                        "query": history_release_query,
                        "matches": tuple(
                            (
                                entry["path"],
                                _defensive_copy(entry["document"]),
                            )
                            for entry in (
                                latest_predecessor_release_matches
                            )
                        ),
                    }
                )
                if len(latest_predecessor_release_matches) == 1:
                    latest_predecessor_restored_authority = (
                        _read_bounded_release_restored_authority(
                            user_ref=user_ref,
                            row_id=checked_row_id,
                            release_document=(
                                latest_predecessor_release_matches[0][
                                    "document"
                                ]
                            ),
                            read=read,
                        )
                    )

            actual_generation_number = preliminary_actual_head[
                "effectiveOwnerGeneration"
            ]
            actual_generation = None
            actual_claim = None
            actual_settlement = None
            if actual_generation_number is not None:
                actual_id = _generation_document_id(
                    row_id=checked_row_id,
                    generation=actual_generation_number,
                )
                actual_generation_ref = user_ref.collection(
                    "rowOwnerGenerations"
                ).document(actual_id)
                actual_generation_exists, actual_generation_payload = read(
                    actual_generation_ref
                )
                if actual_generation_exists:
                    actual_generation = actual_generation_payload
                    try:
                        checked_actual_generation = (
                            validate_owner_generation_document(
                                document=actual_generation_payload
                            )
                        )
                        actual_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_actual_generation["requestId"])
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "settlement current generation is malformed"
                            )
                        )
                    actual_claim_exists, actual_claim_payload = read(
                        actual_claim_ref
                    )
                    if actual_claim_exists:
                        actual_claim = actual_claim_payload
                actual_settlement_ref = user_ref.collection(
                    "rowOwnerSettlements"
                ).document(actual_id)
                actual_settlement_exists, actual_settlement_payload = read(
                    actual_settlement_ref
                )
                if actual_settlement_exists:
                    actual_settlement = actual_settlement_payload

            release_result = None
            release_result_path = None
            released_authority = None
            restored_authority = None
            if (
                preliminary_actual_head["latestSettlementHash"]
                != preliminary_actual_head["effectiveSettlementHash"]
                and preliminary_actual_head["latestOptOutReleaseResultHash"]
                is not None
            ):
                try:
                    release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "contactFanoutResultHash",
                            "==",
                            preliminary_actual_head[
                                "latestOptOutReleaseResultHash"
                            ],
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    release_snapshots = tuple(transaction.get(release_query))
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "settlement release-result query failed before writes"
                    ) from exc
                if len(release_snapshots) != 1:
                    reject(
                        RowAuthorityAmbiguous(
                            "settlement release-result bridge is not unique"
                        )
                    )
                release_snapshot = release_snapshots[0]
                release_exists, release_payload = read(
                    release_snapshot.reference
                )
                if not release_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "settlement release-result bridge is missing"
                        )
                    )
                release_result = release_payload
                release_result_path = release_snapshot.reference.path
                callback_state["query_readbacks"].append(
                    {
                        "kind": "stable",
                        "query": release_query,
                        "matches": (
                            (
                                release_result_path,
                                _defensive_copy(release_payload),
                            ),
                        ),
                    }
                )
                try:
                    checked_release = validate_contact_fanout_result_document(
                        document=release_payload
                    )
                    released_number = checked_release[
                        "releasedRowGeneration"
                    ]
                    released_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=released_number,
                    )
                    released_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(released_id)
                    (
                        released_generation_exists,
                        released_generation_payload,
                    ) = read(released_generation_ref)
                    released_claim_payload = None
                    if released_generation_exists:
                        checked_released_generation = (
                            validate_owner_generation_document(
                                document=released_generation_payload
                            )
                        )
                        released_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_released_generation["requestId"])
                        (
                            released_claim_exists,
                            observed_released_claim,
                        ) = read(released_claim_ref)
                        if released_claim_exists:
                            released_claim_payload = observed_released_claim
                    released_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(released_id)
                    (
                        released_settlement_exists,
                        released_settlement_payload,
                    ) = read(released_settlement_ref)
                    released_authority = {
                        "path": released_settlement_ref.path,
                        "generation": (
                            released_generation_payload
                            if released_generation_exists
                            else None
                        ),
                        "claimSet": released_claim_payload,
                        "settlement": (
                            released_settlement_payload
                            if released_settlement_exists
                            else None
                        ),
                    }
                    restored_number = checked_release[
                        "restoredEffectiveGeneration"
                    ]
                    if restored_number is not None:
                        restored_id = _generation_document_id(
                            row_id=checked_row_id,
                            generation=restored_number,
                        )
                        restored_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(restored_id)
                        restored_generation_exists, restored_generation_payload = read(
                            restored_generation_ref
                        )
                        restored_claim_payload = None
                        if restored_generation_exists:
                            checked_restored_generation = (
                                validate_owner_generation_document(
                                    document=restored_generation_payload
                                )
                            )
                            restored_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_restored_generation["requestId"]
                            )
                            restored_claim_exists, observed_restored_claim = read(
                                restored_claim_ref
                            )
                            if restored_claim_exists:
                                restored_claim_payload = observed_restored_claim
                        restored_settlement_ref = user_ref.collection(
                            "rowOwnerSettlements"
                        ).document(restored_id)
                        restored_settlement_exists, restored_settlement_payload = read(
                            restored_settlement_ref
                        )
                        restored_authority = {
                            "generation": (
                                restored_generation_payload
                                if restored_generation_exists
                                else None
                            ),
                            "claimSet": restored_claim_payload,
                            "settlement": (
                                restored_settlement_payload
                                if restored_settlement_exists
                                else None
                            ),
                        }
                except RowAuthorityRetryable:
                    raise
                except Exception:
                    released_authority = {"malformed": True}
                    restored_authority = {"malformed": True}

            try:
                actual_bounded_history = _validate_bounded_row_history(
                    scope=checked_scope,
                    row_id=checked_row_id,
                    head=preliminary_actual_head,
                    row_state={
                        "currentGeneration": actual_generation,
                        "currentClaimSet": actual_claim,
                        "currentSettlement": actual_settlement,
                        "latestSettlements": latest_settlements,
                        "latestSettlementAuthorities": latest_authorities,
                        "latestPredecessorReleaseMatches": (
                            latest_predecessor_release_matches
                        ),
                        "latestPredecessorRestoredAuthority": (
                            latest_predecessor_restored_authority
                        ),
                        "releaseResult": release_result,
                        "releaseResultPath": release_result_path,
                        "releasedAuthority": released_authority,
                        "restoredAuthority": restored_authority,
                    },
                )
            except RowAuthorityError as exc:
                reject(exc)

            prior_effective_settlement = None
            predecessor_hash = expected["effectiveSettlementHash"]
            if predecessor_hash is not None:
                if generation_number <= 1:
                    reject(
                        RowAuthorityConflict(
                            "first generation cannot have prior effective settlement"
                        )
                    )
                prior_matches = [
                    entry
                    for entry in latest_settlements
                    if entry["document"].get("settlementHash")
                    == predecessor_hash
                ]
                if not prior_matches:
                    try:
                        predecessor_query = (
                            user_ref.collection("rowOwnerSettlements")
                            .where("rowId", "==", checked_row_id)
                            .where(
                                "settlementHash",
                                "==",
                                predecessor_hash,
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        predecessor_snapshots = tuple(
                            transaction.get(predecessor_query)
                        )
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "settlement predecessor query failed before writes"
                        ) from exc
                    prior_matches = []
                    predecessor_query_matches = []
                    for predecessor_snapshot in predecessor_snapshots:
                        predecessor_exists, predecessor_payload = read(
                            predecessor_snapshot.reference
                        )
                        if predecessor_exists:
                            prior_matches.append(
                                {
                                    "path": predecessor_snapshot.reference.path,
                                    "document": predecessor_payload,
                                }
                            )
                            predecessor_query_matches.append(
                                (
                                    predecessor_snapshot.reference.path,
                                    _defensive_copy(predecessor_payload),
                                )
                            )
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": predecessor_query,
                            "matches": tuple(predecessor_query_matches),
                        }
                    )
                if len(prior_matches) != 1:
                    reject(
                        RowAuthorityAmbiguous(
                            "effective predecessor settlement is not unique"
                        )
                    )
                prior_match = prior_matches[0]
                try:
                    checked_prior_settlement = (
                        validate_owner_settlement_document(
                            document=prior_match["document"]
                        )
                    )
                    prior_settlement_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=checked_prior_settlement["generation"],
                    )
                except Exception:
                    reject(
                        RowAuthorityAmbiguous(
                            "effective predecessor settlement is malformed"
                        )
                    )
                if (
                    type(prior_match["path"]) is not str
                    or prior_match["path"].split("/")[-2:]
                    != ["rowOwnerSettlements", prior_settlement_id]
                ):
                    reject(
                        RowAuthorityAmbiguous(
                            "effective predecessor settlement occupies the wrong path"
                        )
                    )
                prior_effective_settlement = prior_match["document"]
            try:
                plan = _plan_owner_generation_settlement(
                    user_scope_hash=checked_scope,
                    row_id=checked_row_id,
                    expected_head=expected,
                    actual_head_document=head_observed[1],
                    identity_document=identity_observed[1],
                    generation_document=generation_observed[1],
                    claim_set_document=claim_observed[1],
                    stored_settlement_document=(
                        settlement_observed[1]
                        if settlement_observed[0]
                        else None
                    ),
                    prior_effective_settlement_document=(
                        prior_effective_settlement
                    ),
                    settled_at=checked_settled_at,
                    operator_action_document=None,
                    actual_bounded_history=actual_bounded_history,
                )
            except (RowAuthorityConflict, RowAuthorityAmbiguous) as exc:
                reject(exc)
            except RowAuthorityError as exc:
                reject(
                    RowAuthorityConflict(
                        "settlement authority is malformed or drifted"
                    )
                )
            mutations = plan["mutations"]
            expected_count = 2 if plan["disposition"] == "settled" else 0
            if len(mutations) != expected_count:
                reject(
                    RowAuthorityConfigError(
                        "settlement callback write plan is not exact"
                    )
                )
            mutation_references = {
                "settlement": settlement_ref,
                "head": head_ref,
            }
            callback_state["mutation_references"] = mutation_references
            for mutation in mutations:
                reference = mutation_references.get(mutation["target"])
                if reference is None:
                    reject(
                        RowAuthorityConfigError(
                            "settlement mutation has no exact reference"
                        )
                    )
                if mutation["operation"] == "create":
                    transaction.create(reference, mutation["document"])
                elif mutation["operation"] == "set":
                    transaction.set(
                        reference,
                        mutation["document"],
                        merge=False,
                    )
                else:
                    reject(
                        RowAuthorityConfigError(
                            "settlement mutation operation is unsupported"
                        )
                    )
            callback_state["prepared"] = bool(mutations)
            callback_state["disposition"] = plan["disposition"]
            callback_state["plan"] = plan
            return plan["disposition"]

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "settlement transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "settlement transaction could not start"
                ) from exc
            plan = callback_state["plan"]
            if plan is None:
                raise RowAuthorityAmbiguous(
                    "settlement commit has no complete prepared plan"
                ) from exc
            try:
                readback = {}
                for path in callback_state["ordered_paths"]:
                    reference = callback_state["references"][path]
                    snapshot = reference.get()
                    readback[path] = (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    )
                query_observed = []
                for query_readback in callback_state["query_readbacks"]:
                    query = query_readback["query"]
                    stream = getattr(query, "stream", None)
                    if callable(stream):
                        query_snapshots = tuple(stream())
                    else:
                        get = getattr(query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "settlement bounded query cannot be read back"
                            )
                        query_snapshots = tuple(get())
                    query_observed.append(
                        tuple(
                            (
                                snapshot.reference.path,
                                snapshot.to_dict(),
                            )
                            for snapshot in query_snapshots
                            if snapshot.exists
                        )
                    )
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "settlement commit outcome cannot be read back"
                ) from readback_exc
            expected_after = dict(callback_state["before"])
            for mutation in plan["mutations"]:
                reference = callback_state["mutation_references"][
                    mutation["target"]
                ]
                expected_after[reference.path] = (
                    True,
                    mutation["document"],
                )
            exact_before = readback == callback_state["before"]
            exact_after = readback == expected_after
            query_before_ok = all(
                observed == query_readback["matches"]
                for observed, query_readback in zip(
                    query_observed,
                    callback_state["query_readbacks"],
                    strict=True,
                )
            )
            query_after_expectations = []
            for query_readback in callback_state["query_readbacks"]:
                expected_matches = query_readback["matches"]
                if query_readback["kind"] == "settlement_history":
                    by_path = {
                        path: _defensive_copy(document)
                        for path, document in expected_matches
                    }
                    for mutation in plan["mutations"]:
                        if mutation["target"] != "settlement":
                            continue
                        document = mutation["document"]
                        if document["rowId"] != query_readback["rowId"]:
                            continue
                        reference = callback_state[
                            "mutation_references"
                        ][mutation["target"]]
                        by_path[reference.path] = _defensive_copy(document)
                    expected_matches = tuple(
                        sorted(
                            by_path.items(),
                            key=lambda item: (
                                item[1]["generation"],
                                item[0],
                            ),
                            reverse=True,
                        )[:2]
                    )
                query_after_expectations.append(expected_matches)
            query_after_ok = all(
                observed == expected_matches
                for observed, expected_matches in zip(
                    query_observed,
                    query_after_expectations,
                    strict=True,
                )
            )
            if exact_after and callback_state["prepared"] and query_after_ok:
                disposition = "settled"
            elif (
                exact_before
                and callback_state["disposition"] == "already_applied"
                and query_before_ok
            ):
                disposition = "already_applied"
            elif exact_before and query_before_ok:
                raise RowAuthorityRetryable(
                    "settlement commit failed before apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "settlement commit readback is partial or drifted"
                ) from exc
        plan = callback_state["plan"]
        if plan is None or disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "settlement transaction returned a mismatched disposition"
            )
        if disposition == "settled" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "settlement transaction reported an unprepared write"
            )
        if disposition not in {"settled", "already_applied"}:
            raise RowAuthorityRetryable(
                "settlement transaction returned no approved disposition"
            )
        return _owner_settlement_result(
            disposition=disposition,
            generation=plan["generation"],
            settlement=plan["settlement"],
            head=plan["head"],
            higher_generation_proven=plan["higherGenerationProven"],
            release_restoration_proven=plan[
                "releaseRestorationProven"
            ],
        )

    def link_b1_source_settlement(
        self,
        *,
        verified_user_id,
        row_id,
        generation,
        linked_at,
    ):
        _require_row_authority_planned_writes(2)
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        checked_row_id = validate_row_id(row_id)
        checked_generation_number = _require_pos(
            generation,
            field_name="generation",
        )
        checked_linked_at = _require_timestamp(
            linked_at,
            field_name="linked_at",
        )
        linked_datetime = _timestamp_as_datetime(
            checked_linked_at,
            field_name="linked_at",
        )
        try:
            user_ref = self._firestore.collection("users").document(
                checked_user_id
            )
            generation_id = _generation_document_id(
                row_id=checked_row_id,
                generation=checked_generation_number,
            )
            row_identity_ref = user_ref.collection("rowIdentities").document(
                checked_row_id
            )
            head_ref = user_ref.collection("rowAuthorityHeads").document(
                checked_row_id
            )
            generation_ref = user_ref.collection(
                "rowOwnerGenerations"
            ).document(generation_id)
            owner_settlements_ref = user_ref.collection(
                "rowOwnerSettlements"
            )
            b2_settlement_ref = owner_settlements_ref.document(generation_id)
            source_links_ref = user_ref.collection(
                "rowSourceSettlementLinks"
            )
            source_link_ref = source_links_ref.document(generation_id)
        except Exception as exc:
            raise RowAuthorityConfigError(
                "source settlement link cannot form exact document paths"
            ) from exc

        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "plan": None,
            "references": {},
            "before": {},
            "ordered_paths": [],
            "mutation_references": {},
            "settlement_query_readback": None,
            "history_query_readbacks": [],
            "query_readback": None,
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "plan": None,
                    "references": {},
                    "before": {},
                    "ordered_paths": [],
                    "mutation_references": {},
                    "settlement_query_readback": None,
                    "history_query_readbacks": [],
                    "query_readback": None,
                }
            )

            def remember(reference, observed):
                path = reference.path
                if path not in callback_state["before"]:
                    callback_state["references"][path] = reference
                    callback_state["before"][path] = observed
                    callback_state["ordered_paths"].append(path)
                return callback_state["before"][path]

            def read(reference):
                path = reference.path
                if path in callback_state["before"]:
                    return callback_state["before"][path]
                try:
                    snapshot = reference.get(transaction=transaction)
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "source link transaction read failed before writes"
                    ) from exc
                return remember(
                    reference,
                    (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    ),
                )

            try:
                settlement_query = (
                    owner_settlements_ref.where(
                        "rowId",
                        "==",
                        checked_row_id,
                    )
                    .order_by("generation", direction="DESCENDING")
                    .limit(2)
                )
                settlement_snapshots = tuple(
                    transaction.get(settlement_query)
                )
            except Exception as exc:
                callback_state["read_failed"] = True
                raise RowAuthorityRetryable(
                    "source link bounded settlement query failed before writes"
                ) from exc
            latest_settlements = []
            latest_settlement_entries = []
            latest_settlement_authorities = []
            settlement_query_matches = []
            for settlement_snapshot in settlement_snapshots:
                settlement_exists, settlement_payload = read(
                    settlement_snapshot.reference
                )
                if not settlement_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link bounded settlement query returned a missing document"
                        )
                    )
                settlement_query_matches.append(
                    (
                        settlement_snapshot.reference.path,
                        _defensive_copy(settlement_payload),
                    )
                )
                try:
                    checked_latest_settlement = (
                        validate_owner_settlement_document(
                            document=settlement_payload
                        )
                    )
                    expected_latest_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=checked_latest_settlement["generation"],
                    )
                    expected_latest_ref = owner_settlements_ref.document(
                        expected_latest_id
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link bounded settlement proof is malformed"
                        )
                    )
                if (
                    settlement_snapshot.reference.path
                    != expected_latest_ref.path
                    or checked_latest_settlement["userScopeHash"]
                    != checked_scope
                    or checked_latest_settlement["rowId"] != checked_row_id
                ):
                    reject(
                        RowAuthorityAmbiguous(
                            "source link bounded settlement occupies the wrong path"
                        )
                    )
                try:
                    latest_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(expected_latest_id)
                    (
                        latest_generation_exists,
                        latest_generation_payload,
                    ) = read(latest_generation_ref)
                    if not latest_generation_exists:
                        raise RowAuthorityAmbiguous(
                            "source link bounded settlement lacks its generation"
                        )
                    checked_latest_generation = (
                        validate_owner_generation_document(
                            document=latest_generation_payload
                        )
                    )
                    latest_claim_ref = user_ref.collection(
                        "rowClaimSets"
                    ).document(checked_latest_generation["requestId"])
                    latest_claim_exists, latest_claim_payload = read(
                        latest_claim_ref
                    )
                    if not latest_claim_exists:
                        raise RowAuthorityAmbiguous(
                            "source link bounded settlement lacks its claim"
                        )
                except RowAuthorityError as exc:
                    reject(exc)
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link bounded settlement authority is malformed"
                        )
                    )
                latest_settlements.append(checked_latest_settlement)
                latest_settlement_entries.append(
                    {
                        "path": settlement_snapshot.reference.path,
                        "document": settlement_payload,
                    }
                )
                latest_settlement_authorities.append(
                    {
                        "generation": latest_generation_payload,
                        "claimSet": latest_claim_payload,
                    }
                )
            callback_state["settlement_query_readback"] = {
                "rowId": checked_row_id,
                "matches": tuple(settlement_query_matches),
            }
            latest_predecessor_release_matches = []
            latest_predecessor_restored_authority = None
            predecessor_release_hash = (
                _bounded_predecessor_release_lookup_hash(
                    latest_settlements=latest_settlement_entries,
                    latest_authorities=latest_settlement_authorities,
                )
            )
            if predecessor_release_hash is not None:
                try:
                    history_release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "releasedRowSettlementHash",
                            "==",
                            predecessor_release_hash,
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    history_release_snapshots = tuple(
                        transaction.get(history_release_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "source link predecessor-history release query failed before writes"
                    ) from exc
                for release_snapshot in history_release_snapshots:
                    release_exists, release_payload = read(
                        release_snapshot.reference
                    )
                    if not release_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "source link predecessor-history release query returned a missing document"
                            )
                        )
                    latest_predecessor_release_matches.append(
                        {
                            "path": release_snapshot.reference.path,
                            "document": release_payload,
                        }
                    )
                callback_state["history_query_readbacks"].append(
                    {
                        "query": history_release_query,
                        "matches": tuple(
                            (
                                entry["path"],
                                _defensive_copy(entry["document"]),
                            )
                            for entry in (
                                latest_predecessor_release_matches
                            )
                        ),
                    }
                )
                if len(latest_predecessor_release_matches) == 1:
                    latest_predecessor_restored_authority = (
                        _read_bounded_release_restored_authority(
                            user_ref=user_ref,
                            row_id=checked_row_id,
                            release_document=(
                                latest_predecessor_release_matches[0][
                                    "document"
                                ]
                            ),
                            read=read,
                        )
                    )
            if len(latest_settlements) == 1 and (
                latest_settlements[0]["generation"] != 1
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link single bounded settlement is not generation one"
                    )
                )
            if len(latest_settlements) == 2 and (
                latest_settlements[0]["generation"]
                != latest_settlements[1]["generation"] + 1
                or latest_settlements[0]["fencingToken"]
                <= latest_settlements[1]["fencingToken"]
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link latest settlements have a gap or fencing regression"
                    )
                )

            row_identity_observed = read(row_identity_ref)
            head_observed = read(head_ref)
            generation_observed = read(generation_ref)
            if not all(
                observed[0]
                for observed in (
                    row_identity_observed,
                    head_observed,
                    generation_observed,
                )
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link is missing row identity, head, or generation"
                    )
                )
            try:
                preliminary_generation = validate_owner_generation_document(
                    document=generation_observed[1]
                )
                if (
                    preliminary_generation["userScopeHash"]
                    != checked_scope
                    or preliminary_generation["rowId"] != checked_row_id
                    or preliminary_generation["generation"]
                    != checked_generation_number
                ):
                    raise RowAuthorityConfigError(
                        "source link generation occupies the wrong path"
                    )
                claim_ref = user_ref.collection("rowClaimSets").document(
                    preliminary_generation["requestId"]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link generation is malformed or drifted"
                    )
                )

            claim_observed = read(claim_ref)
            b2_settlement_observed = read(b2_settlement_ref)
            source_link_observed = read(source_link_ref)
            if not claim_observed[0] or not b2_settlement_observed[0]:
                reject(
                    RowAuthorityAmbiguous(
                        "source link is missing claim or B2 settlement authority"
                    )
                )

            try:
                row_identity = validate_row_identity_document(
                    document=row_identity_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link row identity contains immutable drift"
                    )
                )
            try:
                current_head = validate_row_authority_head(
                    document=head_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "source link current row head is malformed"
                    )
                )
            try:
                checked_generation = validate_owner_generation_document(
                    document=generation_observed[1]
                )
                checked_claim = validate_claim_set_document(
                    document=claim_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link generation or claim contains immutable drift"
                    )
                )
            if (
                row_identity["userScopeHash"] != checked_scope
                or row_identity["rowId"] != checked_row_id
                or current_head["userScopeHash"] != checked_scope
                or current_head["rowId"] != checked_row_id
                or current_head["createdAt"] != row_identity["createdAt"]
            ):
                reject(
                    RowAuthorityConflict(
                        "source link row authority does not correlate"
                    )
                )
            current_generation = None
            current_claim = None
            current_settlement = None
            current_number = current_head["effectiveOwnerGeneration"]
            if current_number is not None:
                current_id = _generation_document_id(
                    row_id=checked_row_id,
                    generation=current_number,
                )
                current_generation_ref = user_ref.collection(
                    "rowOwnerGenerations"
                ).document(current_id)
                current_generation_exists, current_generation_payload = read(
                    current_generation_ref
                )
                if current_generation_exists:
                    current_generation = current_generation_payload
                    try:
                        checked_current_generation = (
                            validate_owner_generation_document(
                                document=current_generation_payload
                            )
                        )
                        current_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_current_generation["requestId"])
                    except Exception as exc:
                        reject(
                            RowAuthorityAmbiguous(
                                "source link current generation is malformed"
                            )
                        )
                    current_claim_exists, current_claim_payload = read(
                        current_claim_ref
                    )
                    if current_claim_exists:
                        current_claim = current_claim_payload
                current_settlement_ref = owner_settlements_ref.document(
                    current_id
                )
                current_settlement_exists, current_settlement_payload = read(
                    current_settlement_ref
                )
                if current_settlement_exists:
                    current_settlement = current_settlement_payload

            release_result = None
            release_result_path = None
            released_authority = None
            restored_authority = None
            if (
                current_head["latestSettlementHash"]
                != current_head["effectiveSettlementHash"]
                and current_head["latestOptOutReleaseResultHash"] is not None
            ):
                try:
                    release_query = (
                        user_ref.collection("contactOptOutFanoutResults")
                        .where("rowId", "==", checked_row_id)
                        .where(
                            "contactFanoutResultHash",
                            "==",
                            current_head["latestOptOutReleaseResultHash"],
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    release_snapshots = tuple(transaction.get(release_query))
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "source link release-result query failed before writes"
                    ) from exc
                if len(release_snapshots) != 1:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link release-result bridge is missing or duplicated"
                        )
                    )
                release_snapshot = release_snapshots[0]
                release_exists, release_payload = read(
                    release_snapshot.reference
                )
                if not release_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link release-result query returned a missing document"
                        )
                    )
                release_result = release_payload
                release_result_path = release_snapshot.reference.path
                callback_state["history_query_readbacks"].append(
                    {
                        "query": release_query,
                        "matches": (
                            (
                                release_result_path,
                                _defensive_copy(release_payload),
                            ),
                        ),
                    }
                )
                try:
                    checked_release = validate_contact_fanout_result_document(
                        document=release_payload
                    )
                    released_number = checked_release[
                        "releasedRowGeneration"
                    ]
                    released_id = _generation_document_id(
                        row_id=checked_row_id,
                        generation=released_number,
                    )
                    released_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(released_id)
                    (
                        released_generation_exists,
                        released_generation_payload,
                    ) = read(released_generation_ref)
                    released_claim_payload = None
                    if released_generation_exists:
                        checked_released_generation = (
                            validate_owner_generation_document(
                                document=released_generation_payload
                            )
                        )
                        released_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(checked_released_generation["requestId"])
                        (
                            released_claim_exists,
                            observed_released_claim,
                        ) = read(released_claim_ref)
                        if released_claim_exists:
                            released_claim_payload = observed_released_claim
                    released_settlement_ref = owner_settlements_ref.document(
                        released_id
                    )
                    (
                        released_settlement_exists,
                        released_settlement_payload,
                    ) = read(released_settlement_ref)
                    released_authority = {
                        "path": released_settlement_ref.path,
                        "generation": (
                            released_generation_payload
                            if released_generation_exists
                            else None
                        ),
                        "claimSet": released_claim_payload,
                        "settlement": (
                            released_settlement_payload
                            if released_settlement_exists
                            else None
                        ),
                    }
                    restored_number = checked_release[
                        "restoredEffectiveGeneration"
                    ]
                    if restored_number is not None:
                        restored_id = _generation_document_id(
                            row_id=checked_row_id,
                            generation=restored_number,
                        )
                        restored_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(restored_id)
                        (
                            restored_generation_exists,
                            restored_generation_payload,
                        ) = read(restored_generation_ref)
                        restored_claim_payload = None
                        if restored_generation_exists:
                            checked_restored_generation = (
                                validate_owner_generation_document(
                                    document=restored_generation_payload
                                )
                            )
                            restored_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_restored_generation["requestId"]
                            )
                            (
                                restored_claim_exists,
                                observed_restored_claim,
                            ) = read(restored_claim_ref)
                            if restored_claim_exists:
                                restored_claim_payload = observed_restored_claim
                        restored_settlement_ref = owner_settlements_ref.document(
                            restored_id
                        )
                        (
                            restored_settlement_exists,
                            restored_settlement_payload,
                        ) = read(restored_settlement_ref)
                        restored_authority = {
                            "generation": (
                                restored_generation_payload
                                if restored_generation_exists
                                else None
                            ),
                            "claimSet": restored_claim_payload,
                            "settlement": (
                                restored_settlement_payload
                                if restored_settlement_exists
                                else None
                            ),
                        }
                except RowAuthorityRetryable:
                    raise
                except Exception:
                    released_authority = {"malformed": True}
                    restored_authority = {"malformed": True}
            try:
                _validate_bounded_row_history(
                    scope=checked_scope,
                    row_id=checked_row_id,
                    head=current_head,
                    row_state={
                        "currentGeneration": current_generation,
                        "currentClaimSet": current_claim,
                        "currentSettlement": current_settlement,
                        "latestSettlements": latest_settlement_entries,
                        "latestSettlementAuthorities": (
                            latest_settlement_authorities
                        ),
                        "latestPredecessorReleaseMatches": (
                            latest_predecessor_release_matches
                        ),
                        "latestPredecessorRestoredAuthority": (
                            latest_predecessor_restored_authority
                        ),
                        "releaseResult": release_result,
                        "releaseResultPath": release_result_path,
                        "releasedAuthority": released_authority,
                        "restoredAuthority": restored_authority,
                    },
                )
            except RowAuthorityError as exc:
                reject(exc)
            latest_settlement = (
                latest_settlements[0] if latest_settlements else None
            )
            expected_latest_settlement_hash = (
                latest_settlement["settlementHash"]
                if latest_settlement is not None
                else None
            )
            if (
                current_head["latestSettlementHash"]
                != expected_latest_settlement_hash
                or any(
                    settlement["settledAt"] > current_head["updatedAt"]
                    for settlement in latest_settlements
                )
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link head does not match bounded settlement history"
                    )
                )
            if (
                checked_generation["requestId"] != checked_claim["requestId"]
                or checked_generation["claimSetHash"]
                != checked_claim["claimSetHash"]
                or checked_generation["ownerKind"] != checked_claim["ownerKind"]
                or checked_generation["ownerKey"] != checked_claim["ownerKey"]
                or checked_generation["priority"]
                != checked_claim["derivedPriority"]
                or checked_claim["outcome"] != "accepted"
                or checked_claim["createdAt"] < row_identity["createdAt"]
                or checked_claim["createdAt"] < current_head["createdAt"]
                or checked_generation["createdAt"]
                < checked_claim["createdAt"]
                or (
                    checked_generation["generation"] == 1
                    and checked_generation["predecessorSettlementHash"]
                    is not None
                )
            ):
                reject(
                    RowAuthorityConflict(
                        "source link generation does not correlate to its claim"
                    )
                )
            matching_decisions = [
                decision
                for decision in checked_claim["rowDecisions"]
                if decision["rowId"] == checked_row_id
            ]
            if (
                len(matching_decisions) != 1
                or matching_decisions[0]["decision"] != "accepted"
                or matching_decisions[0]["plannedGeneration"]
                != checked_generation_number
            ):
                reject(
                    RowAuthorityConflict(
                        "source link claim decision does not authorize the generation"
                    )
                )
            if checked_claim["authorityOrigin"] not in {
                "b1_source",
                "contact_fanout",
            }:
                reject(
                    RowAuthorityConflict(
                        "source link claim origin is not B1-backed"
                    )
                )
            embedded_authority_link = checked_claim["authorityLink"]
            if embedded_authority_link is None:
                reject(
                    RowAuthorityConflict(
                        "source link claim lacks embedded B1 authority"
                    )
                )
            try:
                embedded_authority_link = validate_b1_authority_link(
                    authority_link=embedded_authority_link,
                    user_scope_hash=checked_scope,
                )
                canonical_source_id = _require_firestore_document_id(
                    embedded_authority_link["canonicalSourceId"],
                    field_name="B1 canonicalSourceId",
                )
                b1_references = (
                    user_ref.collection("sourceIdentities").document(
                        canonical_source_id
                    ),
                    user_ref.collection("sourceClassifications").document(
                        canonical_source_id
                    ),
                    user_ref.collection("sourceTransitionOwners").document(
                        canonical_source_id
                    ),
                    user_ref.collection("sourceWorkLedgers").document(
                        canonical_source_id
                    ),
                    user_ref.collection("sourceSettlements").document(
                        canonical_source_id
                    ),
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link B1 authority cannot form exact paths"
                    )
                )

            b1_observed = tuple(read(reference) for reference in b1_references)
            if not all(exists for exists, _payload in b1_observed):
                reject(
                    RowAuthorityAmbiguous(
                        "source link B1 authority bundle is incomplete"
                    )
                )
            (
                b1_identity_document,
                b1_classification_document,
                b1_owner_document,
                b1_ledger_document,
                b1_settlement_document,
            ) = tuple(payload for _exists, payload in b1_observed)
            try:
                b1_identity = _validate_b1_source_identity(
                    b1_identity_document
                )
                if b1_identity["canonicalSourceId"] != canonical_source_id:
                    raise RowAuthorityConfigError(
                        "B1 source identity occupies the wrong path"
                    )
                b1_classification = _validate_b1_classification(
                    b1_classification_document,
                    canonical_source_id=canonical_source_id,
                )
                b1_owner = _validate_b1_owner(
                    b1_owner_document,
                    canonical_source_id=canonical_source_id,
                    classification=b1_classification,
                )
                b1_ledger = _validate_b1_ledger(
                    b1_ledger_document,
                    canonical_source_id=canonical_source_id,
                    classification=b1_classification,
                    owner=b1_owner,
                )
                rebuilt_authority_link = build_b1_authority_link(
                    user_scope_hash=checked_scope,
                    source_identity_document=b1_identity,
                    source_classification_document=b1_classification,
                    source_owner_document=b1_owner,
                    source_ledger_document=b1_ledger,
                    work_key=embedded_authority_link["workKey"],
                )
                if rebuilt_authority_link != embedded_authority_link:
                    raise RowAuthorityConfigError(
                        "B1 authority differs from the immutable claim link"
                    )
                b1_settlement = _validate_b1_source_settlement(
                    b1_settlement_document,
                    canonical_source_id=canonical_source_id,
                    identity=b1_identity,
                    classification=b1_classification,
                    owner=b1_owner,
                    ledger=b1_ledger,
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link B1 authority is malformed or drifted"
                    )
                )

            try:
                b1_thread_binding_ref = user_ref.collection(
                    "threadRowBindings"
                ).document(b1_identity["threadId"])
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link B1 thread cannot form an exact binding path"
                    )
                )
            b1_thread_binding_observed = read(b1_thread_binding_ref)
            if not b1_thread_binding_observed[0]:
                reject(
                    RowAuthorityAmbiguous(
                        "source link B1 thread binding is missing"
                    )
                )
            try:
                b1_thread_binding = validate_thread_row_binding_document(
                    document=b1_thread_binding_observed[1]
                )
                if (
                    b1_thread_binding["userScopeHash"] != checked_scope
                    or b1_thread_binding["threadId"]
                    != b1_identity["threadId"]
                    or b1_thread_binding["createdAt"]
                    > checked_claim["createdAt"]
                    or (
                        checked_claim["authorityOrigin"] == "b1_source"
                        and (
                            b1_thread_binding["clientId"]
                            != row_identity["clientId"]
                            or b1_thread_binding["rowBindings"]
                            != checked_claim["rowBindings"]
                            or b1_thread_binding["primaryRowId"]
                            != checked_claim["primaryRowId"]
                            or b1_thread_binding["bindingCount"]
                            != checked_claim["bindingCount"]
                            or b1_thread_binding["rowBindingsHash"]
                            != checked_claim["rowBindingsHash"]
                        )
                    )
                ):
                    raise RowAuthorityConfigError(
                        "B1 thread binding differs from the immutable claim"
                    )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "source link B1 thread binding is malformed or drifted"
                    )
                )

            b1_readiness = (
                b1_identity["createdAt"],
                b1_classification["snapshotPersistedAt"],
                b1_owner["createdAt"],
                b1_ledger["createdAt"],
            )
            claim_datetime = _timestamp_as_datetime(
                checked_claim["createdAt"],
                field_name="claim.createdAt",
            )
            if claim_datetime < max(
                value.astimezone(timezone.utc) for value in b1_readiness
            ):
                reject(
                    RowAuthorityConflict(
                        "source link claim predates B1 authority readiness"
                    )
                )

            try:
                b2_settlement = _validate_correlated_owner_settlement(
                    scope=checked_scope,
                    row_id=checked_row_id,
                    generation=checked_generation,
                    claim=checked_claim,
                    settlement_document=b2_settlement_observed[1],
                )
            except RowAuthorityError as exc:
                reject(exc)
            if (
                latest_settlement is None
                or b2_settlement["generation"]
                > latest_settlement["generation"]
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link target is not present in bounded settlement history"
                    )
                )
            historical_target = (
                b2_settlement["generation"]
                < latest_settlement["generation"]
            )
            if (
                b2_settlement["outcome"] != "dominated"
                and b2_settlement["outcome"]
                != _expected_owner_settlement_outcome(
                    checked_generation["ownerKind"]
                )
            ):
                reject(
                    RowAuthorityConflict(
                        "source link B2 settlement outcome conflicts with its owner"
                    )
                )
            if (
                b2_settlement["outcome"] == "dominated"
                and checked_generation["ownerKind"] == "contact_optout"
            ):
                reject(
                    RowAuthorityConflict(
                        "contact opt-out generation cannot be dominated"
                    )
                )
            if not _source_link_head_reflects_b2_settlement(
                head_document=current_head,
                generation_document=checked_generation,
                settlement_document=b2_settlement,
                historical_generation_proven=historical_target,
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "source link head does not reflect its B2 settlement"
                    )
                )
            if linked_datetime < b1_settlement["settledAt"].astimezone(
                timezone.utc
            ) or checked_linked_at < b2_settlement["settledAt"]:
                reject(
                    (
                        RowAuthorityAmbiguous(
                            "historical source link proof postdates the link"
                        )
                        if historical_target
                        else RowAuthorityConfigError(
                            "source link time predates B1 or B2 settlement"
                        )
                    )
                )

            candidate_link = build_source_settlement_link_document(
                user_scope_hash=checked_scope,
                row_id=checked_row_id,
                generation=checked_generation_number,
                generation_hash=checked_generation["generationHash"],
                authority_link_hash=embedded_authority_link[
                    "authorityLinkHash"
                ],
                b1_identity_hash=b1_settlement["identityHash"],
                b1_final_ledger_evidence_hash=b1_settlement[
                    "finalLedgerEvidenceHash"
                ],
                b1_settlement_revision=b1_settlement[
                    "settlementRevision"
                ],
                b1_settlement_hash=b1_settlement["settlementHash"],
                b2_settlement_hash=b2_settlement["settlementHash"],
                linked_at=checked_linked_at,
            )
            stored_candidate = None
            if source_link_observed[0]:
                try:
                    stored_candidate = validate_source_settlement_link_document(
                        document=source_link_observed[1]
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityConflict(
                            "stored source settlement link contains immutable drift"
                        )
                    )
                if stored_candidate != candidate_link:
                    reject(
                        RowAuthorityConflict(
                            "stored source settlement link differs from the candidate"
                        )
                    )

            current_pointer = current_head[
                "latestSourceSettlementLinkHash"
            ]
            disposition = None
            result_head = current_head
            mutations = ()
            if current_pointer == candidate_link["sourceSettlementLinkHash"]:
                if stored_candidate is None:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link head points to a missing candidate"
                        )
                    )
                if stored_candidate["linkedAt"] > current_head["updatedAt"]:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link candidate postdates its current head"
                        )
                    )
                disposition = "already_applied"
            elif current_pointer is None:
                if stored_candidate is not None:
                    reject(
                        RowAuthorityAmbiguous(
                            "existing source link has no current head pointer"
                        )
                    )
            else:
                try:
                    query = source_links_ref.where(
                        "sourceSettlementLinkHash",
                        "==",
                        current_pointer,
                    ).order_by("__name__").limit(2)
                    current_snapshots = list(transaction.get(query))
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "source link pointer query failed before writes"
                    ) from exc
                if len(current_snapshots) != 1:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link pointer query is not unique"
                        )
                    )
                current_snapshot = current_snapshots[0]
                current_payload = current_snapshot.to_dict()
                remember(
                    current_snapshot.reference,
                    (bool(current_snapshot.exists), current_payload),
                )
                callback_state["query_readback"] = {
                    "sourceSettlementLinkHash": current_pointer,
                    "matches": (
                        (
                            current_snapshot.reference.path,
                            _defensive_copy(current_payload),
                        ),
                    ),
                }
                try:
                    queried_current_link = (
                        validate_source_settlement_link_document(
                            document=current_payload
                        )
                    )
                    expected_current_id = _generation_document_id(
                        row_id=queried_current_link["rowId"],
                        generation=queried_current_link["generation"],
                    )
                    if (
                        not current_snapshot.exists
                        or current_snapshot.id != expected_current_id
                        or queried_current_link["userScopeHash"]
                        != checked_scope
                        or queried_current_link["rowId"] != checked_row_id
                        or queried_current_link[
                            "sourceSettlementLinkHash"
                        ]
                        != current_pointer
                    ):
                        raise RowAuthorityConfigError(
                            "queried source link does not match the head pointer"
                        )
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link head pointer resolves to malformed authority"
                        )
                    )
                if queried_current_link["linkedAt"] > current_head["updatedAt"]:
                    reject(
                        RowAuthorityAmbiguous(
                            "source link head predates its pointed link"
                        )
                    )
                if stored_candidate is not None:
                    if queried_current_link["linkedAt"] < candidate_link[
                        "linkedAt"
                    ]:
                        reject(
                            RowAuthorityAmbiguous(
                                "source link replay points to an earlier current link"
                            )
                        )
                    disposition = "already_applied"
                elif queried_current_link["linkedAt"] > candidate_link[
                    "linkedAt"
                ]:
                    reject(
                        RowAuthorityAmbiguous(
                            "new source link predates its current pointer"
                        )
                    )

            if disposition is None:
                if candidate_link["linkedAt"] < current_head["updatedAt"]:
                    reject(
                        RowAuthorityConfigError(
                            "new source link predates the current row head"
                        )
                    )
                try:
                    result_head = _build_source_link_advanced_head(
                        expected_head=current_head,
                        source_link_document=candidate_link,
                    )
                except RowAuthorityError as exc:
                    reject(exc)
                disposition = "linked"
                mutations = (
                    {
                        "target": "source_link",
                        "operation": "create",
                        "document": candidate_link,
                    },
                    {
                        "target": "head",
                        "operation": "set",
                        "document": result_head,
                    },
                )

            expected_count = 2 if disposition == "linked" else 0
            if len(mutations) != expected_count:
                reject(
                    RowAuthorityConfigError(
                        "source link callback write plan is not exact"
                    )
                )
            mutation_references = {
                "source_link": source_link_ref,
                "head": head_ref,
            }
            callback_state["mutation_references"] = mutation_references
            for mutation in mutations:
                reference = mutation_references.get(mutation["target"])
                if reference is None:
                    reject(
                        RowAuthorityConfigError(
                            "source link mutation has no exact reference"
                        )
                    )
                if mutation["operation"] == "create":
                    transaction.create(reference, mutation["document"])
                elif mutation["operation"] == "set":
                    transaction.set(
                        reference,
                        mutation["document"],
                        merge=False,
                    )
                else:
                    reject(
                        RowAuthorityConfigError(
                            "source link mutation operation is unsupported"
                        )
                    )
            plan = {
                "disposition": disposition,
                "sourceSettlementLink": candidate_link,
                "head": result_head,
                "mutations": mutations,
                "laterLinkProven": (
                    disposition == "already_applied"
                    and current_pointer
                    != candidate_link["sourceSettlementLinkHash"]
                ),
            }
            callback_state["prepared"] = bool(mutations)
            callback_state["disposition"] = disposition
            callback_state["plan"] = plan
            return disposition

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "source link transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "source link transaction could not start"
                ) from exc
            plan = callback_state["plan"]
            if plan is None:
                raise RowAuthorityAmbiguous(
                    "source link commit has no complete prepared plan"
                ) from exc
            try:
                readback = {}
                for path in callback_state["ordered_paths"]:
                    reference = callback_state["references"][path]
                    snapshot = reference.get()
                    readback[path] = (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    )
                settlement_query_readback = callback_state[
                    "settlement_query_readback"
                ]
                if settlement_query_readback is not None:
                    settlement_query = (
                        owner_settlements_ref.where(
                            "rowId",
                            "==",
                            settlement_query_readback["rowId"],
                        )
                        .order_by("generation", direction="DESCENDING")
                        .limit(2)
                    )
                    stream = getattr(settlement_query, "stream", None)
                    if callable(stream):
                        settlement_snapshots = list(stream())
                    else:
                        get = getattr(settlement_query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "source link settlement query cannot be read back"
                            )
                        settlement_snapshots = list(get())
                    settlement_matches = tuple(
                        (
                            snapshot.reference.path,
                            snapshot.to_dict(),
                        )
                        for snapshot in settlement_snapshots
                        if snapshot.exists
                    )
                    if (
                        settlement_matches
                        != settlement_query_readback["matches"]
                    ):
                        raise RowAuthorityAmbiguous(
                            "source link settlement query changed during readback"
                        )
                for history_query_readback in callback_state[
                    "history_query_readbacks"
                ]:
                    history_query = history_query_readback["query"]
                    stream = getattr(history_query, "stream", None)
                    if callable(stream):
                        history_snapshots = tuple(stream())
                    else:
                        get = getattr(history_query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "source link bounded query cannot be read back"
                            )
                        history_snapshots = tuple(get())
                    history_matches = tuple(
                        (
                            snapshot.reference.path,
                            snapshot.to_dict(),
                        )
                        for snapshot in history_snapshots
                        if snapshot.exists
                    )
                    if history_matches != history_query_readback["matches"]:
                        raise RowAuthorityAmbiguous(
                            "source link bounded query changed during readback"
                        )
                query_readback = callback_state["query_readback"]
                if query_readback is not None:
                    query = source_links_ref.where(
                        "sourceSettlementLinkHash",
                        "==",
                        query_readback["sourceSettlementLinkHash"],
                    ).order_by("__name__").limit(2)
                    stream = getattr(query, "stream", None)
                    if callable(stream):
                        query_snapshots = list(stream())
                    else:
                        get = getattr(query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "source link query cannot be read back"
                            )
                        query_snapshots = list(get())
                    query_matches = tuple(
                        (
                            snapshot.reference.path,
                            snapshot.to_dict(),
                        )
                        for snapshot in query_snapshots
                        if snapshot.exists
                    )
                    if query_matches != query_readback["matches"]:
                        raise RowAuthorityAmbiguous(
                            "source link pointer query changed during readback"
                        )
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "source link commit outcome cannot be read back"
                ) from readback_exc
            expected_after = dict(callback_state["before"])
            for mutation in plan["mutations"]:
                reference = callback_state["mutation_references"][
                    mutation["target"]
                ]
                expected_after[reference.path] = (
                    True,
                    mutation["document"],
                )
            exact_before = readback == callback_state["before"]
            exact_after = readback == expected_after
            if exact_after and callback_state["prepared"]:
                disposition = "linked"
            elif (
                exact_before
                and callback_state["disposition"] == "already_applied"
            ):
                disposition = "already_applied"
            elif exact_before:
                raise RowAuthorityRetryable(
                    "source link commit failed before apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "source link commit readback is partial or drifted"
                ) from exc
        plan = callback_state["plan"]
        if plan is None or disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "source link transaction returned a mismatched disposition"
            )
        if disposition == "linked" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "source link transaction reported an unprepared write"
            )
        if disposition not in {"linked", "already_applied"}:
            raise RowAuthorityRetryable(
                "source link transaction returned no approved disposition"
            )
        return _source_settlement_link_result(
            disposition=disposition,
            source_settlement_link=plan["sourceSettlementLink"],
            head=plan["head"],
            later_link_proven=plan["laterLinkProven"],
        )

    def record_operator_decline(
        self,
        *,
        verified_user_id,
        thread_id,
        actor_scope_hash,
        client_request_id,
        issued_at,
    ):
        _require_row_authority_planned_writes(
            2 + (3 * MAX_ROW_BINDINGS)
        )
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_thread_id = _require_thread_document_id(
            thread_id,
            field_name="thread_id",
        )
        checked_actor_scope = _require_sha256(
            actor_scope_hash,
            field_name="actor_scope_hash",
        )
        checked_client_request_id = _require_opaque(
            client_request_id,
            field_name="client_request_id",
        )
        checked_issued_at = _require_timestamp(
            issued_at,
            field_name="issued_at",
        )
        checked_scope = user_scope_hash(checked_user_id)
        try:
            user_ref = self._firestore.collection("users").document(
                checked_user_id
            )
            binding_ref = user_ref.collection(
                "threadRowBindings"
            ).document(checked_thread_id)
        except Exception as exc:
            raise RowAuthorityConfigError(
                "operator decline cannot form exact document paths"
            ) from exc

        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "plan": None,
            "references": {},
            "before": {},
            "ordered_paths": [],
            "mutation_references": {},
            "query_readbacks": [],
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "plan": None,
                    "references": {},
                    "before": {},
                    "ordered_paths": [],
                    "mutation_references": {},
                    "query_readbacks": [],
                }
            )

            def read(reference):
                path = reference.path
                if path in callback_state["before"]:
                    return callback_state["before"][path]
                try:
                    snapshot = reference.get(transaction=transaction)
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "operator decline transaction read failed before writes"
                    ) from exc
                observed = (
                    bool(snapshot.exists),
                    snapshot.to_dict() if snapshot.exists else None,
                )
                callback_state["references"][path] = reference
                callback_state["before"][path] = observed
                callback_state["ordered_paths"].append(path)
                return observed

            binding_exists, binding_payload = read(binding_ref)
            if not binding_exists:
                reject(
                    RowAuthorityAmbiguous(
                        "operator decline is missing its stable thread binding"
                    )
                )
            try:
                binding = validate_thread_row_binding_document(
                    document=binding_payload
                )
                if (
                    binding["userScopeHash"] != checked_scope
                    or binding["threadId"] != checked_thread_id
                ):
                    raise RowAuthorityConfigError(
                        "thread binding occupies the wrong user or thread path"
                    )
                action = build_operator_action_document(
                    user_scope_hash=checked_scope,
                    actor_scope_hash=checked_actor_scope,
                    row_bindings_hash=binding["rowBindingsHash"],
                    client_request_id=checked_client_request_id,
                    issued_at=checked_issued_at,
                )
                action_ref = user_ref.collection(
                    "rowOperatorActions"
                ).document(action["actionId"])
                request_context = _derive_claim_request_context(
                    user_scope_hash=checked_scope,
                    authority_origin="authenticated_operator",
                    authority_link=None,
                    operator_action_document=action,
                    fanout_id=None,
                    thread_binding_document=binding,
                )
                claim_ref = user_ref.collection("rowClaimSets").document(
                    request_context["requestId"]
                )
            except Exception as exc:
                reject(
                    RowAuthorityConflict(
                        "operator decline binding or request identity is malformed"
                    )
                )

            action_exists, action_payload = read(action_ref)
            claim_exists, claim_payload = read(claim_ref)
            stored_claim = None
            if claim_exists:
                try:
                    stored_claim = validate_claim_set_document(
                        document=claim_payload
                    )
                    if (
                        stored_claim["requestId"]
                        != request_context["requestId"]
                    ):
                        raise RowAuthorityConfigError(
                            "operator claim occupies the wrong request path"
                        )
                except Exception as exc:
                    reject(
                        RowAuthorityConflict(
                            "stored operator claim contains immutable drift"
                        )
                    )

            basic_states = []
            row_references = {}
            for row_binding in binding["rowBindings"]:
                row_id = row_binding["rowId"]
                identity_ref = user_ref.collection(
                    "rowIdentities"
                ).document(row_id)
                head_ref = user_ref.collection(
                    "rowAuthorityHeads"
                ).document(row_id)
                identity_exists, identity_payload = read(identity_ref)
                head_exists, head_payload = read(head_ref)
                if not identity_exists or not head_exists:
                    reject(
                        RowAuthorityAmbiguous(
                            "operator decline is missing a bound identity or head"
                        )
                    )
                try:
                    preliminary_head = validate_row_authority_head(
                        document=head_payload
                    )
                except Exception as exc:
                    reject(
                        RowAuthorityAmbiguous(
                            "operator decline current row head is malformed"
                        )
                    )
                basic_states.append(
                    {
                        "rowId": row_id,
                        "identity": identity_payload,
                        "head": head_payload,
                        "preliminaryHead": preliminary_head,
                    }
                )
                row_references[row_id] = {
                    "identity": identity_ref,
                    "head": head_ref,
                }

            row_states = []
            all_pending = all(
                basic["preliminaryHead"]["state"] == "review_pending"
                and basic["preliminaryHead"]["effectiveOwnerKind"]
                == "human_decision"
                for basic in basic_states
            )
            skip_candidates = (
                (action_exists and not claim_exists)
                or (not action_exists and not claim_exists and all_pending)
            )
            for basic in basic_states:
                row_id = basic["rowId"]
                head = basic["preliminaryHead"]
                current_number = head["effectiveOwnerGeneration"]
                try:
                    settlements_query = (
                        user_ref.collection("rowOwnerSettlements")
                        .where("rowId", "==", row_id)
                        .order_by("generation", direction="DESCENDING")
                        .limit(2)
                    )
                    settlement_snapshots = tuple(
                        transaction.get(settlements_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "operator decline bounded settlement query failed before writes"
                    ) from exc
                latest_settlements = []
                latest_settlement_authorities = []
                for settlement_snapshot in settlement_snapshots:
                    settlement_exists, settlement_payload = read(
                        settlement_snapshot.reference
                    )
                    if not settlement_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline bounded settlement query returned a missing document"
                            )
                        )
                    latest_settlements.append(
                        {
                            "path": settlement_snapshot.reference.path,
                            "document": settlement_payload,
                        }
                    )
                    settlement_generation = None
                    settlement_claim = None
                    try:
                        checked_settlement = (
                            validate_owner_settlement_document(
                                document=settlement_payload
                            )
                        )
                        settlement_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(
                            _generation_document_id(
                                row_id=row_id,
                                generation=checked_settlement["generation"],
                            )
                        )
                        (
                            settlement_generation_exists,
                            settlement_generation_payload,
                        ) = read(settlement_generation_ref)
                        if settlement_generation_exists:
                            settlement_generation = (
                                settlement_generation_payload
                            )
                            checked_settlement_generation = (
                                validate_owner_generation_document(
                                    document=settlement_generation_payload
                                )
                            )
                            settlement_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_settlement_generation["requestId"]
                            )
                            (
                                settlement_claim_exists,
                                settlement_claim_payload,
                            ) = read(settlement_claim_ref)
                            if settlement_claim_exists:
                                settlement_claim = settlement_claim_payload
                    except RowAuthorityRetryable:
                        raise
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "operator decline bounded settlement authority is malformed"
                            )
                        )
                    latest_settlement_authorities.append(
                        {
                            "generation": settlement_generation,
                            "claimSet": settlement_claim,
                        }
                    )
                callback_state["query_readbacks"].append(
                    {
                        "kind": "settlement_history",
                        "rowId": row_id,
                        "query": settlements_query,
                        "matches": tuple(
                            (
                                entry["path"],
                                _defensive_copy(entry["document"]),
                            )
                            for entry in latest_settlements
                        ),
                    }
                )
                latest_predecessor_release_matches = []
                latest_predecessor_restored_authority = None
                predecessor_release_hash = (
                    _bounded_predecessor_release_lookup_hash(
                        latest_settlements=latest_settlements,
                        latest_authorities=latest_settlement_authorities,
                    )
                )
                if predecessor_release_hash is not None:
                    try:
                        history_release_query = (
                            user_ref.collection(
                                "contactOptOutFanoutResults"
                            )
                            .where("rowId", "==", row_id)
                            .where(
                                "releasedRowSettlementHash",
                                "==",
                                predecessor_release_hash,
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        history_release_snapshots = tuple(
                            transaction.get(history_release_query)
                        )
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "operator decline predecessor-history release query failed before writes"
                        ) from exc
                    for release_snapshot in history_release_snapshots:
                        release_exists, release_payload = read(
                            release_snapshot.reference
                        )
                        if not release_exists:
                            reject(
                                RowAuthorityAmbiguous(
                                    "operator decline predecessor-history release query returned a missing document"
                                )
                            )
                        latest_predecessor_release_matches.append(
                            {
                                "path": release_snapshot.reference.path,
                                "document": release_payload,
                            }
                        )
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": history_release_query,
                            "matches": tuple(
                                (
                                    entry["path"],
                                    _defensive_copy(entry["document"]),
                                )
                                for entry in (
                                    latest_predecessor_release_matches
                                )
                            ),
                        }
                    )
                    if len(latest_predecessor_release_matches) == 1:
                        latest_predecessor_restored_authority = (
                            _read_bounded_release_restored_authority(
                                user_ref=user_ref,
                                row_id=row_id,
                                release_document=(
                                    latest_predecessor_release_matches[0][
                                        "document"
                                    ]
                                ),
                                read=read,
                            )
                        )

                current_generation = None
                current_claim = None
                current_settlement = None
                current_settlement_ref = None
                if current_number is not None:
                    current_id = _generation_document_id(
                        row_id=row_id,
                        generation=current_number,
                    )
                    current_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(current_id)
                    current_generation_exists, current_generation_payload = read(
                        current_generation_ref
                    )
                    if current_generation_exists:
                        current_generation = current_generation_payload
                        try:
                            checked_current_generation = (
                                validate_owner_generation_document(
                                    document=current_generation_payload
                                )
                            )
                            current_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_current_generation["requestId"]
                            )
                        except Exception as exc:
                            reject(
                                RowAuthorityConflict(
                                    "operator decline current generation is malformed"
                                )
                            )
                        current_claim_exists, current_claim_payload = read(
                            current_claim_ref
                        )
                        if current_claim_exists:
                            current_claim = current_claim_payload
                            try:
                                validate_claim_set_document(
                                    document=current_claim_payload
                                )
                            except Exception as exc:
                                reject(
                                    RowAuthorityConflict(
                                        "operator decline current claim is malformed"
                                    )
                                )
                    current_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(current_id)
                    (
                        current_settlement_exists,
                        current_settlement_payload,
                    ) = read(current_settlement_ref)
                    if current_settlement_exists:
                        current_settlement = current_settlement_payload

                release_result = None
                release_result_path = None
                released_authority = None
                restored_authority = None
                if (
                    head["latestSettlementHash"]
                    != head["effectiveSettlementHash"]
                    and head["latestOptOutReleaseResultHash"] is not None
                ):
                    try:
                        release_query = (
                            user_ref.collection("contactOptOutFanoutResults")
                            .where("rowId", "==", row_id)
                            .where(
                                "contactFanoutResultHash",
                                "==",
                                head["latestOptOutReleaseResultHash"],
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        release_snapshots = tuple(
                            transaction.get(release_query)
                        )
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "operator decline release-result query failed before writes"
                        ) from exc
                    if len(release_snapshots) != 1:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline release-result bridge is missing or duplicated"
                            )
                        )
                    release_snapshot = release_snapshots[0]
                    release_exists, release_payload = read(
                        release_snapshot.reference
                    )
                    if not release_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline release-result query returned a missing document"
                            )
                        )
                    release_result = release_payload
                    release_result_path = release_snapshot.reference.path
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": release_query,
                            "matches": (
                                (
                                    release_result_path,
                                    _defensive_copy(release_payload),
                                ),
                            ),
                        }
                    )
                    try:
                        checked_release_result = (
                            validate_contact_fanout_result_document(
                                document=release_payload
                            )
                        )
                        released_number = checked_release_result[
                            "releasedRowGeneration"
                        ]
                        released_id = _generation_document_id(
                            row_id=row_id,
                            generation=released_number,
                        )
                        released_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(released_id)
                        (
                            released_generation_exists,
                            released_generation_payload,
                        ) = read(released_generation_ref)
                        released_claim_payload = None
                        if released_generation_exists:
                            checked_released_generation = (
                                validate_owner_generation_document(
                                    document=released_generation_payload
                                )
                            )
                            released_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(
                                checked_released_generation["requestId"]
                            )
                            (
                                released_claim_exists,
                                observed_released_claim,
                            ) = read(released_claim_ref)
                            if released_claim_exists:
                                released_claim_payload = observed_released_claim
                        released_settlement_ref = user_ref.collection(
                            "rowOwnerSettlements"
                        ).document(released_id)
                        (
                            released_settlement_exists,
                            released_settlement_payload,
                        ) = read(released_settlement_ref)
                        released_authority = {
                            "path": released_settlement_ref.path,
                            "generation": (
                                released_generation_payload
                                if released_generation_exists
                                else None
                            ),
                            "claimSet": released_claim_payload,
                            "settlement": (
                                released_settlement_payload
                                if released_settlement_exists
                                else None
                            ),
                        }
                        restored_number = checked_release_result[
                            "restoredEffectiveGeneration"
                        ]
                        if restored_number is not None:
                            restored_id = _generation_document_id(
                                row_id=row_id,
                                generation=restored_number,
                            )
                            restored_generation_ref = user_ref.collection(
                                "rowOwnerGenerations"
                            ).document(restored_id)
                            (
                                restored_generation_exists,
                                restored_generation_payload,
                            ) = read(restored_generation_ref)
                            restored_claim_payload = None
                            if restored_generation_exists:
                                checked_restored_generation = (
                                    validate_owner_generation_document(
                                        document=restored_generation_payload
                                    )
                                )
                                restored_claim_ref = user_ref.collection(
                                    "rowClaimSets"
                                ).document(
                                    checked_restored_generation["requestId"]
                                )
                                (
                                    restored_claim_exists,
                                    observed_restored_claim,
                                ) = read(restored_claim_ref)
                                if restored_claim_exists:
                                    restored_claim_payload = (
                                        observed_restored_claim
                                    )
                            restored_settlement_ref = user_ref.collection(
                                "rowOwnerSettlements"
                            ).document(restored_id)
                            (
                                restored_settlement_exists,
                                restored_settlement_payload,
                            ) = read(restored_settlement_ref)
                            restored_authority = {
                                "generation": (
                                    restored_generation_payload
                                    if restored_generation_exists
                                    else None
                                ),
                                "claimSet": restored_claim_payload,
                                "settlement": (
                                    restored_settlement_payload
                                    if restored_settlement_exists
                                    else None
                                ),
                            }
                    except RowAuthorityRetryable:
                        raise
                    except Exception:
                        released_authority = {"malformed": True}
                        restored_authority = {"malformed": True}

                provisional_state = {
                    **basic,
                    "currentGeneration": current_generation,
                    "currentClaimSet": current_claim,
                    "currentSettlement": current_settlement,
                    "latestSettlements": latest_settlements,
                    "latestSettlementAuthorities": (
                        latest_settlement_authorities
                    ),
                    "latestPredecessorReleaseMatches": (
                        latest_predecessor_release_matches
                    ),
                    "latestPredecessorRestoredAuthority": (
                        latest_predecessor_restored_authority
                    ),
                    "releaseResult": release_result,
                    "releaseResultPath": release_result_path,
                    "releasedAuthority": released_authority,
                    "restoredAuthority": restored_authority,
                }
                try:
                    bounded_history = _validate_bounded_row_history(
                        scope=checked_scope,
                        row_id=row_id,
                        head=head,
                        row_state=provisional_state,
                    )
                except RowAuthorityError as exc:
                    reject(exc)

                try:
                    action_settlements_query = (
                        user_ref.collection("rowOwnerSettlements")
                        .where("rowId", "==", row_id)
                        .where(
                            "operatorActionHash",
                            "==",
                            action["operatorActionHash"],
                        )
                        .order_by("__name__")
                        .limit(2)
                    )
                    action_settlement_snapshots = tuple(
                        transaction.get(action_settlements_query)
                    )
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "operator decline action-settlement query failed before writes"
                    ) from exc
                action_settlement_matches = []
                action_query_matches = []
                for action_settlement_snapshot in action_settlement_snapshots:
                    (
                        action_settlement_exists,
                        action_settlement_payload,
                    ) = read(action_settlement_snapshot.reference)
                    if not action_settlement_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline action-settlement query returned a missing document"
                            )
                        )
                    action_query_matches.append(
                        (
                            action_settlement_snapshot.reference.path,
                            _defensive_copy(action_settlement_payload),
                        )
                    )
                    try:
                        checked_action_settlement = (
                            validate_owner_settlement_document(
                                document=action_settlement_payload
                            )
                        )
                        action_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(
                            _generation_document_id(
                                row_id=row_id,
                                generation=checked_action_settlement[
                                    "generation"
                                ],
                            )
                        )
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "operator decline action settlement is malformed"
                            )
                        )
                    (
                        action_generation_exists,
                        action_generation_payload,
                    ) = read(action_generation_ref)
                    if not action_generation_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline action settlement lacks its generation"
                            )
                        )
                    try:
                        checked_action_generation = (
                            validate_owner_generation_document(
                                document=action_generation_payload
                            )
                        )
                        action_claim_ref = user_ref.collection(
                            "rowClaimSets"
                        ).document(
                            checked_action_generation["requestId"]
                        )
                    except Exception as exc:
                        reject(
                            RowAuthorityConflict(
                                "operator decline action generation is malformed"
                            )
                        )
                    action_claim_exists, action_claim_payload = read(
                        action_claim_ref
                    )
                    if not action_claim_exists:
                        reject(
                            RowAuthorityAmbiguous(
                                "operator decline action settlement lacks its claim"
                            )
                        )
                    action_settlement_matches.append(
                        {
                            "path": action_settlement_snapshot.reference.path,
                            "generation": action_generation_payload,
                            "claimSet": action_claim_payload,
                            "settlement": action_settlement_payload,
                        }
                    )
                callback_state["query_readbacks"].append(
                    {
                        "kind": "operator_action_history",
                        "rowId": row_id,
                        "query": action_settlements_query,
                        "matches": tuple(action_query_matches),
                    }
                )

                stored_decision = None
                if stored_claim is not None:
                    decisions = [
                        decision
                        for decision in stored_claim["rowDecisions"]
                        if decision["rowId"] == row_id
                    ]
                    if len(decisions) != 1:
                        reject(
                            RowAuthorityConflict(
                                "operator claim decisions do not cover its binding"
                            )
                        )
                    stored_decision = decisions[0]
                candidate_generation = None
                candidate_settlement = None
                candidate_predecessor_generation = None
                candidate_predecessor_claim = None
                candidate_predecessor_settlement = None
                candidate_predecessor_release_matches = []
                candidate_predecessor_restored_authority = None
                if not skip_candidates:
                    if (
                        stored_claim is not None
                        and stored_claim["outcome"] == "accepted"
                    ):
                        candidate_number = stored_decision[
                            "plannedGeneration"
                        ]
                    else:
                        candidate_number = bounded_history["nextGeneration"]
                    candidate_id = _generation_document_id(
                        row_id=row_id,
                        generation=candidate_number,
                    )
                    candidate_generation_ref = user_ref.collection(
                        "rowOwnerGenerations"
                    ).document(candidate_id)
                    candidate_settlement_ref = user_ref.collection(
                        "rowOwnerSettlements"
                    ).document(candidate_id)
                    candidate_generation_exists, candidate_generation_payload = read(
                        candidate_generation_ref
                    )
                    candidate_settlement_exists, candidate_settlement_payload = read(
                        candidate_settlement_ref
                    )
                    candidate_generation = (
                        candidate_generation_payload
                        if candidate_generation_exists
                        else None
                    )
                    candidate_settlement = (
                        candidate_settlement_payload
                        if candidate_settlement_exists
                        else None
                    )
                    row_references[row_id].update(
                        {
                            "candidate_generation": candidate_generation_ref,
                            "candidate_settlement": candidate_settlement_ref,
                        }
                    )
                    if (
                        stored_claim is not None
                        and stored_claim["outcome"] == "accepted"
                        and candidate_number > 1
                    ):
                        predecessor_id = _generation_document_id(
                            row_id=row_id,
                            generation=candidate_number - 1,
                        )
                        predecessor_generation_ref = user_ref.collection(
                            "rowOwnerGenerations"
                        ).document(predecessor_id)
                        (
                            predecessor_generation_exists,
                            predecessor_generation_payload,
                        ) = read(predecessor_generation_ref)
                        if predecessor_generation_exists:
                            candidate_predecessor_generation = (
                                predecessor_generation_payload
                            )
                            try:
                                checked_predecessor_generation = (
                                    validate_owner_generation_document(
                                        document=predecessor_generation_payload
                                    )
                                )
                                predecessor_claim_ref = user_ref.collection(
                                    "rowClaimSets"
                                ).document(
                                    checked_predecessor_generation[
                                        "requestId"
                                    ]
                                )
                            except Exception as exc:
                                reject(
                                    RowAuthorityConflict(
                                        "operator claim replay predecessor generation is malformed"
                                    )
                                )
                            (
                                predecessor_claim_exists,
                                predecessor_claim_payload,
                            ) = read(predecessor_claim_ref)
                            if predecessor_claim_exists:
                                candidate_predecessor_claim = (
                                    predecessor_claim_payload
                                )
                        predecessor_settlement_ref = user_ref.collection(
                            "rowOwnerSettlements"
                        ).document(predecessor_id)
                        (
                            predecessor_settlement_exists,
                            predecessor_settlement_payload,
                        ) = read(predecessor_settlement_ref)
                        if predecessor_settlement_exists:
                            candidate_predecessor_settlement = (
                                predecessor_settlement_payload
                            )
                        try:
                            checked_candidate_generation = (
                                validate_owner_generation_document(
                                    document=candidate_generation
                                )
                            )
                            checked_predecessor_settlement = (
                                validate_owner_settlement_document(
                                    document=candidate_predecessor_settlement
                                )
                            )
                        except Exception:
                            checked_candidate_generation = None
                            checked_predecessor_settlement = None
                        if (
                            checked_candidate_generation is not None
                            and checked_predecessor_settlement is not None
                            and checked_candidate_generation["rowId"] == row_id
                            and checked_candidate_generation["generation"]
                            == candidate_number
                            and checked_predecessor_settlement["rowId"] == row_id
                            and checked_predecessor_settlement["generation"]
                            == candidate_number - 1
                            and checked_predecessor_settlement["outcome"]
                            == "contact_optout"
                            and checked_candidate_generation[
                                "predecessorSettlementHash"
                            ]
                            != checked_predecessor_settlement[
                                "settlementHash"
                            ]
                        ):
                            try:
                                predecessor_release_query = (
                                    user_ref.collection(
                                        "contactOptOutFanoutResults"
                                    )
                                    .where("rowId", "==", row_id)
                                    .where(
                                        "releasedRowSettlementHash",
                                        "==",
                                        checked_predecessor_settlement[
                                            "settlementHash"
                                        ],
                                    )
                                    .order_by("__name__")
                                    .limit(2)
                                )
                                predecessor_release_snapshots = tuple(
                                    transaction.get(
                                        predecessor_release_query
                                    )
                                )
                            except Exception as exc:
                                callback_state["read_failed"] = True
                                raise RowAuthorityRetryable(
                                    "operator predecessor-release query failed before writes"
                                ) from exc
                            for release_snapshot in (
                                predecessor_release_snapshots
                            ):
                                release_exists, release_payload = read(
                                    release_snapshot.reference
                                )
                                if not release_exists:
                                    reject(
                                        RowAuthorityAmbiguous(
                                            "operator predecessor-release query returned a missing document"
                                        )
                                    )
                                candidate_predecessor_release_matches.append(
                                    {
                                        "path": release_snapshot.reference.path,
                                        "document": release_payload,
                                    }
                                )
                            callback_state["query_readbacks"].append(
                                {
                                    "kind": "stable",
                                    "query": predecessor_release_query,
                                    "matches": tuple(
                                        (
                                            entry["path"],
                                            _defensive_copy(
                                                entry["document"]
                                            ),
                                        )
                                        for entry in (
                                            candidate_predecessor_release_matches
                                        )
                                    ),
                                }
                            )
                            if len(candidate_predecessor_release_matches) == 1:
                                candidate_predecessor_restored_authority = (
                                    _read_bounded_release_restored_authority(
                                        user_ref=user_ref,
                                        row_id=row_id,
                                        release_document=(
                                            candidate_predecessor_release_matches[
                                                0
                                            ]["document"]
                                        ),
                                        read=read,
                                    )
                                )

                replay_winner_matches = []
                replay_winner_claim = None
                replay_winner_settlement = None
                replay_winner_successor = {
                    "generation": None,
                    "claimSet": None,
                    "settlement": None,
                    "linkReleaseMatches": [],
                    "linkRestoredAuthority": None,
                    "restorationReleaseMatches": [],
                    "restoredWinnerAuthority": None,
                    "restorationExitGeneration": None,
                    "restorationExitClaimSet": None,
                    "restorationExitSettlement": None,
                }
                if (
                    stored_claim is not None
                    and stored_claim["outcome"] == "dominated"
                    and stored_decision["decision"] == "dominated"
                ):
                    try:
                        winner_query = (
                            user_ref.collection("rowOwnerGenerations")
                            .where("rowId", "==", row_id)
                            .where(
                                "generationHash",
                                "==",
                                stored_decision["winnerGenerationHash"],
                            )
                            .order_by("__name__")
                            .limit(2)
                        )
                        winner_snapshots = tuple(
                            transaction.get(winner_query)
                        )
                    except Exception as exc:
                        callback_state["read_failed"] = True
                        raise RowAuthorityRetryable(
                            "operator decline replay winner query failed before writes"
                        ) from exc
                    for winner_snapshot in winner_snapshots:
                        winner_exists, winner_payload = read(
                            winner_snapshot.reference
                        )
                        if winner_exists:
                            replay_winner_matches.append(
                                {
                                    "path": winner_snapshot.reference.path,
                                    "document": winner_payload,
                                }
                            )
                    callback_state["query_readbacks"].append(
                        {
                            "kind": "stable",
                            "query": winner_query,
                            "matches": tuple(
                                (
                                    entry["path"],
                                    _defensive_copy(entry["document"]),
                                )
                                for entry in replay_winner_matches
                            ),
                        }
                    )
                    if len(replay_winner_matches) == 1:
                        try:
                            winner_generation = (
                                validate_owner_generation_document(
                                    document=replay_winner_matches[0][
                                        "document"
                                    ]
                                )
                            )
                            winner_claim_ref = user_ref.collection(
                                "rowClaimSets"
                            ).document(winner_generation["requestId"])
                            winner_id = _generation_document_id(
                                row_id=row_id,
                                generation=winner_generation["generation"],
                            )
                            winner_settlement_ref = user_ref.collection(
                                "rowOwnerSettlements"
                            ).document(winner_id)
                        except Exception as exc:
                            reject(
                                RowAuthorityConflict(
                                    "operator decline replay winner generation is malformed"
                                )
                            )
                        winner_claim_exists, winner_claim_payload = read(
                            winner_claim_ref
                        )
                        if winner_claim_exists:
                            replay_winner_claim = winner_claim_payload
                        (
                            winner_settlement_exists,
                            winner_settlement_payload,
                        ) = read(winner_settlement_ref)
                        if winner_settlement_exists:
                            replay_winner_settlement = (
                                winner_settlement_payload
                            )
                        try:
                            replay_winner_successor = (
                                _read_bounded_replay_winner_successor(
                                    user_ref=user_ref,
                                    row_id=row_id,
                                    winner_generation=winner_generation,
                                    winner_settlement=(
                                        replay_winner_settlement
                                    ),
                                    claim_created_at=stored_claim["createdAt"],
                                    read=read,
                                    transaction=transaction,
                                    query_readbacks=callback_state[
                                        "query_readbacks"
                                    ],
                                )
                            )
                        except RowAuthorityError as exc:
                            reject(exc)

                row_references[row_id]["current_settlement"] = (
                    current_settlement_ref
                )
                row_states.append(
                    {
                        **provisional_state,
                        "actionSettlementMatches": (
                            action_settlement_matches
                        ),
                        "candidateGeneration": candidate_generation,
                        "candidateSettlement": candidate_settlement,
                        "candidatePredecessorGeneration": (
                            candidate_predecessor_generation
                        ),
                        "candidatePredecessorClaimSet": (
                            candidate_predecessor_claim
                        ),
                        "candidatePredecessorSettlement": (
                            candidate_predecessor_settlement
                        ),
                        "candidatePredecessorReleaseMatches": (
                            candidate_predecessor_release_matches
                        ),
                        "candidatePredecessorRestoredAuthority": (
                            candidate_predecessor_restored_authority
                        ),
                        "replayWinnerMatches": replay_winner_matches,
                        "replayWinnerClaimSet": replay_winner_claim,
                        "replayWinnerSettlement": (
                            replay_winner_settlement
                        ),
                        "replayWinnerSuccessorGeneration": (
                            replay_winner_successor["generation"]
                        ),
                        "replayWinnerSuccessorClaimSet": (
                            replay_winner_successor["claimSet"]
                        ),
                        "replayWinnerSuccessorSettlement": (
                            replay_winner_successor["settlement"]
                        ),
                        "replayWinnerSuccessorReleaseMatches": (
                            replay_winner_successor[
                                "linkReleaseMatches"
                            ]
                        ),
                        "replayWinnerSuccessorRestoredAuthority": (
                            replay_winner_successor[
                                "linkRestoredAuthority"
                            ]
                        ),
                        "replayWinnerRestorationReleaseMatches": (
                            replay_winner_successor[
                                "restorationReleaseMatches"
                            ]
                        ),
                        "replayWinnerRestoredAuthority": (
                            replay_winner_successor[
                                "restoredWinnerAuthority"
                            ]
                        ),
                        "replayWinnerRestorationExitGeneration": (
                            replay_winner_successor[
                                "restorationExitGeneration"
                            ]
                        ),
                        "replayWinnerRestorationExitClaimSet": (
                            replay_winner_successor[
                                "restorationExitClaimSet"
                            ]
                        ),
                        "replayWinnerRestorationExitSettlement": (
                            replay_winner_successor[
                                "restorationExitSettlement"
                            ]
                        ),
                    }
                )

            try:
                plan = _plan_operator_decline(
                    user_scope_hash=checked_scope,
                    thread_binding_document=binding,
                    operator_action_document=action,
                    stored_operator_action_document=(
                        action_payload if action_exists else None
                    ),
                    row_states=row_states,
                    stored_claim_set_document=stored_claim,
                )
            except RowAuthorityError as exc:
                reject(exc)
            mutations = plan["mutations"]
            exact_count = len(mutations)
            _require_row_authority_planned_writes(exact_count)
            expected_count = 0
            if plan["disposition"] == "declined":
                expected_count = (
                    plan["claimSet"]["plannedWrites"]
                    if plan["claimSet"]["authorityOrigin"]
                    == "authenticated_operator"
                    else 1 + (2 * binding["bindingCount"])
                )
            elif plan["disposition"] == "dominated":
                expected_count = plan["claimSet"]["plannedWrites"]
            if exact_count != expected_count:
                reject(
                    RowAuthorityConfigError(
                        "operator decline callback write plan is not exact"
                    )
                )

            mutation_references = {
                "action": action_ref,
                "claim_set": claim_ref,
            }
            for generation in plan["generations"]:
                row_id = generation["rowId"]
                generation_ref = user_ref.collection(
                    "rowOwnerGenerations"
                ).document(
                    _generation_document_id(
                        row_id=row_id,
                        generation=generation["generation"],
                    )
                )
                mutation_references[f"generation:{row_id}"] = generation_ref
            for settlement in plan["settlements"]:
                row_id = settlement["rowId"]
                settlement_ref = user_ref.collection(
                    "rowOwnerSettlements"
                ).document(
                    _generation_document_id(
                        row_id=row_id,
                        generation=settlement["generation"],
                    )
                )
                mutation_references[f"settlement:{row_id}"] = settlement_ref
            for head in plan["heads"]:
                mutation_references[f"head:{head['rowId']}"] = row_references[
                    head["rowId"]
                ]["head"]
            callback_state["mutation_references"] = mutation_references
            for mutation in mutations:
                reference = mutation_references.get(mutation["target"])
                if (
                    reference is None
                    or reference.path not in callback_state["before"]
                ):
                    reject(
                        RowAuthorityConfigError(
                            "operator decline mutation has no preread exact reference"
                        )
                    )
                if mutation["operation"] == "create":
                    transaction.create(reference, mutation["document"])
                elif mutation["operation"] == "set":
                    transaction.set(
                        reference,
                        mutation["document"],
                        merge=False,
                    )
                else:
                    reject(
                        RowAuthorityConfigError(
                            "operator decline mutation operation is unsupported"
                        )
                    )
            callback_state["prepared"] = bool(mutations)
            callback_state["disposition"] = plan["disposition"]
            callback_state["plan"] = plan
            return plan["disposition"]

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "operator decline transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "operator decline transaction could not start"
                ) from exc
            plan = callback_state["plan"]
            if plan is None:
                raise RowAuthorityAmbiguous(
                    "operator decline commit has no complete prepared plan"
                ) from exc
            try:
                readback = {}
                for path in callback_state["ordered_paths"]:
                    reference = callback_state["references"][path]
                    snapshot = reference.get()
                    readback[path] = (
                        bool(snapshot.exists),
                        snapshot.to_dict() if snapshot.exists else None,
                    )
                query_observed = []
                for query_readback in callback_state["query_readbacks"]:
                    query = query_readback["query"]
                    stream = getattr(query, "stream", None)
                    if callable(stream):
                        query_snapshots = tuple(stream())
                    else:
                        get = getattr(query, "get", None)
                        if not callable(get):
                            raise RuntimeError(
                                "operator decline bounded query cannot be read back"
                            )
                        query_snapshots = tuple(get())
                    query_observed.append(
                        tuple(
                            (
                                snapshot.reference.path,
                                snapshot.to_dict(),
                            )
                            for snapshot in query_snapshots
                            if snapshot.exists
                        )
                    )
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "operator decline commit outcome cannot be read back"
                ) from readback_exc
            expected_after = dict(callback_state["before"])
            for mutation in plan["mutations"]:
                reference = callback_state["mutation_references"][
                    mutation["target"]
                ]
                expected_after[reference.path] = (
                    True,
                    mutation["document"],
                )
            exact_before = readback == callback_state["before"]
            exact_after = readback == expected_after
            query_before_ok = all(
                observed == query_readback["matches"]
                for observed, query_readback in zip(
                    query_observed,
                    callback_state["query_readbacks"],
                    strict=True,
                )
            )
            query_after_expectations = []
            for query_readback in callback_state["query_readbacks"]:
                expected_matches = query_readback["matches"]
                if query_readback["kind"] in {
                    "settlement_history",
                    "operator_action_history",
                }:
                    by_path = {
                        path: _defensive_copy(document)
                        for path, document in expected_matches
                    }
                    for mutation in plan["mutations"]:
                        if not mutation["target"].startswith("settlement:"):
                            continue
                        document = mutation["document"]
                        if document["rowId"] != query_readback["rowId"]:
                            continue
                        if (
                            query_readback["kind"]
                            == "operator_action_history"
                            and document["operatorActionHash"]
                            != plan["action"]["operatorActionHash"]
                        ):
                            continue
                        reference = callback_state[
                            "mutation_references"
                        ][mutation["target"]]
                        by_path[reference.path] = _defensive_copy(document)
                    expected_matches = tuple(
                        sorted(
                            by_path.items(),
                            key=lambda item: (
                                item[1]["generation"],
                                item[0],
                            ),
                            reverse=True,
                        )[:2]
                    )
                query_after_expectations.append(expected_matches)
            query_after_ok = all(
                observed == expected_matches
                for observed, expected_matches in zip(
                    query_observed,
                    query_after_expectations,
                    strict=True,
                )
            )
            if exact_after and query_after_ok:
                disposition = plan["disposition"]
            elif exact_before and query_before_ok and not plan["mutations"]:
                disposition = plan["disposition"]
            elif exact_before and query_before_ok:
                raise RowAuthorityRetryable(
                    "operator decline commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "operator decline commit readback is partial or drifted"
                ) from exc
        plan = callback_state["plan"]
        if plan is None or disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "operator decline transaction returned a mismatched disposition"
            )
        if plan["mutations"] and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "operator decline reported an unprepared mutation"
            )
        return _operator_decline_result(
            disposition=disposition,
            action=plan["action"],
            claim_set=plan["claimSet"],
            generations=plan["generations"],
            settlements=plan["settlements"],
            heads=plan["heads"],
        )

    def record_contact_row_association(
        self,
        *,
        verified_user_id,
        canonical_mailbox_identity_hash,
        exact_identity_hash,
        row_id,
        thread_id,
        created_at,
    ):
        maximum_planned_writes = 3
        if maximum_planned_writes > MAX_ROW_AUTHORITY_PLANNED_WRITES:
            raise RowAuthorityConfigError(
                "contact association exceeds the planned-write ceiling"
            )
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        checked_canonical_hash = _require_sha256(
            canonical_mailbox_identity_hash,
            field_name="canonical_mailbox_identity_hash",
        )
        checked_exact_hash = _require_sha256(
            exact_identity_hash,
            field_name="exact_identity_hash",
        )
        checked_row_id = validate_row_id(row_id)
        checked_thread_id = _require_thread_document_id(
            thread_id,
            field_name="thread_id",
        )
        checked_created_at = _require_timestamp(
            created_at,
            field_name="created_at",
        )
        proposed_association = build_contact_row_binding_document(
            user_scope_hash=checked_scope,
            canonical_mailbox_identity_hash=checked_canonical_hash,
            row_id=checked_row_id,
            created_at=checked_created_at,
        )
        try:
            user_ref = self._firestore.collection("users").document(
                checked_user_id
            )
            optout_head_ref = user_ref.collection(
                "contactOptOutHeads"
            ).document(checked_canonical_hash)
            thread_binding_ref = user_ref.collection(
                "threadRowBindings"
            ).document(checked_thread_id)
            row_identity_ref = user_ref.collection("rowIdentities").document(
                checked_row_id
            )
            row_head_ref = user_ref.collection("rowAuthorityHeads").document(
                checked_row_id
            )
            association_ref = user_ref.collection("contactRowBindings").document(
                proposed_association["edgeId"]
            )
            contact_binding_head_ref = user_ref.collection(
                "contactRowBindingHeads"
            ).document(checked_canonical_hash)
        except Exception as exc:
            raise RowAuthorityConfigError(
                "contact association cannot form exact document paths"
            ) from exc

        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "read_failed": False,
            "disposition": None,
            "references": None,
            "observed": None,
            "plan": None,
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "read_failed": False,
                    "disposition": None,
                    "references": None,
                    "observed": None,
                    "plan": None,
                }
            )

            def read_one(reference):
                try:
                    return self._read_reference_payloads(
                        (reference,),
                        transaction=transaction,
                    )[0]
                except Exception as exc:
                    callback_state["read_failed"] = True
                    raise RowAuthorityRetryable(
                        "contact association transaction read failed before writes"
                    ) from exc

            optout_observed = read_one(optout_head_ref)
            if optout_observed[0]:
                reject(
                    RowAuthorityConflict(
                        "an existing contact opt-out head blocks association"
                    )
                )

            binding_observed = read_one(thread_binding_ref)
            if not binding_observed[0]:
                reject(
                    RowAuthorityAmbiguous(
                        "contact association is missing its thread binding"
                    )
                )
            try:
                thread_binding = validate_thread_row_binding_document(
                    document=binding_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "contact association thread binding is malformed"
                    )
                )
            if (
                thread_binding["userScopeHash"] != checked_scope
                or thread_binding["threadId"] != checked_thread_id
            ):
                reject(
                    RowAuthorityConflict(
                        "contact association thread binding does not correlate"
                    )
                )
            matching_reverse = [
                document
                for document in build_row_thread_binding_documents(
                    thread_binding_document=thread_binding
                )
                if document["rowId"] == checked_row_id
            ]
            if len(matching_reverse) != 1:
                reject(
                    RowAuthorityConflict(
                        "thread binding does not authorize the requested row"
                    )
                )
            expected_reverse = matching_reverse[0]
            proposed_evidence = (
                build_contact_row_binding_evidence_document(
                    user_scope_hash=checked_scope,
                    edge_id=proposed_association["edgeId"],
                    thread_id=checked_thread_id,
                    thread_binding_hash=thread_binding["bindingHash"],
                    exact_identity_hash=checked_exact_hash,
                    created_at=checked_created_at,
                )
            )
            try:
                reverse_ref = user_ref.collection(
                    "rowThreadBindings"
                ).document(expected_reverse["edgeId"])
                evidence_ref = user_ref.collection(
                    "contactRowBindingEvidence"
                ).document(proposed_evidence["evidenceId"])
            except Exception as exc:
                reject(
                    RowAuthorityConfigError(
                        "contact association cannot form derived document paths"
                    )
                )

            trailing_references = (
                reverse_ref,
                row_identity_ref,
                row_head_ref,
                association_ref,
                evidence_ref,
                contact_binding_head_ref,
            )
            trailing_observed = tuple(
                read_one(reference) for reference in trailing_references
            )
            references = (
                optout_head_ref,
                thread_binding_ref,
                *trailing_references,
            )
            observed = (
                optout_observed,
                binding_observed,
                *trailing_observed,
            )
            callback_state["references"] = references
            callback_state["observed"] = observed

            reverse_observed, identity_observed, row_head_observed = (
                trailing_observed[:3]
            )
            if not all(
                item[0]
                for item in (
                    reverse_observed,
                    identity_observed,
                    row_head_observed,
                )
            ):
                reject(
                    RowAuthorityAmbiguous(
                        "contact association is missing row authority proof"
                    )
                )
            try:
                reverse_binding = validate_row_thread_binding_document(
                    document=reverse_observed[1]
                )
                row_identity = validate_row_identity_document(
                    document=identity_observed[1]
                )
                row_head = validate_row_authority_head(
                    document=row_head_observed[1]
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "contact association row authority proof is malformed"
                    )
                )
            if reverse_binding != expected_reverse:
                reject(
                    RowAuthorityConflict(
                        "contact association reverse binding does not correlate"
                    )
                )

            association_observed, evidence_observed, binding_head_observed = (
                trailing_observed[3:]
            )
            try:
                plan = _plan_contact_row_association(
                    thread_binding_document=thread_binding,
                    reverse_binding_document=reverse_binding,
                    row_identity_document=row_identity,
                    row_head_document=row_head,
                    proposed_association_document=proposed_association,
                    proposed_evidence_document=proposed_evidence,
                    stored_association_document=(
                        association_observed[1]
                        if association_observed[0]
                        else None
                    ),
                    stored_evidence_document=(
                        evidence_observed[1] if evidence_observed[0] else None
                    ),
                    contact_binding_head_document=(
                        binding_head_observed[1]
                        if binding_head_observed[0]
                        else None
                    ),
                )
            except RowAuthorityError as exc:
                reject(exc)

            mutations = plan["mutations"]
            if len(mutations) not in {0, 1, 3}:
                reject(
                    RowAuthorityConfigError(
                        "contact association produced an invalid write count"
                    )
                )
            if len(mutations) > MAX_ROW_AUTHORITY_PLANNED_WRITES:
                reject(
                    RowAuthorityConfigError(
                        "contact association exceeds the planned-write ceiling"
                    )
                )
            target_references = {
                "association": association_ref,
                "evidence": evidence_ref,
                "binding_head": contact_binding_head_ref,
            }
            callback_state["plan"] = plan
            callback_state["disposition"] = plan["disposition"]
            callback_state["prepared"] = bool(mutations)
            for mutation in mutations:
                reference = target_references[mutation["target"]]
                if mutation["operation"] == "create":
                    transaction.create(reference, mutation["document"])
                elif mutation["operation"] == "set":
                    transaction.set(
                        reference,
                        mutation["document"],
                        merge=False,
                    )
                else:
                    reject(
                        RowAuthorityConfigError(
                            "contact association produced an invalid mutation"
                        )
                    )
            return plan["disposition"]

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "contact association transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["read_failed"]:
                raise
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "contact association transaction could not start"
                ) from exc
            references = callback_state["references"]
            observed = callback_state["observed"]
            plan = callback_state["plan"]
            if references is None or observed is None or plan is None:
                raise RowAuthorityAmbiguous(
                    "contact association commit has no complete before-image"
                ) from exc
            try:
                readback = self._read_reference_payloads(references)
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "contact association commit outcome cannot be read back"
                ) from readback_exc

            expected_after = list(observed)
            target_indexes = {
                "association": 5,
                "evidence": 6,
                "binding_head": 7,
            }
            for mutation in plan["mutations"]:
                expected_after[target_indexes[mutation["target"]]] = (
                    True,
                    mutation["document"],
                )
            exact_before = readback == observed
            exact_after = readback == tuple(expected_after)
            if exact_after:
                disposition = plan["disposition"]
            elif exact_before and plan["mutations"]:
                raise RowAuthorityRetryable(
                    "contact association commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "contact association commit readback is partial or drifted"
                ) from exc

        if disposition not in {
            "created",
            "evidence_created",
            "already_applied",
        }:
            raise RowAuthorityRetryable(
                "contact association returned no approved disposition"
            )
        if disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "contact association returned a mismatched disposition"
            )
        plan = callback_state["plan"]
        if plan is None:
            raise RowAuthorityRetryable(
                "contact association returned without an observed plan"
            )
        if plan["mutations"] and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "contact association returned an unprepared write disposition"
            )
        return _contact_association_result(
            disposition=disposition,
            association=plan["association"],
            evidence=plan["evidence"],
            binding_head=plan["bindingHead"],
        )

    def advance_row_location(
        self,
        *,
        verified_user_id,
        row_id,
        expected_head,
        observations,
        lifecycle,
        observed_at,
    ):
        planned_writes = 2
        if planned_writes > MAX_ROW_AUTHORITY_PLANNED_WRITES:
            raise RowAuthorityConfigError(
                "row location change exceeds the planned-write ceiling"
            )
        expected = validate_row_authority_head(document=expected_head)
        checked_row_id = validate_row_id(row_id)
        checked_user_id = _require_firestore_document_id(
            verified_user_id,
            field_name="verified_user_id",
        )
        checked_scope = user_scope_hash(checked_user_id)
        if expected["userScopeHash"] != checked_scope:
            raise RowAuthorityConfigError(
                "expected head does not belong to the verified user"
            )
        if expected["rowId"] != checked_row_id:
            raise RowAuthorityConfigError(
                "expected head does not belong to the requested row"
            )
        checked_observed_at = _require_timestamp(
            observed_at,
            field_name="observed_at",
        )
        if checked_observed_at < expected["updatedAt"]:
            raise RowAuthorityConfigError(
                "location observation cannot predate the expected head"
            )
        canonical_observations = _validated_evidence_observations(
            lifecycle=lifecycle,
            observations=observations,
        )
        previous_number = expected["currentLocationRevision"]
        candidate_number = previous_number + 1
        references = self._location_references(
            verified_user_id=checked_user_id,
            row_id=checked_row_id,
            previous_revision=previous_number,
            candidate_revision=candidate_number,
        )
        identity_ref, head_ref, previous_ref, candidate_ref = references
        callback_state = {
            "entered": False,
            "prepared": False,
            "rejected": False,
            "disposition": None,
            "identity": None,
            "previous": None,
            "candidate": None,
            "result_head": None,
        }

        def reject(error):
            callback_state["rejected"] = True
            raise error

        def validate_required_documents(observed):
            if any(not exists for exists, _payload in observed[:3]):
                reject(
                    RowAuthorityAmbiguous(
                        "row location state is missing a required document"
                    )
                )
            identity_payload = observed[0][1]
            actual_head_payload = observed[1][1]
            previous_payload = observed[2][1]
            try:
                identity = validate_row_identity_document(
                    document=identity_payload
                )
                actual_head = validate_row_authority_head(
                    document=actual_head_payload
                )
                previous = _validate_row_location_revision(previous_payload)
                candidate = (
                    _validate_row_location_revision(observed[3][1])
                    if observed[3][0]
                    else None
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "row location state contains a malformed document"
                    )
                )
            identity_correlations = (
                identity["userScopeHash"] == expected["userScopeHash"],
                identity["rowId"] == expected["rowId"],
                identity["createdAt"] == expected["createdAt"],
            )
            if not all(identity_correlations):
                reject(
                    RowAuthorityConflict(
                        "row identity does not correlate to the expected head"
                    )
                )
            previous_correlations = (
                previous["userScopeHash"] == identity["userScopeHash"],
                previous["rowId"] == identity["rowId"],
                previous["spreadsheetId"] == identity["spreadsheetId"],
                previous["sheetId"] == identity["sheetId"],
                previous["markerHash"] == identity["markerHash"],
                previous["revision"] == previous_number,
                previous["revisionHash"] == expected["currentLocationHash"],
                previous["lifecycle"]
                == expected["currentLocationLifecycle"],
                identity["createdAt"]
                <= previous["observedAt"]
                <= expected["updatedAt"],
            )
            if not all(previous_correlations):
                reject(
                    RowAuthorityConflict(
                        "immutable location revision does not match the head"
                    )
                )
            if (
                actual_head["userScopeHash"] != expected["userScopeHash"]
                or actual_head["rowId"] != expected["rowId"]
            ):
                reject(
                    RowAuthorityConflict(
                        "actual head does not correlate to the requested row"
                    )
                )
            if candidate is not None:
                candidate_correlations = (
                    candidate["userScopeHash"] == identity["userScopeHash"],
                    candidate["rowId"] == identity["rowId"],
                    candidate["spreadsheetId"] == identity["spreadsheetId"],
                    candidate["sheetId"] == identity["sheetId"],
                    candidate["markerHash"] == identity["markerHash"],
                    candidate["revision"] == candidate_number,
                    candidate["previousRevisionHash"]
                    == expected["currentLocationHash"],
                )
                if not all(candidate_correlations):
                    reject(
                        RowAuthorityConflict(
                            "candidate location revision is immutable drift"
                        )
                    )
            return identity, actual_head, previous, candidate

        def prepare(transaction):
            callback_state.update(
                {
                    "entered": True,
                    "prepared": False,
                    "rejected": False,
                    "disposition": None,
                    "identity": None,
                    "previous": None,
                    "candidate": None,
                    "result_head": None,
                }
            )
            try:
                observed = self._read_reference_payloads(
                    references,
                    transaction=transaction,
                )
            except Exception as exc:
                reject(
                    RowAuthorityAmbiguous(
                        "row location state cannot be read transactionally"
                    )
                )
            if expected["currentLocationLifecycle"] == "deleted":
                reject(
                    RowAuthorityConflict(
                        "a deleted row identity cannot be reactivated"
                    )
                )
            identity, actual_head, previous, stored_candidate = (
                validate_required_documents(observed)
            )
            callback_state["identity"] = identity
            callback_state["previous"] = previous
            try:
                candidate = build_row_location_revision_document(
                    identity_document=identity,
                    revision=candidate_number,
                    lifecycle=lifecycle,
                    observations=canonical_observations,
                    previous_revision_hash=expected["currentLocationHash"],
                    observed_at=checked_observed_at,
                )
                result_head = build_location_advanced_head(
                    expected_head=expected,
                    location_revision_document=candidate,
                )
            except RowAuthorityConfigError as exc:
                reject(
                    RowAuthorityConflict(
                        "row observations do not match the immutable identity"
                    )
                )
            callback_state["candidate"] = candidate
            callback_state["result_head"] = result_head
            if actual_head == result_head and stored_candidate == candidate:
                callback_state["disposition"] = "already_applied"
                return "already_applied"
            if actual_head != expected:
                reject(
                    RowAuthorityConflict(
                        "actual row authority head is stale or drifted"
                    )
                )
            if stored_candidate is not None:
                reject(
                    RowAuthorityConflict(
                        "candidate location revision already exists without its head"
                    )
                )
            if _location_semantics(candidate) == _location_semantics(previous):
                callback_state["disposition"] = "unchanged"
                return "unchanged"
            callback_state["prepared"] = True
            callback_state["disposition"] = "advanced"
            transaction.create(candidate_ref, candidate)
            transaction.set(head_ref, result_head, merge=False)
            return "advanced"

        try:
            transaction = self._firestore.transaction()
        except Exception as exc:
            raise RowAuthorityRetryable(
                "row location transaction could not be created"
            ) from exc
        try:
            disposition = self._transaction_executor(transaction, prepare)
        except Exception as exc:
            if callback_state["rejected"]:
                raise
            if not callback_state["entered"]:
                raise RowAuthorityRetryable(
                    "row location transaction could not start"
                ) from exc
            identity = callback_state["identity"]
            previous = callback_state["previous"]
            candidate = callback_state["candidate"]
            result_head = callback_state["result_head"]
            readback_references = (
                identity_ref,
                previous_ref,
                candidate_ref,
                head_ref,
            )
            try:
                readback = self._read_reference_payloads(readback_references)
            except Exception as readback_exc:
                raise RowAuthorityAmbiguous(
                    "row location commit outcome cannot be read back"
                ) from readback_exc
            exact_before = (
                identity is not None
                and previous is not None
                and readback
                == (
                    (True, identity),
                    (True, previous),
                    (False, None),
                    (True, expected),
                )
            )
            exact_after = (
                identity is not None
                and previous is not None
                and candidate is not None
                and result_head is not None
                and readback
                == (
                    (True, identity),
                    (True, previous),
                    (True, candidate),
                    (True, result_head),
                )
            )
            if exact_after and callback_state["prepared"]:
                disposition = "advanced"
            elif (
                exact_after
                and callback_state["disposition"] == "already_applied"
            ):
                disposition = "already_applied"
            elif exact_before and callback_state["disposition"] == "unchanged":
                disposition = "unchanged"
            elif exact_before:
                raise RowAuthorityRetryable(
                    "row location commit failed before any apply"
                ) from exc
            else:
                raise RowAuthorityAmbiguous(
                    "row location commit readback is partial or drifted"
                ) from exc
        if disposition not in {"advanced", "unchanged", "already_applied"}:
            raise RowAuthorityRetryable(
                "row location transaction returned no approved disposition"
            )
        if disposition == "advanced" and not callback_state["prepared"]:
            raise RowAuthorityRetryable(
                "row location transaction reported an unprepared advance"
            )
        if disposition != callback_state["disposition"]:
            raise RowAuthorityRetryable(
                "row location transaction returned a mismatched disposition"
            )
        identity = callback_state["identity"]
        if disposition == "unchanged":
            revision = callback_state["previous"]
            result_head = expected
        else:
            revision = callback_state["candidate"]
            result_head = callback_state["result_head"]
        if any(value is None for value in (identity, revision, result_head)):
            raise RowAuthorityRetryable(
                "row location transaction returned an incomplete result"
            )
        return _location_result(
            disposition=disposition,
            identity=identity,
            revision=revision,
            head=result_head,
        )
