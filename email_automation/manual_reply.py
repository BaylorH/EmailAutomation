"""Canonical source identity helpers for exact dashboard manual replies."""

from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from google.cloud.firestore import SERVER_TIMESTAMP

from .outbound_safety import validate_outbound_body


_DOMAIN = "sitesift-manual-reply-resolution:v1"
_AUTHORITY_DOMAIN = "sitesift-manual-reply-authority:v1"
_SOURCE = "dashboard_inline_reply"
_IMMUTABLE_ID_PREFERENCE = 'IdType="ImmutableId"'
_SNAPSHOT_FIELDS = frozenset(
    {
        "outbox",
        "activeClientSlot",
        "archivedClientSlot",
        "thread",
        "followUp",
        "notification",
        "actionAudit",
        "sourceBinding",
        "selectedAccount",
        "graphMe",
        "serverAuthority",
        "logicalResolution",
        "globalAccess",
        "clientDecision",
        "outboundMode",
    }
)
_ACCOUNT_FIELDS = (
    "home_account_id",
    "local_account_id",
    "environment",
    "realm",
    "username",
)
_TASK7_REQUIRED_FIELDS = frozenset({
    "manualReplyLaneVersion",
    "source",
    "actionType",
    "status",
    "assignedEmails",
    "ccEmails",
    "script",
    "clientId",
    "threadId",
    "replyToMessageId",
    "sourceMessageId",
    "sourceGraphMessageId",
    "sourceInternetMessageId",
    "notificationId",
    "notificationClientId",
    "deleteNotificationOnSend",
    "resumeThreadOnSend",
    "scriptSelectionMode",
    "forceScript",
    "actionAuditId",
})
_TASK7_ALLOWED_FIELDS = frozenset({
    "manualReplyLaneVersion",
    "source",
    "actionType",
    "status",
    "cancelRequested",
    "assignedEmails",
    "ccEmails",
    "script",
    "clientId",
    "subject",
    "contactName",
    "rowNumber",
    "isPersonalized",
    "createdAt",
    "threadId",
    "replyToMessageId",
    "sourceMessageId",
    "sourceGraphMessageId",
    "sourceInternetMessageId",
    "sourceMessage",
    "notificationId",
    "notificationClientId",
    "deleteNotificationOnSend",
    "sourceDeadLetterId",
    "resumeThreadOnSend",
    "scriptSelectionMode",
    "forceScript",
    "actionReason",
    "actionAuditId",
})
_TASK9A_SERVER_OUTBOX_FIELDS = frozenset({
    "processingBy",
    "processingAt",
    "serverRoute",
})
_ELIGIBLE_AUTHORITY_REQUIRED_FIELDS = frozenset({
    "schemaVersion",
    "status",
    "uid",
    "clientId",
    "threadId",
    "notificationId",
    "source",
    "graphLookupMessageId",
    "normalizedInternetMessageId",
    "conversationId",
    "authenticatedMailboxAddress",
    "fromAddress",
    "senderAddress",
    "sourceAudience",
    "audience",
    "createdAt",
    "updatedAt",
})
_ELIGIBLE_AUTHORITY_ALLOWED_FIELDS = (
    _ELIGIBLE_AUTHORITY_REQUIRED_FIELDS | {"immutableGraphMessageId"}
)
_CLAIMED_AUTHORITY_FIELDS = _ELIGIBLE_AUTHORITY_REQUIRED_FIELDS | {
    "immutableGraphMessageId",
    "ownerOutboxId",
    "actionAuditId",
    "internetMessageId",
    "reviewedBodyHash",
    "snapshotHash",
    "fence",
    "claimedAt",
}
_INTERNET_BOUNDARY_WHITESPACE = " \t\r\n"
_PUBLIC_RESULT_REASONS = {
    "processed": frozenset({"sent"}),
    "terminal_no_effect": frozenset({"cancelled", "not_found"}),
    "manual_review": frozenset({"send_lane_pending", "item_not_sendable"}),
    "invalid": frozenset({"invalid_request"}),
}
_TASK1_BLOCKED_STATUSES = frozenset(
    {
        "blocked_state_changed",
        "blocked_non_manual",
        "blocked_generic_owned",
        "blocked_invalid_client",
        "blocked_invalid_thread",
        "blocked_invalid_notification",
        "blocked_invalid_action_audit",
        "blocked_missing_action_audit",
        "blocked_audit_status",
        "blocked_audit_actor",
        "blocked_audit_source",
        "blocked_audit_action_type",
        "blocked_audit_client",
        "blocked_audit_thread",
        "blocked_audit_notification",
        "blocked_audit_outbox",
    }
)
_BOUNDARY_REJECTED_CODE_POINTS = frozenset(
    {
        0x0009,
        0x000A,
        0x000B,
        0x000C,
        0x000D,
        0x0020,
        0x0085,
        0x00A0,
        0x1680,
        0x2000,
        0x2001,
        0x2002,
        0x2003,
        0x2004,
        0x2005,
        0x2006,
        0x2007,
        0x2008,
        0x2009,
        0x200A,
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    }
)


def _require_string(value: object, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be a primitive string")
    return value


def _utf8_bytes(value: str, field: str, maximum: int) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} is not valid UTF-8") from error
    if not 1 <= len(encoded) <= maximum:
        raise ValueError(f"{field} is outside its UTF-8 byte limit")
    return encoded


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _has_unsafe_body_control(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc"
        and character not in {"\t", "\n", "\r"}
        for character in value
    )


def _require_aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a persisted datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{field} must be timezone-aware") from error
    if value.tzinfo is None or offset is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _has_rejected_boundary(value: str) -> bool:
    return (
        ord(value[0]) in _BOUNDARY_REJECTED_CODE_POINTS
        or ord(value[-1]) in _BOUNDARY_REJECTED_CODE_POINTS
    )


def _validate_document_id(value: object, field: str) -> str:
    canonical = _require_string(value, field)
    _utf8_bytes(canonical, field, 1500)
    if _has_rejected_boundary(canonical):
        raise ValueError(f"{field} has a rejected boundary code point")
    if (
        "/" in canonical
        or _has_control(canonical)
        or canonical in {".", ".."}
        or (canonical.startswith("__") and canonical.endswith("__"))
    ):
        raise ValueError(f"{field} is not a canonical document ID")
    return canonical


def is_canonical_document_id(value: object) -> bool:
    """Return whether *value* is one canonical Firestore document ID."""

    try:
        _validate_document_id(value, "document_id")
    except (TypeError, ValueError):
        return False
    return True


def bounded_manual_reply_result(result: object) -> dict[str, str]:
    """Return the public status/reason envelope or reject an unknown result."""

    if not isinstance(result, dict):
        raise ValueError("manual reply result must be an object")
    status = result.get("status")
    reason = result.get("reason")
    if type(status) is not str or type(reason) is not str:
        raise ValueError("manual reply result must contain status and reason enums")
    if reason not in _PUBLIC_RESULT_REASONS.get(status, frozenset()):
        raise ValueError("manual reply result contains an unsupported enum")
    return {"status": status, "reason": reason}


def process_outbox_item(uid: str, outbox_id: str) -> dict[str, str]:
    """Continue one exact Task 2 item into the reviewed manual-reply lane.

    Task 8 adds only the narrow runner seam.  A ready item remains fail-closed
    for manual review until Task 9 installs the independently reviewed sender;
    this function acquires no token and performs no Graph or broad-pipeline work.
    """

    try:
        canonical_uid = _validate_document_id(uid, "uid")
        canonical_outbox_id = _validate_document_id(outbox_id, "outbox_id")
    except (TypeError, ValueError):
        return {"status": "invalid", "reason": "invalid_request"}

    # Lazy import avoids making the exact Task 2 classifier part of main.py's
    # long-lived runner API. Task 9 replaces this handoff inside this module.
    from .email import process_outbox_item as classify_exact_outbox_item

    classification = classify_exact_outbox_item(
        canonical_uid,
        canonical_outbox_id,
    )
    task1_status = (
        classification.get("status")
        if isinstance(classification, dict)
        else None
    )
    if task1_status == "manual_ready":
        return {"status": "manual_review", "reason": "send_lane_pending"}
    if task1_status in {"cancelled", "not_found"}:
        return {
            "status": "terminal_no_effect",
            "reason": task1_status,
        }
    if task1_status in _TASK1_BLOCKED_STATUSES:
        return {"status": "manual_review", "reason": "item_not_sendable"}
    raise ValueError("exact outbox classifier returned an unsupported status")


def _validate_graph_message_id(value: object) -> str:
    opaque = _require_string(value, "immutable_graph_message_id")
    _utf8_bytes(opaque, "immutable_graph_message_id", 2048)
    if _has_rejected_boundary(opaque) or _has_control(opaque):
        raise ValueError("immutable_graph_message_id is not canonical")
    return opaque


def normalize_internet_message_id(internet_message_id: str) -> str:
    """Validate and ASCII-case-normalize one Internet Message-ID."""

    raw = _require_string(internet_message_id, "internet_message_id")
    normalized = raw.strip(_INTERNET_BOUNDARY_WHITESPACE)
    try:
        encoded = normalized.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("internet_message_id must be ASCII") from error
    if not 1 <= len(encoded) <= 998:
        raise ValueError("internet_message_id is outside its byte limit")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        raise ValueError("internet_message_id must contain printable ASCII")
    if normalized[0] != "<" or normalized[-1] != ">":
        raise ValueError("internet_message_id must be angle-bracketed")

    inner = normalized[1:-1]
    if inner.count("@") != 1:
        raise ValueError("internet_message_id must contain exactly one at-sign")
    local, domain = inner.split("@")
    if not local or not domain:
        raise ValueError("internet_message_id pair must be nonblank")
    if any(character.isspace() or character in "<>" for character in inner):
        raise ValueError("internet_message_id contains an invalid interior character")
    return normalized.lower()


def manual_reply_resolution_key(
    *,
    uid: str,
    thread_id: str,
    immutable_graph_message_id: str,
    internet_message_id: str,
    source: str,
) -> str:
    """Return the versioned, length-framed source identity SHA-256 key."""

    canonical_uid = _validate_document_id(uid, "uid")
    canonical_thread_id = _validate_document_id(thread_id, "thread_id")
    opaque_graph_id = _validate_graph_message_id(immutable_graph_message_id)
    normalized_internet_id = normalize_internet_message_id(internet_message_id)
    canonical_source = _require_string(source, "source")
    if canonical_source != _SOURCE:
        raise ValueError("source is not supported")

    digest = hashlib.sha256()
    for member in (
        _DOMAIN,
        canonical_uid,
        canonical_thread_id,
        opaque_graph_id,
        normalized_internet_id,
        canonical_source,
    ):
        encoded = member.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def manual_reply_authority_key(
    *,
    uid: str,
    client_id: str,
    notification_id: str,
) -> str:
    """Return the user/client/notification-scoped authority document key."""

    members = (
        _AUTHORITY_DOMAIN,
        _validate_document_id(uid, "uid"),
        _validate_document_id(client_id, "client_id"),
        _validate_document_id(notification_id, "notification_id"),
    )
    digest = hashlib.sha256()
    for member in members:
        encoded = member.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _require_exact_keys(value: object, expected: set[str], field: str) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{field} has an unsupported shape")
    return value


def _validate_snapshot_shape(snapshot: dict) -> None:
    if type(snapshot["outbox"]) is not dict:
        raise ValueError("snapshot.outbox has an unsupported shape")
    outbox = dict(snapshot["outbox"])
    _validate_document_id(outbox.pop("id", None), "snapshot.outbox.id")
    _validate_task7_outbox(outbox, snapshot["outbox"]["id"])
    if outbox.get("status") != "manual_reply_claimed":
        raise ValueError("snapshot.outbox is not claimed")

    active = _require_exact_keys(snapshot["activeClientSlot"], {
        "present", "location", "record",
    }, "snapshot.activeClientSlot")
    if active["record"] is not None:
        _require_exact_keys(active["record"], {"status"}, "snapshot.activeClientSlot.record")
    archived = _require_exact_keys(snapshot["archivedClientSlot"], {
        "present", "record",
    }, "snapshot.archivedClientSlot")
    if archived["record"] is not None:
        _require_exact_keys(archived["record"], {"status"}, "snapshot.archivedClientSlot.record")
    _require_exact_keys(snapshot["thread"], {"id", "status"}, "snapshot.thread")
    _require_exact_keys(snapshot["followUp"], {
        "enabled", "processingBy",
    }, "snapshot.followUp")
    _require_exact_keys(snapshot["notification"], {
        "id", "kind", "authorityKey",
    }, "snapshot.notification")
    action_audit = snapshot["actionAudit"]
    action_audit_required = {
        "id", "status", "actorUid", "source", "actionType", "clientId",
        "threadId", "notificationId", "finalBody",
        "finalRecipients", "finalCcRecipients", "replyToMessageId",
        "sourceMessageId", "sourceGraphMessageId", "sourceInternetMessageId",
    }
    if (
        type(action_audit) is not dict
        or not action_audit_required.issubset(action_audit)
        or not set(action_audit).issubset(action_audit_required | {"outboxId"})
    ):
        raise ValueError("snapshot.actionAudit has an unsupported shape")
    source = _require_exact_keys(snapshot["sourceBinding"], {
        "graphLookupMessageId", "normalizedInternetMessageId", "conversationId",
        "authenticatedMailboxAddress", "fromAddress", "senderAddress",
        "sourceAudience", "audience",
    }, "snapshot.sourceBinding")
    _require_exact_keys(source["sourceAudience"], {
        "to", "cc", "bcc", "replyTo",
    }, "snapshot.sourceBinding.sourceAudience")
    _require_exact_keys(source["audience"], {"to", "cc", "bcc"}, "snapshot.sourceBinding.audience")
    _require_exact_keys(snapshot["selectedAccount"], set(_ACCOUNT_FIELDS), "snapshot.selectedAccount")
    _require_exact_keys(snapshot["graphMe"], {
        "id", "mail", "userPrincipalName",
    }, "snapshot.graphMe")
    authority = _require_exact_keys(
        snapshot["serverAuthority"],
        set(_CLAIMED_AUTHORITY_FIELDS) | {"authorityPath"},
        "snapshot.serverAuthority",
    )
    _require_exact_keys(authority["sourceAudience"], {
        "to", "cc", "bcc", "replyTo",
    }, "snapshot.serverAuthority.sourceAudience")
    _require_exact_keys(authority["audience"], {"to", "cc", "bcc"}, "snapshot.serverAuthority.audience")
    for field in ("createdAt", "updatedAt", "claimedAt"):
        _require_aware_datetime(
            authority[field],
            f"snapshot.serverAuthority.{field}",
        )
    resolution = _require_exact_keys(snapshot["logicalResolution"], {
        "present", "record",
    }, "snapshot.logicalResolution")
    _require_exact_keys(resolution["record"], {
        "status", "uid", "outboxId", "resolutionKey", "workerId", "fence",
    }, "snapshot.logicalResolution.record")
    _require_exact_keys(snapshot["globalAccess"], {"state"}, "snapshot.globalAccess")
    _require_exact_keys(snapshot["clientDecision"], {
        "state", "stopKind",
    }, "snapshot.clientDecision")
    _require_string(snapshot["outboundMode"], "snapshot.outboundMode")


def _canonical_json_value(value: object, field: str) -> object:
    """Return a type-tagged value while rejecting coercive or unstable types."""

    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is str:
        return ["string", value]
    if isinstance(value, datetime):
        timestamp = _require_aware_datetime(value, field)
        return ["datetime", timestamp.astimezone(timezone.utc).isoformat()]
    if type(value) is list:
        return ["list", [
            _canonical_json_value(item, f"{field}[]")
            for item in value
        ]]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError(f"{field} contains a non-string key")
        canonical = []
        for key in sorted(value):
            canonical.append([
                key,
                _canonical_json_value(value[key], f"{field}.{key}"),
            ])
        return ["object", canonical]
    raise ValueError(f"{field} contains a non-canonical JSON value")


def manual_reply_snapshot_hash(snapshot: object) -> str:
    """Hash the closed, typed pre-send snapshot without string coercion."""

    if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_FIELDS:
        raise ValueError("manual reply snapshot has an unsupported shape")
    _validate_snapshot_shape(snapshot)
    canonical = _canonical_json_value(snapshot, "snapshot")
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manual_reply_resolution_disposition(
    record: object,
    *,
    uid: str,
    outbox_id: str,
    resolution_key: str,
    worker_id: str,
) -> str:
    """Classify one exact logical barrier without granting send authority."""

    expected = {
        "uid": _validate_document_id(uid, "uid"),
        "outboxId": _validate_document_id(outbox_id, "outbox_id"),
        "resolutionKey": _validate_document_id(
            resolution_key,
            "resolution_key",
        ),
        "workerId": _validate_document_id(worker_id, "worker_id"),
    }
    if record is None:
        return "prepare"
    if type(record) is not dict:
        return "manual_review"
    if any(record.get(key) != value for key, value in expected.items()):
        return "manual_review"
    status = record.get("status")
    if status == "definite_failure":
        return "prepare"
    if status in {"accepted", "finalized"}:
        return "finalize_only"
    return "manual_review"


def manual_reply_authority_decision(
    *,
    uid: str,
    global_access: object,
    active_client: object,
    archived_client: object,
    client_binding: object,
    outbound_mode: object,
) -> dict[str, object]:
    """Return a fresh fail-closed policy decision; never consume a permit."""

    try:
        canonical_uid = _validate_document_id(uid, "uid")
    except (TypeError, ValueError):
        return {"allowed": False, "reason": "invalid_uid"}
    if type(global_access) is not dict or global_access.get("state") != "enabled":
        return {"allowed": False, "reason": "global_access_blocked"}
    if outbound_mode != "live":
        return {"allowed": False, "reason": "outbound_mode_blocked"}
    if type(active_client) is not dict or active_client.get("status") != "live":
        return {"allowed": False, "reason": "client_not_live"}
    if archived_client is not None:
        return {"allowed": False, "reason": "archived_client_present"}
    if type(client_binding) is not dict:
        return {"allowed": False, "reason": "binding_invalid"}
    client_id = client_binding.get("clientId")
    bindings_match = (
        client_binding.get("userPathUid") == canonical_uid
        and client_binding.get("activeLocation") == "clients"
        and type(client_id) is str
        and client_id
        and client_binding.get("outboxClientId") == client_id
        and client_binding.get("threadClientId") == client_id
        and client_binding.get("notificationClientId") == client_id
    )
    if not bindings_match:
        return {"allowed": False, "reason": "binding_invalid"}
    return {"allowed": True, "reason": "allowed"}


def manual_reply_global_access_decision(
    *,
    uid: str,
    policy: object,
    exists: object,
    read_error: object,
) -> dict[str, str]:
    """Classify the raw global policy without a historical UID fallback."""

    try:
        canonical_uid = _validate_document_id(uid, "uid")
    except (TypeError, ValueError):
        return {"state": "malformed"}
    if type(read_error) is not bool or type(exists) is not bool:
        return {"state": "malformed"}
    if read_error:
        return {"state": "read_error"}
    if not exists:
        return {"state": "missing"}
    if type(policy) is not dict or set(policy) != {"automationEnabled", "allowedUids"}:
        return {"state": "malformed"}
    enabled = policy.get("automationEnabled")
    allowed_uids = policy.get("allowedUids")
    if type(enabled) is not bool or type(allowed_uids) is not list:
        return {"state": "malformed"}
    if any(not is_canonical_document_id(value) for value in allowed_uids):
        return {"state": "malformed"}
    return {
        "state": "enabled"
        if enabled or canonical_uid in allowed_uids
        else "disabled"
    }


def _stable_tuple_hash(domain: str, *members: str) -> str:
    digest = hashlib.sha256()
    for member in (domain, *members):
        encoded = member.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot_data(snapshot: object) -> dict | None:
    if not getattr(snapshot, "exists", False):
        return None
    data = snapshot.to_dict()
    return data if type(data) is dict else None


def _user_document(firestore_client: object, uid: str):
    return firestore_client.collection("users").document(uid)


def _canonical_address_list(
    value: object,
    field: str,
    *,
    size: int | None = None,
) -> list[str]:
    if type(value) is not list or (size is not None and len(value) != size):
        raise ValueError(f"{field} is not an exact address list")
    return [
        _normalize_address(address, f"{field}[]")
        for address in value
    ]


def _canonical_source_audience(value: object, field: str) -> dict:
    audience = _require_exact_keys(
        value,
        {"to", "cc", "bcc", "replyTo"},
        field,
    )
    return {
        "to": _canonical_address_list(audience["to"], f"{field}.to"),
        "cc": _canonical_address_list(audience["cc"], f"{field}.cc"),
        "bcc": _canonical_address_list(audience["bcc"], f"{field}.bcc"),
        "replyTo": _canonical_address_list(
            audience["replyTo"],
            f"{field}.replyTo",
        ),
    }


def _canonical_source_projection(value: object) -> dict:
    required = {
        "graphLookupMessageId", "immutableGraphMessageId", "internetMessageId",
        "conversationId", "fromAddress", "senderAddress", "sender", "audience",
    }
    extended = {"authenticatedMailboxAddress", "sourceAudience"}
    if (
        type(value) is not dict
        or not required.issubset(value)
        or not set(value).issubset(required | extended)
        or bool(extended & set(value)) != extended.issubset(value)
    ):
        raise ValueError("canonical_source has an unsupported shape")
    source = value
    lookup_id = _validate_graph_message_id(source.get("graphLookupMessageId"))
    immutable_id = _validate_graph_message_id(source.get("immutableGraphMessageId"))
    internet_id = normalize_internet_message_id(source.get("internetMessageId"))
    conversation_id = _require_string(source.get("conversationId"), "conversation_id")
    if not conversation_id or _has_control(conversation_id):
        raise ValueError("conversation_id is not canonical")
    from_address = _normalize_address(source.get("fromAddress"), "source from")
    sender_address = _normalize_address(source.get("senderAddress"), "source sender")
    if _normalize_address(source.get("sender"), "source sender alias") != sender_address:
        raise ValueError("source sender aliases disagree")
    audience = _require_exact_keys(source.get("audience"), {
        "to", "cc", "bcc",
    }, "canonical_source.audience")
    to_addresses = _canonical_address_list(
        audience.get("to"),
        "canonical_source.audience.to",
        size=1,
    )
    if (
        _canonical_address_list(
            audience.get("cc"),
            "canonical_source.audience.cc",
            size=0,
        )
        or _canonical_address_list(
            audience.get("bcc"),
            "canonical_source.audience.bcc",
            size=0,
        )
    ):
        raise ValueError("canonical source audience is not exact")
    recipient = to_addresses[0]
    if from_address != recipient or sender_address != recipient:
        raise ValueError("canonical source sender and audience disagree")
    canonical = {
        "graphLookupMessageId": lookup_id,
        "immutableGraphMessageId": immutable_id,
        "internetMessageId": internet_id,
        "conversationId": conversation_id,
        "fromAddress": from_address,
        "senderAddress": sender_address,
        "sender": sender_address,
        "audience": {"to": [recipient], "cc": [], "bcc": []},
    }
    if extended.issubset(source):
        authenticated_mailbox = _normalize_address(
            source["authenticatedMailboxAddress"],
            "canonical_source.authenticatedMailboxAddress",
        )
        source_audience = _canonical_source_audience(
            source["sourceAudience"],
            "canonical_source.sourceAudience",
        )
        if source_audience != {
            "to": [authenticated_mailbox],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        }:
            raise ValueError("canonical source mailbox audience is not exact")
        canonical.update({
            "authenticatedMailboxAddress": authenticated_mailbox,
            "sourceAudience": source_audience,
        })
    return canonical


def _validate_task7_outbox(data: object, outbox_id: str) -> dict:
    if type(data) is not dict:
        raise ValueError("outbox is missing")
    status = data.get("status")
    allowed = set(_TASK7_ALLOWED_FIELDS)
    required = set(_TASK7_REQUIRED_FIELDS)
    if status == "manual_reply_claimed":
        allowed.update(_TASK9A_SERVER_OUTBOX_FIELDS)
        required.update(_TASK9A_SERVER_OUTBOX_FIELDS)
    elif status != "queued":
        raise ValueError("outbox has an unsupported status")
    if not required.issubset(data) or not set(data).issubset(allowed):
        raise ValueError("outbox is incomplete")
    if (
        data.get("manualReplyLaneVersion") != 1
        or data.get("source") != _SOURCE
        or data.get("actionType") != "reply"
        or data.get("forceScript") is not True
        or data.get("scriptSelectionMode") != "exact"
        or data.get("deleteNotificationOnSend") is not True
        or data.get("resumeThreadOnSend") is not True
        or data.get("ccEmails") != []
        or (
            "cancelRequested" in data
            and data.get("cancelRequested") is not False
        )
    ):
        raise ValueError("outbox is not an exact manual reply")
    for field in (
        "actionAuditId", "clientId", "notificationClientId",
        "notificationId", "threadId", "sourceMessageId",
    ):
        _validate_document_id(data.get(field), field)
    reply_alias = _validate_graph_message_id(data.get("replyToMessageId"))
    graph_alias = _validate_graph_message_id(data.get("sourceGraphMessageId"))
    if data.get("notificationClientId") != data.get("clientId"):
        raise ValueError("notification client binding changed")
    if reply_alias != graph_alias:
        raise ValueError("source aliases disagree")
    internet_id = normalize_internet_message_id(
        data.get("sourceInternetMessageId")
    )
    if normalize_internet_message_id(data.get("sourceMessageId")) != internet_id:
        raise ValueError("stored source message key is not the Internet Message-ID")
    recipients = data.get("assignedEmails")
    if type(recipients) is not list or len(recipients) != 1:
        raise ValueError("manual reply must have one recipient")
    _normalize_address(recipients[0], "outbox recipient")
    script = _require_string(data.get("script"), "script")
    if (
        not script.strip()
        or _has_unsafe_body_control(script)
        or not validate_outbound_body(script).is_safe
    ):
        raise ValueError("outbox script is not sendable")
    if "sourceMessage" in data and type(data["sourceMessage"]) is not dict:
        raise ValueError("outbox sourceMessage must be an object")
    if "createdAt" in data:
        _require_aware_datetime(data["createdAt"], "outbox.createdAt")
    if "rowNumber" in data and type(data["rowNumber"]) is not int:
        raise ValueError("outbox.rowNumber must be an integer")
    if "isPersonalized" in data and type(data["isPersonalized"]) is not bool:
        raise ValueError("outbox.isPersonalized must be a boolean")
    if status == "manual_reply_claimed":
        _validate_document_id(data["processingBy"], "outbox.processingBy")
        _require_aware_datetime(data["processingAt"], "outbox.processingAt")
        route = _require_exact_keys(data["serverRoute"], {
            "kind", "resolutionKey", "fence",
        }, "outbox.serverRoute")
        if route["kind"] != "manual_reply":
            raise ValueError("outbox server route is not manual reply")
        _validate_document_id(route["resolutionKey"], "resolution_key")
        _validate_document_id(route["fence"], "fence")
    _validate_document_id(outbox_id, "outbox_id")
    return data


def _validate_action_audit(data: object, *, uid: str, outbox_id: str, outbox: dict) -> dict:
    if type(data) is not dict:
        raise ValueError("action audit is missing")
    expected = {
        "status": "queued",
        "actorUid": uid,
        "source": _SOURCE,
        "actionType": "reply",
        "clientId": outbox["clientId"],
        "threadId": outbox["threadId"],
        "notificationId": outbox["notificationId"],
        "finalBody": outbox["script"],
        "finalRecipients": outbox["assignedEmails"],
        "finalCcRecipients": [],
        "replyToMessageId": outbox["replyToMessageId"],
        "sourceMessageId": outbox["sourceMessageId"],
        "sourceGraphMessageId": outbox["sourceGraphMessageId"],
    }
    if any(data.get(field) != value for field, value in expected.items()):
        raise ValueError("action audit binding changed")
    if "outboxId" in data and data.get("outboxId") != outbox_id:
        raise ValueError("action audit outbox binding changed")
    if normalize_internet_message_id(data.get("sourceInternetMessageId")) != normalize_internet_message_id(
        outbox["sourceInternetMessageId"]
    ):
        raise ValueError("action audit source pair changed")
    return data


def _validate_eligible_authority(
    data: object,
    *,
    uid: str,
    outbox: dict,
    canonical_source: dict,
    selected_account: dict,
) -> dict:
    if (
        type(data) is not dict
        or not _ELIGIBLE_AUTHORITY_REQUIRED_FIELDS.issubset(data)
        or not set(data).issubset(_ELIGIBLE_AUTHORITY_ALLOWED_FIELDS)
    ):
        raise ValueError("manual_reply_authority has an unsupported shape")
    authority = data
    authenticated_mailbox = _normalize_address(
        authority.get("authenticatedMailboxAddress"),
        "authority authenticated mailbox",
    )
    source_audience = _canonical_source_audience(
        authority.get("sourceAudience"),
        "authority.sourceAudience",
    )
    authority_audience = _require_exact_keys(
        authority.get("audience"),
        {"to", "cc", "bcc"},
        "authority.audience",
    )
    audience = {
        "to": _canonical_address_list(
            authority_audience["to"],
            "authority.audience.to",
            size=1,
        ),
        "cc": _canonical_address_list(
            authority_audience["cc"],
            "authority.audience.cc",
            size=0,
        ),
        "bcc": _canonical_address_list(
            authority_audience["bcc"],
            "authority.audience.bcc",
            size=0,
        ),
    }
    expected = {
        "schemaVersion": 1,
        "status": "eligible",
        "uid": uid,
        "clientId": outbox["clientId"],
        "threadId": outbox["threadId"],
        "notificationId": outbox["notificationId"],
        "source": _SOURCE,
        "graphLookupMessageId": canonical_source["graphLookupMessageId"],
        "conversationId": canonical_source["conversationId"],
        "fromAddress": canonical_source["fromAddress"],
        "senderAddress": canonical_source["senderAddress"],
    }
    if any(authority.get(field) != value for field, value in expected.items()):
        raise ValueError("manual reply authority changed")
    if (
        authenticated_mailbox != selected_account["username"]
        or source_audience != {
            "to": [authenticated_mailbox],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        }
        or audience != canonical_source["audience"]
        or _normalize_address(authority.get("fromAddress"), "authority from")
        != canonical_source["fromAddress"]
        or _normalize_address(authority.get("senderAddress"), "authority sender")
        != canonical_source["senderAddress"]
    ):
        raise ValueError("manual reply authority audience changed")
    if normalize_internet_message_id(authority.get("normalizedInternetMessageId")) != canonical_source[
        "internetMessageId"
    ]:
        raise ValueError("manual reply authority source pair changed")
    if (
        "immutableGraphMessageId" in authority
        and _validate_graph_message_id(authority["immutableGraphMessageId"])
        != canonical_source["immutableGraphMessageId"]
    ):
        raise ValueError("manual reply authority immutable ID changed")
    created_at = _require_aware_datetime(
        authority.get("createdAt"),
        "authority.createdAt",
    )
    updated_at = _require_aware_datetime(
        authority.get("updatedAt"),
        "authority.updatedAt",
    )
    if created_at != updated_at:
        raise ValueError("eligible authority timestamps do not share one commit")
    return authority


def _graph_me_projection(value: object) -> dict:
    if type(value) is not dict:
        raise ValueError("Graph /me is missing")
    graph_id = _require_string(value.get("id"), "graph_me.id")
    principal = _normalize_address(value.get("userPrincipalName"), "graph_me.userPrincipalName")
    mail = _normalize_address(value.get("mail"), "graph_me.mail")
    return {"id": graph_id, "mail": mail, "userPrincipalName": principal}


def _hash_canonical_payload(value: object, field: str) -> str:
    encoded = json.dumps(
        _canonical_json_value(value, field),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _claim_authority_projection(authority: dict, canonical_source: dict) -> dict:
    projection = {
        field: authority[field]
        for field in _ELIGIBLE_AUTHORITY_REQUIRED_FIELDS
    }
    projection["status"] = "eligible"
    projection["updatedAt"] = authority["createdAt"]
    projection["immutableGraphMessageId"] = canonical_source[
        "immutableGraphMessageId"
    ]
    return projection


def _claim_snapshot_hash(
    *,
    uid: str,
    outbox_id: str,
    worker_id: str,
    authority_key: str,
    outbox: dict,
    action_audit: dict,
    authority: dict,
    canonical_source: dict,
    selected_account: dict,
    graph_me: dict,
) -> str:
    return _hash_canonical_payload({
        "schemaVersion": 1,
        "uid": uid,
        "outboxId": outbox_id,
        "workerId": worker_id,
        "authorityKey": authority_key,
        "outbox": outbox,
        "actionAudit": action_audit,
        "producerAuthority": _claim_authority_projection(
            authority,
            canonical_source,
        ),
        "canonicalSource": canonical_source,
        "selectedAccount": selected_account,
        "graphMe": graph_me,
    }, "manual_reply_claim_snapshot")


def claim_manual_reply_item(
    *,
    firestore_client: object,
    uid: str,
    outbox_id: str,
    worker_id: str,
    authority_key: str,
    canonical_source: object,
    selected_account: object,
    graph_me: object,
) -> dict[str, str]:
    """Atomically fence one exact Task 7 item without granting provider send."""

    try:
        canonical_uid = _validate_document_id(uid, "uid")
        canonical_outbox_id = _validate_document_id(outbox_id, "outbox_id")
        canonical_worker_id = _validate_document_id(worker_id, "worker_id")
        canonical_authority_key = _validate_document_id(authority_key, "authority_key")
        source = _canonical_source_projection(canonical_source)
        account = _canonical_selected_account(selected_account)
        me = _graph_me_projection(graph_me)
        if not _graph_me_matches_account(me, account):
            raise ValueError("selected account and Graph /me disagree")
    except (TypeError, ValueError):
        return {"status": "invalid", "reason": "invalid_request"}

    user_ref = _user_document(firestore_client, canonical_uid)
    outbox_ref = user_ref.collection("outbox").document(canonical_outbox_id)
    authority_ref = user_ref.collection("manualReplyAuthorities").document(
        canonical_authority_key
    )
    fence_seed = secrets.token_hex(32)

    from google.cloud import firestore

    @firestore.transactional
    def claim(transaction):
        outbox_snapshot = outbox_ref.get(transaction=transaction)
        outbox = _validate_task7_outbox(
            _snapshot_data(outbox_snapshot),
            canonical_outbox_id,
        )
        if outbox.get("status") != "queued" or outbox.get("processingBy") not in (None, ""):
            return {"status": "manual_review", "reason": "item_not_queued"}
        expected_authority_key = manual_reply_authority_key(
            uid=canonical_uid,
            client_id=outbox["clientId"],
            notification_id=outbox["notificationId"],
        )
        if canonical_authority_key != expected_authority_key:
            return {"status": "manual_review", "reason": "authority_key_mismatch"}
        if (
            outbox["sourceGraphMessageId"] != source["graphLookupMessageId"]
            or normalize_internet_message_id(outbox["sourceInternetMessageId"])
            != source["internetMessageId"]
            or _normalize_address(outbox["assignedEmails"][0], "outbox recipient")
            != source["audience"]["to"][0]
        ):
            return {"status": "manual_review", "reason": "source_binding_mismatch"}

        final_resolution_key = manual_reply_resolution_key(
            uid=canonical_uid,
            thread_id=outbox["threadId"],
            immutable_graph_message_id=source["immutableGraphMessageId"],
            internet_message_id=source["internetMessageId"],
            source=_SOURCE,
        )
        audit_ref = user_ref.collection("actionAudit").document(outbox["actionAuditId"])
        resolution_ref = user_ref.collection("manualReplyResolutions").document(
            final_resolution_key
        )
        audit_snapshot = audit_ref.get(transaction=transaction)
        authority_snapshot = authority_ref.get(transaction=transaction)
        resolution_snapshot = resolution_ref.get(transaction=transaction)
        if getattr(resolution_snapshot, "exists", False):
            return {"status": "already_claimed", "reason": "logical_item_exists"}
        try:
            audit = _validate_action_audit(
                _snapshot_data(audit_snapshot),
                uid=canonical_uid,
                outbox_id=canonical_outbox_id,
                outbox=outbox,
            )
            authority = _validate_eligible_authority(
                _snapshot_data(authority_snapshot),
                uid=canonical_uid,
                outbox=outbox,
                canonical_source=source,
                selected_account=account,
            )
        except (TypeError, ValueError):
            return {"status": "manual_review", "reason": "authority_binding_mismatch"}

        reviewed_body_hash = hashlib.sha256(outbox["script"].encode("utf-8")).hexdigest()
        claim_snapshot_hash = _claim_snapshot_hash(
            uid=canonical_uid,
            outbox_id=canonical_outbox_id,
            worker_id=canonical_worker_id,
            authority_key=canonical_authority_key,
            outbox=outbox,
            action_audit=audit,
            authority=authority,
            canonical_source=source,
            selected_account=account,
            graph_me=me,
        )
        outbox_patch = {
            "status": "manual_reply_claimed",
            "processingBy": canonical_worker_id,
            "processingAt": SERVER_TIMESTAMP,
            "serverRoute": {
                "kind": "manual_reply",
                "resolutionKey": final_resolution_key,
                "fence": fence_seed,
            },
        }
        authority_patch = {
            "status": "claimed",
            "ownerOutboxId": canonical_outbox_id,
            "actionAuditId": outbox["actionAuditId"],
            "immutableGraphMessageId": source["immutableGraphMessageId"],
            "internetMessageId": source["internetMessageId"],
            "reviewedBodyHash": reviewed_body_hash,
            "snapshotHash": claim_snapshot_hash,
            "fence": fence_seed,
            "claimedAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        }
        resolution = {
            "status": "claimed",
            "uid": canonical_uid,
            "outboxId": canonical_outbox_id,
            "resolutionKey": final_resolution_key,
            "workerId": canonical_worker_id,
            "fence": fence_seed,
        }
        transaction.update(outbox_ref, outbox_patch)
        transaction.set(authority_ref, authority_patch, merge=True)
        transaction.create(resolution_ref, resolution)
        return {
            "status": "claimed",
            "reason": "claim_created",
            "resolutionKey": final_resolution_key,
            "authorityKey": canonical_authority_key,
            "snapshotHash": claim_snapshot_hash,
            "fence": fence_seed,
        }

    try:
        return claim(firestore_client.transaction())
    except Exception:
        return {"status": "manual_review", "reason": "claim_unavailable"}


def _eligible_authority_source(data: object, *, uid: str, outbox: dict) -> dict:
    if (
        type(data) is not dict
        or not _ELIGIBLE_AUTHORITY_REQUIRED_FIELDS.issubset(data)
        or not set(data).issubset(_ELIGIBLE_AUTHORITY_ALLOWED_FIELDS)
    ):
        raise ValueError("manual_reply_authority has an unsupported shape")
    authority = data
    if (
        authority.get("schemaVersion") != 1
        or authority.get("status") != "eligible"
        or authority.get("uid") != uid
        or authority.get("clientId") != outbox["clientId"]
        or authority.get("threadId") != outbox["threadId"]
        or authority.get("notificationId") != outbox["notificationId"]
        or authority.get("source") != _SOURCE
    ):
        raise ValueError("manual reply authority is not eligible")
    lookup_id = _validate_graph_message_id(authority.get("graphLookupMessageId"))
    if lookup_id != outbox["sourceGraphMessageId"]:
        raise ValueError("authority lookup alias changed")
    internet_id = normalize_internet_message_id(
        authority.get("normalizedInternetMessageId")
    )
    if internet_id != normalize_internet_message_id(outbox["sourceInternetMessageId"]):
        raise ValueError("authority Internet Message-ID changed")
    conversation_id = _require_string(authority.get("conversationId"), "conversation_id")
    if not conversation_id or _has_control(conversation_id):
        raise ValueError("authority conversation is not canonical")
    authenticated_mailbox = _normalize_address(
        authority.get("authenticatedMailboxAddress"),
        "authority authenticated mailbox",
    )
    from_address = _normalize_address(authority.get("fromAddress"), "authority from")
    sender_address = _normalize_address(authority.get("senderAddress"), "authority sender")
    source_audience = _canonical_source_audience(
        authority.get("sourceAudience"),
        "authority.sourceAudience",
    )
    audience = _require_exact_keys(
        authority.get("audience"),
        {"to", "cc", "bcc"},
        "authority.audience",
    )
    canonical_audience = {
        "to": _canonical_address_list(
            audience.get("to"),
            "authority.audience.to",
            size=1,
        ),
        "cc": _canonical_address_list(
            audience.get("cc"),
            "authority.audience.cc",
            size=0,
        ),
        "bcc": _canonical_address_list(
            audience.get("bcc"),
            "authority.audience.bcc",
            size=0,
        ),
    }
    recipient = _normalize_address(outbox["assignedEmails"][0], "outbox recipient")
    if (
        canonical_audience != {"to": [recipient], "cc": [], "bcc": []}
        or source_audience != {
            "to": [authenticated_mailbox],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        }
        or from_address != recipient
        or sender_address != recipient
    ):
        raise ValueError("authority audience changed")
    created_at = _require_aware_datetime(
        authority.get("createdAt"),
        "authority.createdAt",
    )
    updated_at = _require_aware_datetime(
        authority.get("updatedAt"),
        "authority.updatedAt",
    )
    if created_at != updated_at:
        raise ValueError("eligible authority timestamps do not share one commit")
    source = {
        "graphLookupMessageId": lookup_id,
        "internetMessageId": internet_id,
        "conversationId": conversation_id,
        "authenticatedMailboxAddress": authenticated_mailbox,
        "fromAddress": from_address,
        "senderAddress": sender_address,
        "sender": sender_address,
        "sourceAudience": source_audience,
        "audience": canonical_audience,
    }
    if "immutableGraphMessageId" in authority:
        source["expectedImmutableGraphMessageId"] = _validate_graph_message_id(
            authority["immutableGraphMessageId"]
        )
    return source


def _validate_token_context(context: object, *, attempt_id: str) -> dict:
    token_context = _require_exact_keys(context, {
        "headers", "accounts", "selected_account", "token_account",
        "token_claims", "token_cache_path",
    }, "graph_context")
    accounts = token_context.get("accounts")
    if type(accounts) is not list or len(accounts) != 1:
        raise ValueError("exactly one MSAL account is required")
    account = _canonical_selected_account(accounts[0])
    selected = _canonical_selected_account(token_context.get("selected_account"))
    token_account = _canonical_selected_account(token_context.get("token_account"))
    if selected != account or token_account != account:
        raise ValueError("MSAL selected/token account changed")
    claims = _require_exact_keys(token_context.get("token_claims"), {
        "oid", "tid", "preferred_username",
    }, "token_claims")
    if (
        claims.get("oid") != account["local_account_id"]
        or claims.get("tid") != account["realm"]
        or _normalize_address(
            claims.get("preferred_username"),
            "token preferred_username",
        ) != account["username"]
    ):
        raise ValueError("token claims and selected account disagree")
    cache_path = _require_string(
        token_context.get("token_cache_path"),
        "token_cache_path",
    )
    expected_leaf = f"{attempt_id}.cache"
    if (
        len(attempt_id) != 64
        or any(character not in "0123456789abcdef" for character in attempt_id)
        or not cache_path
        or "\\" in cache_path
        or cache_path.rsplit("/", 1)[-1] != expected_leaf
    ):
        raise ValueError("token cache is not attempt-scoped")
    headers = _exact_graph_headers(token_context.get("headers"))
    return {
        "headers": headers,
        "selected_account": account,
        "token_claims": claims,
        "token_cache_path": cache_path,
    }


def _graph_address_list(value: object, field: str) -> list[str]:
    if type(value) is not list:
        raise ValueError(f"{field} is not an address list")
    addresses = []
    for entry in value:
        if type(entry) is not dict:
            raise ValueError(f"{field} contains an invalid recipient")
        email_address = entry.get("emailAddress")
        if type(email_address) is not dict:
            raise ValueError(f"{field} contains an invalid recipient")
        addresses.append(_normalize_address(
            email_address.get("address"),
            f"{field}.address",
        ))
    return addresses


def _resolve_canonical_graph_source(
    *,
    http_client: object,
    headers: dict[str, str],
    selected_account: dict,
    authority_source: dict,
) -> tuple[dict, dict]:
    graph_me = _response_json(http_client.get(
        "/me",
        headers=headers,
        retry=False,
    ))
    if not _graph_me_matches_account(graph_me, selected_account):
        raise ValueError("Graph /me and selected account disagree")
    if (
        authority_source["authenticatedMailboxAddress"]
        != selected_account["username"]
    ):
        raise ValueError("authority mailbox and selected account disagree")
    lookup_path = _graph_message_path(authority_source["graphLookupMessageId"])
    source = _response_json(http_client.get(
        lookup_path,
        headers=headers,
        retry=False,
    ))
    immutable_id = _validate_graph_message_id(source.get("id"))
    internet_id = normalize_internet_message_id(source.get("internetMessageId"))
    graph_from = _normalize_address(
        source.get("from", {}).get("emailAddress", {}).get("address"),
        "Graph source from",
    )
    graph_sender = _normalize_address(
        source.get("sender", {}).get("emailAddress", {}).get("address"),
        "Graph source sender",
    )
    graph_source_audience = {
        "to": _graph_address_list(source.get("toRecipients"), "source.toRecipients"),
        "cc": _graph_address_list(source.get("ccRecipients"), "source.ccRecipients"),
        "bcc": _graph_address_list(source.get("bccRecipients"), "source.bccRecipients"),
        "replyTo": _graph_address_list(source.get("replyTo"), "source.replyTo"),
    }
    expected_immutable = authority_source.get("expectedImmutableGraphMessageId")
    if (
        internet_id != authority_source["internetMessageId"]
        or source.get("conversationId") != authority_source["conversationId"]
        or graph_from != authority_source["fromAddress"]
        or graph_sender != authority_source["senderAddress"]
        or graph_source_audience != authority_source["sourceAudience"]
        or source.get("isDraft") is not False
        or (expected_immutable is not None and immutable_id != expected_immutable)
    ):
        raise ValueError("Graph source and server authority disagree")
    canonical_source = {
        **{
            key: value
            for key, value in authority_source.items()
            if key != "expectedImmutableGraphMessageId"
        },
        "immutableGraphMessageId": immutable_id,
    }
    return canonical_source, _graph_me_projection(graph_me)


def _final_manual_reply_snapshot(
    *,
    firestore_client: object,
    uid: str,
    outbox_id: str,
    worker_id: str,
    authority_key: str,
    resolution_key: str,
    canonical_source: dict,
    selected_account: dict,
    graph_me: dict,
    claim_snapshot_hash: str,
    outbound_mode_reader,
) -> dict:
    user_ref = _user_document(firestore_client, uid)
    outbox_ref = user_ref.collection("outbox").document(outbox_id)
    authority_ref = user_ref.collection("manualReplyAuthorities").document(authority_key)
    resolution_ref = user_ref.collection("manualReplyResolutions").document(resolution_key)

    from google.cloud import firestore

    @firestore.transactional
    def reread(transaction):
        outbox_snapshot = outbox_ref.get(transaction=transaction)
        outbox = _validate_task7_outbox(_snapshot_data(outbox_snapshot), outbox_id)
        client_id = outbox["clientId"]
        thread_id = outbox["threadId"]
        notification_id = outbox["notificationId"]
        audit_ref = user_ref.collection("actionAudit").document(outbox["actionAuditId"])
        active_ref = user_ref.collection("clients").document(client_id)
        archived_ref = user_ref.collection("archivedClients").document(client_id)
        thread_ref = user_ref.collection("threads").document(thread_id)
        notification_ref = active_ref.collection("notifications").document(notification_id)
        source_ref = thread_ref.collection("messages").document(outbox["sourceMessageId"])
        global_ref = firestore_client.collection("systemConfig").document("campaignAccess")

        active_snapshot = active_ref.get(transaction=transaction)
        archived_snapshot = archived_ref.get(transaction=transaction)
        thread_snapshot = thread_ref.get(transaction=transaction)
        notification_snapshot = notification_ref.get(transaction=transaction)
        audit_snapshot = audit_ref.get(transaction=transaction)
        source_snapshot = source_ref.get(transaction=transaction)
        authority_snapshot = authority_ref.get(transaction=transaction)
        resolution_snapshot = resolution_ref.get(transaction=transaction)
        try:
            global_snapshot = global_ref.get(transaction=transaction)
            global_read_error = False
        except Exception:
            global_snapshot = None
            global_read_error = True

        active = _snapshot_data(active_snapshot)
        archived = _snapshot_data(archived_snapshot)
        thread = _snapshot_data(thread_snapshot)
        notification = _snapshot_data(notification_snapshot)
        audit = _snapshot_data(audit_snapshot)
        source_record = _snapshot_data(source_snapshot)
        authority = _snapshot_data(authority_snapshot)
        resolution = _snapshot_data(resolution_snapshot)
        global_raw = _snapshot_data(global_snapshot) if global_snapshot is not None else None
        global_access = manual_reply_global_access_decision(
            uid=uid,
            policy=global_raw,
            exists=(global_snapshot is not None and getattr(global_snapshot, "exists", False)),
            read_error=global_read_error,
        )
        if global_access["state"] != "enabled":
            reason = (
                "global_access_disabled"
                if global_access["state"] == "disabled"
                else "global_access_unavailable"
            )
            return {"status": "manual_review", "reason": reason}

        processing_at = outbox.get("processingAt")
        if (
            outbox.get("status") != "manual_reply_claimed"
            or outbox.get("processingBy") != worker_id
            or outbox["serverRoute"].get("resolutionKey") != resolution_key
        ):
            return {"status": "manual_review", "reason": "claim_changed"}
        fence = outbox["serverRoute"].get("fence")
        try:
            audit = _validate_action_audit(
                audit,
                uid=uid,
                outbox_id=outbox_id,
                outbox=outbox,
            )
        except (TypeError, ValueError):
            return {"status": "manual_review", "reason": "audit_changed"}
        try:
            if type(authority) is not dict or set(authority) != set(
                _CLAIMED_AUTHORITY_FIELDS
            ):
                raise ValueError("claimed authority shape changed")
            authority_source_audience = _canonical_source_audience(
                authority.get("sourceAudience"),
                "claimed_authority.sourceAudience",
            )
            authority_audience_raw = _require_exact_keys(
                authority.get("audience"),
                {"to", "cc", "bcc"},
                "claimed_authority.audience",
            )
            authority_audience = {
                "to": _canonical_address_list(
                    authority_audience_raw["to"],
                    "claimed_authority.audience.to",
                    size=1,
                ),
                "cc": _canonical_address_list(
                    authority_audience_raw["cc"],
                    "claimed_authority.audience.cc",
                    size=0,
                ),
                "bcc": _canonical_address_list(
                    authority_audience_raw["bcc"],
                    "claimed_authority.audience.bcc",
                    size=0,
                ),
            }
            claimed_at = _require_aware_datetime(
                authority.get("claimedAt"),
                "claimed_authority.claimedAt",
            )
            updated_at = _require_aware_datetime(
                authority.get("updatedAt"),
                "claimed_authority.updatedAt",
            )
            _require_aware_datetime(
                authority.get("createdAt"),
                "claimed_authority.createdAt",
            )
            expected_body_hash = hashlib.sha256(
                outbox["script"].encode("utf-8")
            ).hexdigest()
            if (
                authority.get("schemaVersion") != 1
                or authority.get("status") != "claimed"
                or authority.get("uid") != uid
                or authority.get("clientId") != client_id
                or authority.get("threadId") != thread_id
                or authority.get("notificationId") != notification_id
                or authority.get("source") != _SOURCE
                or authority.get("graphLookupMessageId")
                != canonical_source["graphLookupMessageId"]
                or normalize_internet_message_id(
                    authority.get("normalizedInternetMessageId")
                ) != canonical_source["internetMessageId"]
                or authority.get("conversationId")
                != canonical_source["conversationId"]
                or _normalize_address(
                    authority.get("authenticatedMailboxAddress"),
                    "claimed authority mailbox",
                ) != canonical_source["authenticatedMailboxAddress"]
                or _normalize_address(
                    authority.get("fromAddress"),
                    "claimed authority from",
                ) != canonical_source["fromAddress"]
                or _normalize_address(
                    authority.get("senderAddress"),
                    "claimed authority sender",
                ) != canonical_source["senderAddress"]
                or authority_source_audience != canonical_source["sourceAudience"]
                or authority_audience != canonical_source["audience"]
                or authority.get("ownerOutboxId") != outbox_id
                or authority.get("actionAuditId") != outbox["actionAuditId"]
                or authority.get("fence") != fence
                or authority.get("immutableGraphMessageId")
                != canonical_source["immutableGraphMessageId"]
                or normalize_internet_message_id(authority.get("internetMessageId"))
                != canonical_source["internetMessageId"]
                or authority.get("reviewedBodyHash") != expected_body_hash
                or authority.get("snapshotHash") != claim_snapshot_hash
                or claimed_at != processing_at
                or updated_at != processing_at
            ):
                raise ValueError("claimed authority binding changed")
        except (TypeError, ValueError):
            return {"status": "manual_review", "reason": "authority_changed"}

        queued_outbox = dict(outbox)
        queued_outbox["status"] = "queued"
        for field in _TASK9A_SERVER_OUTBOX_FIELDS:
            queued_outbox.pop(field, None)
        try:
            recomputed_claim_hash = _claim_snapshot_hash(
                uid=uid,
                outbox_id=outbox_id,
                worker_id=worker_id,
                authority_key=authority_key,
                outbox=queued_outbox,
                action_audit=audit,
                authority=authority,
                canonical_source=canonical_source,
                selected_account=selected_account,
                graph_me=graph_me,
            )
        except (TypeError, ValueError):
            return {"status": "manual_review", "reason": "claim_hash_invalid"}
        if recomputed_claim_hash != claim_snapshot_hash:
            return {"status": "manual_review", "reason": "claim_hash_changed"}

        if (
            type(resolution) is not dict
            or resolution != {
                "status": "claimed",
                "uid": uid,
                "outboxId": outbox_id,
                "resolutionKey": resolution_key,
                "workerId": worker_id,
                "fence": fence,
            }
        ):
            return {"status": "manual_review", "reason": "resolution_changed"}
        if archived is not None or type(active) is not dict or active.get("status") != "live":
            return {"status": "manual_review", "reason": "client_changed"}
        follow_up = thread.get("followUpConfig") if type(thread) is dict else None
        if (
            type(thread) is not dict
            or thread.get("clientId") != client_id
            or thread.get("status") != "paused"
            or type(follow_up) is not dict
            or follow_up.get("enabled") is not False
            or follow_up.get("processingBy") not in (None, "")
        ):
            return {"status": "manual_review", "reason": "thread_changed"}
        if (
            type(notification) is not dict
            or notification.get("kind") != "action_needed"
            or notification.get("threadId") != thread_id
            or notification.get("manualReplyAuthorityKey") != authority_key
            or _normalize_address(notification.get("email"), "notification email")
            != canonical_source["audience"]["to"][0]
            or type(notification.get("meta")) is not dict
            or notification.get("meta", {}).get("replyToMessageId")
            != canonical_source["graphLookupMessageId"]
        ):
            return {"status": "manual_review", "reason": "notification_changed"}
        source_message = source_record.get("sourceMessage") if type(source_record) is dict else None
        if (
            type(source_record) is not dict
            or source_record.get("direction") != "inbound"
            or _normalize_address(source_record.get("from"), "stored source from")
            != canonical_source["fromAddress"]
            or type(source_message) is not dict
            or source_message.get("graphMessageId")
            != canonical_source["graphLookupMessageId"]
            or normalize_internet_message_id(source_message.get("internetMessageId"))
            != canonical_source["internetMessageId"]
        ):
            return {"status": "manual_review", "reason": "source_record_changed"}
        try:
            outbound_mode = outbound_mode_reader()
        except Exception:
            outbound_mode = "unavailable"
        client_decision = manual_reply_authority_decision(
            uid=uid,
            global_access=global_access,
            active_client=active,
            archived_client=archived,
            client_binding={
                "userPathUid": uid,
                "clientId": client_id,
                "outboxClientId": client_id,
                "threadClientId": thread["clientId"],
                "notificationClientId": outbox["notificationClientId"],
                "activeLocation": "clients",
            },
            outbound_mode=outbound_mode,
        )
        if not client_decision["allowed"]:
            return {"status": "manual_review", "reason": client_decision["reason"]}

        snapshot = {
            "outbox": {"id": outbox_id, **outbox},
            "activeClientSlot": {
                "present": True,
                "location": "clients",
                "record": {"status": active["status"]},
            },
            "archivedClientSlot": {"present": False, "record": None},
            "thread": {"id": thread_id, "status": thread["status"]},
            "followUp": {
                "enabled": follow_up["enabled"],
                "processingBy": follow_up.get("processingBy"),
            },
            "notification": {
                "id": notification_id,
                "kind": notification["kind"],
                "authorityKey": notification["manualReplyAuthorityKey"],
            },
            "actionAudit": {
                "id": outbox["actionAuditId"],
                **{
                    key: audit[key]
                    for key in (
                        "status", "actorUid", "source", "actionType",
                        "clientId", "threadId", "notificationId", "finalBody",
                        "finalRecipients", "finalCcRecipients",
                        "replyToMessageId", "sourceMessageId",
                        "sourceGraphMessageId", "sourceInternetMessageId",
                    )
                },
                **({"outboxId": audit["outboxId"]} if "outboxId" in audit else {}),
            },
            "sourceBinding": {
                "graphLookupMessageId": canonical_source["graphLookupMessageId"],
                "normalizedInternetMessageId": canonical_source["internetMessageId"],
                "conversationId": canonical_source["conversationId"],
                "authenticatedMailboxAddress": canonical_source[
                    "authenticatedMailboxAddress"
                ],
                "fromAddress": canonical_source["fromAddress"],
                "senderAddress": canonical_source["senderAddress"],
                "sourceAudience": canonical_source["sourceAudience"],
                "audience": canonical_source["audience"],
            },
            "selectedAccount": selected_account,
            "graphMe": graph_me,
            "serverAuthority": {
                "authorityPath": f"users/{uid}/manualReplyAuthorities/{authority_key}",
                **authority,
            },
            "logicalResolution": {"present": True, "record": resolution},
            "globalAccess": global_access,
            "clientDecision": {"state": "allow", "stopKind": "none"},
            "outboundMode": outbound_mode,
        }
        try:
            final_hash = manual_reply_snapshot_hash(snapshot)
        except (TypeError, ValueError):
            return {"status": "manual_review", "reason": "snapshot_invalid"}
        return {
            "status": "ready",
            "reason": "snapshot_bound",
            "snapshotHash": final_hash,
        }

    try:
        return reread(firestore_client.transaction())
    except Exception:
        return {"status": "manual_review", "reason": "snapshot_unavailable"}


def prepare_manual_reply_item(
    *,
    firestore_client: object,
    uid: str,
    outbox_id: str,
    worker_id: str,
    graph_context_provider,
    http_client: object,
    outbound_mode_reader,
) -> dict[str, str]:
    """Prepare one canonical draft; Task 9A never sends or writes cap state."""

    try:
        canonical_uid = _validate_document_id(uid, "uid")
        canonical_outbox_id = _validate_document_id(outbox_id, "outbox_id")
        canonical_worker_id = _validate_document_id(worker_id, "worker_id")
        user_ref = _user_document(firestore_client, canonical_uid)
        outbox_ref = user_ref.collection("outbox").document(canonical_outbox_id)
        outbox = _validate_task7_outbox(_snapshot_data(outbox_ref.get()), canonical_outbox_id)
        if outbox.get("status") != "queued":
            raise ValueError("outbox is not queued")
        authority_key = manual_reply_authority_key(
            uid=canonical_uid,
            client_id=outbox["clientId"],
            notification_id=outbox["notificationId"],
        )
        authority_ref = user_ref.collection("manualReplyAuthorities").document(authority_key)
        authority_source = _eligible_authority_source(
            _snapshot_data(authority_ref.get()),
            uid=canonical_uid,
            outbox=outbox,
        )
    except Exception:
        return {"status": "manual_review", "reason": "authority_unavailable"}

    attempt_id = secrets.token_hex(32)
    try:
        token_context = _validate_token_context(
            graph_context_provider(
                uid=canonical_uid,
                outbox_id=canonical_outbox_id,
                attempt_id=attempt_id,
            ),
            attempt_id=attempt_id,
        )
        canonical_source, graph_me = _resolve_canonical_graph_source(
            http_client=http_client,
            headers=token_context["headers"],
            selected_account=token_context["selected_account"],
            authority_source=authority_source,
        )
    except Exception:
        return {"status": "manual_review", "reason": "source_validation_failed"}

    claim = claim_manual_reply_item(
        firestore_client=firestore_client,
        uid=canonical_uid,
        outbox_id=canonical_outbox_id,
        worker_id=canonical_worker_id,
        authority_key=authority_key,
        canonical_source=canonical_source,
        selected_account=token_context["selected_account"],
        graph_me=graph_me,
    )
    if claim.get("status") != "claimed":
        return {"status": "manual_review", "reason": claim.get("reason", "claim_blocked")}

    final = _final_manual_reply_snapshot(
        firestore_client=firestore_client,
        uid=canonical_uid,
        outbox_id=canonical_outbox_id,
        worker_id=canonical_worker_id,
        authority_key=authority_key,
        resolution_key=claim["resolutionKey"],
        canonical_source=canonical_source,
        selected_account=token_context["selected_account"],
        graph_me=graph_me,
        claim_snapshot_hash=claim["snapshotHash"],
        outbound_mode_reader=outbound_mode_reader,
    )
    if final.get("status") != "ready":
        return {"status": "manual_review", "reason": final.get("reason", "snapshot_blocked")}

    prepared = prepare_canonical_manual_reply_draft(
        http_client=http_client,
        headers=token_context["headers"],
        source_binding=canonical_source,
        selected_account=token_context["selected_account"],
        recipient=canonical_source["audience"]["to"][0],
        body=outbox["script"],
    )
    if prepared.get("status") != "prepared":
        return prepared
    return {
        **prepared,
        "snapshotHash": final["snapshotHash"],
        "fence": claim["fence"],
        "resolutionKey": claim["resolutionKey"],
    }


def _exact_graph_headers(headers: object) -> dict[str, str]:
    if type(headers) is not dict:
        raise ValueError("Graph headers must be an object")
    authorization = headers.get("Authorization")
    if type(authorization) is not str or not authorization:
        raise ValueError("Graph authorization is missing")
    return {
        "Authorization": authorization,
        "Prefer": _IMMUTABLE_ID_PREFERENCE,
    }


def _response_json(response: object) -> dict:
    status_code = getattr(response, "status_code", None)
    if type(status_code) is not int or not 200 <= status_code < 300:
        raise ValueError("Graph request was not successful")
    payload = response.json()
    if type(payload) is not dict:
        raise ValueError("Graph response was not an object")
    return payload


def _normalize_address(value: object, field: str) -> str:
    address = _require_string(value, field)
    if address != address.strip() or _has_control(address):
        raise ValueError(f"{field} is not canonical")
    local, separator, domain = address.rpartition("@")
    if not separator or not local or not domain or "." not in domain:
        raise ValueError(f"{field} is not an email address")
    return address.casefold()


def _canonical_selected_account(selected_account: object) -> dict[str, str]:
    if type(selected_account) is not dict or set(selected_account) != set(_ACCOUNT_FIELDS):
        raise ValueError("selected account has an unsupported shape")
    canonical = {}
    for field in _ACCOUNT_FIELDS:
        value = _require_string(selected_account.get(field), field)
        if not value or value != value.strip() or _has_control(value):
            raise ValueError(f"selected account {field} is not canonical")
        canonical[field] = value
    canonical["username"] = _normalize_address(
        canonical["username"],
        "selected account username",
    )
    return canonical


def _graph_me_matches_account(payload: object, selected_account: object) -> bool:
    try:
        account = _canonical_selected_account(selected_account)
        if type(payload) is not dict:
            return False
        graph_id = _require_string(payload.get("id"), "graph_me.id")
        principal_name = _normalize_address(
            payload.get("userPrincipalName"),
            "graph_me.userPrincipalName",
        )
    except (TypeError, ValueError):
        return False
    return (
        graph_id == account["local_account_id"]
        and principal_name == account["username"]
    )


def _graph_message_path(message_id: object) -> str:
    canonical = _validate_graph_message_id(message_id)
    return f"/me/messages/{quote(canonical, safe='')}"


def _delete_draft(http_client: object, path: str, headers: dict[str, str]) -> None:
    try:
        http_client.delete(path, headers=headers, retry=False)
    except Exception:
        # Best-effort cleanup must never broaden into a fallback send path.
        return


def prepare_canonical_manual_reply_draft(
    *,
    http_client: object,
    headers: object,
    source_binding: object,
    selected_account: object,
    recipient: object,
    body: object,
) -> dict[str, str]:
    """Create and validate one exact reply draft, but never send it."""

    try:
        graph_headers = _exact_graph_headers(headers)
        account = _canonical_selected_account(selected_account)
        canonical_recipient = _normalize_address(recipient, "recipient")
        reviewed_body = _require_string(body, "body")
        if not reviewed_body.strip() or _has_unsafe_body_control(reviewed_body):
            raise ValueError("body is not a reviewed nonblank string")
        if not validate_outbound_body(reviewed_body).is_safe:
            raise ValueError("body failed the shared outbound safety policy")
        source_projection = _canonical_source_projection(source_binding)
        if source_projection["audience"] != {
            "to": [canonical_recipient],
            "cc": [],
            "bcc": [],
        }:
            raise ValueError("source audience is not exact")
    except (TypeError, ValueError):
        return {"status": "manual_review", "reason": "invalid_draft_input"}

    draft_path = None
    try:
        graph_me = _response_json(http_client.get(
            "/me",
            headers=graph_headers,
            retry=False,
        ))
        if not _graph_me_matches_account(graph_me, account):
            return {"status": "manual_review", "reason": "sender_mismatch"}

        if (
            source_projection.get("authenticatedMailboxAddress", account["username"])
            != account["username"]
        ):
            return {"status": "manual_review", "reason": "sender_mismatch"}

        source_path = _graph_message_path(
            source_projection["immutableGraphMessageId"]
        )
        source = _response_json(http_client.get(
            source_path,
            headers=graph_headers,
            retry=False,
        ))
        graph_from = _normalize_address(
            source.get("from", {}).get("emailAddress", {}).get("address"),
            "Graph source from",
        )
        graph_sender = _normalize_address(
            source.get("sender", {}).get("emailAddress", {}).get("address"),
            "Graph source sender",
        )
        graph_source_audience = {
            "to": _graph_address_list(
                source.get("toRecipients"),
                "source.toRecipients",
            ),
            "cc": _graph_address_list(
                source.get("ccRecipients"),
                "source.ccRecipients",
            ),
            "bcc": _graph_address_list(
                source.get("bccRecipients"),
                "source.bccRecipients",
            ),
            "replyTo": _graph_address_list(
                source.get("replyTo"),
                "source.replyTo",
            ),
        }
        expected_source_audience = source_projection.get("sourceAudience", {
            "to": [account["username"]],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        })
        if (
            source.get("id") != source_projection["immutableGraphMessageId"]
            or normalize_internet_message_id(source.get("internetMessageId"))
            != source_projection["internetMessageId"]
            or source.get("conversationId") != source_projection["conversationId"]
            or graph_from != source_projection["fromAddress"]
            or graph_sender != source_projection["senderAddress"]
            or graph_source_audience != expected_source_audience
            or source.get("isDraft") is not False
            or source_projection["senderAddress"] != canonical_recipient
        ):
            return {"status": "manual_review", "reason": "source_mismatch"}

        reply_response = http_client.post(
            f"{source_path}/createReply",
            headers=graph_headers,
            json={},
            retry=False,
        )
        reply = _response_json(reply_response)
        immutable_draft_id = _validate_graph_message_id(reply.get("id"))
        draft_path = _graph_message_path(immutable_draft_id)
        patch_response = http_client.patch(
            draft_path,
            headers=graph_headers,
            json={
                "body": {"contentType": "Text", "content": reviewed_body},
                "toRecipients": [{
                    "emailAddress": {"address": canonical_recipient},
                }],
                "ccRecipients": [],
                "bccRecipients": [],
            },
            retry=False,
        )
        _response_json(patch_response)
        draft = _response_json(http_client.get(
            draft_path,
            headers=graph_headers,
            retry=False,
        ))
        to_recipients = draft.get("toRecipients")
        cc_recipients = draft.get("ccRecipients")
        bcc_recipients = draft.get("bccRecipients")
        draft_body = draft.get("body")
        exact_to = (
            type(to_recipients) is list
            and len(to_recipients) == 1
            and _normalize_address(
                to_recipients[0].get("emailAddress", {}).get("address"),
                "draft recipient",
            ) == canonical_recipient
        )
        exact_body = (
            type(draft_body) is dict
            and draft_body.get("contentType") == "Text"
            and draft_body.get("content") == reviewed_body
        )
        if (
            draft.get("id") != immutable_draft_id
            or draft.get("conversationId")
            != source_projection["conversationId"]
            or draft.get("isDraft") is not True
            or not exact_to
            or cc_recipients != []
            or bcc_recipients != []
            or not exact_body
        ):
            _delete_draft(http_client, draft_path, graph_headers)
            return {"status": "manual_review", "reason": "draft_audience_mismatch"}
        return {
            "status": "prepared",
            "reason": "draft_prepared",
            "immutableDraftId": immutable_draft_id,
        }
    except Exception:
        if draft_path is not None:
            _delete_draft(http_client, draft_path, graph_headers)
        return {"status": "manual_review", "reason": "draft_preparation_failed"}


def send_prepared_manual_reply_once(
    *,
    http_client: object,
    headers: object,
    immutable_draft_id: object,
    selected_account: object,
    outbound_mode_reader,
) -> dict[str, object]:
    """Attempt one injected draft send, with no automatic transport retry."""

    try:
        graph_headers = _exact_graph_headers(headers)
        account = _canonical_selected_account(selected_account)
        draft_path = _graph_message_path(immutable_draft_id)
        if outbound_mode_reader() != "live":
            return {"status": "manual_review", "reason": "outbound_mode_blocked"}
        graph_me = _response_json(http_client.get(
            "/me",
            headers=graph_headers,
            retry=False,
        ))
        if not _graph_me_matches_account(graph_me, account):
            _delete_draft(http_client, draft_path, graph_headers)
            return {"status": "manual_review", "reason": "sender_mismatch"}
        if outbound_mode_reader() != "live":
            _delete_draft(http_client, draft_path, graph_headers)
            return {"status": "manual_review", "reason": "outbound_mode_blocked"}
        response = http_client.post(
            f"{draft_path}/send",
            headers=graph_headers,
            json={},
            retry=False,
        )
        status_code = getattr(response, "status_code", None)
        if type(status_code) is not int:
            return {"status": "manual_review", "reason": "provider_outcome_unknown"}
        return {
            "status": "observed",
            "reason": "provider_http_observed",
            "statusCode": status_code,
        }
    except Exception:
        return {"status": "manual_review", "reason": "provider_outcome_unknown"}


def reconcile_canonical_manual_reply(
    *,
    http_client: object,
    headers: object,
    immutable_draft_id: object,
    immutable_source_message_id: object,
) -> dict[str, object]:
    """Read only the exact known Graph IDs; mailbox scans are forbidden."""

    try:
        graph_headers = _exact_graph_headers(headers)
        draft_id = _validate_graph_message_id(immutable_draft_id)
        source_id = _validate_graph_message_id(immutable_source_message_id)
        draft = _response_json(http_client.get(
            _graph_message_path(draft_id),
            headers=graph_headers,
            retry=False,
        ))
        source = _response_json(http_client.get(
            _graph_message_path(source_id),
            headers=graph_headers,
            retry=False,
        ))
        if (
            draft.get("id") != draft_id
            or source.get("id") != source_id
            or draft.get("conversationId") != source.get("conversationId")
            or type(draft.get("isDraft")) is not bool
        ):
            raise ValueError("Graph reconciliation identity changed")
    except Exception:
        return {"status": "manual_review", "reason": "reconciliation_unavailable"}
    return {
        "status": "observed",
        "reason": "exact_identity_observed",
        "isDraft": draft["isDraft"],
    }


__all__ = [
    "bounded_manual_reply_result",
    "claim_manual_reply_item",
    "is_canonical_document_id",
    "manual_reply_authority_key",
    "manual_reply_authority_decision",
    "manual_reply_global_access_decision",
    "manual_reply_resolution_key",
    "manual_reply_resolution_disposition",
    "manual_reply_snapshot_hash",
    "normalize_internet_message_id",
    "prepare_canonical_manual_reply_draft",
    "prepare_manual_reply_item",
    "process_outbox_item",
    "reconcile_canonical_manual_reply",
    "send_prepared_manual_reply_once",
]
