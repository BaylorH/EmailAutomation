"""Validation primitives for the production-readiness evidence registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


TOP_LEVEL_KEYS = {
    "schemaVersion",
    "updatedAt",
    "releaseIdentity",
    "rolloutGates",
    "evidence",
    "qualityItems",
}
GATE_DECISIONS = {"go", "ready_for_canary", "hold"}
PROOF_LEVELS = {
    "live_production",
    "production_readback",
    "deterministic_test",
    "source_review",
    "historical",
}
EVIDENCE_RESULTS = {"pass", "partial", "fail"}
QUALITY_STATES = {"proven_live", "source_only", "partial", "open", "ready_for_live"}

_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_RAW_MESSAGE_FIELDS = {
    "bcc",
    "body",
    "cc",
    "emailbody",
    "from",
    "headers",
    "htmlbody",
    "message",
    "messagebody",
    "rawmessage",
    "rawmessagebody",
    "recipient",
    "recipients",
    "sender",
    "subject",
    "textbody",
    "to",
}
_RAW_MESSAGE_MARKERS = ("message", "rawbody", "subject", "sender", "recipient")
_SECRET_MARKERS = ("apikey", "authorization", "credential", "password", "privatekey", "secret", "token")


class RegistryError(ValueError):
    """Raised when authored readiness data violates the registry contract."""


@dataclass(frozen=True)
class ValidatedRegistry:
    registry: Mapping[str, Any]
    feature_by_id: Mapping[str, Mapping[str, Any]]
    fixture_matrix: Mapping[str, Any]
    gate_ids: frozenset[str]
    evidence_by_id: Mapping[str, Mapping[str, Any]]
    quality_by_id: Mapping[str, Mapping[str, Any]]


def parse_utc(text: str) -> datetime:
    """Parse a strict ISO-8601 UTC timestamp ending in a literal ``Z``."""

    if not isinstance(text, str) or _UTC_Z.fullmatch(text) is None:
        raise RegistryError("timestamp: expected strict UTC-Z date-time")
    try:
        return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError as exc:
        raise RegistryError("timestamp: invalid UTC-Z date-time") from exc


def _mapping(value: Any, owner: str, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryError(f"{owner}: {field} must be an object")
    return value


def _list(value: Any, owner: str, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise RegistryError(f"{owner}: {field} must be a list")
    return value


def _nonempty_string(value: Any, owner: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{owner}: {field} must be a nonempty string")
    return value


def _string_list(value: Any, owner: str, field: str) -> list[str]:
    values = _list(value, owner, field)
    if any(not isinstance(item, str) or not item for item in values):
        raise RegistryError(f"{owner}: {field} must contain stable string IDs")
    if len(values) != len(set(values)):
        raise RegistryError(f"{owner}: {field} contains duplicate IDs")
    return values


def _timestamp(value: Any, owner: str, field: str) -> datetime:
    try:
        return parse_utc(value)
    except RegistryError as exc:
        raise RegistryError(f"{owner}: {field} must be a strict UTC-Z date-time") from exc


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _scan_safe(value: Any, owner: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in _RAW_MESSAGE_FIELDS or any(
                marker in normalized for marker in _RAW_MESSAGE_MARKERS
            ):
                raise RegistryError(f"{owner}: raw message fields are forbidden")
            if any(marker in normalized for marker in _SECRET_MARKERS):
                raise RegistryError(f"{owner}: secret-like fields are forbidden")
            _scan_safe(child, owner)
    elif isinstance(value, list):
        for child in value:
            _scan_safe(child, owner)
    elif isinstance(value, str):
        if _EMAIL.search(value):
            raise RegistryError(f"{owner}: email addresses are forbidden")
        if "/Users/" in value:
            raise RegistryError(f"{owner}: local user paths are forbidden")
        if "file://" in value.lower():
            raise RegistryError(f"{owner}: file URIs are forbidden")


def _index(items: Any, collection: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_item in enumerate(_list(items, "registry", collection)):
        owner = f"{collection}[{index}]"
        item = _mapping(raw_item, owner, "item")
        stable_id = _nonempty_string(item.get("id"), owner, "id")
        _scan_safe(stable_id, owner)
        if stable_id in result:
            raise RegistryError(f"{stable_id}: duplicate stable ID")
        result[stable_id] = item
    return result


def _feature_index(feature_registry: Any) -> dict[str, Mapping[str, Any]]:
    source = _mapping(feature_registry, "feature-registry", "document")
    return _index(source.get("features"), "features")


def _validate_refs(
    item: Mapping[str, Any],
    owner: str,
    field: str,
    known: set[str],
    *,
    required: bool = False,
) -> list[str]:
    if field not in item and not required:
        return []
    refs = _string_list(item.get(field), owner, field)
    if any(ref not in known for ref in refs):
        raise RegistryError(f"{owner}: {field} contains an unknown stable ID")
    return refs


def _validate_artifact(artifact: Any, evidence_id: str, repo_root: Path) -> None:
    relative = _nonempty_string(artifact, evidence_id, "artifact")
    path = Path(relative)
    if path.is_absolute():
        raise RegistryError(f"{evidence_id}: artifact must be repo-relative")
    resolved = (repo_root / path).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise RegistryError(f"{evidence_id}: artifact escapes repo root") from exc
    if not resolved.is_file():
        raise RegistryError(f"{evidence_id}: artifact does not resolve to an existing file")


def validate_registry(
    registry: Any,
    feature_registry: Any,
    gradebook: Any,
    fixture_map: Any,
    *,
    repo_root: Path | str,
) -> ValidatedRegistry:
    """Validate authored readiness data and return stable lookup indexes."""

    document = _mapping(registry, "registry", "document")
    if set(document) != TOP_LEVEL_KEYS:
        raise RegistryError("registry: top-level keys must exactly match schema version 1")
    version = document.get("schemaVersion")
    if type(version) is not int or version != 1:
        raise RegistryError("registry: schemaVersion must be integer 1")

    updated_at = _timestamp(document.get("updatedAt"), "registry", "updatedAt")
    release_identity = _mapping(document.get("releaseIdentity"), "releaseIdentity", "value")
    _scan_safe(release_identity, "releaseIdentity")

    feature_by_id = _feature_index(feature_registry)
    known_features = set(feature_by_id)
    gradebook_doc = _mapping(gradebook, "gradebook", "document")
    event_taxonomy = _mapping(gradebook_doc.get("eventTaxonomy"), "gradebook", "eventTaxonomy")
    feature_scenarios = _mapping(
        gradebook_doc.get("featureScenarios"), "gradebook", "featureScenarios"
    )
    known_scenarios = set(event_taxonomy) | set(feature_scenarios)

    fixture_doc = _mapping(fixture_map, "fixture-map", "document")
    fixture_matrix = _mapping(
        fixture_doc.get("featureFixtureMatrix"), "fixture-map", "featureFixtureMatrix"
    )
    unknown_fixture_features = set(fixture_matrix) - known_features
    if unknown_fixture_features:
        unknown = sorted(unknown_fixture_features)[0]
        raise RegistryError(f"{unknown}: fixture map references an unknown feature")

    gate_by_id = _index(document.get("rolloutGates"), "rolloutGates")
    evidence_by_id = _index(document.get("evidence"), "evidence")
    quality_by_id = _index(document.get("qualityItems"), "qualityItems")
    all_ids = [*gate_by_id, *evidence_by_id, *quality_by_id]
    if len(all_ids) != len(set(all_ids)):
        for stable_id in all_ids:
            if all_ids.count(stable_id) > 1:
                raise RegistryError(f"{stable_id}: stable ID is reused across namespaces")

    evidence_times: dict[str, tuple[datetime, datetime | None]] = {}
    for evidence_id, evidence in evidence_by_id.items():
        _scan_safe(evidence, evidence_id)
        if evidence.get("proofLevel") not in PROOF_LEVELS:
            raise RegistryError(f"{evidence_id}: invalid proofLevel")
        if evidence.get("result") not in EVIDENCE_RESULTS:
            raise RegistryError(f"{evidence_id}: invalid result")
        _validate_refs(evidence, evidence_id, "featureIds", known_features)
        _validate_refs(evidence, evidence_id, "scenarioIds", known_scenarios)
        observed_at = _timestamp(evidence.get("observedAt"), evidence_id, "observedAt")
        expires_raw = evidence.get("expiresAt")
        expires_at = None if expires_raw is None else _timestamp(expires_raw, evidence_id, "expiresAt")
        if expires_at is not None and expires_at <= observed_at:
            raise RegistryError(f"{evidence_id}: expiresAt must be after observedAt")
        _validate_artifact(evidence.get("artifact"), evidence_id, Path(repo_root).resolve())
        evidence_times[evidence_id] = (observed_at, expires_at)

    gate_ids = set(gate_by_id)
    evidence_ids = set(evidence_by_id)
    quality_ids = set(quality_by_id)
    for quality_id, quality in quality_by_id.items():
        _scan_safe(quality, quality_id)
        if quality.get("state") not in QUALITY_STATES:
            raise RegistryError(f"{quality_id}: invalid state")
        _validate_refs(quality, quality_id, "featureIds", known_features)
        _validate_refs(quality, quality_id, "scenarioIds", known_scenarios)
        _validate_refs(quality, quality_id, "evidenceIds", evidence_ids)
        _validate_refs(quality, quality_id, "blocksGates", gate_ids, required=True)

    gate_evidence: dict[str, list[str]] = {}
    gate_blockers: dict[str, list[str]] = {}
    for gate_id, gate in gate_by_id.items():
        _scan_safe(gate, gate_id)
        decision = gate.get("decision")
        if decision not in GATE_DECISIONS:
            raise RegistryError(f"{gate_id}: invalid authored decision")
        _validate_refs(gate, gate_id, "featureIds", known_features)
        _validate_refs(gate, gate_id, "scenarioIds", known_scenarios)
        gate_evidence[gate_id] = _validate_refs(
            gate, gate_id, "evidenceIds", evidence_ids, required=True
        )
        gate_blockers[gate_id] = _validate_refs(
            gate, gate_id, "blockerIds", quality_ids, required=True
        )
        if decision == "ready_for_canary":
            _nonempty_string(gate.get("scope"), gate_id, "scope")
            if not _string_list(gate.get("forbids"), gate_id, "forbids"):
                raise RegistryError(f"{gate_id}: forbids must be nonempty")
            _nonempty_string(gate.get("nextAction"), gate_id, "nextAction")
            if not gate_blockers[gate_id]:
                raise RegistryError(f"{gate_id}: blockerIds must be nonempty")
            _nonempty_string(gate.get("rollback"), gate_id, "rollback")

    for quality_id, quality in quality_by_id.items():
        for gate_id in quality["blocksGates"]:
            if quality_id not in gate_blockers[gate_id]:
                raise RegistryError(f"{quality_id}: blocksGates is not reflected by the gate")
    for gate_id, blocker_ids in gate_blockers.items():
        for quality_id in blocker_ids:
            if gate_id not in quality_by_id[quality_id]["blocksGates"]:
                raise RegistryError(f"{gate_id}: blocker is not explicit in blocksGates")

    for gate_id, gate in gate_by_id.items():
        if gate["decision"] != "go":
            continue
        if gate_blockers[gate_id]:
            raise RegistryError(f"{gate_id}: go requires zero blockers")
        if not gate_evidence[gate_id]:
            raise RegistryError(f"{gate_id}: go requires passing evidence")
        for evidence_id in gate_evidence[gate_id]:
            evidence = evidence_by_id[evidence_id]
            expires_at = evidence_times[evidence_id][1]
            if evidence["result"] != "pass":
                raise RegistryError(f"{gate_id}: go requires passing evidence")
            if expires_at is not None and updated_at >= expires_at:
                raise RegistryError(f"{gate_id}: go requires unexpired evidence")

    return ValidatedRegistry(
        registry=document,
        feature_by_id=feature_by_id,
        fixture_matrix=fixture_matrix,
        gate_ids=frozenset(gate_ids),
        evidence_by_id=evidence_by_id,
        quality_by_id=quality_by_id,
    )


def effective_gate_decisions(
    validated: ValidatedRegistry, *, at: datetime | str
) -> dict[str, str]:
    """Return generated gate decisions without altering the authored registry."""

    if isinstance(at, str):
        at_time = parse_utc(at)
    elif isinstance(at, datetime) and at.tzinfo is not None and at.utcoffset() is not None:
        at_time = at.astimezone(timezone.utc)
    else:
        raise RegistryError("at: expected an aware UTC date-time")

    decisions: dict[str, str] = {}
    for gate in validated.registry["rolloutGates"]:
        gate_id = gate["id"]
        decision = gate["decision"]
        if decision == "go":
            for evidence_id in gate["evidenceIds"]:
                expires_raw = validated.evidence_by_id[evidence_id].get("expiresAt")
                if expires_raw is not None and at_time >= parse_utc(expires_raw):
                    decision = "stale"
                    break
        decisions[gate_id] = decision
    return decisions
