"""Canonical, byte-identical payload construction for managed N=1 recovery."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List


_INPUT_KEYS = ("uid", "clientId", "outboxId", "recoveryRunId", "outbox")
_OUTBOX_KEYS = ("assignedEmails", "script", "subject", "askFields", "rowNumber")
_PAYLOAD_KEYS = (
    "schemaVersion",
    "recoveryProfile",
    "uid",
    "clientId",
    "outboxId",
    "recoveryRunId",
    "source",
    "actionType",
    "assignedEmails",
    "script",
    "subject",
    "askFields",
    "rowNumber",
)
_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_ECMASCRIPT_TRIM_CHARACTERS = (
    "\u0009\u000b\u000c\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u202f\u205f\u3000\ufeff\u000a\u000d\u2028\u2029"
)
_MAX_SAFE_INTEGER = (2**53) - 1
_SUBJECT_LINE_BREAKS = frozenset("\u000a\u000b\u000c\u000d\u0085\u2028\u2029")


def _require_exact_keys(value: Any, expected_keys: tuple[str, ...], label: str) -> dict:
    if type(value) is not dict:
        raise TypeError(f"{label} fields are invalid")
    actual_keys = tuple(value)
    if len(actual_keys) != len(expected_keys) or any(
        type(key) is not str for key in actual_keys
    ):
        raise TypeError(f"{label} fields are invalid")
    for expected_key in expected_keys:
        if not any(actual_key == expected_key for actual_key in actual_keys):
            raise TypeError(f"{label} fields are invalid")
    return value


def _require_string(value: Any, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TypeError(f"{label} must be valid Unicode") from error
    return value


def _trim(value: str) -> str:
    return value.strip(_ECMASCRIPT_TRIM_CHARACTERS)


def _normalize_id(value: Any, label: str) -> str:
    normalized = _trim(_require_string(value, label))
    if _ID_PATTERN.fullmatch(normalized) is None:
        raise TypeError(f"{label} is invalid")
    return normalized


def _ascii_lower(value: str) -> str:
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character
        for character in value
    )


def _normalize_email(value: Any) -> str:
    normalized = _ascii_lower(_trim(_require_string(value, "assigned email")))
    separator = normalized.rfind("@")
    if (
        len(normalized) > 254
        or separator < 1
        or separator > 64
        or _EMAIL_PATTERN.fullmatch(normalized) is None
    ):
        raise TypeError("assigned email is invalid")
    return normalized


def _normalize_assigned_emails(value: Any) -> List[str]:
    if type(value) is not list or len(value) != 1:
        raise TypeError("assignedEmails must contain exactly one recipient")
    return [_normalize_email(value[0])]


def _normalize_script(value: Any) -> str:
    script = _require_string(value, "script").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    if not _trim(script):
        raise TypeError("script must be nonempty")
    return script


def _normalize_subject(value: Any) -> str:
    subject = _trim(_require_string(value, "subject"))
    if not subject:
        raise TypeError("subject must be nonempty")
    if any(character in _SUBJECT_LINE_BREAKS for character in subject):
        raise TypeError("subject must be one line")
    if len(subject) > 255:
        raise TypeError("subject must be at most 255 characters")
    return subject


def _normalize_ask_fields(value: Any) -> List[str]:
    if type(value) is not list or len(value) > 250:
        raise TypeError("askFields must be an array of at most 250 strings")

    normalized: List[str] = []
    seen = set()
    for raw_field in value:
        field = _trim(_require_string(raw_field, "ask field"))
        if not field:
            raise TypeError("ask fields must be nonempty after trimming")
        comparison_key = _ascii_lower(field)
        if comparison_key not in seen:
            seen.add(comparison_key)
            normalized.append(field)
    return normalized


def _normalize_row_number(value: Any) -> int | None:
    if value is None:
        return None
    if (
        type(value) is not int
        or value <= 0
        or value > _MAX_SAFE_INTEGER
    ):
        raise TypeError("rowNumber must be null or a positive integer")
    return value


def build_canonical_recovery_payload(input_data: Any) -> Dict[str, Any]:
    """Validate closed input shapes and construct the canonical ordered payload."""

    test_input = _require_exact_keys(input_data, _INPUT_KEYS, "recovery payload input")
    outbox = _require_exact_keys(test_input["outbox"], _OUTBOX_KEYS, "recovery outbox")

    return {
        "schemaVersion": 1,
        "recoveryProfile": "managedInitialOutreachN1",
        "uid": _normalize_id(test_input["uid"], "uid"),
        "clientId": _normalize_id(test_input["clientId"], "clientId"),
        "outboxId": _normalize_id(test_input["outboxId"], "outboxId"),
        "recoveryRunId": _normalize_id(
            test_input["recoveryRunId"], "recoveryRunId"
        ),
        "source": "managed_initial_outreach_n1",
        "actionType": "campaign_launch",
        "assignedEmails": _normalize_assigned_emails(outbox["assignedEmails"]),
        "script": _normalize_script(outbox["script"]),
        "subject": _normalize_subject(outbox["subject"]),
        "askFields": _normalize_ask_fields(outbox["askFields"]),
        "rowNumber": _normalize_row_number(outbox["rowNumber"]),
    }


def _canonical_payload_from_output(payload: Any) -> Dict[str, Any]:
    candidate = _require_exact_keys(payload, _PAYLOAD_KEYS, "canonical recovery payload")
    if (
        type(candidate["schemaVersion"]) is not int
        or candidate["schemaVersion"] != 1
        or type(candidate["recoveryProfile"]) is not str
        or candidate["recoveryProfile"] != "managedInitialOutreachN1"
        or type(candidate["source"]) is not str
        or candidate["source"] != "managed_initial_outreach_n1"
        or type(candidate["actionType"]) is not str
        or candidate["actionType"] != "campaign_launch"
    ):
        raise TypeError("canonical recovery payload constants are invalid")

    canonical = build_canonical_recovery_payload(
        {
            "uid": candidate["uid"],
            "clientId": candidate["clientId"],
            "outboxId": candidate["outboxId"],
            "recoveryRunId": candidate["recoveryRunId"],
            "outbox": {
                "assignedEmails": candidate["assignedEmails"],
                "script": candidate["script"],
                "subject": candidate["subject"],
                "askFields": candidate["askFields"],
                "rowNumber": candidate["rowNumber"],
            },
        }
    )
    if any(candidate[key] != canonical[key] for key in _PAYLOAD_KEYS):
        raise TypeError("recovery payload is not canonical")
    return canonical


def serialize_canonical_recovery_payload(payload: Any) -> bytes:
    """Serialize one already-canonical payload as compact UTF-8 JSON bytes."""

    canonical = _canonical_payload_from_output(payload)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def hash_recovery_payload(payload: Any) -> str:
    """Return the lowercase SHA-256 hex digest of canonical payload bytes."""

    return hashlib.sha256(serialize_canonical_recovery_payload(payload)).hexdigest()


def hash_recovery_script(script: Any) -> str:
    """Return the lowercase SHA-256 hex digest of normalized script UTF-8."""

    return hashlib.sha256(_normalize_script(script).encode("utf-8")).hexdigest()
