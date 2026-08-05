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
MAX_MAILBOX_BYTES = 320
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
