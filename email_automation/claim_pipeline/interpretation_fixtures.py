"""Strict fixture catalog for evidence normalization and entity resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .contracts import (
    Actor,
    ActorRole,
    Direction,
    EntityType,
    EvidenceFreshness,
    EvidenceSource,
)
from .entities import EntitySeed
from .evidence import ExternalEvidenceInput, RawMessageEvidence


INTERPRETATION_FIXTURE_SCHEMA_VERSION = 3
_ROOT_KEYS = frozenset({"schemaVersion", "catalogId", "cases"})
_CASE_KEYS = frozenset({"caseId", "campaignId", "message", "seeds", "expected"})
_MESSAGE_KEYS = frozenset(
    {
        "tenantId",
        "messageId",
        "direction",
        "actor",
        "observedAt",
        "subject",
        "body",
        "signature",
        "external",
    }
)
_ACTOR_KEYS = frozenset({"name", "email", "role"})
_EXTERNAL_KEYS = frozenset({"sourceKind", "location", "content", "error"})
_SEED_KEYS = frozenset(
    {"entityType", "label", "canonicalAddress", "suite", "relationship", "aliases"}
)
_EXPECTED_KEYS = frozenset(
    {"sourceCounts", "evidenceSequence", "failures", "entities", "issues"}
)
_EXPECTED_ENTITY_KEYS = frozenset(
    {
        "entityType",
        "label",
        "canonicalAddress",
        "suite",
        "relationship",
        "evidenceIndexes",
    }
)
_EXPECTED_EVIDENCE_KEYS = frozenset(
    {
        "sourceKind",
        "freshness",
        "location",
        "content",
        "parentIndex",
        "actorEmail",
        "actorRole",
    }
)
_EXPECTED_FAILURE_KEYS = frozenset(
    {"sourceKind", "location", "reason", "parentIndex"}
)
_EXPECTED_ISSUE_KEYS = frozenset({"code", "evidenceIndexes"})


class InterpretationFixtureValidationError(ValueError):
    """Raised when an interpretation fixture cannot be trusted."""


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise InterpretationFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise InterpretationFixtureValidationError(
            f"{label} is missing keys: {sorted(missing)}"
        )
    if unknown:
        raise InterpretationFixtureValidationError(
            f"{label} has unknown keys: {sorted(unknown)}"
        )


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InterpretationFixtureValidationError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise InterpretationFixtureValidationError(f"{label} must be non-empty")
    return value


def _enum(value: Any, enum_type: type, label: str):
    cleaned = _text(value, label)
    try:
        return enum_type(cleaned)
    except ValueError as exc:
        raise InterpretationFixtureValidationError(
            f"{label} has invalid value {cleaned!r}"
        ) from exc


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InterpretationFixtureValidationError(f"{label} must be a list")
    return tuple(_text(item, f"{label} item") for item in value)


def _index_list(value: Any, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in value
    ):
        raise InterpretationFixtureValidationError(
            f"{label} must be a list of non-negative integers"
        )
    if value != sorted(set(value)):
        raise InterpretationFixtureValidationError(
            f"{label} must be sorted and contain no duplicates"
        )
    return tuple(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _parse_external(raw: Any, label: str) -> ExternalEvidenceInput:
    _exact_keys(raw, _EXTERNAL_KEYS, label)
    try:
        return ExternalEvidenceInput(
            source_kind=_enum(
                raw["sourceKind"], EvidenceSource, f"{label} sourceKind"
            ),
            location=_text(raw["location"], f"{label} location"),
            content=_text(raw["content"], f"{label} content", allow_empty=True),
            error=_text(raw["error"], f"{label} error", allow_empty=True),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InterpretationFixtureValidationError):
            raise
        raise InterpretationFixtureValidationError(f"{label}: {exc}") from exc


def _parse_message(raw: Any, label: str, campaign_id: str) -> RawMessageEvidence:
    _exact_keys(raw, _MESSAGE_KEYS, label)
    actor_raw = raw["actor"]
    _exact_keys(actor_raw, _ACTOR_KEYS, f"{label} actor")
    external_raw = raw["external"]
    if not isinstance(external_raw, list):
        raise InterpretationFixtureValidationError(f"{label} external must be a list")
    try:
        actor = Actor(
            name=_text(actor_raw["name"], f"{label} actor name", allow_empty=True),
            email=_text(actor_raw["email"], f"{label} actor email", allow_empty=True),
            role=_enum(actor_raw["role"], ActorRole, f"{label} actor role"),
        )
        return RawMessageEvidence(
            tenant_id=_text(raw["tenantId"], f"{label} tenantId"),
            campaign_id=campaign_id,
            message_id=_text(raw["messageId"], f"{label} messageId"),
            direction=_enum(raw["direction"], Direction, f"{label} direction"),
            actor=actor,
            observed_at=_text(raw["observedAt"], f"{label} observedAt"),
            subject=_text(raw["subject"], f"{label} subject", allow_empty=True),
            body=_text(raw["body"], f"{label} body", allow_empty=True),
            signature=_text(
                raw["signature"], f"{label} signature", allow_empty=True
            ),
            external=tuple(
                _parse_external(item, f"{label} external {index}")
                for index, item in enumerate(external_raw)
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InterpretationFixtureValidationError):
            raise
        raise InterpretationFixtureValidationError(f"{label}: {exc}") from exc


def _parse_seeds(raw: Any, label: str) -> tuple[EntitySeed, ...]:
    if not isinstance(raw, list) or not raw:
        raise InterpretationFixtureValidationError(
            f"{label} must be a non-empty list"
        )
    seeds = []
    for index, item in enumerate(raw):
        item_label = f"{label} {index}"
        _exact_keys(item, _SEED_KEYS, item_label)
        try:
            seeds.append(
                EntitySeed(
                    entity_type=_enum(
                        item["entityType"], EntityType, f"{item_label} entityType"
                    ),
                    label=_text(item["label"], f"{item_label} label"),
                    canonical_address=_text(
                        item["canonicalAddress"],
                        f"{item_label} canonicalAddress",
                        allow_empty=True,
                    ),
                    suite=_text(
                        item["suite"], f"{item_label} suite", allow_empty=True
                    ),
                    relationship=_text(
                        item["relationship"], f"{item_label} relationship"
                    ),
                    aliases=_string_list(item["aliases"], f"{item_label} aliases"),
                )
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InterpretationFixtureValidationError):
                raise
            raise InterpretationFixtureValidationError(f"{item_label}: {exc}") from exc
    return tuple(seeds)


def _parse_expected(raw: Any, label: str) -> Mapping[str, Any]:
    _exact_keys(raw, _EXPECTED_KEYS, label)
    source_counts = raw["sourceCounts"]
    if not isinstance(source_counts, dict):
        raise InterpretationFixtureValidationError(
            f"{label} sourceCounts must be an object"
        )
    for source, count in source_counts.items():
        _enum(source, EvidenceSource, f"{label} sourceCounts key")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise InterpretationFixtureValidationError(
                f"{label} sourceCounts values must be non-negative integers"
            )
    evidence_sequence = raw["evidenceSequence"]
    if not isinstance(evidence_sequence, list):
        raise InterpretationFixtureValidationError(
            f"{label} evidenceSequence must be a list"
        )
    for index, item in enumerate(evidence_sequence):
        item_label = f"{label} evidenceSequence {index}"
        _exact_keys(item, _EXPECTED_EVIDENCE_KEYS, item_label)
        _enum(item["sourceKind"], EvidenceSource, f"{item_label} sourceKind")
        _enum(item["freshness"], EvidenceFreshness, f"{item_label} freshness")
        _text(item["location"], f"{item_label} location")
        _text(item["content"], f"{item_label} content")
        _text(item["actorEmail"], f"{item_label} actorEmail", allow_empty=True)
        _enum(item["actorRole"], ActorRole, f"{item_label} actorRole")
        parent = item["parentIndex"]
        if parent is not None and (
            not isinstance(parent, int) or isinstance(parent, bool) or parent < 0
        ):
            raise InterpretationFixtureValidationError(
                f"{item_label} parentIndex must be null or a non-negative integer"
            )

    failures = raw["failures"]
    if not isinstance(failures, list):
        raise InterpretationFixtureValidationError(f"{label} failures must be a list")
    for index, item in enumerate(failures):
        item_label = f"{label} failure {index}"
        _exact_keys(item, _EXPECTED_FAILURE_KEYS, item_label)
        source = _enum(item["sourceKind"], EvidenceSource, f"{item_label} sourceKind")
        if source not in (EvidenceSource.ATTACHMENT, EvidenceSource.LINK):
            raise InterpretationFixtureValidationError(
                f"{item_label} sourceKind must be attachment or link"
            )
        _text(item["location"], f"{item_label} location")
        _text(item["reason"], f"{item_label} reason")
        parent = item["parentIndex"]
        if parent is not None and (
            not isinstance(parent, int) or isinstance(parent, bool) or parent < 0
        ):
            raise InterpretationFixtureValidationError(
                f"{item_label} parentIndex must be null or a non-negative integer"
            )
    entities = raw["entities"]
    if not isinstance(entities, list):
        raise InterpretationFixtureValidationError(f"{label} entities must be a list")
    descriptors: set[tuple[str, str, str, str]] = set()
    for index, item in enumerate(entities):
        item_label = f"{label} entity {index}"
        _exact_keys(item, _EXPECTED_ENTITY_KEYS, item_label)
        entity_type = _enum(
            item["entityType"], EntityType, f"{item_label} entityType"
        ).value
        descriptor = (
            entity_type,
            _text(item["label"], f"{item_label} label"),
            _text(
                item["canonicalAddress"],
                f"{item_label} canonicalAddress",
                allow_empty=True,
            ),
            _text(item["suite"], f"{item_label} suite", allow_empty=True),
            _text(item["relationship"], f"{item_label} relationship"),
        )
        if descriptor in descriptors:
            raise InterpretationFixtureValidationError(
                f"{label} contains a duplicate entity descriptor"
            )
        descriptors.add(descriptor)
        _index_list(item["evidenceIndexes"], f"{item_label} evidenceIndexes")

    issues = raw["issues"]
    if not isinstance(issues, list):
        raise InterpretationFixtureValidationError(f"{label} issues must be a list")
    issue_codes = set()
    for index, item in enumerate(issues):
        item_label = f"{label} issue {index}"
        _exact_keys(item, _EXPECTED_ISSUE_KEYS, item_label)
        code = _text(item["code"], f"{item_label} code")
        if code in issue_codes:
            raise InterpretationFixtureValidationError(
                f"{label} contains a duplicate issue code"
            )
        issue_codes.add(code)
        _index_list(item["evidenceIndexes"], f"{item_label} evidenceIndexes")
    return _freeze(raw)


@dataclass(frozen=True)
class InterpretationFixtureCase:
    case_id: str
    campaign_id: str
    message: RawMessageEvidence
    seeds: Tuple[EntitySeed, ...]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class InterpretationFixtureCatalog:
    schema_version: int
    catalog_id: str
    cases: Tuple[InterpretationFixtureCase, ...]
    manifest_hash: str


def load_interpretation_fixture_catalog(
    path: str | Path,
) -> InterpretationFixtureCatalog:
    fixture_path = Path(path)
    try:
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InterpretationFixtureValidationError(
            f"could not read interpretation fixtures: {exc}"
        ) from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["schemaVersion"] != INTERPRETATION_FIXTURE_SCHEMA_VERSION:
        raise InterpretationFixtureValidationError(
            "unsupported interpretation fixture schemaVersion"
        )
    catalog_id = _text(raw["catalogId"], "catalogId")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise InterpretationFixtureValidationError("cases must be a non-empty list")

    cases: list[InterpretationFixtureCase] = []
    case_ids: set[str] = set()
    for index, item in enumerate(raw["cases"]):
        label = f"case {index}"
        _exact_keys(item, _CASE_KEYS, label)
        case_id = _text(item["caseId"], f"{label} caseId")
        if case_id in case_ids:
            raise InterpretationFixtureValidationError(f"duplicate caseId {case_id!r}")
        case_ids.add(case_id)
        cases.append(
            InterpretationFixtureCase(
                case_id=case_id,
                campaign_id=_text(item["campaignId"], f"{label} campaignId"),
                message=_parse_message(
                    item["message"],
                    f"{label} message",
                    _text(item["campaignId"], f"{label} campaignId"),
                ),
                seeds=_parse_seeds(item["seeds"], f"{label} seeds"),
                expected=_parse_expected(item["expected"], f"{label} expected"),
            )
        )

    encoded = json.dumps(
        raw,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return InterpretationFixtureCatalog(
        schema_version=raw["schemaVersion"],
        catalog_id=catalog_id,
        cases=tuple(cases),
        manifest_hash=hashlib.sha256(encoded).hexdigest(),
    )


__all__ = [
    "INTERPRETATION_FIXTURE_SCHEMA_VERSION",
    "InterpretationFixtureCase",
    "InterpretationFixtureCatalog",
    "InterpretationFixtureValidationError",
    "load_interpretation_fixture_catalog",
]
