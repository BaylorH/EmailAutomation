"""
Automatic Follow-Up Email System
================================

This module handles automatic follow-up emails when brokers don't respond
within configurable time periods.

Key features:
- 0-3 configurable follow-ups per thread
- Hours or days timing
- Pause/resume when broker responds then goes silent
- Sends as replies to maintain thread continuity

Called from main.py after inbox scanning.
"""

import hashlib
import json
import socket
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo
from google.cloud.firestore import SERVER_TIMESTAMP

from .clients import _fs
from .utils import (
    exponential_backoff_request,
    format_email_body_with_footer,
    get_signature_attachments,
    needs_signature_attachments,
    safe_preview,
    resolve_signature_settings,
    validate_recipient_emails,
)
from .messaging import save_message
from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_sent_conversation_continuation_for_retry,
    find_matching_sent_message_for_retry,
    sent_after_from_retry_data,
)
from .campaign_safety import (
    campaign_suppression_kind as classify_campaign_suppression,
    get_client_automation_decision,
    stopped_followup_patch,
)
from .outbound_safety import validate_outbound_body
from .email import (
    OUTBOUND_MODE_LIVE,
    _kill_switch_suppressed,
    _safe_greeting_first_name,
    resolve_outbound_mode,
)
from .column_config import (
    get_column_config_error,
    response_requests_nonrequestable_fields,
)

_FOLLOWUP_DATETIME_TYPE = datetime

# Claim timeout for follow-up processing (prevent duplicate sends)
FOLLOWUP_CLAIM_TIMEOUT_SECONDS = 60
FOLLOWUP_SEND_LEASE_SECONDS = 10 * 60
SYNTHETIC_OUTBOUND_SOURCES = {"dashboard_outbox_reply", "followup_scheduler"}
DEFAULT_FOLLOWUP_BUSINESS_TIMEZONE = "America/New_York"
FOLLOWUP_BUSINESS_START_HOUR = 9

# Bounds for client-written followUpConfig. The dashboard writes this config
# onto client/outbox docs directly, so the backend must not trust it:
# waitTime must be a positive number within the per-unit max (~90 days) and
# the followUps sequence is capped. Out-of-range config is rejected fail-closed
# (disabled + needs_review), never scheduled.
FOLLOWUP_MAX_STEPS = 10
FOLLOWUP_WAIT_UNIT_MAX = {
    "minutes": 129600,  # 90 days
    "hours": 2160,      # 90 days
    "days": 90,
}
FOLLOWUP_INVALID_CONFIG_REASON = "followup_config_invalid"

@dataclass(frozen=True)
class FollowupSendOutcome:
    error: Optional[str] = None
    attempt_at: Optional[datetime] = None
    guard_failed_closed: bool = False
    campaign_suppression_kind: Optional[str] = None
    campaign_decision: Optional[Any] = None
    attempt_id: Optional[str] = None
    attempt_marker: Optional[Dict[str, Any]] = None
    attempt_expected_absent: bool = False


@dataclass(frozen=True)
class FollowupClaim:
    owner: str
    index: int
    thread_data: Dict[str, Any]
    followup_config: Dict[str, Any]
    reconciliation_required: bool = False


class FollowupScheduleOutcome(str, Enum):
    SCHEDULED = "scheduled"
    MAX_REACHED = "max_reached"
    INBOUND_PRESERVED = "inbound_preserved"
    TERMINAL_PRESERVED = "terminal_preserved"
    PAUSED_PRESERVED = "paused_preserved"
    ALREADY_COMMITTED = "already_committed"
    AMBIGUOUS = "ambiguous"


_FOLLOWUP_SEND_OUTCOME = ContextVar(
    "followup_send_outcome",
    default=FollowupSendOutcome(),
)


def _set_followup_campaign_suppression(decision) -> None:
    kind = classify_campaign_suppression(decision)
    _set_followup_send_outcome(
        campaign_suppression_kind=kind,
        campaign_decision=decision,
        error=f"Campaign automation suppressed before Graph send: {decision.reason}",
        guard_failed_closed=kind == "terminal",
    )


def _mirror_followup_send_outcome(outcome: FollowupSendOutcome) -> None:
    _send_followup_email.last_error = outcome.error
    _send_followup_email.last_attempt_at = outcome.attempt_at
    _send_followup_email.guard_failed_closed = outcome.guard_failed_closed
    _send_followup_email.campaign_suppression_kind = outcome.campaign_suppression_kind


def _set_followup_send_outcome(**changes) -> FollowupSendOutcome:
    outcome = replace(_FOLLOWUP_SEND_OUTCOME.get(), **changes)
    _FOLLOWUP_SEND_OUTCOME.set(outcome)
    _mirror_followup_send_outcome(outcome)
    return outcome


def _reset_followup_send_outcome() -> FollowupSendOutcome:
    outcome = FollowupSendOutcome()
    _FOLLOWUP_SEND_OUTCOME.set(outcome)
    _mirror_followup_send_outcome(outcome)
    return outcome


def _get_followup_send_outcome() -> FollowupSendOutcome:
    return _FOLLOWUP_SEND_OUTCOME.get()


def _get_followup_campaign_suppression():
    outcome = _get_followup_send_outcome()
    return outcome.campaign_suppression_kind, outcome.campaign_decision


def _get_local_followup_campaign_suppression():
    """Return suppression produced by this execution context only."""
    return _get_followup_campaign_suppression()


_FOLLOWUP_CONFIG_RUNTIME_FIELDS = {
    "processingBy",
    "processingAt",
    "processingLeaseUntil",
    "lastSendError",
    "lastSendAttemptAt",
    "lastSendAttemptIndex",
    "lastFollowUpSentAt",
    "lastWeekendDeferralAt",
    "automationSuppressedAt",
    "automationSuppressedReason",
    "automationSuppressedState",
}


def _canonical_followup_value(value: Any) -> Any:
    """Convert Firestore values into deterministic JSON-compatible values."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_followup_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_followup_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def _typed_followup_identity_value(value: Any) -> Dict[str, Any]:
    """Encode a value deterministically without Python's cross-type equality."""
    if isinstance(value, _FOLLOWUP_DATETIME_TYPE):
        return {"type": "datetime", "value": value.isoformat()}
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    if value is None:
        return {"type": type_name}
    if isinstance(value, bool):
        return {"type": type_name, "value": value}
    if isinstance(value, str):
        return {"type": type_name, "value": value}
    if isinstance(value, bytes):
        return {"type": type_name, "value": value.hex()}
    if isinstance(value, int):
        return {"type": type_name, "value": str(value)}
    if isinstance(value, float):
        return {"type": type_name, "value": value.hex()}
    if isinstance(value, dict):
        items = [
            {
                "key": _typed_followup_identity_value(key),
                "value": _typed_followup_identity_value(item),
            }
            for key, item in value.items()
        ]
        items.sort(
            key=lambda entry: json.dumps(
                entry["key"],
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {"type": type_name, "items": items}
    if isinstance(value, (list, tuple)):
        return {
            "type": type_name,
            "items": [_typed_followup_identity_value(item) for item in value],
        }
    if isinstance(value, (set, frozenset)):
        items = [_typed_followup_identity_value(item) for item in value]
        items.sort(
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {"type": type_name, "items": items}
    return {
        "type": type_name,
        "value": _canonical_followup_value(value),
    }


def _followup_values_exactly_match(left: Any, right: Any) -> bool:
    """Compare protocol values without Python's bool/int/container coercion."""
    return (
        _typed_followup_identity_value(left)
        == _typed_followup_identity_value(right)
    )


def _followup_index_is_valid(value: Any) -> bool:
    return type(value) is int and value >= 0


def _followup_indexes_exactly_match(left: Any, right: Any) -> bool:
    return (
        _followup_index_is_valid(left)
        and _followup_index_is_valid(right)
        and _followup_values_exactly_match(left, right)
    )


_FOLLOWUP_TYPED_VALUE_MISSING = object()


def _followup_value_from_typed(value: Any) -> Any:
    """Decode the typed wire format; callers validate canonical round-tripping."""
    if not isinstance(value, dict):
        return _FOLLOWUP_TYPED_VALUE_MISSING
    value_type = value.get("type")
    if value_type == "builtins.NoneType":
        return None
    if value_type == "builtins.bool":
        decoded = value.get("value")
        return decoded if type(decoded) is bool else _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "builtins.str":
        decoded = value.get("value")
        return decoded if isinstance(decoded, str) else _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "builtins.bytes":
        encoded = value.get("value")
        if not isinstance(encoded, str):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        try:
            return bytes.fromhex(encoded)
        except ValueError:
            return _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "builtins.int":
        encoded = value.get("value")
        if not isinstance(encoded, str):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        try:
            decoded = int(encoded)
        except ValueError:
            return _FOLLOWUP_TYPED_VALUE_MISSING
        return decoded if str(decoded) == encoded else _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "builtins.float":
        encoded = value.get("value")
        if not isinstance(encoded, str):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        try:
            return float.fromhex(encoded)
        except ValueError:
            return _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "datetime":
        encoded = value.get("value")
        if not isinstance(encoded, str):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        try:
            return _FOLLOWUP_DATETIME_TYPE.fromisoformat(encoded)
        except ValueError:
            return _FOLLOWUP_TYPED_VALUE_MISSING
    if value_type == "builtins.dict":
        items = value.get("items")
        if not isinstance(items, list):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        decoded_dict = {}
        for item in items:
            if not isinstance(item, dict):
                return _FOLLOWUP_TYPED_VALUE_MISSING
            decoded_key = _followup_value_from_typed(item.get("key"))
            decoded_value = _followup_value_from_typed(item.get("value"))
            if (
                decoded_key is _FOLLOWUP_TYPED_VALUE_MISSING
                or decoded_value is _FOLLOWUP_TYPED_VALUE_MISSING
            ):
                return _FOLLOWUP_TYPED_VALUE_MISSING
            try:
                decoded_dict[decoded_key] = decoded_value
            except TypeError:
                return _FOLLOWUP_TYPED_VALUE_MISSING
        return decoded_dict
    collection_types = {
        "builtins.list": list,
        "builtins.tuple": tuple,
        "builtins.set": set,
        "builtins.frozenset": frozenset,
    }
    collection_type = collection_types.get(value_type)
    if collection_type is not None:
        items = value.get("items")
        if not isinstance(items, list):
            return _FOLLOWUP_TYPED_VALUE_MISSING
        decoded_items = []
        for item in items:
            decoded = _followup_value_from_typed(item)
            if decoded is _FOLLOWUP_TYPED_VALUE_MISSING:
                return _FOLLOWUP_TYPED_VALUE_MISSING
            decoded_items.append(decoded)
        try:
            return collection_type(decoded_items)
        except TypeError:
            return _FOLLOWUP_TYPED_VALUE_MISSING
    return _FOLLOWUP_TYPED_VALUE_MISSING


def _followup_value_from_exact_typed(value: Any) -> Any:
    """Decode only canonical typed values, rejecting lossy/malformed proofs."""
    decoded = _followup_value_from_typed(value)
    if decoded is _FOLLOWUP_TYPED_VALUE_MISSING:
        return _FOLLOWUP_TYPED_VALUE_MISSING
    if not _followup_values_exactly_match(
        _typed_followup_identity_value(decoded),
        value,
    ):
        return _FOLLOWUP_TYPED_VALUE_MISSING
    return decoded


def _decode_followup_field_signature(
    signature: Any,
) -> Optional[Tuple[bool, Any]]:
    """Return ``(present, value)`` for an exact field signature."""
    if not isinstance(signature, dict) or set(signature) != {"present", "value"}:
        return None
    present = signature["present"]
    if type(present) is not bool:
        return None
    if not present:
        if _followup_values_exactly_match(signature["value"], {"type": "missing"}):
            return False, _FOLLOWUP_TYPED_VALUE_MISSING
        return None
    decoded = _followup_value_from_exact_typed(signature["value"])
    if decoded is _FOLLOWUP_TYPED_VALUE_MISSING:
        return None
    return True, decoded


def _followup_field_signature(
    source: Dict[str, Any],
    key: str,
) -> Dict[str, Any]:
    """Snapshot a field without collapsing absence, ``None``, or value type."""
    source = source if isinstance(source, dict) else {}
    present = key in source
    return {
        "present": present,
        "value": (
            _typed_followup_identity_value(source[key])
            if present
            else {"type": "missing"}
        ),
    }


def _followup_durable_config(
    followup_config: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        key: value
        for key, value in (followup_config or {}).items()
        if key not in _FOLLOWUP_CONFIG_RUNTIME_FIELDS
    }


def _followup_config_fingerprint(followup_config: Dict[str, Any]) -> str:
    """Fingerprint durable scheduling inputs while excluding runtime claim fields."""
    durable_config = _followup_durable_config(followup_config)
    encoded = json.dumps(
        _canonical_followup_value(durable_config),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _followup_retry_signature(
    thread_data: Dict[str, Any],
    followup_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the exact retry fence that a claimed worker observed."""
    return _canonical_followup_value({
        "processingBy": _followup_field_signature(
            followup_config,
            "processingBy",
        ),
        "currentFollowUpIndex": _followup_field_signature(
            followup_config,
            "currentFollowUpIndex",
        ),
        "lastSendError": _followup_field_signature(
            followup_config,
            "lastSendError",
        ),
        "lastSendAttemptAt": _followup_field_signature(
            followup_config,
            "lastSendAttemptAt",
        ),
        "lastSendAttemptIndex": _followup_field_signature(
            followup_config,
            "lastSendAttemptIndex",
        ),
        "followUpSendAttempt": _followup_field_signature(
            thread_data,
            "followUpSendAttempt",
        ),
    })


def _followup_raw_message(followup_config: Dict[str, Any], followup_index: int) -> str:
    followups = (followup_config or {}).get("followUps") or []
    if not _followup_index_is_valid(followup_index):
        return ""
    if not isinstance(followups, list) or followup_index >= len(followups):
        return ""
    step = followups[followup_index]
    if not isinstance(step, dict):
        return ""
    return str(step.get("message") or "")


def _resolve_followup_message(
    followup_config: Dict[str, Any],
    followup_index: int,
    contact_name: Any,
) -> str:
    """Resolve the exact final body from durable follow-up inputs."""
    message = (
        _followup_raw_message(followup_config, followup_index)
        or _get_default_followup_message(followup_index)
    )
    safe_contact_name = _safe_followup_contact_name(contact_name)
    first_name = (
        _safe_greeting_first_name(safe_contact_name)
        if safe_contact_name
        else None
    )
    if first_name and "[NAME]" in message:
        message = message.replace("[NAME]", first_name)
    return message


def _safe_followup_contact_name(contact_name: Any) -> str:
    """Return a durable contact-name string only when its greeting is safe."""
    if not isinstance(contact_name, str):
        return ""
    candidate = contact_name.strip()
    if not candidate or not _safe_greeting_first_name(candidate):
        return ""
    return candidate


def _followup_primary_recipient(thread_data: Dict[str, Any]) -> str:
    recipient_emails = (thread_data or {}).get("email", [])
    if isinstance(recipient_emails, list):
        recipient = recipient_emails[0] if recipient_emails else ""
    else:
        recipient = recipient_emails
    return str(recipient or "").strip().lower()


def _normalize_followup_recipients(recipients: Any) -> List[str]:
    """Return ordered, case-normalized addresses from strings or Graph entries."""
    normalized = []
    seen = set()
    if isinstance(recipients, (str, dict)):
        recipients = [recipients]
    for item in recipients or []:
        if isinstance(item, dict):
            address = ((item.get("emailAddress") or {}).get("address") or "")
        else:
            address = item
        address = str(address or "").strip().lower()
        if address and address not in seen:
            seen.add(address)
            normalized.append(address)
    return normalized


_FOLLOWUP_IDENTITY_THREAD_FIELDS = (
    "clientId",
    "email",
    "contactName",
    "ccEmails",
    "ccRecipients",
    "rowNumber",
)
_FOLLOWUP_IDENTITY_CONFIG_FIELDS = (
    "currentFollowUpIndex",
    "followUps",
)


def _followup_send_identity(
    thread_data: Dict[str, Any],
    followup_config: Dict[str, Any],
    followup_index: int,
) -> Dict[str, Any]:
    """Capture Firestore inputs that can change the external send."""
    thread_data = thread_data or {}
    cc_recipients = (
        thread_data.get("ccEmails")
        or thread_data.get("ccRecipients")
        or []
    )
    contact_name_present = "contactName" in thread_data
    contact_name = thread_data.get("contactName") if contact_name_present else None
    contact_name_type = (
        f"{type(contact_name).__module__}.{type(contact_name).__qualname__}"
        if contact_name_present
        else "missing"
    )
    input_signatures = {
        "thread": {
            key: _followup_field_signature(thread_data, key)
            for key in _FOLLOWUP_IDENTITY_THREAD_FIELDS
        },
        "config": {
            "currentFollowUpIndex": _followup_field_signature(
                followup_config,
                "currentFollowUpIndex",
            ),
            "followUps": _followup_field_signature(
                followup_config,
                "followUps",
            ),
            "durable": {
                "present": True,
                "value": _typed_followup_identity_value(
                    _followup_durable_config(followup_config)
                ),
            },
        },
        "followupIndex": {
            "present": True,
            "value": _typed_followup_identity_value(followup_index),
        },
    }
    return _canonical_followup_value({
        "clientId": thread_data.get("clientId"),
        "recipient": _followup_primary_recipient(thread_data),
        "rawMessage": _followup_raw_message(followup_config, followup_index),
        "contactName": contact_name,
        "contactNameExact": (
            _typed_followup_identity_value(contact_name)
            if contact_name_present
            else {"type": "missing"}
        ),
        "contactNamePresent": contact_name_present,
        "contactNameType": contact_name_type,
        "ccRecipients": cc_recipients,
        "configFingerprint": _followup_config_fingerprint(followup_config),
        "inputSignatures": input_signatures,
    })


def _followup_inputs_from_send_identity(
    identity: Any,
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], int]]:
    """Decode a complete identity into its exact thread/config/index inputs."""
    if not isinstance(identity, dict):
        return None
    input_signatures = identity.get("inputSignatures")
    if not isinstance(input_signatures, dict) or set(input_signatures) != {
        "thread",
        "config",
        "followupIndex",
    }:
        return None
    thread_signatures = input_signatures.get("thread")
    config_signatures = input_signatures.get("config")
    index_signature = input_signatures.get("followupIndex")
    if not isinstance(thread_signatures, dict) or set(thread_signatures) != set(
        _FOLLOWUP_IDENTITY_THREAD_FIELDS
    ):
        return None
    if not isinstance(config_signatures, dict) or set(config_signatures) != {
        *_FOLLOWUP_IDENTITY_CONFIG_FIELDS,
        "durable",
    }:
        return None

    thread_data = {}
    for field in _FOLLOWUP_IDENTITY_THREAD_FIELDS:
        decoded = _decode_followup_field_signature(thread_signatures[field])
        if decoded is None:
            return None
        present, value = decoded
        if present:
            thread_data[field] = value
    if "email" not in thread_data:
        return None
    if "rowNumber" in thread_data and (
        type(thread_data["rowNumber"]) is not int
        or thread_data["rowNumber"] <= 0
    ):
        return None

    config_values = {}
    for field in (*_FOLLOWUP_IDENTITY_CONFIG_FIELDS, "durable"):
        decoded = _decode_followup_field_signature(config_signatures[field])
        if decoded is None or not decoded[0]:
            return None
        config_values[field] = decoded[1]
    decoded_index = _decode_followup_field_signature(index_signature)
    if decoded_index is None or not decoded_index[0]:
        return None
    followup_index = decoded_index[1]
    if not _followup_indexes_exactly_match(
        config_values["currentFollowUpIndex"],
        followup_index,
    ):
        return None

    durable_config = config_values["durable"]
    if not isinstance(durable_config, dict):
        return None
    if not _followup_values_exactly_match(
        _followup_durable_config(durable_config),
        durable_config,
    ):
        return None

    rebuilt_identity = _followup_send_identity(
        thread_data,
        durable_config,
        followup_index,
    )
    if not _followup_values_exactly_match(identity, rebuilt_identity):
        return None
    return thread_data, durable_config, followup_index


def _followup_send_identity_has_complete_proof(identity: Any) -> bool:
    """Return whether an identity exactly rebuilds from its typed raw proofs."""
    return _followup_inputs_from_send_identity(identity) is not None


def _followup_send_identity_matches(
    left: Any,
    right: Any,
    *,
    allow_config_fingerprint_change: bool = False,
) -> bool:
    """Compare send inputs, optionally tolerating terminal scheduling changes."""
    if not _followup_send_identity_has_complete_proof(left):
        return False
    if not _followup_send_identity_has_complete_proof(right):
        return False
    canonical_left = _canonical_followup_value(left)
    canonical_right = _canonical_followup_value(right)
    if _followup_values_exactly_match(canonical_left, canonical_right):
        return True
    if not allow_config_fingerprint_change:
        return False
    canonical_left = dict(canonical_left)
    canonical_right = dict(canonical_right)
    canonical_left.pop("configFingerprint", None)
    canonical_right.pop("configFingerprint", None)
    for identity in (canonical_left, canonical_right):
        input_signatures = identity.get("inputSignatures")
        if not isinstance(input_signatures, dict):
            continue
        input_signatures = dict(input_signatures)
        config_signatures = input_signatures.get("config")
        if isinstance(config_signatures, dict):
            config_signatures = dict(config_signatures)
            config_signatures.pop("durable", None)
            input_signatures["config"] = config_signatures
        identity["inputSignatures"] = input_signatures
    return _followup_values_exactly_match(
        canonical_left,
        canonical_right,
    )


_FOLLOWUP_SEND_ENVELOPE_FIELDS = (
    "id",
    "owner",
    "index",
    "createdAt",
    "sendStartedAt",
    "sendIdentity",
    "configFingerprint",
    "clientId",
    "recipient",
    "body",
    "subject",
    "conversationId",
    "draftId",
    "toRecipients",
    "ccRecipients",
)


def _followup_send_envelope_payload(marker: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(marker, dict) or any(
        field not in marker for field in _FOLLOWUP_SEND_ENVELOPE_FIELDS
    ):
        return None
    return {
        field: marker[field]
        for field in _FOLLOWUP_SEND_ENVELOPE_FIELDS
    }


def _followup_send_envelope_hash(envelope_proof: Any) -> str:
    encoded = json.dumps(
        _canonical_followup_value(envelope_proof),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _followup_typed_value_hash(value: Any) -> str:
    return _followup_send_envelope_hash(_typed_followup_identity_value(value))


def _followup_utc_timestamp(value: Any) -> Optional[datetime]:
    """Normalize only aware datetime, Firestore, or ISO timestamp values."""
    try:
        if isinstance(value, str):
            if not value or value != value.strip():
                return None
            encoded = value[:-1] + "+00:00" if value.endswith("Z") else value
            value = _FOLLOWUP_DATETIME_TYPE.fromisoformat(encoded)
        elif not isinstance(value, _FOLLOWUP_DATETIME_TYPE):
            return None

        if (
            not isinstance(value, _FOLLOWUP_DATETIME_TYPE)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            return None
        normalized = value.astimezone(timezone.utc)
        normalized.timestamp()
        return normalized
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _validated_followup_retry_timestamp(
    followup_config: Any,
    *,
    expected_signature: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[datetime, Dict[str, Any]]]:
    """Return a current legacy retry timestamp plus its exact field signature."""
    config = followup_config if isinstance(followup_config, dict) else {}
    signature = _followup_field_signature(config, "lastSendAttemptAt")
    if expected_signature is not None and not _followup_values_exactly_match(
        signature,
        expected_signature,
    ):
        return None
    timestamp = _followup_utc_timestamp(config.get("lastSendAttemptAt"))
    now = _followup_utc_timestamp(datetime.now(timezone.utc))
    if timestamp is None or now is None:
        return None
    if timestamp > now:
        return None
    return timestamp, signature


def _followup_send_envelope_timestamps_are_valid(
    marker: Any,
    payload: Optional[Dict[str, Any]] = None,
) -> bool:
    """Validate the immutable send timeline and its mutable recovery lease."""
    payload = payload or _followup_send_envelope_payload(marker)
    if payload is None:
        return False
    created_at = _followup_utc_timestamp(payload["createdAt"])
    send_started_at = _followup_utc_timestamp(payload["sendStartedAt"])
    now = _followup_utc_timestamp(datetime.now(timezone.utc))
    if created_at is None or send_started_at is None or now is None:
        return False
    if created_at > send_started_at:
        return False
    if send_started_at > now:
        return False
    marker_state = str(marker.get("state") or "").strip().lower()
    if marker_state in {"sending", "uncertain"} and "leaseUntil" not in marker:
        return False
    if "leaseUntil" in marker:
        lease_until = _followup_utc_timestamp(marker["leaseUntil"])
        if lease_until is None or lease_until < send_started_at:
            return False
    return True


def _seal_followup_send_envelope(marker: Dict[str, Any]) -> Dict[str, Any]:
    """Seal immutable send inputs while leaving recovery state mutable."""
    sealed_marker = dict(marker)
    payload = _followup_send_envelope_payload(sealed_marker)
    if payload is None:
        raise ValueError("follow-up send envelope is incomplete")
    if not _followup_send_envelope_timestamps_are_valid(sealed_marker, payload):
        raise ValueError("follow-up send envelope timestamps are invalid")
    envelope_fields = dict(payload)
    send_identity = envelope_fields.pop("sendIdentity")
    envelope_proof = _typed_followup_identity_value({
        "fields": envelope_fields,
        "sendIdentityHash": _followup_typed_value_hash(send_identity),
    })
    sealed_marker["envelopeProof"] = envelope_proof
    sealed_marker["inputHash"] = _followup_send_envelope_hash(envelope_proof)
    return sealed_marker


def _followup_send_envelope_is_complete(
    marker: Any,
    *,
    expected_identity: Optional[Dict[str, Any]] = None,
    allow_config_fingerprint_change: bool = False,
) -> bool:
    """Validate the immutable marker and bind it to durable send inputs."""
    payload = _followup_send_envelope_payload(marker)
    if payload is None:
        return False
    if not _followup_send_envelope_timestamps_are_valid(marker, payload):
        return False
    envelope_proof = marker.get("envelopeProof")
    decoded_proof = _followup_value_from_exact_typed(envelope_proof)
    if not isinstance(decoded_proof, dict) or set(decoded_proof) != {
        "fields",
        "sendIdentityHash",
    }:
        return False
    proved_fields = decoded_proof["fields"]
    expected_fields = dict(payload)
    send_identity = expected_fields.pop("sendIdentity")
    if not _followup_values_exactly_match(proved_fields, expected_fields):
        return False
    if not _followup_values_exactly_match(
        decoded_proof["sendIdentityHash"],
        _followup_typed_value_hash(send_identity),
    ):
        return False
    if not _followup_values_exactly_match(
        marker.get("inputHash"),
        _followup_send_envelope_hash(envelope_proof),
    ):
        return False

    identity_inputs = _followup_inputs_from_send_identity(send_identity)
    if identity_inputs is None:
        return False
    identity_thread, identity_config, identity_index = identity_inputs
    if not _followup_indexes_exactly_match(payload["index"], identity_index):
        return False
    if expected_identity is not None and not _followup_send_identity_matches(
        send_identity,
        expected_identity,
        allow_config_fingerprint_change=allow_config_fingerprint_change,
    ):
        return False
    if not _followup_values_exactly_match(
        payload["configFingerprint"],
        send_identity.get("configFingerprint"),
    ):
        return False
    if not _followup_values_exactly_match(
        payload["clientId"],
        send_identity.get("clientId"),
    ):
        return False
    if not _followup_values_exactly_match(
        payload["recipient"],
        send_identity.get("recipient"),
    ):
        return False

    expected_body = _resolve_followup_message(
        identity_config,
        identity_index,
        identity_thread.get("contactName"),
    )
    if not _followup_values_exactly_match(payload["body"], expected_body):
        return False

    if not all(
        isinstance(payload[field], str) and payload[field]
        for field in ("id", "owner", "recipient", "body", "subject")
    ):
        return False
    if payload["conversationId"] is not None and not isinstance(
        payload["conversationId"],
        str,
    ):
        return False
    if payload["draftId"] is not None and not isinstance(payload["draftId"], str):
        return False

    to_recipients = payload["toRecipients"]
    cc_recipients = payload["ccRecipients"]
    if not isinstance(to_recipients, list) or not isinstance(cc_recipients, list):
        return False
    if not _followup_values_exactly_match(
        to_recipients,
        _normalize_followup_recipients(to_recipients),
    ):
        return False
    if not _followup_values_exactly_match(
        cc_recipients,
        _normalize_followup_recipients(cc_recipients),
    ):
        return False
    if payload["recipient"] not in to_recipients:
        return False
    return True


def _followup_preservation_outcome(
    thread_data: Dict[str, Any],
) -> Optional[FollowupScheduleOutcome]:
    """Classify a newer business state that accepted-send bookkeeping preserves."""
    data = thread_data or {}
    status = str(data.get("status") or "").strip().lower()
    followup_status = str(data.get("followUpStatus") or "").strip().lower()
    status_reason = str(data.get("statusReason") or "").strip().lower()
    pending_terminal_reason = str(
        data.get("pendingTerminalReason") or ""
    ).strip().lower()
    if data.get("hasInboundReply"):
        return FollowupScheduleOutcome.INBOUND_PRESERVED
    if (
        status == "paused"
        or followup_status == "paused"
        or status_reason == "manual_continuation"
    ):
        return FollowupScheduleOutcome.PAUSED_PRESERVED
    if (
        pending_terminal_reason
        or status in {"stopped", "completed", "archived", "action_needed"}
        or (followup_status and followup_status != "waiting")
    ):
        return FollowupScheduleOutcome.TERMINAL_PRESERVED
    return None


def _followup_column_contract_error(message: str, campaign_decision) -> Optional[str]:
    client_data = getattr(campaign_decision, "client_data", None) or {}
    column_config = client_data.get("columnConfig")
    config_error = get_column_config_error(column_config)
    if config_error:
        return f"Follow-up has invalid persisted columnConfig: {config_error}"
    if response_requests_nonrequestable_fields(message, column_config):
        return "Follow-up requests a non-requestable Note, Skip, or formula field"
    return None


def _clear_followup_campaign_suppression() -> None:
    _set_followup_send_outcome(
        campaign_suppression_kind=None,
        campaign_decision=None,
    )


def _validate_followup_steps(followups) -> Optional[str]:
    """Validate a client-supplied followUps sequence.

    Returns None when valid, otherwise a human-readable rejection reason.
    Steps may omit waitTime/waitUnit (module defaults apply), but any value
    present must be in bounds.
    """
    if not isinstance(followups, list):
        return f"followUps must be a list, got {type(followups).__name__}"
    if len(followups) > FOLLOWUP_MAX_STEPS:
        return f"followUps has {len(followups)} steps (max {FOLLOWUP_MAX_STEPS})"
    for index, step in enumerate(followups):
        if not isinstance(step, dict):
            return f"followUps[{index}] must be an object, got {type(step).__name__}"
        wait_unit = step.get("waitUnit", "days")
        if wait_unit not in FOLLOWUP_WAIT_UNIT_MAX:
            return (
                f"followUps[{index}].waitUnit {wait_unit!r} is not one of "
                f"{sorted(FOLLOWUP_WAIT_UNIT_MAX)}"
            )
        wait_time = step.get("waitTime")
        if wait_time is None:
            continue  # module defaults are safe
        if isinstance(wait_time, bool) or not isinstance(wait_time, (int, float)):
            return (
                f"followUps[{index}].waitTime must be a number, "
                f"got {type(wait_time).__name__}"
            )
        if not wait_time > 0:  # also rejects NaN
            return f"followUps[{index}].waitTime must be positive, got {wait_time}"
        if wait_time > FOLLOWUP_WAIT_UNIT_MAX[wait_unit]:
            return (
                f"followUps[{index}].waitTime {wait_time} {wait_unit} exceeds "
                f"max {FOLLOWUP_WAIT_UNIT_MAX[wait_unit]}"
            )
    return None


def _followup_wait_delta(step: Dict, default_wait: float):
    """Compute a clamped wait delta for one follow-up step.

    Defense in depth for configs already stored on thread docs (writable
    straight to Firestore by the dashboard): non-numeric / non-positive
    waitTime falls back to default_wait, and the result is capped at the
    per-unit max so a poisoned doc can never schedule an immediate or
    absurdly distant follow-up.

    Returns (delta, wait_time, wait_unit).
    """
    wait_unit = step.get("waitUnit", "days")
    if wait_unit not in FOLLOWUP_WAIT_UNIT_MAX:
        wait_unit = "days"
    wait_time = step.get("waitTime", default_wait)
    if isinstance(wait_time, bool) or not isinstance(wait_time, (int, float)) or not wait_time > 0:
        wait_time = default_wait
    wait_time = min(wait_time, FOLLOWUP_WAIT_UNIT_MAX[wait_unit])
    if wait_unit == "minutes":
        delta = timedelta(minutes=wait_time)
    elif wait_unit == "hours":
        delta = timedelta(hours=wait_time)
    else:
        delta = timedelta(days=wait_time)
    return delta, wait_time, wait_unit


def _followup_business_timezone(followup_config: Optional[Dict[str, Any]] = None):
    timezone_name = (
        (followup_config or {}).get("timeZone")
        or (followup_config or {}).get("timezone")
        or DEFAULT_FOLLOWUP_BUSINESS_TIMEZONE
    )
    try:
        return ZoneInfo(str(timezone_name))
    except Exception:
        return ZoneInfo(DEFAULT_FOLLOWUP_BUSINESS_TIMEZONE)


def _next_business_followup_time(
    candidate: datetime,
    followup_config: Optional[Dict[str, Any]] = None,
) -> datetime:
    """Move weekend follow-up times to Monday morning in the campaign business timezone."""
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)

    business_tz = _followup_business_timezone(followup_config)
    local_candidate = candidate.astimezone(business_tz)
    weekday = local_candidate.weekday()
    if weekday < 5:
        return candidate

    days_until_monday = 7 - weekday
    local_monday = (local_candidate + timedelta(days=days_until_monday)).replace(
        hour=FOLLOWUP_BUSINESS_START_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    return local_monday.astimezone(timezone.utc)


def _claim_followup(
    user_id: str,
    thread_id: str,
    current_index: int,
) -> Optional[FollowupClaim]:
    """
    Atomically claim a follow-up for processing to prevent duplicate sends.

    The transaction revalidates that the thread is still waiting, sendable,
    due, and at the expected index before replacing any stale claim.

    Returns the unique owner plus the authoritative transaction snapshot.
    """
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(current_index):
        return None

    thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)

    @transactional
    def claim_transaction(transaction, thread_ref, expected_index):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return None

        data = snapshot.to_dict() or {}
        followup_config = data.get("followUpConfig", {})
        if not isinstance(followup_config, dict):
            return None

        actual_index = followup_config.get("currentFollowUpIndex", 0)
        if not _followup_index_is_valid(actual_index):
            print(f"   ⏭️ Follow-up index is invalid: {actual_index!r}")
            return None
        processing_by = followup_config.get("processingBy")
        processing_at = followup_config.get("processingAt")
        send_attempt = data.get("followUpSendAttempt")
        if not isinstance(send_attempt, dict):
            send_attempt = {}
        attempt_state = str(send_attempt.get("state") or "").strip().lower()
        if attempt_state == "needs_review":
            print(
                "   ⏭️ Follow-up send attempt requires manual review before "
                "the sequence can resume"
            )
            return None
        if (
            attempt_state in {"sending", "uncertain"}
            and not _followup_indexes_exactly_match(
                send_attempt.get("index"),
                actual_index,
            )
        ):
            attempt_index = send_attempt.get("index")
            reason = (
                "unresolved durable follow-up attempt exists at a different "
                f"index ({attempt_index} vs {actual_index}); manual review required"
            )
            inconsistent_update = {
                "followUpConfig.enabled": False,
                "followUpConfig.nextFollowUpAt": None,
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "followUpConfig.processingLeaseUntil": None,
                "followUpConfig.lastSendError": reason,
                "updatedAt": SERVER_TIMESTAMP,
            }
            review_attempt = dict(send_attempt)
            review_attempt.update({
                "state": "needs_review",
                "resolution": "ambiguous",
                "error": reason,
                "finalizedAt": datetime.now(timezone.utc),
            })
            inconsistent_update["followUpSendAttempt"] = review_attempt
            current_status = str(data.get("status") or "").strip().lower()
            current_followup_status = str(
                data.get("followUpStatus") or ""
            ).strip().lower()
            preserves_business_state = (
                data.get("hasInboundReply")
                or current_status in {
                    "paused", "stopped", "completed", "archived", "action_needed"
                }
                or (
                    current_followup_status
                    and current_followup_status != "waiting"
                )
            )
            if not preserves_business_state:
                inconsistent_update.update({
                    "followUpStatus": "needs_review",
                    "status": "action_needed",
                    "statusReason": "followup_send_guard_failed",
                })
            transaction.update(thread_ref, inconsistent_update)
            print(f"   🛑 {reason}")
            return None
        if (
            _followup_indexes_exactly_match(
                send_attempt.get("index"),
                actual_index,
            )
            and attempt_state == "committed"
        ):
            print(
                "   ⏭️ Follow-up send attempt is already committed at "
                f"index {expected_index}, skipping"
            )
            return None
        reconciliation_required = (
            _followup_indexes_exactly_match(
                send_attempt.get("index"),
                actual_index,
            )
            and attempt_state in {"sending", "uncertain"}
        )

        if not _followup_indexes_exactly_match(actual_index, expected_index):
            print(f"   ⏭️ Follow-up index changed ({expected_index} → {actual_index}), skipping")
            return None

        # Ordinary sends require a waiting, enabled, non-terminal thread.
        # An unresolved durable attempt is recovery-only: it must be claimed
        # even if an inbound reply or terminal/manual state arrived after
        # Graph acceptance, and that business state is preserved later.
        if not reconciliation_required:
            block_reason = _followup_terminal_block_reason(
                data,
                followup_config,
                expected_index,
            )
            if block_reason:
                print(f"   ⏭️ Follow-up no longer sendable: {block_reason}")
                return None

            followup_status = str(data.get("followUpStatus") or "").strip().lower()
            if followup_status != "waiting":
                print(
                    f"   ⏭️ Follow-up state changed to "
                    f"{followup_status or 'unset'}, skipping"
                )
                return None

        now = datetime.now(timezone.utc)

        if not reconciliation_required:
            next_followup_at = followup_config.get("nextFollowUpAt")
            try:
                next_followup_dt = datetime.fromtimestamp(
                    next_followup_at.timestamp(),
                    tz=timezone.utc,
                )
            except (AttributeError, TypeError, ValueError, OSError):
                print("   ⏭️ Follow-up no longer has a valid due time, skipping")
                return None
            if now < next_followup_dt:
                print("   ⏭️ Follow-up due time moved into the future, skipping")
                return None

        lease_until = (
            send_attempt.get("leaseUntil")
            if reconciliation_required
            else followup_config.get("processingLeaseUntil")
        )
        if lease_until is not None:
            try:
                lease_until_dt = datetime.fromtimestamp(
                    lease_until.timestamp(),
                    tz=timezone.utc,
                )
            except (AttributeError, TypeError, ValueError, OSError):
                lease_until_dt = None
            if lease_until_dt is not None and now < lease_until_dt:
                print(
                    f"   ⏭️ Follow-up send lease is active until "
                    f"{lease_until_dt.isoformat()}"
                )
                return None

        if processing_by and processing_at and not reconciliation_required:
            if hasattr(processing_at, 'timestamp'):
                claim_age = (now - processing_at.replace(tzinfo=timezone.utc)).total_seconds()
            else:
                claim_age = (now - processing_at).total_seconds()

            if claim_age < FOLLOWUP_CLAIM_TIMEOUT_SECONDS:
                print(f"   ⏭️ Follow-up already being processed by {processing_by} ({int(claim_age)}s ago)")
                return None

        # Claim the follow-up
        worker_id = f"followup-{socket.gethostname()[:20]}-{uuid4().hex}"
        claim_update = {
            "followUpConfig.processingBy": worker_id,
            "followUpConfig.processingAt": now,
            "followUpConfig.processingLeaseUntil": (
                now + timedelta(seconds=FOLLOWUP_SEND_LEASE_SECONDS)
                if reconciliation_required
                else None
            ),
        }

        claimed_attempt = send_attempt
        if reconciliation_required:
            claimed_attempt = dict(send_attempt)
            claimed_attempt.update({
                "state": "uncertain",
                "reconciliationOwner": worker_id,
                "leaseUntil": now + timedelta(seconds=FOLLOWUP_SEND_LEASE_SECONDS),
            })
            claim_update["followUpSendAttempt"] = claimed_attempt

        transaction.update(thread_ref, claim_update)

        claimed_config = dict(followup_config)
        claimed_config.update({
            "processingBy": worker_id,
            "processingAt": now,
            "processingLeaseUntil": claim_update[
                "followUpConfig.processingLeaseUntil"
            ],
        })
        claimed_data = dict(data)
        claimed_data["followUpConfig"] = claimed_config
        if reconciliation_required:
            claimed_data["followUpSendAttempt"] = claimed_attempt

        return FollowupClaim(
            owner=worker_id,
            index=actual_index,
            thread_data=claimed_data,
            followup_config=claimed_config,
            reconciliation_required=reconciliation_required,
        )

    try:
        transaction = _fs.transaction()
        return claim_transaction(transaction, thread_ref, current_index)
    except Exception as e:
        print(f"   ⚠️ Failed to claim follow-up for {thread_id[:20]}...: {e}")
        return None


def _release_followup_claim(
    user_id: str,
    thread_id: str,
    *,
    reason: Optional[str] = None,
    attempted_at: Optional[datetime] = None,
    current_index: Optional[int] = None,
    claim_owner: Optional[str] = None,
    send_attempt_id: Optional[str] = None,
    send_attempt_marker: Optional[Dict[str, Any]] = None,
    expected_no_send_attempt: bool = False,
    fail_closed: bool = False,
) -> bool:
    """Release an owned claim without overwriting newer or terminal state."""
    from google.cloud.firestore import transactional

    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)

        @transactional
        def release_transaction(transaction, thread_ref):
            snapshot = thread_ref.get(transaction=transaction)
            if not snapshot.exists:
                return False

            data = snapshot.to_dict() or {}
            followup_config = data.get("followUpConfig", {})
            current_owner = followup_config.get("processingBy")
            if claim_owner is not None and current_owner != claim_owner:
                print(
                    f"   ⏭️ Follow-up claim ownership changed from {claim_owner} "
                    f"to {current_owner}; not releasing"
                )
                return False

            actual_index = followup_config.get("currentFollowUpIndex", 0)
            if not _followup_index_is_valid(actual_index):
                print(f"   ⏭️ Follow-up index is invalid: {actual_index!r}")
                return False
            if current_index is not None and not _followup_indexes_exactly_match(
                actual_index,
                current_index,
            ):
                print(
                    f"   ⏭️ Follow-up index changed from {current_index} "
                    f"to {actual_index}; not releasing"
                )
                return False

            current_attempt_raw = data.get("followUpSendAttempt")
            current_attempt = (
                current_attempt_raw
                if isinstance(current_attempt_raw, dict)
                else None
            )
            if send_attempt_id:
                current_attempt_id = (
                    current_attempt.get("id")
                    if current_attempt is not None
                    else None
                )
                if current_attempt_id != send_attempt_id:
                    expected_absence_matches = (
                        expected_no_send_attempt
                        and current_attempt_raw is None
                        and isinstance(send_attempt_marker, dict)
                        and send_attempt_marker.get("id") == send_attempt_id
                    )
                    if expected_absence_matches:
                        current_attempt = None
                    else:
                        print(
                            f"   ⏭️ Follow-up send attempt changed from "
                            f"{send_attempt_id} to {current_attempt_id}; not releasing"
                        )
                        return False

            update_payload = {
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "followUpConfig.processingLeaseUntil": None,
            }
            if reason:
                update_payload["followUpConfig.lastSendError"] = reason
            if attempted_at:
                update_payload["followUpConfig.lastSendAttemptAt"] = attempted_at
            if current_index is not None:
                update_payload["followUpConfig.lastSendAttemptIndex"] = current_index
            if fail_closed:
                update_payload.update({
                    "followUpConfig.enabled": False,
                    "followUpConfig.nextFollowUpAt": None,
                })
                review_source = current_attempt
                if (
                    review_source is None
                    and send_attempt_id
                    and expected_no_send_attempt
                    and isinstance(send_attempt_marker, dict)
                    and send_attempt_marker.get("id") == send_attempt_id
                ):
                    review_source = send_attempt_marker
                if send_attempt_id and isinstance(review_source, dict):
                    review_attempt = dict(review_source)
                    review_attempt.update({
                        "state": "needs_review",
                        "resolution": "ambiguous",
                        "error": reason or "follow-up send guard failed",
                        "finalizedAt": datetime.now(timezone.utc),
                    })
                    update_payload["followUpSendAttempt"] = review_attempt
                block_reason = _followup_terminal_block_reason(
                    data,
                    followup_config,
                    actual_index,
                )
                if block_reason:
                    print(
                        f"   ⏭️ Preserving newer blocked follow-up state: {block_reason}"
                    )
                else:
                    update_payload.update({
                        "followUpStatus": "needs_review",
                        "status": "action_needed",
                        "statusReason": "followup_send_guard_failed",
                    })
            transaction.update(thread_ref, update_payload)
            return True

        transaction = _fs.transaction()
        return release_transaction(transaction, thread_ref)
    except Exception as e:
        print(f"   ⚠️ Failed to release follow-up claim: {e}")
        return False


def _terminalize_owned_followup(
    user_id: str,
    thread_id: str,
    *,
    reason: str,
    current_index: int,
    claim_owner: Optional[str],
    expected_client_id: Optional[str],
) -> Tuple[bool, Optional[str]]:
    """Apply campaign terminal state only to the exact active claim."""
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(current_index):
        return False, f"the claimed follow-up index is invalid: {current_index!r}"

    thread_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
    )

    @transactional
    def terminalize_transaction(transaction, thread_ref):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False, "the thread no longer exists"

        data = snapshot.to_dict() or {}
        current_client_id = data.get("clientId")
        if not _followup_values_exactly_match(
            current_client_id,
            expected_client_id,
        ):
            return False, (
                f"the thread client changed from {expected_client_id} "
                f"to {current_client_id}"
            )
        current_config = data.get("followUpConfig")
        if not isinstance(current_config, dict):
            return False, "the current follow-up config is missing"
        current_owner = current_config.get("processingBy")
        if not claim_owner or current_owner != claim_owner:
            return False, (
                f"claim ownership changed from {claim_owner} to {current_owner}"
            )
        actual_index = current_config.get("currentFollowUpIndex", 0)
        if not _followup_indexes_exactly_match(actual_index, current_index):
            return False, (
                f"the follow-up index changed from {current_index} to {actual_index}"
            )

        current_attempt = data.get("followUpSendAttempt")
        if isinstance(current_attempt, dict) and str(
            current_attempt.get("state") or ""
        ).strip().lower() in {"sending", "uncertain"}:
            return False, "an unresolved durable send attempt appeared"

        status = str(data.get("status") or "").strip().lower()
        followup_status = str(data.get("followUpStatus") or "").strip().lower()
        preserve_business_state = (
            data.get("hasInboundReply")
            or status in {
                "paused", "stopped", "completed", "archived", "action_needed"
            }
            or (followup_status and followup_status != "waiting")
        )
        if preserve_business_state:
            terminal_patch = {
                "automationPaused": True,
                "automationPauseReason": reason,
                "followUpConfig.enabled": False,
                "followUpConfig.nextFollowUpAt": None,
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "followUpConfig.processingLeaseUntil": None,
                "updatedAt": SERVER_TIMESTAMP,
            }
        else:
            terminal_patch = stopped_followup_patch(reason)
            terminal_patch["followUpConfig.processingLeaseUntil"] = None
        transaction.update(thread_ref, terminal_patch)
        return True, None

    try:
        return terminalize_transaction(_fs.transaction(), thread_ref)
    except Exception as exc:
        return False, str(exc)


def _save_followup_message(
    user_id: str,
    thread_id: str,
    recipient: str,
    subject: str,
    body: str,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    cc_recipients: Optional[List[str]] = None,
    to_recipients: Optional[List[str]] = None,
    attempt_id: Optional[str] = None,
) -> bool:
    """Persist a sent follow-up into thread history for dashboard reconciliation."""
    try:
        synthetic_id = (
            f"followup-{thread_id}-{attempt_id}"
            if attempt_id
            else f"followup-{thread_id}-{int(time.time() * 1000)}"
        )
        html_body = format_email_body_with_footer(
            body,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        return save_message(
            user_id,
            thread_id,
            synthetic_id,
            {
                "direction": "outbound",
                "from": "me",
                "to": (
                    _normalize_followup_recipients(to_recipients)
                    if to_recipients is not None
                    else ([recipient] if recipient else [])
                ),
                "cc": _normalize_followup_recipients(cc_recipients),
                "subject": subject,
                "body": html_body,
                "bodyPreview": safe_preview(body, 300),
                "sentDateTime": datetime.now(timezone.utc).isoformat(),
                "headers": {"internetMessageId": synthetic_id},
                "source": "followup_scheduler",
            },
        )
    except Exception as e:
        print(f"   ⚠️ Could not save follow-up message for {thread_id[:20]}...: {e}")
        return False


def _clear_followup_row_highlight(user_id: str, thread_id: str) -> bool:
    """Clear Sheet highlight when a follow-up sequence reaches a terminal state."""
    try:
        from .clients import _get_sheet_id_or_fail
        from .sheets import clear_row_highlight

        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()
        if not thread_doc.exists:
            return False
        thread_data = thread_doc.to_dict() or {}
        client_id = thread_data.get("clientId")
        row_number = thread_data.get("rowNumber")
        if not client_id or not row_number:
            return False
        sheet_id = _get_sheet_id_or_fail(user_id, client_id)
        return clear_row_highlight(sheet_id, row_number)
    except Exception as e:
        print(f"   ⚠️ Could not clear terminal follow-up row highlight for {thread_id[:20]}...: {e}")
        return False


def _is_graph_backed_outbound_message(message_data: Dict[str, Any]) -> bool:
    """True when an outbound history entry can be found again through Microsoft Graph."""
    if (message_data or {}).get("source") in SYNTHETIC_OUTBOUND_SOURCES:
        return False

    internet_msg_id = ((message_data or {}).get("headers") or {}).get("internetMessageId")
    if not internet_msg_id:
        return False

    return not str(internet_msg_id).startswith(("dashboard-reply-", "followup-"))


def _select_reply_anchor_message(outbound_message_docs: List[Any]) -> Optional[Dict[str, Any]]:
    """Pick the newest outbound message that has a real Graph internetMessageId."""
    for doc in outbound_message_docs:
        data = doc.to_dict() or {}
        if _is_graph_backed_outbound_message(data):
            return data
    return None


def _followup_terminal_block_reason(
    thread_data: Dict[str, Any],
    followup_config: Dict[str, Any],
    followup_index: int,
) -> Optional[str]:
    """Return a human-readable reason when a follow-up must not send now."""
    if not _followup_index_is_valid(followup_index):
        return f"the requested follow-up index is invalid: {followup_index!r}"
    status = str((thread_data or {}).get("status") or "").strip().lower()
    followup_status = str((thread_data or {}).get("followUpStatus") or "").strip().lower()
    status_reason = str((thread_data or {}).get("statusReason") or "").strip().lower()
    pending_terminal_reason = str(
        (thread_data or {}).get("pendingTerminalReason") or ""
    ).strip().lower()

    if pending_terminal_reason:
        return f"the thread has a pending terminal decision: {pending_terminal_reason}"
    if (thread_data or {}).get("hasInboundReply"):
        return "the broker has already replied"
    if status in {"stopped", "completed", "archived", "action_needed", "paused"}:
        return f"the thread is {status}"
    if followup_status and followup_status != "waiting":
        return f"follow-up tracking is {followup_status}"
    if status_reason in {"manual_continuation", "followup_send_guard_failed"}:
        return f"the thread requires review for {status_reason}"
    if "enabled" in (followup_config or {}) and not (followup_config or {}).get("enabled"):
        return "follow-up tracking is disabled"

    current_index = (followup_config or {}).get("currentFollowUpIndex")
    if current_index is not None and not _followup_indexes_exactly_match(
        current_index,
        followup_index,
    ):
        return f"the follow-up index changed from {followup_index} to {current_index}"

    followups = (followup_config or {}).get("followUps") or []
    if followups and followup_index >= len(followups):
        return "the max follow-up count has already been reached"

    return None


def _read_followup_send_precondition(
    user_id: str,
    thread_id: str,
    followup_index: int,
    fallback_config: Dict[str, Any],
    *,
    claim_owner: Optional[str] = None,
):
    """Read current thread state and return ``(data, block_reason)``."""
    latest_thread_doc = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
        .get()
    )
    if not latest_thread_doc.exists:
        return {}, "the thread no longer exists"

    latest_thread_data = latest_thread_doc.to_dict() or {}
    if "followUpConfig" in latest_thread_data:
        latest_followup_config = latest_thread_data.get("followUpConfig") or {}
    else:
        latest_followup_config = fallback_config

    block_reason = _followup_terminal_block_reason(
        latest_thread_data,
        latest_followup_config,
        followup_index,
    )
    if not block_reason and claim_owner is not None:
        current_owner = latest_followup_config.get("processingBy")
        if current_owner != claim_owner:
            block_reason = (
                f"follow-up claim ownership changed from {claim_owner} "
                f"to {current_owner}"
            )

    return latest_thread_data, block_reason


def _persist_followup_send_intent(
    user_id: str,
    thread_id: str,
    *,
    claim_owner: str,
    followup_index: int,
    expected_thread_data: Dict[str, Any],
    expected_followup_config: Dict[str, Any],
    recipient: str,
    body: str,
    subject: str,
    conversation_id: Optional[str],
    draft_id: str,
    to_recipients: Optional[List[str]] = None,
    cc_recipients: Optional[List[str]] = None,
    fallback_contact_name: Optional[str] = None,
    expected_retry_timestamp_signature: Optional[Dict[str, Any]] = None,
):
    """Fence an irreversible Graph send with an exact transactional intent."""
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(followup_index):
        return None, f"the claimed follow-up index is invalid: {followup_index!r}"
    expected_index = (expected_followup_config or {}).get("currentFollowUpIndex")
    if not _followup_indexes_exactly_match(expected_index, followup_index):
        return None, "the claimed config index differs from the follow-up index"

    expected_identity = _followup_send_identity(
        expected_thread_data,
        expected_followup_config,
        followup_index,
    )
    expected_retry = _followup_retry_signature(
        expected_thread_data,
        expected_followup_config,
    )
    normalized_to_recipients = _normalize_followup_recipients(
        to_recipients if to_recipients is not None else [recipient]
    )
    normalized_cc_recipients = _normalize_followup_recipients(
        cc_recipients
        if cc_recipients is not None
        else expected_identity.get("ccRecipients")
    )
    thread_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
    )

    @transactional
    def persist_transaction(transaction, thread_ref):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return None, "the thread no longer exists"

        data = snapshot.to_dict() or {}
        current_config = data.get("followUpConfig")
        if not isinstance(current_config, dict):
            return None, "the current follow-up config is missing"
        if (
            expected_retry_timestamp_signature is not None
            and _validated_followup_retry_timestamp(
                current_config,
                expected_signature=expected_retry_timestamp_signature,
            )
            is None
        ):
            return None, "the legacy retry timestamp changed or is invalid"

        block_reason = _followup_terminal_block_reason(
            data,
            current_config,
            followup_index,
        )
        followup_status = str(data.get("followUpStatus") or "").strip().lower()
        if block_reason or followup_status != "waiting":
            return None, block_reason or f"follow-up tracking is {followup_status or 'unset'}"

        current_owner = current_config.get("processingBy")
        if not claim_owner or current_owner != claim_owner:
            return None, (
                f"follow-up claim ownership changed from {claim_owner} "
                f"to {current_owner}"
            )
        current_index = current_config.get("currentFollowUpIndex", 0)
        if not _followup_indexes_exactly_match(current_index, followup_index):
            return None, (
                f"the follow-up index changed from {followup_index} to {current_index}"
            )

        current_identity = _followup_send_identity(
            data,
            current_config,
            followup_index,
        )
        if not _followup_values_exactly_match(
            current_identity,
            expected_identity,
        ):
            return None, "recipient, message, client, or follow-up config changed"
        if not _followup_values_exactly_match(
            _followup_retry_signature(data, current_config),
            expected_retry,
        ):
            return None, "follow-up retry metadata changed"
        if current_identity.get("recipient") != str(recipient or "").strip().lower():
            return None, "the normalized primary recipient changed"

        send_identity = current_identity
        body_contact_name = data.get("contactName")
        contact_name_update = {}
        if fallback_contact_name is not None:
            current_contact_name = data.get("contactName")
            current_name_is_missing = (
                current_contact_name is None
                or (
                    isinstance(current_contact_name, str)
                    and not current_contact_name.strip()
                )
            )
            safe_fallback_name = _safe_followup_contact_name(fallback_contact_name)
            if not current_name_is_missing:
                return None, "the thread contact name changed before send"
            if not safe_fallback_name:
                return None, "the sheet fallback contact name is not safe"

            body_contact_name = safe_fallback_name
            resolved_thread_data = dict(data)
            resolved_thread_data["contactName"] = safe_fallback_name
            send_identity = _followup_send_identity(
                resolved_thread_data,
                current_config,
                followup_index,
            )
            contact_name_update["contactName"] = safe_fallback_name

        expected_body = _resolve_followup_message(
            current_config,
            followup_index,
            body_contact_name,
        )
        if str(body or "") != expected_body:
            return None, "the final follow-up body differs from the claimed message"
        body_validation = validate_outbound_body(expected_body)
        if not body_validation.is_safe:
            return None, (
                "the final follow-up body failed outbound safety: "
                f"{body_validation.reason}"
            )
        if current_identity.get("recipient") not in normalized_to_recipients:
            return None, "the primary recipient is missing from the final Graph payload"
        if not _followup_send_identity_has_complete_proof(send_identity):
            return None, "the follow-up send identity proof is incomplete"

        existing_attempt = data.get("followUpSendAttempt")
        if isinstance(existing_attempt, dict):
            existing_state = str(existing_attempt.get("state") or "").strip().lower()
            if (
                existing_state in {"sending", "uncertain", "needs_review"}
                or (
                    _followup_indexes_exactly_match(
                        existing_attempt.get("index"),
                        followup_index,
                    )
                    and existing_state == "committed"
                )
            ):
                return None, (
                    f"follow-up attempt {existing_attempt.get('id')} is already "
                    f"{existing_state}"
                )

        now = datetime.now(timezone.utc)
        send_started_at = now
        lease_until = now + timedelta(seconds=FOLLOWUP_SEND_LEASE_SECONDS)
        attempt_id = f"followup-attempt-{uuid4().hex}"
        marker = _seal_followup_send_envelope({
            "id": attempt_id,
            "state": "sending",
            "owner": claim_owner,
            "index": followup_index,
            "createdAt": now,
            "sendStartedAt": send_started_at,
            "leaseUntil": lease_until,
            "sendIdentity": send_identity,
            "configFingerprint": send_identity.get("configFingerprint"),
            "clientId": send_identity.get("clientId"),
            "recipient": recipient,
            "body": body,
            "subject": subject,
            "conversationId": conversation_id,
            "draftId": draft_id,
            "toRecipients": normalized_to_recipients,
            "ccRecipients": normalized_cc_recipients,
        })
        transaction.update(thread_ref, {
            **contact_name_update,
            "followUpSendAttempt": marker,
            "followUpConfig.processingAt": now,
            "followUpConfig.processingLeaseUntil": lease_until,
            "followUpConfig.lastSendError": "Graph send outcome pending reconciliation",
            "followUpConfig.lastSendAttemptAt": send_started_at,
            "followUpConfig.lastSendAttemptIndex": followup_index,
            "updatedAt": SERVER_TIMESTAMP,
        })
        return marker, None

    return persist_transaction(_fs.transaction(), thread_ref)


def _record_reconciled_followup_attempt(
    user_id: str,
    thread_id: str,
    *,
    claim_owner: Optional[str],
    followup_index: int,
    expected_attempt: Dict[str, Any],
    expected_identity: Dict[str, Any],
    expected_retry: Dict[str, Any],
    sent_match: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """CAS a Sent Items match before writing any reconciliation audit state."""
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(followup_index):
        return None, f"the reconciliation index is invalid: {followup_index!r}"

    thread_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
    )

    @transactional
    def record_transaction(transaction, thread_ref):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return None, "the thread no longer exists"

        data = snapshot.to_dict() or {}
        current_config = data.get("followUpConfig")
        if not isinstance(current_config, dict):
            return None, "the current follow-up config is missing"

        current_owner = current_config.get("processingBy")
        if not claim_owner or current_owner != claim_owner:
            return None, (
                f"claim ownership changed from {claim_owner} to {current_owner}"
            )

        current_index = current_config.get("currentFollowUpIndex", 0)
        if not _followup_indexes_exactly_match(current_index, followup_index):
            return None, (
                f"the follow-up index changed from {followup_index} to {current_index}"
            )

        current_attempt = data.get("followUpSendAttempt")
        if not isinstance(current_attempt, dict):
            return None, "the durable send attempt is missing"
        if not _followup_indexes_exactly_match(
            current_attempt.get("index"),
            followup_index,
        ):
            return None, "the durable send attempt index changed"
        if not _followup_values_exactly_match(
            current_attempt,
            expected_attempt,
        ):
            return None, "the durable send attempt changed"
        current_state = str(current_attempt.get("state") or "").strip().lower()
        if current_state not in {"sending", "uncertain"}:
            return None, f"the durable send attempt is already {current_state or 'unset'}"

        current_identity = _followup_send_identity(
            data,
            current_config,
            followup_index,
        )
        preservation_outcome = _followup_preservation_outcome(data)
        allow_terminal_config_change = preservation_outcome is not None
        if not _followup_send_identity_matches(
            current_identity,
            expected_identity,
            allow_config_fingerprint_change=allow_terminal_config_change,
        ):
            return None, "recipient, message, client, or follow-up config changed"
        if not _followup_send_envelope_is_complete(
            current_attempt,
            expected_identity=current_identity,
            allow_config_fingerprint_change=allow_terminal_config_change,
        ):
            return None, "the accepted send envelope is missing, changed, or incomplete"
        if not _followup_values_exactly_match(
            _followup_retry_signature(data, current_config),
            expected_retry,
        ):
            return None, "follow-up retry metadata changed"

        reconciled_at = datetime.now(timezone.utc)
        reconciled_attempt = dict(current_attempt)
        reconciled_attempt.update({
            "reconciliationOwner": claim_owner,
            "reconciledAt": reconciled_at,
            "sentMatchId": sent_match.get("id"),
            "sentMatchDateTime": sent_match.get("sentDateTime"),
        })
        transaction.update(thread_ref, {
            "followUpSendAttempt": reconciled_attempt,
            "lastOutboundAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
            "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP,
            "followUpConfig.lastSendError": None,
            "followUpConfig.lastSendAttemptAt": sent_match.get("sentDateTime"),
        })
        return reconciled_attempt, None

    return record_transaction(_fs.transaction(), thread_ref)


def _legacy_followup_attempt_components(
    user_id: str,
    thread_id: str,
    *,
    followup_index: int,
    expected_thread_data: Dict[str, Any],
    expected_followup_config: Dict[str, Any],
    recipient: str,
    body: str,
    subject: str,
    conversation_id: Optional[str],
) -> Tuple[Dict[str, Any], Dict[str, Any], str, str]:
    """Build the deterministic identity for a pre-marker ambiguous send."""
    expected_identity = _followup_send_identity(
        expected_thread_data,
        expected_followup_config,
        followup_index,
    )
    expected_retry = _followup_retry_signature(
        expected_thread_data,
        expected_followup_config,
    )
    legacy_payload = _canonical_followup_value({
        "userId": user_id,
        "threadId": thread_id,
        "index": followup_index,
        "sendIdentity": expected_identity,
        "retry": expected_retry,
        "recipient": recipient,
        "body": body,
        "subject": subject,
        "conversationId": conversation_id,
    })
    legacy_digest = hashlib.sha256(
        json.dumps(
            legacy_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    attempt_id = f"followup-legacy-{legacy_digest[:32]}"
    return expected_identity, expected_retry, legacy_digest, attempt_id


def _legacy_followup_review_attempt(
    user_id: str,
    thread_id: str,
    *,
    claim_owner: Optional[str],
    followup_index: int,
    expected_thread_data: Dict[str, Any],
    expected_followup_config: Dict[str, Any],
    recipient: str,
    body: str,
    subject: str,
    conversation_id: Optional[str],
    sent_match: Dict[str, Any],
    error: str,
) -> Dict[str, Any]:
    """Build a deterministic marker for a legacy ambiguity requiring review."""
    expected_identity, _expected_retry, _legacy_digest, attempt_id = (
        _legacy_followup_attempt_components(
            user_id,
            thread_id,
            followup_index=followup_index,
            expected_thread_data=expected_thread_data,
            expected_followup_config=expected_followup_config,
            recipient=recipient,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
        )
    )
    now = datetime.now(timezone.utc)
    validated_retry_timestamp = _validated_followup_retry_timestamp(
        expected_followup_config
    )
    legacy_started_at = (
        validated_retry_timestamp[0]
        if validated_retry_timestamp is not None
        else now
    )
    return _seal_followup_send_envelope({
        "id": attempt_id,
        "state": "needs_review",
        "resolution": "ambiguous",
        "error": error,
        "owner": claim_owner,
        "index": followup_index,
        "createdAt": legacy_started_at,
        "finalizedAt": now,
        "sendStartedAt": legacy_started_at,
        "sendIdentity": expected_identity,
        "configFingerprint": expected_identity.get("configFingerprint"),
        "clientId": expected_identity.get("clientId"),
        "recipient": recipient,
        "body": body,
        "subject": subject,
        "conversationId": conversation_id,
        "draftId": None,
        "toRecipients": _normalize_followup_recipients([recipient]),
        "ccRecipients": _normalize_followup_recipients(
            expected_identity.get("ccRecipients")
        ),
        "legacyRecovered": True,
        "sentMatchId": sent_match.get("id"),
        "sentMatchDateTime": sent_match.get("sentDateTime"),
    })


def _migrate_legacy_sent_match(
    user_id: str,
    thread_id: str,
    *,
    claim_owner: Optional[str],
    followup_index: int,
    expected_thread_data: Dict[str, Any],
    expected_followup_config: Dict[str, Any],
    recipient: str,
    body: str,
    subject: str,
    conversation_id: Optional[str],
    sent_match: Dict[str, Any],
    expected_retry_timestamp_signature: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """Migrate a pre-marker Sent Items match into the durable protocol."""
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(followup_index):
        return (
            None,
            f"the legacy reconciliation index is invalid: {followup_index!r}",
            "followup-legacy-invalid-index",
        )
    expected_config_index = (expected_followup_config or {}).get(
        "currentFollowUpIndex"
    )
    if not _followup_indexes_exactly_match(
        expected_config_index,
        followup_index,
    ):
        return (
            None,
            "the claimed config index differs from the reconciliation index",
            "followup-legacy-index-mismatch",
        )

    expected_identity, expected_retry, _legacy_digest, attempt_id = (
        _legacy_followup_attempt_components(
            user_id,
            thread_id,
            followup_index=followup_index,
            expected_thread_data=expected_thread_data,
            expected_followup_config=expected_followup_config,
            recipient=recipient,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
        )
    )
    validated_retry_timestamp = _validated_followup_retry_timestamp(
        expected_followup_config,
        expected_signature=expected_retry_timestamp_signature,
    )
    if validated_retry_timestamp is None:
        return (
            None,
            "the legacy lastSendAttemptAt timestamp is missing or invalid",
            attempt_id,
        )
    expected_send_started_at, retry_timestamp_signature = (
        validated_retry_timestamp
    )
    thread_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
    )

    @transactional
    def migrate_transaction(transaction, thread_ref):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return None, "the thread no longer exists"

        data = snapshot.to_dict() or {}
        current_config = data.get("followUpConfig")
        if not isinstance(current_config, dict):
            return None, "the current follow-up config is missing"

        current_owner = current_config.get("processingBy")
        if not claim_owner or current_owner != claim_owner:
            return None, (
                f"claim ownership changed from {claim_owner} to {current_owner}"
            )
        current_index = current_config.get("currentFollowUpIndex", 0)
        if not _followup_indexes_exactly_match(current_index, followup_index):
            return None, (
                f"the follow-up index changed from {followup_index} to {current_index}"
            )
        current_identity = _followup_send_identity(
            data,
            current_config,
            followup_index,
        )
        if not _followup_send_identity_has_complete_proof(expected_identity):
            return None, "the claimed send identity proof is incomplete"
        if not _followup_send_identity_has_complete_proof(current_identity):
            return None, "the current send identity proof is incomplete"
        if not _followup_values_exactly_match(
            current_identity,
            expected_identity,
        ):
            return None, "recipient, message, client, or follow-up config changed"
        current_retry_timestamp = _validated_followup_retry_timestamp(
            current_config,
            expected_signature=retry_timestamp_signature,
        )
        if current_retry_timestamp is None:
            return None, "the legacy lastSendAttemptAt timestamp changed or is invalid"
        current_send_started_at, _current_timestamp_signature = (
            current_retry_timestamp
        )
        if current_send_started_at != expected_send_started_at:
            return None, "the legacy lastSendAttemptAt timestamp changed or is invalid"
        if not _followup_values_exactly_match(
            _followup_retry_signature(data, current_config),
            expected_retry,
        ):
            return None, "follow-up retry metadata changed"
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=FOLLOWUP_SEND_LEASE_SECONDS)
        marker = _seal_followup_send_envelope({
            "id": attempt_id,
            "state": "uncertain",
            "owner": claim_owner,
            "reconciliationOwner": claim_owner,
            "index": followup_index,
            "createdAt": current_send_started_at,
            "sendStartedAt": current_send_started_at,
            "leaseUntil": lease_until,
            "sendIdentity": expected_identity,
            "configFingerprint": expected_identity.get("configFingerprint"),
            "clientId": expected_identity.get("clientId"),
            "recipient": recipient,
            "body": body,
            "subject": subject,
            "conversationId": conversation_id,
            "draftId": None,
            "toRecipients": _normalize_followup_recipients([recipient]),
            "ccRecipients": _normalize_followup_recipients(
                expected_identity.get("ccRecipients")
            ),
            "legacyRecovered": True,
            "reconciledAt": now,
            "sentMatchId": sent_match.get("id"),
            "sentMatchDateTime": sent_match.get("sentDateTime"),
        })
        transaction.update(thread_ref, {
            "followUpSendAttempt": marker,
            "lastOutboundAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
            "followUpConfig.processingAt": now,
            "followUpConfig.processingLeaseUntil": lease_until,
            "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP,
            "followUpConfig.lastSendError": None,
            "followUpConfig.lastSendAttemptAt": sent_match.get("sentDateTime"),
            "followUpConfig.lastSendAttemptIndex": followup_index,
        })
        return marker, None

    marker, error = migrate_transaction(_fs.transaction(), thread_ref)
    return marker, error, attempt_id


def _reconcile_durable_followup_attempt(
    user_id: str,
    thread_id: str,
    headers: Dict[str, str],
    thread_data: Dict[str, Any],
    followup_index: int,
    claim_owner: Optional[str],
) -> Optional[bool]:
    """Resolve an uncertain durable attempt without ever blindly resending it."""
    if not _followup_index_is_valid(followup_index):
        _set_followup_send_outcome(
            error=f"Invalid follow-up reconciliation index: {followup_index!r}",
            guard_failed_closed=True,
        )
        return False
    marker = (thread_data or {}).get("followUpSendAttempt")
    if not isinstance(marker, dict):
        return None
    marker_state = str(marker.get("state") or "").strip().lower()
    if marker_state not in {"sending", "uncertain"}:
        return None
    if not _followup_indexes_exactly_match(marker.get("index"), followup_index):
        _set_followup_send_outcome(
            error="Unresolved follow-up marker has a mismatched or invalid index",
            attempt_id=marker.get("id"),
            attempt_marker=marker,
            guard_failed_closed=True,
        )
        return False

    expected_attempt = marker
    expected_config = (thread_data or {}).get("followUpConfig") or {}
    expected_identity = _followup_send_identity(
        thread_data,
        expected_config,
        followup_index,
    )
    expected_retry = _followup_retry_signature(thread_data, expected_config)
    allow_terminal_config_change = (
        _followup_preservation_outcome(thread_data) is not None
    )
    if not _followup_send_envelope_is_complete(
        marker,
        expected_identity=expected_identity,
        allow_config_fingerprint_change=allow_terminal_config_change,
    ):
        failure_reason = (
            "Durable follow-up send envelope is missing, changed, or incomplete; "
            "manual review required"
        )
        _set_followup_send_outcome(
            error=failure_reason,
            attempt_at=marker.get("sendStartedAt"),
            attempt_id=marker.get("id"),
            attempt_marker=marker,
            guard_failed_closed=True,
        )
        print(f"   🛑 {failure_reason}")
        return False

    recipient = str(marker.get("recipient") or "").strip()
    body = str(marker.get("body") or "")
    subject = str(marker.get("subject") or "Follow-up")
    conversation_id = marker.get("conversationId")
    sent_after = sent_after_from_retry_data({
        "lastSendAttemptAt": marker.get("sendStartedAt") or marker.get("createdAt"),
    })

    try:
        sent_match = find_matching_sent_message_for_retry(
            headers,
            recipient=recipient,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
            sent_after=sent_after,
        )
    except SentMailGuardLookupError as exc:
        failure_reason = f"Sent Items durable-attempt guard failed: {exc}"
        _set_followup_send_outcome(
            error=failure_reason,
            attempt_at=marker.get("sendStartedAt"),
            attempt_id=marker.get("id"),
            attempt_marker=marker,
            guard_failed_closed=True,
        )
        print(f"   ⚠️ {failure_reason}")
        return False

    if sent_match:
        print("   ⚠️ Durable follow-up attempt found in Sent Items; not resending")
        try:
            reconciled_attempt, reconcile_error = _record_reconciled_followup_attempt(
                user_id,
                thread_id,
                claim_owner=claim_owner,
                followup_index=followup_index,
                expected_attempt=expected_attempt,
                expected_identity=expected_identity,
                expected_retry=expected_retry,
                sent_match=sent_match,
            )
        except Exception as exc:
            reconciled_attempt = None
            reconcile_error = f"could not persist reconciliation audit: {exc}"

        if not reconciled_attempt:
            failure_reason = (
                f"Durable follow-up reconciliation state changed: {reconcile_error}; "
                "manual review required"
            )
            _set_followup_send_outcome(
                error=failure_reason,
                attempt_at=marker.get("sendStartedAt"),
                attempt_id=marker.get("id"),
                attempt_marker=marker,
                guard_failed_closed=True,
            )
            print(f"   🛑 {failure_reason}")
            return False

        _set_followup_send_outcome(
            attempt_at=marker.get("sendStartedAt"),
            attempt_id=marker.get("id"),
            attempt_marker=reconciled_attempt,
        )
        history_saved = _save_followup_message(
            user_id,
            thread_id,
            recipient,
            subject,
            body,
            to_recipients=(
                marker.get("toRecipients")
                or ([recipient] if recipient else [])
            ),
            cc_recipients=marker.get("ccRecipients") or [],
            attempt_id=marker.get("id"),
        )
        if not history_saved:
            failure_reason = (
                "Durable follow-up matched Sent Items but history persistence "
                "failed; reconciliation will retry"
            )
            _set_followup_send_outcome(error=failure_reason)
            print(f"   ⚠️ {failure_reason}")
            return False
        return True

    try:
        manual_continuation = find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=conversation_id,
            sent_after=sent_after,
        )
    except SentMailGuardLookupError as exc:
        failure_reason = f"Sent Items durable continuation guard failed: {exc}"
        _set_followup_send_outcome(
            error=failure_reason,
            attempt_at=marker.get("sendStartedAt"),
            attempt_id=marker.get("id"),
            attempt_marker=marker,
            guard_failed_closed=True,
        )
        print(f"   ⚠️ {failure_reason}")
        return False

    if manual_continuation:
        failure_reason = (
            "Durable follow-up attempt overlaps a manual conversation continuation; "
            "manual review required"
        )
    else:
        failure_reason = (
            "Durable follow-up send outcome could not be conclusively matched in "
            "Sent Items; manual review required and automatic resend suppressed"
        )
    _set_followup_send_outcome(
        error=failure_reason,
        attempt_at=marker.get("sendStartedAt"),
        attempt_id=marker.get("id"),
        attempt_marker=marker,
        guard_failed_closed=True,
    )
    print(f"   🛑 {failure_reason}")
    return False


def _followup_operation_state(
    status: str,
    thread_id: Optional[str] = None,
    error: Optional[Any] = None,
    *,
    operation: str = "followup_send",
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an operation-state for a follow-up scan or send outcome.

    Shape matches ``main._combine_graph_operation_states`` (GO-condition #3).
    """
    state: Dict[str, Any] = {"status": status, "operation": operation}
    if code:
        state["code"] = code
    if thread_id:
        state["threadId"] = thread_id
    if error is not None:
        state["error"] = str(error)[:1500]
    return state


def check_and_send_followups(user_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Main entry point: scan threads needing follow-ups and send them.

    Called from main.py every 30 minutes.

    Returns a list of Graph operation-states (GO-condition #3): one per follow-up
    that reached a send outcome, so a swallowed per-item Graph send failure now
    escalates the health rail via ``main._combine_graph_operation_states``.
    """
    print(f"\n{'='*60}")
    print("FOLLOW-UP CHECK")
    print(f"{'='*60}")

    now = datetime.now(timezone.utc)
    followups_sent = 0
    operation_states: List[Dict[str, Any]] = []

    # Query threads with active follow-up tracking
    threads_ref = _fs.collection("users").document(user_id).collection("threads")

    # Ordinary sends come from waiting threads. Recovery is queried
    # independently so an inbound reply/manual pause/terminal transition that
    # lands after Graph acceptance cannot strand an unresolved durable marker.
    try:
        query = threads_ref.where("followUpStatus", "==", "waiting")
        waiting_threads = list(query.stream())
    except Exception as e:
        print(f"   Error querying follow-up threads: {e}")
        return [
            _followup_operation_state(
                "error",
                error=e,
                operation="followup_waiting_query",
                code="followup_waiting_query_failed",
            )
        ]

    recovery_threads = []
    try:
        for attempt_state in ("sending", "uncertain"):
            recovery_query = threads_ref.where(
                "followUpSendAttempt.state",
                "==",
                attempt_state,
            )
            recovery_threads.extend(recovery_query.stream())
    except Exception as e:
        print(f"   ⚠️ Error querying unresolved follow-up attempts: {e}")
        return [
            _followup_operation_state(
                "error",
                error=e,
                operation="followup_recovery_query",
                code="followup_recovery_query_failed",
            )
        ]

    threads_by_id = {}
    for thread_doc in [*waiting_threads, *recovery_threads]:
        threads_by_id[thread_doc.id] = thread_doc
    waiting_threads = list(threads_by_id.values())

    if not waiting_threads:
        print("   No threads waiting for follow-up or recovery")
        return operation_states

    print(f"   Found {len(waiting_threads)} threads with follow-up tracking or recovery")
    total_threads = len(waiting_threads)

    for idx, thread_doc in enumerate(waiting_threads):
        thread_data = thread_doc.to_dict()
        thread_id = thread_doc.id

        followup_config = thread_data.get("followUpConfig", {})
        send_attempt = thread_data.get("followUpSendAttempt")
        recovery_hint = (
            isinstance(send_attempt, dict)
            and str(send_attempt.get("state") or "").strip().lower()
            in {"sending", "uncertain"}
        )

        if not recovery_hint and not followup_config.get("enabled", False):
            continue

        next_followup_at = followup_config.get("nextFollowUpAt")
        if not recovery_hint and not next_followup_at:
            continue

        # Convert Firestore timestamp to datetime for ordinary sends. Durable
        # recovery is lease-driven inside the authoritative claim transaction.
        if not recovery_hint and hasattr(next_followup_at, 'timestamp'):
            next_followup_dt = datetime.fromtimestamp(
                next_followup_at.timestamp(),
                tz=timezone.utc
            )
        elif not recovery_hint:
            continue

        # Check if it's time for follow-up
        if not recovery_hint and now < next_followup_dt:
            time_remaining = next_followup_dt - now
            print(f"   Thread {thread_id[:20]}... - {time_remaining} until follow-up")
            continue

        if not recovery_hint:
            safe_send_time = _next_business_followup_time(now, followup_config)
            if safe_send_time > now:
                print(
                    f"   🗓️ Weekend follow-up window for {thread_id[:20]}...; "
                    f"waiting until {safe_send_time.strftime('%Y-%m-%d %H:%M')} UTC"
                )
                continue

        # Query values are hints only. The transaction below is authoritative
        # for reply/terminal state, index, config, and retry metadata.
        current_index = followup_config.get("currentFollowUpIndex", 0)

        # Claim the follow-up to prevent duplicate sends
        claim_result = _claim_followup(user_id, thread_id, current_index)
        if not claim_result:
            continue
        if isinstance(claim_result, FollowupClaim) or all(
            hasattr(claim_result, attribute)
            for attribute in ("owner", "index", "thread_data", "followup_config")
        ):
            claim_owner = claim_result.owner
            current_index = claim_result.index
            thread_data = claim_result.thread_data
            followup_config = claim_result.followup_config
        else:
            # Compatibility for older injected test doubles. Production claims
            # always return FollowupClaim and therefore authoritative data.
            claim_owner = claim_result if isinstance(claim_result, str) else None

        # Send the follow-up
        _reset_followup_send_outcome()
        success = _send_followup_email(
            user_id=user_id,
            headers=headers,
            thread_id=thread_id,
            thread_data=thread_data,
            followup_config=followup_config,
            followup_index=current_index,
            claim_owner=claim_owner,
        )

        if success:
            followups_sent += 1
            send_outcome = _get_followup_send_outcome()
            try:
                # Schedule next follow-up if there are more. This must surface
                # transaction failures: the Graph send has already happened,
                # so leaving the claim/index untouched could resend it later.
                schedule_outcome = _schedule_next_followup(
                    user_id=user_id,
                    thread_id=thread_id,
                    followup_config=followup_config,
                    just_sent_index=current_index,
                    claim_owner=claim_owner,
                    send_attempt_id=send_outcome.attempt_id,
                    send_attempt_marker=send_outcome.attempt_marker,
                )
            except Exception as exc:
                failure_reason = f"Follow-up post-send scheduling failed: {exc}"
                print(f"   ⚠️ {failure_reason}")
                persisted = _release_followup_claim(
                    user_id,
                    thread_id,
                    reason=failure_reason,
                    attempted_at=send_outcome.attempt_at,
                    current_index=current_index,
                    claim_owner=claim_owner,
                    send_attempt_id=send_outcome.attempt_id,
                    send_attempt_marker=send_outcome.attempt_marker,
                    expected_no_send_attempt=send_outcome.attempt_expected_absent,
                    fail_closed=True,
                )
                if not persisted:
                    failure_reason += "; fail-closed state could not be persisted"
                operation_states.append(
                    _followup_operation_state(
                        "error",
                        thread_id=thread_id,
                        error=failure_reason,
                    )
                )
            else:
                safe_outcomes = {
                    FollowupScheduleOutcome.SCHEDULED,
                    FollowupScheduleOutcome.MAX_REACHED,
                    FollowupScheduleOutcome.INBOUND_PRESERVED,
                    FollowupScheduleOutcome.TERMINAL_PRESERVED,
                    FollowupScheduleOutcome.PAUSED_PRESERVED,
                    FollowupScheduleOutcome.ALREADY_COMMITTED,
                }
                if schedule_outcome not in safe_outcomes:
                    failure_reason = (
                        "Follow-up post-send scheduling is ambiguous: "
                        f"{getattr(schedule_outcome, 'value', schedule_outcome)!r}"
                    )
                    print(f"   ⚠️ {failure_reason}")
                    persisted = _release_followup_claim(
                        user_id,
                        thread_id,
                        reason=failure_reason,
                        attempted_at=send_outcome.attempt_at,
                        current_index=current_index,
                        claim_owner=claim_owner,
                        send_attempt_id=send_outcome.attempt_id,
                        send_attempt_marker=send_outcome.attempt_marker,
                        expected_no_send_attempt=send_outcome.attempt_expected_absent,
                        fail_closed=True,
                    )
                    if not persisted:
                        failure_reason += "; reconciliation fence could not be updated"
                    operation_states.append(
                        _followup_operation_state(
                            "error",
                            thread_id=thread_id,
                            error=failure_reason,
                        )
                    )
                else:
                    operation_states.append(
                        _followup_operation_state("healthy", thread_id=thread_id)
                    )

            # Stagger follow-up sends by 2 minutes to avoid spam detection
            # Only sleep if there are more threads to process
            remaining_threads = total_threads - (idx + 1)
            if remaining_threads > 0:
                print(f"   ⏳ Waiting 2 minutes before next follow-up ({remaining_threads} remaining)...")
                time.sleep(120)  # 2 minutes
        else:
            send_outcome = _get_followup_send_outcome()
            campaign_suppression_kind = send_outcome.campaign_suppression_kind
            if campaign_suppression_kind == "terminal":
                stop_reason = (
                    send_outcome.campaign_decision.reason
                    if send_outcome.campaign_decision
                    else send_outcome.error
                )
                terminalized, terminalize_error = _terminalize_owned_followup(
                    user_id,
                    thread_id,
                    reason=stop_reason,
                    current_index=current_index,
                    claim_owner=claim_owner,
                    expected_client_id=thread_data.get("clientId"),
                )
                if not terminalized:
                    failure_reason = (
                        "Stopped-campaign follow-up transition was not applied: "
                        f"{terminalize_error}"
                    )
                    print(f"   ⚠️ {failure_reason}")
                    operation_states.append(
                        _followup_operation_state(
                            "error",
                            thread_id=thread_id,
                            error=failure_reason,
                        )
                    )
                continue
            if campaign_suppression_kind in {"maintenance", "unknown"}:
                _release_followup_claim(
                    user_id,
                    thread_id,
                    reason=send_outcome.error,
                    current_index=current_index,
                    claim_owner=claim_owner,
                    send_attempt_id=send_outcome.attempt_id,
                    send_attempt_marker=send_outcome.attempt_marker,
                    expected_no_send_attempt=send_outcome.attempt_expected_absent,
                    fail_closed=False,
                )
                continue

            # Release the claim so it can be retried
            _release_followup_claim(
                user_id,
                thread_id,
                reason=send_outcome.error,
                attempted_at=send_outcome.attempt_at,
                current_index=current_index,
                claim_owner=claim_owner,
                send_attempt_id=send_outcome.attempt_id,
                send_attempt_marker=send_outcome.attempt_marker,
                expected_no_send_attempt=send_outcome.attempt_expected_absent,
                fail_closed=send_outcome.guard_failed_closed,
            )
            # Swallowed per-item Graph send failure -> surface to the health rail.
            operation_states.append(
                _followup_operation_state(
                    "error",
                    thread_id=thread_id,
                    error=send_outcome.error or "follow-up send failed",
                )
            )

    print(f"\n   Sent {followups_sent} follow-up email(s)")
    return operation_states


def _send_followup_email(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    thread_data: Dict,
    followup_config: Dict,
    followup_index: int,
    claim_owner: Optional[str] = None,
) -> bool:
    """Send a follow-up email for a specific thread."""
    import requests

    _reset_followup_send_outcome()
    if not _followup_index_is_valid(followup_index):
        _set_followup_send_outcome(
            error=f"Invalid follow-up index: {followup_index!r}",
            guard_failed_closed=True,
        )
        return False
    config_has_index = (
        isinstance(followup_config, dict)
        and "currentFollowUpIndex" in followup_config
    )
    config_index = (followup_config or {}).get("currentFollowUpIndex")
    # Reject supplied type/value mismatches during preflight.  The durable
    # send-intent transaction below also requires the index to be present.
    if config_has_index and not _followup_indexes_exactly_match(
        config_index,
        followup_index,
    ):
        _set_followup_send_outcome(
            error="Follow-up config index does not exactly match the requested index",
            guard_failed_closed=True,
        )
        return False
    reconciliation_result = _reconcile_durable_followup_attempt(
        user_id,
        thread_id,
        headers,
        thread_data,
        followup_index,
        claim_owner,
    )
    if reconciliation_result is not None:
        return reconciliation_result

    outbound_mode = resolve_outbound_mode()
    if outbound_mode != OUTBOUND_MODE_LIVE:
        reason = (
            "suppressed_by_kill_switch "
            f"(SITESIFT_OUTBOUND_MODE={outbound_mode})"
        )
        _set_followup_send_outcome(error=reason)
        _kill_switch_suppressed(
            outbound_mode,
            context=f"_send_followup_email thread {thread_id}",
        )
        return False

    try:
        campaign_decision = get_client_automation_decision(
            user_id,
            thread_data.get("clientId"),
        )
        if campaign_decision.denies_autonomous_work:
            _set_followup_campaign_suppression(campaign_decision)
            print(f"   🛑 {_get_followup_send_outcome().error}")
            return False

        followups = followup_config.get("followUps", [])
        if followup_index >= len(followups):
            return False

        followup = followups[followup_index]
        followup_message = followup.get("message", "")

        if not followup_message:
            followup_message = _get_default_followup_message(followup_index)

        contract_error = _followup_column_contract_error(
            followup_message,
            campaign_decision,
        )
        if contract_error:
            failure_reason = f"{contract_error}; manual review required before sending follow-up"
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        recipient_emails = thread_data.get("email", [])
        if not recipient_emails:
            print(f"   No recipient email for thread {thread_id[:20]}...")
            return False

        recipient = recipient_emails[0] if isinstance(recipient_emails, list) else recipient_emails
        valid_recipients, invalid_recipients = validate_recipient_emails([recipient])
        if invalid_recipients or not valid_recipients:
            invalid_value = invalid_recipients[0] if invalid_recipients else recipient
            failure_reason = (
                f"Invalid follow-up recipient {invalid_value}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        recipient = valid_recipients[0]
        try:
            from .processing import is_contact_opted_out
            optout_record = is_contact_opted_out(user_id, recipient)
        except Exception as e:
            failure_reason = (
                f"Could not verify follow-up opt-out status for {recipient}: {e}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        if optout_record:
            failure_reason = (
                f"Follow-up recipient {recipient} is opted out; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        # Get the last outbound message to reply to
        messages_ref = (_fs.collection("users").document(user_id)
                       .collection("threads").document(thread_id)
                       .collection("messages"))

        try:
            outbound_messages = list(
                messages_ref.where("direction", "==", "outbound")
                .order_by("sentDateTime", direction="DESCENDING")
                .limit(10)
                .stream()
            )
        except Exception as e:
            # Index might not exist, try without order_by
            outbound_messages = [
                doc for doc in messages_ref.stream()
                if doc.to_dict().get("direction") == "outbound"
            ]
            if outbound_messages:
                outbound_messages.sort(
                    key=lambda doc: (doc.to_dict() or {}).get("sentDateTime", ""),
                    reverse=True
                )

        if not outbound_messages:
            print(f"   No outbound messages found in thread {thread_id[:20]}...")
            return False

        last_outbound = _select_reply_anchor_message(outbound_messages)
        if not last_outbound:
            print(f"   No Graph-backed outbound message found in thread {thread_id[:20]}...")
            return False

        internet_msg_id = last_outbound.get("headers", {}).get("internetMessageId")

        # Find the Graph message ID
        base = "https://graph.microsoft.com/v1.0"

        if internet_msg_id:
            # Search by internetMessageId
            search_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages",
                    headers=headers,
                    params={
                        "$filter": f"internetMessageId eq '{internet_msg_id}'",
                        "$select": "id,subject,conversationId"
                    },
                    timeout=30
                )
            )

            if search_resp.status_code != 200:
                print(f"   Failed to find message: {search_resp.status_code}")
                return False

            messages = search_resp.json().get("value", [])
            if not messages:
                print(f"   Message not found in mailbox")
                return False

            graph_msg_id = messages[0]["id"]
            subject = messages[0].get("subject", thread_data.get("subject", "Follow-up"))
            conversation_id = messages[0].get("conversationId")
        else:
            print(f"   No internetMessageId for reply")
            return False

        # Personalize the message with contact name if available
        contact_name = thread_data.get("contactName", "")
        sheet_contact_name = None

        # Fallback: fetch contact name from sheet if not on thread
        contact_name_is_missing = (
            contact_name is None
            or (isinstance(contact_name, str) and not contact_name.strip())
        )
        if contact_name_is_missing and "[NAME]" in followup_message:
            try:
                from .clients import _get_sheet_id_or_fail, _sheets_client
                client_id = thread_data.get("clientId")
                row_number = thread_data.get("rowNumber")
                if client_id and row_number:
                    sheet_id = _get_sheet_id_or_fail(user_id, client_id)
                    sheets = _sheets_client()
                    # Fetch the row to get Leasing Contact (column E = index 4)
                    result = sheets.spreadsheets().values().get(
                        spreadsheetId=sheet_id,
                        range=f"A{row_number}:F{row_number}"
                    ).execute()
                    row_values = result.get("values", [[]])[0]
                    if len(row_values) >= 5:
                        sheet_contact_name = _safe_followup_contact_name(
                            row_values[4]
                        )
                        if sheet_contact_name:
                            contact_name = sheet_contact_name
                            print(
                                "   Fetched safe contact name from sheet: "
                                f"{contact_name}"
                            )
            except Exception as e:
                print(f"   Could not fetch contact name from sheet: {e}")

        followup_message = _resolve_followup_message(
            followup_config,
            followup_index,
            contact_name,
        )

        body_validation = validate_outbound_body(followup_message)
        if not body_validation.is_safe:
            failure_reason = (
                f"{body_validation.reason}; manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        # Get user's signature settings
        user_doc = _fs.collection("users").document(user_id).get()
        user_signature = None
        signature_mode = None
        user_email = None
        if user_doc.exists:
            user_data = user_doc.to_dict() or {}
            user_signature, signature_mode, user_email = resolve_signature_settings(user_data)

        # Format as HTML with signature
        html_content = format_email_body_with_footer(
            followup_message,
            user_signature,
            signature_mode,
            user_email=user_email,
        )

        last_attempt_index = followup_config.get("lastSendAttemptIndex")
        if (
            last_attempt_index is not None
            and not _followup_index_is_valid(last_attempt_index)
        ):
            failure_reason = "Follow-up retry index is malformed; manual review required"
            _set_followup_send_outcome(
                error=failure_reason,
                guard_failed_closed=True,
            )
            print(f"   🛑 {failure_reason}")
            return False
        legacy_retry_exists = any(
            followup_config.get(field) is not None
            for field in (
                "lastSendError",
                "lastSendAttemptAt",
                "lastSendAttemptIndex",
            )
        )
        legacy_retry_timestamp = None
        legacy_retry_timestamp_signature = None
        if legacy_retry_exists:
            validated_retry_timestamp = _validated_followup_retry_timestamp(
                followup_config
            )
            if validated_retry_timestamp is None:
                failure_reason = (
                    "Follow-up retry timestamp is missing or invalid; "
                    "manual review required"
                )
                _set_followup_send_outcome(
                    error=failure_reason,
                    guard_failed_closed=True,
                )
                print(f"   🛑 {failure_reason}")
                return False
            (
                legacy_retry_timestamp,
                legacy_retry_timestamp_signature,
            ) = validated_retry_timestamp
        retry_state_matches_current_followup = (
            last_attempt_index is None
            or _followup_indexes_exactly_match(
                last_attempt_index,
                followup_index,
            )
        )
        if retry_state_matches_current_followup and legacy_retry_exists:
            legacy_retry_sent_after = (
                legacy_retry_timestamp - timedelta(seconds=30)
            )
            try:
                sent_match = find_matching_sent_message_for_retry(
                    headers,
                    recipient=recipient,
                    body=followup_message,
                    subject=subject,
                    conversation_id=conversation_id,
                    sent_after=legacy_retry_sent_after,
                )
            except SentMailGuardLookupError as exc:
                failure_reason = f"Sent Items retry guard failed: {exc}"
                _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                print(f"   ⚠️ {failure_reason}")
                return False

            if sent_match:
                print(f"   ⚠️ Prior follow-up send found in Sent Items; recording without resending")
                try:
                    legacy_attempt, migrate_error, legacy_attempt_id = (
                        _migrate_legacy_sent_match(
                            user_id,
                            thread_id,
                            claim_owner=claim_owner,
                            followup_index=followup_index,
                            expected_thread_data=thread_data,
                            expected_followup_config=followup_config,
                            recipient=recipient,
                            body=followup_message,
                            subject=subject,
                            conversation_id=conversation_id,
                            sent_match=sent_match,
                            expected_retry_timestamp_signature=(
                                legacy_retry_timestamp_signature
                            ),
                        )
                    )
                except Exception as exc:
                    legacy_attempt = None
                    migrate_error = f"could not persist legacy reconciliation: {exc}"
                    legacy_attempt_id = "followup-legacy-unpersisted"

                if not legacy_attempt:
                    failure_reason = (
                        "legacy reconciliation state changed: "
                        f"{migrate_error}; manual review required"
                    )
                    legacy_review_attempt = _legacy_followup_review_attempt(
                        user_id,
                        thread_id,
                        claim_owner=claim_owner,
                        followup_index=followup_index,
                        expected_thread_data=thread_data,
                        expected_followup_config=followup_config,
                        recipient=recipient,
                        body=followup_message,
                        subject=subject,
                        conversation_id=conversation_id,
                        sent_match=sent_match,
                        error=failure_reason,
                    )
                    _set_followup_send_outcome(
                        error=failure_reason,
                        attempt_at=legacy_review_attempt.get("sendStartedAt"),
                        attempt_id=legacy_review_attempt.get("id"),
                        attempt_marker=legacy_review_attempt,
                        attempt_expected_absent=True,
                        guard_failed_closed=True,
                    )
                    print(f"   🛑 {failure_reason}")
                    return False

                _set_followup_send_outcome(
                    attempt_at=legacy_attempt.get("sendStartedAt"),
                    attempt_id=legacy_attempt.get("id"),
                    attempt_marker=legacy_attempt,
                )
                history_saved = _save_followup_message(
                    user_id, thread_id, recipient, subject,
                    followup_message, user_signature, signature_mode, user_email,
                    to_recipients=legacy_attempt.get("toRecipients") or [recipient],
                    cc_recipients=legacy_attempt.get("ccRecipients") or [],
                    attempt_id=legacy_attempt.get("id"),
                )
                if not history_saved:
                    failure_reason = (
                        "Prior follow-up was found in Sent Items but history "
                        "persistence failed; reconciliation will retry"
                    )
                    _set_followup_send_outcome(error=failure_reason)
                    print(f"   ⚠️ {failure_reason}")
                    return False
                return True
            try:
                manual_continuation = find_sent_conversation_continuation_for_retry(
                    headers,
                    conversation_id=conversation_id,
                    sent_after=legacy_retry_sent_after,
                )
            except SentMailGuardLookupError as exc:
                failure_reason = f"Sent Items manual continuation guard failed: {exc}"
                _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                print(f"   ⚠️ {failure_reason}")
                return False

            if manual_continuation:
                failure_reason = (
                    "Follow-up stopped because Sent Items shows the user manually continued "
                    "this conversation; review before retrying the stale follow-up."
                )
                _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                print(f"   ⚠️ {failure_reason}")
                return False

        # Send as a filtered reply-all draft so broker CCs are preserved safely.
        send_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        _set_followup_send_outcome(attempt_at=send_attempt_at)

        try:
            latest_thread_data, terminal_reason = _read_followup_send_precondition(
                user_id,
                thread_id,
                followup_index,
                followup_config,
                claim_owner=claim_owner,
            )
        except Exception as exc:
            failure_reason = (
                f"Could not verify latest follow-up thread state: {exc}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        if terminal_reason:
            failure_reason = (
                f"Follow-up stopped before send because {terminal_reason}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            print(f"   🛑 {failure_reason}")
            return False

        from .email import (
            _delete_graph_reply_draft,
            _filter_reply_all_draft_recipients,
            _hydrate_reply_all_draft_recipients,
            _reviewed_recipient_reply_all_fallback,
            _source_message_reply_all_fallback,
        )

        create_reply_resp = exponential_backoff_request(
            lambda: requests.post(
                f"{base}/me/messages/{graph_msg_id}/createReplyAll",
                headers=headers,
                timeout=30,
            )
        )
        if not create_reply_resp or create_reply_resp.status_code not in [200, 201, 202]:
            failure_reason = (
                f"createReplyAll failed: {create_reply_resp.status_code if create_reply_resp else 'no response'}"
            )
            _set_followup_send_outcome(error=failure_reason)
            print(f"   ❌ {failure_reason}")
            return False

        reply_draft = create_reply_resp.json() or {}
        reply_draft_id = reply_draft.get("id")
        if not reply_draft_id:
            failure_reason = "createReplyAll returned no draft id"
            _set_followup_send_outcome(error=failure_reason)
            print(f"   ❌ {failure_reason}")
            return False

        source_message = dict(last_outbound or {})
        source_message["replyToEmails"] = [recipient]

        reply_draft = _hydrate_reply_all_draft_recipients(headers, reply_draft, base=base)
        reply_draft = _source_message_reply_all_fallback(reply_draft, source_message)
        reply_draft = _reviewed_recipient_reply_all_fallback(
            reply_draft,
            to_emails=[recipient],
            cc_emails=(
                thread_data.get("ccEmails")
                or thread_data.get("ccRecipients")
                or source_message.get("ccRecipients")
                or source_message.get("cc")
                or []
            ),
        )

        try:
            recipient_result = _filter_reply_all_draft_recipients(
                user_id,
                reply_draft,
                user_email=user_email,
            )
        except Exception as exc:
            failure_reason = (
                f"Could not filter reply-all recipients: {exc}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        recipient_payload = recipient_result["payload"]
        if not recipient_payload["toRecipients"] and recipient:
            recipient_lower = recipient.lower()
            safe_sent_recipients = {
                (address or "").strip().lower()
                for address in recipient_result.get("sentRecipients", [])
            }
            if recipient_lower not in safe_sent_recipients:
                failure_reason = (
                    "Primary follow-up recipient did not pass reply-all safety filtering; "
                    "manual review required before sending follow-up"
                )
                _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                _delete_graph_reply_draft(headers, reply_draft_id, base=base)
                print(f"   🛑 {failure_reason}")
                return False
            recipient_payload["ccRecipients"] = [
                cc_recipient
                for cc_recipient in recipient_payload["ccRecipients"]
                if (
                    ((cc_recipient.get("emailAddress") or {}).get("address") or "")
                    .strip()
                    .lower()
                    != recipient_lower
                )
            ]
            recipient_payload["toRecipients"] = [{"emailAddress": {"address": recipient}}]
        if not (recipient_payload["toRecipients"] or recipient_payload["ccRecipients"]):
            failure_reason = "No safe reply-all recipients remained after filtering"
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   ❌ {failure_reason}")
            return False

        final_to_recipients = _normalize_followup_recipients(
            recipient_payload["toRecipients"]
        )
        final_cc_recipients = _normalize_followup_recipients(
            recipient_payload["ccRecipients"]
        )

        patch_resp = exponential_backoff_request(
            lambda: requests.patch(
                f"{base}/me/messages/{reply_draft_id}",
                headers=headers,
                json={
                    "body": {"contentType": "HTML", "content": html_content},
                    "toRecipients": recipient_payload["toRecipients"],
                    "ccRecipients": recipient_payload["ccRecipients"],
                },
                timeout=30,
            )
        )
        if not patch_resp or patch_resp.status_code not in [200, 202]:
            failure_reason = (
                f"Patch reply-all draft failed: {patch_resp.status_code if patch_resp else 'no response'}"
            )
            _set_followup_send_outcome(error=failure_reason)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   ❌ {failure_reason}")
            return False

        if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
            signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)
            for attachment in signature_attachments:
                try:
                    att_resp = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{reply_draft_id}/attachments",
                            headers=headers,
                            json=att,
                            timeout=30
                        )
                    )
                    if att_resp and att_resp.status_code in [200, 201]:
                        print(f"      📎 Attached {attachment['name']}")
                    else:
                        failure_reason = (
                            f"Could not attach required signature asset {attachment['name']}; "
                            "manual review required before sending follow-up"
                        )
                        _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                        _delete_graph_reply_draft(headers, reply_draft_id, base=base)
                        print(f"      🛑 {failure_reason}")
                        return False
                except Exception as e:
                    failure_reason = (
                        f"Could not attach required signature asset {attachment['name']}: {e}; "
                        "manual review required before sending follow-up"
                    )
                    _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
                    _delete_graph_reply_draft(headers, reply_draft_id, base=base)
                    print(f"      🛑 {failure_reason}")
                    return False

        campaign_decision = get_client_automation_decision(
            user_id,
            (latest_thread_data or thread_data).get("clientId")
            or thread_data.get("clientId"),
        )
        if campaign_decision.denies_autonomous_work:
            _set_followup_campaign_suppression(campaign_decision)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {_get_followup_send_outcome().error}")
            return False
        contract_error = _followup_column_contract_error(
            followup_message,
            campaign_decision,
        )
        if contract_error:
            failure_reason = f"{contract_error}; manual review required before sending follow-up"
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        try:
            _final_thread_data, terminal_reason = _read_followup_send_precondition(
                user_id,
                thread_id,
                followup_index,
                followup_config,
                claim_owner=claim_owner,
            )
        except Exception as exc:
            failure_reason = (
                f"Could not revalidate follow-up thread state at Graph send: {exc}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        if terminal_reason:
            failure_reason = (
                f"Follow-up stopped at Graph send because {terminal_reason}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(error=failure_reason, guard_failed_closed=True)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        outbound_mode = resolve_outbound_mode()
        if outbound_mode != OUTBOUND_MODE_LIVE:
            reason = (
                "suppressed_by_kill_switch "
                f"(SITESIFT_OUTBOUND_MODE={outbound_mode})"
            )
            _set_followup_send_outcome(error=reason)
            _kill_switch_suppressed(
                outbound_mode,
                context=f"_send_followup_email thread {thread_id} at Graph send",
            )
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            return False

        if not claim_owner:
            failure_reason = (
                "Follow-up stopped at Graph send because the durable claim owner "
                "is missing; manual review required before sending follow-up"
            )
            _set_followup_send_outcome(
                error=failure_reason,
                guard_failed_closed=True,
            )
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        try:
            send_attempt, intent_error = _persist_followup_send_intent(
                user_id,
                thread_id,
                claim_owner=claim_owner,
                followup_index=followup_index,
                expected_thread_data=thread_data,
                expected_followup_config=followup_config,
                recipient=recipient,
                body=followup_message,
                subject=subject,
                conversation_id=conversation_id,
                draft_id=reply_draft_id,
                to_recipients=final_to_recipients,
                cc_recipients=final_cc_recipients,
                fallback_contact_name=sheet_contact_name,
                expected_retry_timestamp_signature=(
                    legacy_retry_timestamp_signature
                ),
            )
        except Exception as exc:
            send_attempt = None
            intent_error = f"could not persist durable send intent: {exc}"

        if not send_attempt:
            failure_reason = (
                f"Follow-up stopped at Graph send because {intent_error}; "
                "manual review required before sending follow-up"
            )
            _set_followup_send_outcome(
                error=failure_reason,
                guard_failed_closed=True,
            )
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {failure_reason}")
            return False

        _set_followup_send_outcome(
            attempt_at=send_attempt.get("sendStartedAt"),
            attempt_id=send_attempt.get("id"),
            attempt_marker=send_attempt,
        )

        reply_resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{reply_draft_id}/send", headers=headers, timeout=30),
            max_retries=1,
            operation="graph_send",
        )

        if reply_resp.status_code in [200, 201, 202]:
            print(f"   Sent follow-up #{followup_index + 1} for thread {thread_id[:20]}...")
            history_saved = _save_followup_message(
                user_id, thread_id, recipient, subject,
                followup_message, user_signature, signature_mode, user_email,
                to_recipients=final_to_recipients,
                cc_recipients=final_cc_recipients,
                attempt_id=send_attempt.get("id"),
            )
            if not history_saved:
                failure_reason = (
                    "Follow-up was accepted by Graph but history persistence "
                    "failed; reconciliation will retry"
                )
                _set_followup_send_outcome(error=failure_reason)
                print(f"   ⚠️ {failure_reason}")
                return False

            # Update thread
            _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
                "lastOutboundAt": SERVER_TIMESTAMP,
                "updatedAt": SERVER_TIMESTAMP,
                "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP
            })

            return True
        else:
            print(f"   Failed to send follow-up: {reply_resp.status_code}")
            _set_followup_send_outcome(
                error=f"Follow-up Graph send returned HTTP {reply_resp.status_code}"
            )
            return False

    except Exception as e:
        _set_followup_send_outcome(error=str(e))
        print(f"   Error sending follow-up: {e}")
        return False


def _schedule_next_followup(
    user_id: str,
    thread_id: str,
    followup_config: Dict,
    just_sent_index: int,
    claim_owner: str,
    send_attempt_id: Optional[str] = None,
    send_attempt_marker: Optional[Dict[str, Any]] = None,
) -> FollowupScheduleOutcome:
    """Advance the exact active claim without overwriting newer thread state.

    ``followup_config`` is the authoritative snapshot returned by the claim;
    the transactional read below fences every post-send decision against it.
    """
    from google.cloud.firestore import transactional

    if not _followup_index_is_valid(just_sent_index):
        return FollowupScheduleOutcome.AMBIGUOUS

    thread_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
    )

    @transactional
    def schedule_transaction(transaction, thread_ref):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return FollowupScheduleOutcome.AMBIGUOUS, None

        data = snapshot.to_dict() or {}
        current_config = data.get("followUpConfig")
        if not isinstance(current_config, dict):
            return FollowupScheduleOutcome.AMBIGUOUS, None

        current_attempt = data.get("followUpSendAttempt")
        if not isinstance(current_attempt, dict):
            current_attempt = {}
        current_attempt_id = current_attempt.get("id")

        def committed_attempt_update():
            if not send_attempt_id or current_attempt_id != send_attempt_id:
                return {}
            committed_attempt = dict(current_attempt)
            committed_attempt.update({
                "state": "committed",
                "finalizedAt": datetime.now(timezone.utc),
                "resolution": "sent",
                "error": None,
            })
            return {"followUpSendAttempt": committed_attempt}

        followup_status = str(data.get("followUpStatus") or "").strip().lower()
        preservation_outcome = _followup_preservation_outcome(data)

        current_index = current_config.get("currentFollowUpIndex", 0)
        if (
            _followup_indexes_exactly_match(
                current_index,
                just_sent_index + 1,
            )
            and send_attempt_id
            and current_attempt_id == send_attempt_id
            and current_attempt.get("state") == "committed"
        ):
            return FollowupScheduleOutcome.ALREADY_COMMITTED, None

        current_owner = current_config.get("processingBy")
        if not claim_owner or current_owner != claim_owner:
            print(
                f"   ⏭️ Follow-up claim ownership changed from {claim_owner} "
                f"to {current_owner}; not advancing"
            )
            return FollowupScheduleOutcome.AMBIGUOUS, None

        if not _followup_indexes_exactly_match(current_index, just_sent_index):
            print(
                f"   ⏭️ Follow-up index changed from {just_sent_index} "
                f"to {current_index}; not advancing"
            )
            return FollowupScheduleOutcome.AMBIGUOUS, None

        if send_attempt_id and current_attempt_id != send_attempt_id:
            print(
                f"   ⏭️ Durable send attempt changed from {send_attempt_id} "
                f"to {current_attempt_id}; not advancing"
            )
            return FollowupScheduleOutcome.AMBIGUOUS, None
        if send_attempt_id:
            if not _followup_indexes_exactly_match(
                current_attempt.get("index"),
                just_sent_index,
            ):
                print("   ⏭️ Durable send attempt index changed")
                return FollowupScheduleOutcome.AMBIGUOUS, None
            current_attempt_state = str(
                current_attempt.get("state") or ""
            ).strip().lower()
            if current_attempt_state not in {"sending", "uncertain"}:
                print(
                    "   ⏭️ Durable send attempt is no longer active: "
                    f"{current_attempt_state or 'unset'}"
                )
                return FollowupScheduleOutcome.AMBIGUOUS, None
            attempt_owner_matches = (
                current_attempt.get("owner") == claim_owner
                or current_attempt.get("reconciliationOwner") == claim_owner
            )
            if not attempt_owner_matches:
                print("   ⏭️ Durable send attempt ownership changed")
                return FollowupScheduleOutcome.AMBIGUOUS, None
            if (
                send_attempt_marker is not None
                and not _followup_values_exactly_match(
                    current_attempt,
                    send_attempt_marker,
                )
            ):
                print("   ⏭️ Durable send attempt changed after acceptance")
                return FollowupScheduleOutcome.AMBIGUOUS, None

            current_identity = _followup_send_identity(
                data,
                current_config,
                just_sent_index,
            )
            allow_terminal_config_change = preservation_outcome is not None
            if not _followup_send_envelope_is_complete(
                current_attempt,
                expected_identity=current_identity,
                allow_config_fingerprint_change=allow_terminal_config_change,
            ):
                print("   ⏭️ Accepted send envelope is incomplete or changed")
                return FollowupScheduleOutcome.AMBIGUOUS, None

        config_changed = (
            _followup_config_fingerprint(current_config)
            != _followup_config_fingerprint(followup_config)
        )
        if config_changed and not (preservation_outcome and send_attempt_id):
            print("   ⏭️ Follow-up config changed after send; not advancing")
            return FollowupScheduleOutcome.AMBIGUOUS, None

        # Once every accepted-send fence matches, preserve a reply or
        # terminal/manual state that landed after Graph acceptance. Only the
        # attempt/index bookkeeping is finalized; business state is never
        # revived, paused, or terminalized again.
        if preservation_outcome:
            preservation_update = {
                "followUpConfig.currentFollowUpIndex": just_sent_index + 1,
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "followUpConfig.processingLeaseUntil": None,
                "followUpConfig.lastSendError": None,
                "followUpConfig.lastSendAttemptAt": None,
                "followUpConfig.lastSendAttemptIndex": None,
                "updatedAt": SERVER_TIMESTAMP,
            }
            attempt_update = committed_attempt_update()
            if attempt_update:
                preservation_update.update(attempt_update)
            transaction.update(thread_ref, preservation_update)
            return preservation_outcome, None

        block_reason = _followup_terminal_block_reason(
            data,
            current_config,
            just_sent_index,
        )
        if block_reason or followup_status != "waiting":
            reason = block_reason or f"follow-up tracking is {followup_status or 'unset'}"
            print(f"   ⏭️ Follow-up state is blocked but unclassified: {reason}")
            return FollowupScheduleOutcome.AMBIGUOUS, None

        followups = current_config.get("followUps")
        if not isinstance(followups, list) or not followups:
            print("   ⏭️ Current follow-up sequence is missing; not advancing")
            return FollowupScheduleOutcome.AMBIGUOUS, None

        next_index = just_sent_index + 1
        if next_index >= len(followups):
            update_payload = {
                "followUpStatus": "max_reached",
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "followUpConfig.processingLeaseUntil": None,
                "followUpConfig.lastSendError": None,
                "followUpConfig.lastSendAttemptAt": None,
                "followUpConfig.lastSendAttemptIndex": None,
                "status": "stopped",
                "statusReason": "max_followups_reached",
                "updatedAt": SERVER_TIMESTAMP,
            }
            update_payload.update(committed_attempt_update())
            transaction.update(thread_ref, update_payload)
            return FollowupScheduleOutcome.MAX_REACHED, None

        # Calculate from the transaction's current config, not the stale query
        # snapshot. Stored config remains untrusted, so wait bounds stay clamped.
        delta, _wait_time, _wait_unit = _followup_wait_delta(
            followups[next_index],
            default_wait=3,
        )
        next_followup_at = _next_business_followup_time(
            datetime.now(timezone.utc) + delta,
            current_config,
        )
        update_payload = {
            "followUpConfig.currentFollowUpIndex": next_index,
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "followUpConfig.processingBy": None,
            "followUpConfig.processingAt": None,
            "followUpConfig.processingLeaseUntil": None,
            "followUpConfig.lastSendError": None,
            "followUpConfig.lastSendAttemptAt": None,
            "followUpConfig.lastSendAttemptIndex": None,
            "followUpStatus": "waiting",
            "updatedAt": SERVER_TIMESTAMP,
        }
        update_payload.update(committed_attempt_update())
        transaction.update(thread_ref, update_payload)
        return FollowupScheduleOutcome.SCHEDULED, next_followup_at

    outcome, next_followup_at = schedule_transaction(_fs.transaction(), thread_ref)

    if outcome == FollowupScheduleOutcome.MAX_REACHED:
        _clear_followup_row_highlight(user_id, thread_id)
        print(f"   Follow-up sequence complete for thread {thread_id[:20]}... (max_reached)")
    elif outcome == FollowupScheduleOutcome.SCHEDULED:
        print(
            f"   Next follow-up scheduled for "
            f"{next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC"
        )
    return outcome


def schedule_followup_after_auto_response(user_id: str, thread_id: str) -> bool:
    """Resume follow-up tracking after the system sends an automatic mid-thread reply."""
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return False

        thread_data = thread_doc.to_dict() or {}
        if thread_data.get("pendingTerminalReason"):
            return False
        if thread_data.get("status") in {"completed", "stopped"}:
            return False
        if thread_data.get("followUpStatus") == "max_reached":
            return False

        followup_config = thread_data.get("followUpConfig", {})
        if not followup_config.get("enabled", False):
            return False

        followups = followup_config.get("followUps", [])
        current_index = followup_config.get("currentFollowUpIndex", 0)
        if current_index >= len(followups):
            return False

        next_followup = followups[current_index]
        # Clamped: stored config is untrusted (dashboard writes to Firestore)
        delta, wait_time, wait_unit = _followup_wait_delta(next_followup, default_wait=3)

        next_followup_at = _next_business_followup_time(
            datetime.now(timezone.utc) + delta,
            followup_config,
        )
        thread_ref.update({
            "followUpStatus": "waiting",
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "followUpConfig.pausedAt": None,
            "hasInboundReply": False,
            "lastOutboundAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        })

        print(f"   Follow-up rescheduled after auto-response for thread {thread_id[:20]}...")
        return True

    except Exception as e:
        print(f"   Error rescheduling follow-up after auto-response: {e}")
        return False


def _pause_followup(user_id: str, thread_id: str):
    """Pause follow-up sequence when broker responds."""
    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpStatus": "paused",
        "followUpConfig.pausedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })
    print(f"   Paused follow-up for thread {thread_id[:20]}... (broker responded)")


def _mark_followup_complete(user_id: str, thread_id: str, reason: str):
    """Mark follow-up sequence as complete."""
    update_data = {
        "followUpStatus": reason,
        "followUpConfig.processingBy": None,
        "followUpConfig.processingAt": None,
        "updatedAt": SERVER_TIMESTAMP
    }
    if reason == "max_reached":
        update_data.update({
            "status": "stopped",
            "statusReason": "max_followups_reached",
        })
        _clear_followup_row_highlight(user_id, thread_id)

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update(update_data)
    print(f"   Follow-up sequence complete for thread {thread_id[:20]}... ({reason})")


def schedule_followup_for_thread(
    user_id: str,
    thread_id: str,
    followup_config: Dict
):
    """
    Schedule follow-ups for a newly sent thread.
    Called from email.py after sending initial outbound email.

    Args:
        user_id: Firebase user ID
        thread_id: Thread document ID
        followup_config: Configuration from outbox containing:
            - enabled: bool
            - followUps: [{waitTime, waitUnit, message}, ...]
    """
    if not followup_config or not followup_config.get("enabled", False):
        return

    followups = followup_config.get("followUps", [])
    if not followups:
        return

    # Client-written config is untrusted: reject out-of-range waits or an
    # oversized sequence fail-closed (disabled + flagged for review) so the
    # scheduler can never fire an immediate or unbounded auto-send sequence.
    invalid_reason = _validate_followup_steps(followups)
    if invalid_reason:
        print(
            f"   🛑 Rejecting follow-up config for thread {thread_id[:20]}...: "
            f"{invalid_reason}"
        )
        _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
            "followUpConfig": {
                "enabled": False,
                "invalidReason": invalid_reason,
                "rejectedAt": SERVER_TIMESTAMP,
            },
            "followUpStatus": "needs_review",
            "status": "action_needed",
            "statusReason": FOLLOWUP_INVALID_CONFIG_REASON,
            "updatedAt": SERVER_TIMESTAMP,
        })
        return

    # Calculate first follow-up time
    first_followup = followups[0]
    delta, wait_time, wait_unit = _followup_wait_delta(first_followup, default_wait=5)

    next_followup_at = _next_business_followup_time(
        datetime.now(timezone.utc) + delta,
        followup_config,
    )

    # Update thread with follow-up config
    thread_followup_config = {
        "enabled": True,
        "followUps": followups,
        "currentFollowUpIndex": 0,
        "nextFollowUpAt": next_followup_at,
        "conversationStage": "initial",
        "pausedAt": None,
        "lastFollowUpSentAt": None
    }

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpConfig": thread_followup_config,
        "followUpStatus": "waiting",
        "hasInboundReply": False,
        "lastOutboundAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })

    print(f"   Follow-up scheduled: {wait_time} {wait_unit} ({next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC)")


def cancel_followup_on_response(user_id: str, thread_id: str):
    """
    Pause pending follow-up when broker responds.
    Called from processing.py when inbound message is detected.

    The sequence can resume if the broker goes silent again.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return

        thread_data = thread_doc.to_dict()
        followup_config = thread_data.get("followUpConfig", {})

        if not followup_config.get("enabled", False):
            return

        current_status = thread_data.get("followUpStatus")
        if current_status in ["paused", "completed", "max_reached"]:
            return

        thread_ref.update({
            "hasInboundReply": True,
            "lastInboundAt": SERVER_TIMESTAMP,
            "followUpStatus": "paused",
            "followUpConfig.pausedAt": SERVER_TIMESTAMP,
            "followUpConfig.conversationStage": "mid_conversation",
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Follow-up paused for thread {thread_id[:20]}... (broker responded)")

    except Exception as e:
        print(f"   Error pausing follow-up: {e}")


def resume_followup_if_silent(user_id: str, thread_id: str, silence_threshold_days: int = 3):
    """
    Resume follow-up sequence if broker went silent after responding.

    This is called to check paused threads and see if they should resume.
    Typically called from check_and_send_followups for paused threads.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return False

        thread_data = thread_doc.to_dict()

        if thread_data.get("followUpStatus") != "paused":
            return False

        last_inbound_at = thread_data.get("lastInboundAt")
        if not last_inbound_at:
            return False

        # Check if enough time has passed since last inbound
        if hasattr(last_inbound_at, 'timestamp'):
            last_inbound_dt = datetime.fromtimestamp(
                last_inbound_at.timestamp(),
                tz=timezone.utc
            )
        else:
            return False

        now = datetime.now(timezone.utc)
        silence_duration = now - last_inbound_dt

        if silence_duration < timedelta(days=silence_threshold_days):
            return False

        # Resume the sequence
        followup_config = thread_data.get("followUpConfig", {})
        current_index = followup_config.get("currentFollowUpIndex", 0)
        followups = followup_config.get("followUps", [])

        if current_index >= len(followups):
            return False

        # Calculate next follow-up time (short delay). Use the unit-aware delta
        # from _followup_wait_delta (which also clamps untrusted stored
        # waitTime: negative/non-numeric -> default), then cap the delta itself
        # at 1 day so a minute/hour step keeps its unit instead of being
        # reinterpreted as days.
        next_followup = followups[current_index]
        delta, _wait, _unit = _followup_wait_delta(next_followup, default_wait=1)
        delta = min(delta, timedelta(days=1))  # Cap at 1 day for resumed

        next_followup_at = now + delta

        thread_ref.update({
            "followUpStatus": "waiting",
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "hasInboundReply": False,  # Reset for next check
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Resumed follow-up for thread {thread_id[:20]}... (broker went silent)")
        return True

    except Exception as e:
        print(f"   Error resuming follow-up: {e}")
        return False


def _get_default_followup_message(index: int) -> str:
    """Return default follow-up message based on sequence position."""
    messages = [
        # Follow-up 1: Friendly reminder
        """Hi [NAME],

I wanted to follow up on my previous email regarding the property above. I understand you're busy, but I wanted to confirm whether this space might be a fit for my client's requirements.

If you could share the key specs (SF, asking rent, NNN, clear height, doors, power), that would be very helpful.

Thanks for your time!""",

        # Follow-up 2: Gentle nudge
        """Hi [NAME],

Just a quick check-in on my earlier emails about the property above. If you have a moment, I'd appreciate any details you can share.

If this property is no longer available or not a good fit, please let me know and I'll update my records.

Thank you!""",

        # Follow-up 3: Final attempt
        """Hi [NAME],

This will be my final follow-up regarding the property above. I'll assume this one isn't a fit for my client's needs, but if you'd like to discuss, I'm happy to connect.

If anything else comes available in the area that might work, please keep me in mind.

Thanks again for your time!"""
    ]

    if index < len(messages):
        return messages[index]
    return messages[-1]
