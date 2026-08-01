"""Fail-closed resolution of per-user v2 campaign capabilities."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional


_CAPABILITY_NAMES = ("start", "initialDispatch", "inboundAutomation")
_SUPPORTED_SCHEMA_VERSION = 2
_MAX_SAFE_INTEGER = (2**53) - 1


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    reason_code: str


@dataclass(frozen=True)
class CampaignCapabilitiesResolution:
    source_path: str
    schema_version: Optional[int]
    revision: Optional[int]
    decisions: Mapping[str, CapabilityDecision]


def _normalize_uid(uid: Any) -> str:
    return uid.strip() if isinstance(uid, str) else ""


def _source_path(uid: Any) -> str:
    return f"campaignCapabilities/{_normalize_uid(uid)}"


def _normalized_revision(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not float(value).is_integer() or value <= 0 or value > _MAX_SAFE_INTEGER:
        return None
    return int(value)


def _schema_version(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _SUPPORTED_SCHEMA_VERSION if value == _SUPPORTED_SCHEMA_VERSION else None


def _is_firestore_timestamp(value: Any) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        return value.tzinfo is not None and value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _decision(allowed: bool, reason_code: str) -> CapabilityDecision:
    return CapabilityDecision(allowed=allowed, reason_code=reason_code)


def _denied_resolution(
    *,
    uid: Any,
    reason_code: str,
    schema_version: Optional[int] = None,
    revision: Optional[int] = None,
) -> CampaignCapabilitiesResolution:
    return CampaignCapabilitiesResolution(
        source_path=_source_path(uid),
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

    normalized_uid = _normalize_uid(uid)
    if not normalized_uid:
        return _denied_resolution(uid=uid, reason_code="capability_uid_invalid")
    if data is None:
        return _denied_resolution(
            uid=uid, reason_code="capability_document_missing"
        )
    if not isinstance(data, dict):
        return _denied_resolution(
            uid=uid, reason_code="capability_document_malformed"
        )

    schema_version = _schema_version(data.get("schemaVersion"))
    revision = _normalized_revision(data.get("revision"))

    if document_id != normalized_uid:
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
    if (
        not _is_firestore_timestamp(data.get("updatedAt"))
        or not isinstance(data.get("updatedBy"), str)
        or not data["updatedBy"].strip()
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
        if not isinstance(value, bool):
            decisions[name] = _decision(False, "capability_value_invalid")
        elif value:
            decisions[name] = _decision(True, "allowed")
        else:
            decisions[name] = _decision(False, "capability_disabled")

    return CampaignCapabilitiesResolution(
        source_path=_source_path(uid),
        schema_version=schema_version,
        revision=revision,
        decisions=decisions,
    )


def read_campaign_capabilities(
    *,
    firestore_client: Any,
    uid: Any,
) -> CampaignCapabilitiesResolution:
    """Read only ``campaignCapabilities/{uid}`` and convert errors to denial."""

    normalized_uid = _normalize_uid(uid)
    if not normalized_uid:
        return resolve_campaign_capabilities(
            uid=uid,
            document_id="",
            data=None,
        )

    try:
        snapshot = (
            firestore_client.collection("campaignCapabilities")
            .document(normalized_uid)
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
        capability_name not in _CAPABILITY_NAMES
        or not isinstance(resolution, CampaignCapabilitiesResolution)
        or not isinstance(resolution.decisions, Mapping)
    ):
        return False
    decision_value = resolution.decisions.get(capability_name)
    return (
        isinstance(decision_value, CapabilityDecision)
        and decision_value.allowed is True
    )
