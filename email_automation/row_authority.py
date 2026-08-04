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
MAX_OPAQUE_BYTES = 512
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
