"""Validation primitives for the production-readiness evidence registry."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
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

_GATE_LABELS = {
    "login_view": "Login / view",
    "supervised_campaign_use": "Supervised campaign use",
    "autonomous_campaign_use": "Autonomous campaign use",
}
_GATE_ORDER = {
    "login_view": 0,
    "supervised_campaign_use": 1,
    "autonomous_campaign_use": 2,
}
_READINESS_SOURCE_PATH = Path("docs/release-safety/readiness-registry.json")
_FEATURE_SOURCE_PATH = Path("docs/release-safety/feature-registry.json")
_GRADEBOOK_SOURCE_PATH = Path("docs/release-safety/feature-gradebook.json")
_FIXTURE_SOURCE_PATH = Path("docs/release-safety/production-v1-fixture-map.json")
_CURRENT_VIEW_PATH = Path("docs/release-safety/current-user-readiness.md")
_FULL_VIEW_PATH = Path("docs/release-safety/full-quality-coverage.md")

_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
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
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[A-Za-z]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
)


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


def _stable_id(value: Any, owner: str, field: str) -> str:
    stable_id = _nonempty_string(value, owner, field)
    if _STABLE_ID.fullmatch(stable_id) is None:
        raise RegistryError(f"{owner}: {field} violates stable ID syntax")
    return stable_id


def _string_list(value: Any, owner: str, field: str) -> list[str]:
    values = _list(value, owner, field)
    for item in values:
        _stable_id(item, owner, field)
    if len(values) != len(set(values)):
        raise RegistryError(f"{owner}: {field} contains duplicate IDs")
    return values


def _text_list(value: Any, owner: str, field: str) -> list[str]:
    values = _list(value, owner, field)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise RegistryError(f"{owner}: {field} must contain nonempty text")
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
        if any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS):
            raise RegistryError(f"{owner}: credential-shaped strings are forbidden")
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
        stable_id = _stable_id(item.get("id"), owner, "id")
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
    for ref in refs:
        _scan_safe(ref, owner)
        if ref not in known:
            raise RegistryError(f"{owner}: {field} references unknown stable ID {ref}")
    return refs


def _release_refs(
    evidence: Mapping[str, Any],
    evidence_id: str,
    release_identity: Mapping[str, Any],
    *,
    required: bool,
) -> Mapping[str, str]:
    if "releaseRefs" not in evidence and not required:
        return {}
    refs = _mapping(evidence.get("releaseRefs"), evidence_id, "releaseRefs")
    if required and not refs:
        raise RegistryError(f"{evidence_id}: releaseRefs must be nonempty")
    for key, value in refs.items():
        safe_key = _stable_id(key, evidence_id, "releaseRefs key")
        _scan_safe(safe_key, evidence_id)
        if safe_key not in release_identity:
            raise RegistryError(f"{evidence_id}: releaseRefs references unknown key {safe_key}")
        _nonempty_string(value, evidence_id, "releaseRefs value")
    return refs


def _is_current_release(
    evidence: Mapping[str, Any], release_identity: Mapping[str, Any]
) -> bool:
    refs = evidence.get("releaseRefs")
    return bool(refs) and all(release_identity.get(key) == value for key, value in refs.items())


def _scope_overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_features = set(left["featureIds"])
    right_features = set(right["featureIds"])
    left_scenarios = set(left["scenarioIds"])
    right_scenarios = set(right["scenarioIds"])
    left_control = (
        left["proofLevel"] == "production_readback"
        and not left_features
        and not left_scenarios
    )
    right_control = (
        right["proofLevel"] == "production_readback"
        and not right_features
        and not right_scenarios
    )
    if left_control or right_control:
        return (
            left_control
            and right_control
            and left["id"] in right.get("supersedes", [])
        )
    return bool(left_features & right_features and left_scenarios & right_scenarios)


def _go_evidence_is_current(
    evidence: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    release_identity: Mapping[str, Any],
    *,
    at: datetime,
) -> bool:
    if evidence["proofLevel"] == "historical" or evidence["result"] != "pass":
        return False
    if not _is_current_release(evidence, release_identity):
        return False
    observed_at = parse_utc(evidence["observedAt"])
    if observed_at > at:
        return False
    expires_raw = evidence.get("expiresAt")
    if expires_raw is not None and at >= parse_utc(expires_raw):
        return False
    for candidate in evidence_by_id.values():
        if candidate["proofLevel"] == "historical" or candidate["result"] != "fail":
            continue
        failed_at = parse_utc(candidate["observedAt"])
        if observed_at <= failed_at <= at and _is_current_release(candidate, release_identity):
            if _scope_overlaps(evidence, candidate):
                return False
    return True


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
    for key, value in release_identity.items():
        _stable_id(key, "releaseIdentity", "key")
        _nonempty_string(value, "releaseIdentity", "value")
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
    for feature_id, fixture_row in fixture_matrix.items():
        _stable_id(feature_id, "fixture-map", "featureFixtureMatrix key")
        _scan_safe(feature_id, "fixture-map")
        _mapping(fixture_row, feature_id, "fixture row")
    unknown_fixture_features = set(fixture_matrix) - known_features
    if unknown_fixture_features:
        unknown = sorted(unknown_fixture_features)[0]
        raise RegistryError(f"{unknown}: fixture map references an unknown feature")

    gate_by_id = _index(document.get("rolloutGates"), "rolloutGates")
    evidence_by_id = _index(document.get("evidence"), "evidence")
    quality_by_id = _index(document.get("qualityItems"), "qualityItems")
    evidence_ids = set(evidence_by_id)
    all_ids = [*gate_by_id, *evidence_by_id, *quality_by_id]
    if len(all_ids) != len(set(all_ids)):
        for stable_id in all_ids:
            if all_ids.count(stable_id) > 1:
                raise RegistryError(f"{stable_id}: stable ID is reused across namespaces")

    for evidence_id, evidence in evidence_by_id.items():
        if evidence.get("proofLevel") not in PROOF_LEVELS:
            raise RegistryError(f"{evidence_id}: invalid proofLevel")
        if evidence.get("result") not in EVIDENCE_RESULTS:
            raise RegistryError(f"{evidence_id}: invalid result")
        _nonempty_string(evidence.get("claim"), evidence_id, "claim")
        feature_refs = _validate_refs(
            evidence, evidence_id, "featureIds", known_features, required=True
        )
        scenario_refs = _validate_refs(
            evidence, evidence_id, "scenarioIds", known_scenarios, required=True
        )
        _validate_refs(evidence, evidence_id, "supersedes", evidence_ids)
        if not feature_refs or not scenario_refs:
            is_control_plane = (
                evidence["proofLevel"] == "production_readback"
                and not feature_refs
                and not scenario_refs
            )
            if not is_control_plane:
                raise RegistryError(
                    f"{evidence_id}: behavioral evidence requires featureIds and scenarioIds"
                )
        readbacks = _text_list(evidence.get("readbacks"), evidence_id, "readbacks")
        limitations = _text_list(evidence.get("limitations"), evidence_id, "limitations")
        retest_on = _text_list(evidence.get("retestOn"), evidence_id, "retestOn")
        production_proof = evidence["proofLevel"] in {
            "live_production",
            "production_readback",
        }
        _release_refs(
            evidence,
            evidence_id,
            release_identity,
            required=production_proof,
        )
        _scan_safe(evidence, evidence_id)
        if production_proof and not (readbacks and limitations and retest_on):
            raise RegistryError(
                f"{evidence_id}: production evidence requires nonempty proof details"
            )
        observed_at = _timestamp(evidence.get("observedAt"), evidence_id, "observedAt")
        expires_raw = evidence.get("expiresAt")
        expires_at = None if expires_raw is None else _timestamp(expires_raw, evidence_id, "expiresAt")
        if expires_at is not None and expires_at <= observed_at:
            raise RegistryError(f"{evidence_id}: expiresAt must be after observedAt")
        _validate_artifact(evidence.get("artifact"), evidence_id, Path(repo_root).resolve())

    gate_ids = set(gate_by_id)
    quality_ids = set(quality_by_id)
    for quality_id, quality in quality_by_id.items():
        if quality.get("state") not in QUALITY_STATES:
            raise RegistryError(f"{quality_id}: invalid state")
        _validate_refs(quality, quality_id, "featureIds", known_features)
        _validate_refs(quality, quality_id, "scenarioIds", known_scenarios)
        _validate_refs(quality, quality_id, "evidenceIds", evidence_ids)
        _validate_refs(quality, quality_id, "blocksGates", gate_ids, required=True)
        _scan_safe(quality, quality_id)

    gate_evidence: dict[str, list[str]] = {}
    gate_blockers: dict[str, list[str]] = {}
    for gate_id, gate in gate_by_id.items():
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
        _scan_safe(gate, gate_id)
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
            if not _go_evidence_is_current(
                evidence,
                evidence_by_id,
                release_identity,
                at=updated_at,
            ):
                raise RegistryError(
                    f"{gate_id}: go requires current nonhistorical unregressed evidence"
                )

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
    release_identity = validated.registry["releaseIdentity"]
    for gate in validated.registry["rolloutGates"]:
        gate_id = gate["id"]
        decision = gate["decision"]
        if decision == "go":
            for evidence_id in gate["evidenceIds"]:
                if not _go_evidence_is_current(
                    validated.evidence_by_id[evidence_id],
                    validated.evidence_by_id,
                    release_identity,
                    at=at_time,
                ):
                    decision = "stale"
                    break
        decisions[gate_id] = decision
    return decisions


def _at_time(at: datetime | str) -> datetime:
    if isinstance(at, str):
        return parse_utc(at)
    if isinstance(at, datetime) and at.tzinfo is not None and at.utcoffset() is not None:
        return at.astimezone(timezone.utc)
    raise RegistryError("at: expected an aware UTC date-time")


def _format_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc).isoformat()
    return normalized.replace("+00:00", "Z")


def _markdown_cell(value: Any) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("|", "\\|").replace("\n", "<br>")


def _humanize_id(value: str) -> str:
    return value.replace("_", " ").replace("-", " ")


def _gate_label(gate_id: str) -> str:
    return _GATE_LABELS.get(gate_id, _humanize_id(gate_id).title())


def _display_ids(values: Any) -> str:
    if not values:
        return "None"
    return ", ".join(f"`{_markdown_cell(value)}`" for value in values)


def _display_texts(values: Any) -> str:
    if not values:
        return "None"
    return "; ".join(_markdown_cell(value) for value in values)


def _evidence_is_current(
    evidence: Mapping[str, Any],
    release_identity: Mapping[str, Any],
    *,
    at: datetime,
) -> bool:
    if evidence["proofLevel"] == "historical":
        return False
    if parse_utc(evidence["observedAt"]) > at:
        return False
    expires_at = evidence.get("expiresAt")
    if expires_at is not None and at >= parse_utc(expires_at):
        return False
    refs = evidence.get("releaseRefs", {})
    if refs and not _is_current_release(evidence, release_identity):
        return False
    if evidence["proofLevel"] in {"live_production", "production_readback"}:
        return _is_current_release(evidence, release_identity)
    return True


def _age_text(observed_at: datetime, at: datetime) -> str:
    seconds = int((at - observed_at).total_seconds())
    if seconds < 0:
        return "not yet observed"
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h old"
    if hours:
        return f"{hours}h {minutes}m old"
    return f"{minutes}m old"


def _gate_evidence_line(
    evidence: Mapping[str, Any],
    release_identity: Mapping[str, Any],
    *,
    at: datetime,
) -> str:
    observed_at = parse_utc(evidence["observedAt"])
    currency = "current" if _evidence_is_current(evidence, release_identity, at=at) else "stale"
    expiry = evidence.get("expiresAt")
    expiry_text = "no fixed expiry" if expiry is None else f"expires {expiry}"
    scenarios = _display_ids(evidence.get("scenarioIds", []))
    return (
        f"`{evidence['id']}` — {evidence['proofLevel']} / {evidence['result']} / "
        f"{currency}; scenarios {scenarios}; observed {evidence['observedAt']} "
        f"({_age_text(observed_at, at)}); {expiry_text}."
    )


def render_current_readiness(
    validated: ValidatedRegistry, *, at: datetime | str
) -> str:
    """Render rollout gates and the exact capability boundary at ``at``."""

    at_time = _at_time(at)
    decisions = effective_gate_decisions(validated, at=at_time)
    gates = sorted(
        validated.registry["rolloutGates"],
        key=lambda gate: (_GATE_ORDER.get(gate["id"], 99), gate["id"]),
    )
    lines = [
        "<!-- Generated by scripts/generate_readiness_views.py; do not edit. -->",
        "",
        "# Current user readiness",
        "",
        f"As of `{_format_utc(at_time)}`.",
        "",
        "`READY FOR CANARY` means one monitored canary under the listed guardrails; it is not a broad production pass.",
        "",
        "| Capability | Decision | Scope |",
        "| --- | --- | --- |",
    ]
    for gate in gates:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(_gate_label(gate["id"])),
                    decisions[gate["id"]].replace("_", " ").upper(),
                    _markdown_cell(gate.get("scope", "Not specified")),
                )
            )
            + " |"
        )

    release_identity = validated.registry["releaseIdentity"]
    for gate in gates:
        gate_id = gate["id"]
        lines.extend(
            [
                "",
                f"## {_gate_label(gate_id)} — {decisions[gate_id].replace('_', ' ').upper()}",
                "",
                f"- Scope: {_markdown_cell(gate.get('scope', 'Not specified'))}",
                f"- Allowed: {_display_ids(gate.get('allows', []))}",
                f"- Forbidden: {_display_ids(gate.get('forbids', []))}",
                f"- Guardrails: {_display_texts(gate.get('guardrails', []))}",
                f"- Blocking quality items: {_display_ids(gate.get('blockerIds', []))}",
                "- Evidence:",
            ]
        )
        evidence_ids = gate.get("evidenceIds", [])
        if evidence_ids:
            for evidence_id in evidence_ids:
                lines.append(
                    "  - "
                    + _gate_evidence_line(
                        validated.evidence_by_id[evidence_id],
                        release_identity,
                        at=at_time,
                    )
                )
        else:
            lines.append("  - None")
        next_action = gate.get("nextAction")
        lines.extend(
            [
                f"- Next action: {_markdown_cell(next_action) if next_action else 'None'}",
                f"- Rollback: {_markdown_cell(gate.get('rollback', 'Not specified'))}",
            ]
        )
    return "\n".join(lines) + "\n"


def _feature_evidence_status(
    evidence_items: list[Mapping[str, Any]],
    release_identity: Mapping[str, Any],
    *,
    at: datetime,
) -> str:
    current = [
        evidence
        for evidence in evidence_items
        if _evidence_is_current(evidence, release_identity, at=at)
    ]
    if not current:
        return "UNPROVEN"

    if any(evidence["result"] == "fail" for evidence in current):
        label = "FAIL"
    elif any(
        evidence["result"] == "pass"
        and evidence["proofLevel"] in {"live_production", "production_readback"}
        for evidence in current
    ):
        label = "PASS"
    elif any(evidence["result"] == "partial" for evidence in current):
        label = "PARTIAL"
    elif any(
        evidence["result"] == "pass"
        and evidence["proofLevel"] in {"deterministic_test", "source_review"}
        for evidence in current
    ):
        label = "DETERMINISTIC / SOURCE ONLY"
    else:
        return "UNPROVEN"

    details = []
    for evidence in sorted(current, key=lambda item: item["id"]):
        scenarios = ", ".join(evidence.get("scenarioIds", [])) or "control-plane"
        details.append(
            f"{evidence['id']} ({evidence['proofLevel']}; {evidence['result']}; {scenarios})"
        )
    return f"{label} — " + "; ".join(details)


def render_full_quality_coverage(
    validated: ValidatedRegistry, *, at: datetime | str
) -> str:
    """Render every core feature without treating fixture mapping as live proof."""

    at_time = _at_time(at)
    core_features = sorted(
        (
            feature
            for feature in validated.feature_by_id.values()
            if feature.get("lane") == "production_v1_core"
        ),
        key=lambda feature: feature["id"],
    )
    lines = [
        "<!-- Generated by scripts/generate_readiness_views.py; do not edit. -->",
        "",
        "# Full quality coverage",
        "",
        f"As of `{_format_utc(at_time)}`.",
        "",
        "Mapped fixtures are deterministic coverage, not proof of live production behavior.",
        "Evidence remains limited to the exact feature and scenario IDs shown in each cell.",
        "",
        "| Feature ID | Feature | Mapped fixtures | Live/source evidence | Quality items | Retest triggers |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    release_identity = validated.registry["releaseIdentity"]
    for feature in core_features:
        feature_id = feature["id"]
        fixture_row = validated.fixture_matrix.get(feature_id, {})
        covered = sorted(
            fixture_id
            for fixture_id, fixture in fixture_row.items()
            if isinstance(fixture, Mapping) and fixture.get("status") == "covered"
        )
        mapped_fixtures = "0" if not covered else f"{len(covered)}: " + ", ".join(covered)
        evidence_items = [
            evidence
            for evidence in validated.evidence_by_id.values()
            if feature_id in evidence.get("featureIds", [])
        ]
        evidence_status = _feature_evidence_status(
            evidence_items, release_identity, at=at_time
        )
        quality_items = sorted(
            (
                quality
                for quality in validated.quality_by_id.values()
                if feature_id in quality.get("featureIds", [])
            ),
            key=lambda quality: quality["id"],
        )
        quality_text = (
            "None"
            if not quality_items
            else "; ".join(
                f"{quality['id']} ({quality['state']})" for quality in quality_items
            )
        )
        retest_triggers = sorted(
            {
                trigger
                for evidence in evidence_items
                for trigger in evidence.get("retestOn", [])
            }
        )
        retest_text = "None" if not retest_triggers else ", ".join(retest_triggers)
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    feature_id,
                    feature.get("name", feature_id),
                    mapped_fixtures,
                    evidence_status,
                    quality_text,
                    retest_text,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _load_json(path: Path, stable_name: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"{stable_name}: unable to read JSON source") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{stable_name}: invalid JSON source") from exc


def render_outputs(
    repo_root: Path | str, *, at: datetime | str | None = None
) -> dict[Path, str]:
    """Load validated sources and return both deterministic Markdown payloads."""

    root = Path(repo_root).resolve()
    registry = _load_json(root / _READINESS_SOURCE_PATH, _READINESS_SOURCE_PATH.name)
    feature_registry = _load_json(root / _FEATURE_SOURCE_PATH, _FEATURE_SOURCE_PATH.name)
    gradebook = _load_json(root / _GRADEBOOK_SOURCE_PATH, _GRADEBOOK_SOURCE_PATH.name)
    fixture_map = _load_json(root / _FIXTURE_SOURCE_PATH, _FIXTURE_SOURCE_PATH.name)
    validated = validate_registry(
        registry,
        feature_registry,
        gradebook,
        fixture_map,
        repo_root=root,
    )
    at_time = (
        parse_utc(validated.registry["updatedAt"])
        if at is None
        else _at_time(at)
    )
    return {
        root / _CURRENT_VIEW_PATH: render_current_readiness(validated, at=at_time),
        root / _FULL_VIEW_PATH: render_full_quality_coverage(validated, at=at_time),
    }


def _atomic_write_outputs(outputs: Mapping[Path, str]) -> None:
    staged: list[tuple[Path, Path]] = []
    try:
        for target, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                text=True,
            )
            temp_path = Path(temp_name)
            staged.append((temp_path, target))
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        for temp_path, target in staged:
            os.replace(temp_path, target)
    finally:
        for temp_path, _target in staged:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate production-readiness views.")
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--at", help="strict UTC-Z evaluation time")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        at = parse_utc(args.at) if args.at else None
        outputs = render_outputs(repo_root, at=at)
        if args.check:
            mismatches = []
            for target, payload in sorted(outputs.items(), key=lambda item: str(item[0])):
                try:
                    existing = target.read_text(encoding="utf-8")
                except OSError:
                    existing = None
                if existing != payload:
                    mismatches.append(_relative_path(target, repo_root))
            if mismatches:
                print("readiness view drift:", file=sys.stderr)
                for mismatch in mismatches:
                    print(mismatch, file=sys.stderr)
                return 2
            return 0
        _atomic_write_outputs(outputs)
        for target in sorted(outputs, key=str):
            print(_relative_path(target, repo_root))
        return 0
    except (RegistryError, OSError) as exc:
        print(f"readiness error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
