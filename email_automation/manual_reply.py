"""Canonical source identity helpers for exact dashboard manual replies."""

from __future__ import annotations

import hashlib
import unicodedata


_DOMAIN = "sitesift-manual-reply-resolution:v1"
_SOURCE = "dashboard_inline_reply"
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


__all__ = [
    "bounded_manual_reply_result",
    "is_canonical_document_id",
    "manual_reply_resolution_key",
    "normalize_internet_message_id",
    "process_outbox_item",
]
