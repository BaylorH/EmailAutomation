"""Provider-free primitive contracts for B2 stable row authority."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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
        "sitesift.row.authority_head.v1",
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
    elif state == "settled":
        if (
            not has_owner
            or has_lease
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
        "sitesift.row.authority_head.v1",
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
