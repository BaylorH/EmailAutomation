"""Fail-closed resolution of per-user v2 campaign capabilities."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional
from weakref import WeakSet


_BASE_DATETIME_TYPE = datetime
_DATETIME_TZINFO_DESCRIPTOR = datetime.tzinfo
_DATETIME_UTCOFFSET = datetime.utcoffset
_BASE_TIMEDELTA_TYPE = timedelta
_TIMEDELTA_TOTAL_SECONDS = timedelta.total_seconds
_ISINSTANCE = isinstance
_CAPABILITY_NAMES = ("start", "initialDispatch", "inboundAutomation")
_SUPPORTED_SCHEMA_VERSION = 2
_MAX_SAFE_INTEGER = (2**53) - 1
_MAX_UTC_OFFSET_SECONDS = 24 * 60 * 60
_BOUNDARY_WHITESPACE = frozenset(
    (
        "\u0009",
        "\u000a",
        "\u000b",
        "\u000c",
        "\u000d",
        "\u0020",
        "\u0085",
        "\u00a0",
        "\u1680",
        "\u2000",
        "\u2001",
        "\u2002",
        "\u2003",
        "\u2004",
        "\u2005",
        "\u2006",
        "\u2007",
        "\u2008",
        "\u2009",
        "\u200a",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    )
)


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    reason_code: str


@dataclass(frozen=True, eq=False)
class CampaignCapabilitiesResolution:
    source_path: str
    schema_version: Optional[int]
    revision: Optional[int]
    decisions: Mapping[str, CapabilityDecision]


_MODULE_RESOLUTIONS: WeakSet[CampaignCapabilitiesResolution] = WeakSet()


def _is_exact_uid(uid: Any) -> bool:
    return (
        type(uid) is str
        and bool(uid)
        and uid[0] not in _BOUNDARY_WHITESPACE
        and uid[-1] not in _BOUNDARY_WHITESPACE
        and "/" not in uid
    )


def _source_path(uid: Any) -> str:
    return f"campaignCapabilities/{uid}" if _is_exact_uid(uid) else "campaignCapabilities/"


def _is_nonblank_audit_actor(value: Any) -> bool:
    return type(value) is str and any(
        character not in _BOUNDARY_WHITESPACE for character in value
    )


def _normalized_revision(value: Any) -> Optional[int]:
    if type(value) is int:
        return value if 0 < value <= _MAX_SAFE_INTEGER else None
    if type(value) is float:
        if isfinite(value) and value.is_integer() and 0 < value <= _MAX_SAFE_INTEGER:
            return int(value)
    return None


def _schema_version(value: Any) -> Optional[int]:
    if type(value) not in (int, float):
        return None
    return _SUPPORTED_SCHEMA_VERSION if value == _SUPPORTED_SCHEMA_VERSION else None


def _is_firestore_timestamp(value: Any) -> bool:
    try:
        base_datetime_type = _BASE_DATETIME_TYPE
        datetime_tzinfo_descriptor = _DATETIME_TZINFO_DESCRIPTOR
        datetime_utcoffset = _DATETIME_UTCOFFSET
        base_timedelta_type = _BASE_TIMEDELTA_TYPE
        timedelta_total_seconds = _TIMEDELTA_TOTAL_SECONDS
        is_instance = _ISINSTANCE
        max_offset_seconds = _MAX_UTC_OFFSET_SECONDS

        if not is_instance(value, base_datetime_type):
            return False
        intrinsic_tzinfo = datetime_tzinfo_descriptor.__get__(
            value, base_datetime_type
        )
        if intrinsic_tzinfo is None:
            return False
        intrinsic_offset = datetime_utcoffset(value)
        if not is_instance(intrinsic_offset, base_timedelta_type):
            return False
        offset_seconds = timedelta_total_seconds(intrinsic_offset)
        if not -max_offset_seconds < offset_seconds < max_offset_seconds:
            return False

        public_tzinfo = value.tzinfo
        public_offset = value.utcoffset()
        return (
            public_tzinfo is intrinsic_tzinfo
            and is_instance(public_offset, base_timedelta_type)
            and timedelta_total_seconds(public_offset) == offset_seconds
        )
    except Exception:
        return False


def _decision(allowed: bool, reason_code: str) -> CapabilityDecision:
    return CapabilityDecision(allowed=allowed, reason_code=reason_code)


def _resolution(
    *,
    uid: Any,
    schema_version: Optional[int],
    revision: Optional[int],
    decisions: Mapping[str, CapabilityDecision],
) -> CampaignCapabilitiesResolution:
    immutable_decisions = MappingProxyType(
        {name: decisions[name] for name in _CAPABILITY_NAMES}
    )
    resolved = CampaignCapabilitiesResolution(
        source_path=_source_path(uid),
        schema_version=schema_version,
        revision=revision,
        decisions=immutable_decisions,
    )
    _MODULE_RESOLUTIONS.add(resolved)
    return resolved


def _denied_resolution(
    *,
    uid: Any,
    reason_code: str,
    schema_version: Optional[int] = None,
    revision: Optional[int] = None,
) -> CampaignCapabilitiesResolution:
    return _resolution(
        uid=uid,
        schema_version=schema_version,
        revision=revision,
        decisions={
            name: _decision(False, reason_code) for name in _CAPABILITY_NAMES
        },
    )


def resolve_campaign_capabilities(
    *,
    uid: Any,
    document_id: Any,
    data: Any,
) -> CampaignCapabilitiesResolution:
    """Resolve one authoritative capability document without implication edges."""

    if not _is_exact_uid(uid):
        return _denied_resolution(uid=uid, reason_code="capability_uid_invalid")
    if data is None:
        return _denied_resolution(
            uid=uid, reason_code="capability_document_missing"
        )
    if type(data) is not dict:
        return _denied_resolution(
            uid=uid, reason_code="capability_document_malformed"
        )

    schema_version = None
    revision = None
    try:
        schema_version = _schema_version(data.get("schemaVersion"))
        revision = _normalized_revision(data.get("revision"))

        if type(document_id) is not str or document_id != uid:
            return _denied_resolution(
                uid=uid,
                schema_version=schema_version,
                revision=revision,
                reason_code="capability_document_id_mismatch",
            )
        if schema_version is None:
            return _denied_resolution(
                uid=uid,
                schema_version=schema_version,
                revision=revision,
                reason_code="capability_schema_unsupported",
            )
        if revision is None:
            return _denied_resolution(
                uid=uid,
                schema_version=schema_version,
                revision=revision,
                reason_code="capability_revision_invalid",
            )
        updated_by = data.get("updatedBy")
        if (
            not _is_firestore_timestamp(data.get("updatedAt"))
            or not _is_nonblank_audit_actor(updated_by)
        ):
            return _denied_resolution(
                uid=uid,
                schema_version=schema_version,
                revision=revision,
                reason_code="capability_audit_invalid",
            )

        decisions: Dict[str, CapabilityDecision] = {}
        for name in _CAPABILITY_NAMES:
            value = data.get(name)
            if type(value) is not bool:
                decisions[name] = _decision(False, "capability_value_invalid")
            elif value:
                decisions[name] = _decision(True, "allowed")
            else:
                decisions[name] = _decision(False, "capability_disabled")

        return _resolution(
            uid=uid,
            schema_version=schema_version,
            revision=revision,
            decisions=decisions,
        )
    except Exception:
        return _denied_resolution(
            uid=uid,
            schema_version=schema_version,
            revision=revision,
            reason_code="capability_document_malformed",
        )


def read_campaign_capabilities(
    *,
    firestore_client: Any,
    uid: Any,
) -> CampaignCapabilitiesResolution:
    """Read only ``campaignCapabilities/{uid}`` and convert errors to denial."""

    if not _is_exact_uid(uid):
        return _denied_resolution(uid=uid, reason_code="capability_uid_invalid")

    try:
        snapshot = (
            firestore_client.collection("campaignCapabilities")
            .document(uid)
            .get()
        )
        return resolve_campaign_capabilities(
            uid=uid,
            document_id=getattr(snapshot, "id", None),
            data=snapshot.to_dict() if getattr(snapshot, "exists", False) else None,
        )
    except Exception:
        return _denied_resolution(uid=uid, reason_code="capability_read_error")


def capability_allowed(
    resolution: Optional[CampaignCapabilitiesResolution],
    capability_name: str,
) -> bool:
    """Return true only for an exact known capability with an allowed decision."""

    if (
        type(capability_name) is not str
        or capability_name not in _CAPABILITY_NAMES
        or type(resolution) is not CampaignCapabilitiesResolution
    ):
        return False
    try:
        if resolution not in _MODULE_RESOLUTIONS:
            return False
        decision_value = resolution.decisions.get(capability_name)
    except Exception:
        return False
    return (
        isinstance(decision_value, CapabilityDecision)
        and decision_value.allowed is True
    )
