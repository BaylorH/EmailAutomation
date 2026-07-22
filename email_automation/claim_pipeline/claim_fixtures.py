"""Strict fixture catalog for broad claim-extraction behavior."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .contracts import ActorRole, ClaimModality, ClaimPolarity, ClaimPredicate


CLAIM_FIXTURE_SCHEMA_VERSION = 1
_ROOT_KEYS = frozenset({"schemaVersion", "catalogId", "cases"})
_CASE_KEYS = frozenset(
    {"caseId", "interpretationCaseId", "claims", "review", "expected"}
)
_CLAIM_KEYS = frozenset(
    {
        "evidenceIndex",
        "subject",
        "predicate",
        "value",
        "evidenceText",
        "actorRole",
        "polarity",
        "modality",
        "confidence",
        "unit",
        "effectiveAt",
        "supersedesClaimId",
    }
)
_SUBJECT_KEYS = frozenset({"relationship", "suite", "canonicalAddress"})
_REVIEW_KEYS = frozenset({"evidenceIndex", "reason"})
_EXPECTED_KEYS = frozenset({"accepted", "issueCodes"})
_ACCEPTED_KEYS = frozenset({"predicate", "value", "relationship", "suite"})


class ClaimFixtureValidationError(ValueError):
    """Raised when a claim fixture cannot be trusted."""


def _exact_keys(value: object, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ClaimFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise ClaimFixtureValidationError(f"{label} is missing keys: {sorted(missing)}")
    if unknown:
        raise ClaimFixtureValidationError(f"{label} has unknown keys: {sorted(unknown)}")


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ClaimFixtureValidationError(f"{label} must be text")
    return value


def _index(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ClaimFixtureValidationError(f"{label} must be a non-negative integer")
    return value


def _enum(value: object, enum_type: type, label: str) -> None:
    try:
        enum_type(_text(value, label))
    except ValueError as exc:
        raise ClaimFixtureValidationError(f"{label} has an invalid value") from exc


def _validate_json(value: object, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ClaimFixtureValidationError(f"{label} must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{label} {index}")
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key, item in value.items():
            _validate_json(item, f"{label} {key}")
        return
    raise ClaimFixtureValidationError(f"{label} is not JSON-safe")


def _validate_subject(raw: object, label: str) -> None:
    _exact_keys(raw, _SUBJECT_KEYS, label)
    _text(raw["relationship"], f"{label} relationship")
    _text(raw["suite"], f"{label} suite", allow_empty=True)
    _text(raw["canonicalAddress"], f"{label} canonicalAddress", allow_empty=True)


def _validate_claim(raw: object, label: str) -> None:
    _exact_keys(raw, _CLAIM_KEYS, label)
    _index(raw["evidenceIndex"], f"{label} evidenceIndex")
    _validate_subject(raw["subject"], f"{label} subject")
    _enum(raw["predicate"], ClaimPredicate, f"{label} predicate")
    _validate_json(raw["value"], f"{label} value")
    _text(raw["evidenceText"], f"{label} evidenceText")
    _enum(raw["actorRole"], ActorRole, f"{label} actorRole")
    _enum(raw["polarity"], ClaimPolarity, f"{label} polarity")
    _enum(raw["modality"], ClaimModality, f"{label} modality")
    confidence = raw["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ClaimFixtureValidationError(f"{label} confidence is invalid")
    for key in ("unit", "effectiveAt", "supersedesClaimId"):
        if raw[key] is not None:
            _text(raw[key], f"{label} {key}")


def _validate_review(raw: object, label: str) -> None:
    _exact_keys(raw, _REVIEW_KEYS, label)
    _index(raw["evidenceIndex"], f"{label} evidenceIndex")
    _text(raw["reason"], f"{label} reason")


def _validate_expected(raw: object, label: str) -> None:
    _exact_keys(raw, _EXPECTED_KEYS, label)
    if not isinstance(raw["accepted"], list):
        raise ClaimFixtureValidationError(f"{label} accepted must be a list")
    for index, item in enumerate(raw["accepted"]):
        item_label = f"{label} accepted {index}"
        _exact_keys(item, _ACCEPTED_KEYS, item_label)
        _enum(item["predicate"], ClaimPredicate, f"{item_label} predicate")
        _validate_json(item["value"], f"{item_label} value")
        _text(item["relationship"], f"{item_label} relationship")
        _text(item["suite"], f"{item_label} suite", allow_empty=True)
    if not isinstance(raw["issueCodes"], list):
        raise ClaimFixtureValidationError(f"{label} issueCodes must be a list")
    for index, item in enumerate(raw["issueCodes"]):
        _text(item, f"{label} issueCodes {index}")


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ClaimFixtureCase:
    case_id: str
    interpretation_case_id: str
    claims: Tuple[Mapping[str, Any], ...]
    review: Tuple[Mapping[str, Any], ...]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class ClaimFixtureCatalog:
    schema_version: int
    catalog_id: str
    manifest_hash: str
    cases: Tuple[ClaimFixtureCase, ...]


def load_claim_fixture_catalog(path: Path) -> ClaimFixtureCatalog:
    """Load an exact, immutable claim fixture catalog."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimFixtureValidationError("claim fixture JSON is unreadable") from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["schemaVersion"] != CLAIM_FIXTURE_SCHEMA_VERSION:
        raise ClaimFixtureValidationError("claim fixture schema version is unsupported")
    catalog_id = _text(raw["catalogId"], "catalogId")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise ClaimFixtureValidationError("cases must be a non-empty list")

    cases = []
    case_ids = set()
    for index, item in enumerate(raw["cases"]):
        label = f"case {index}"
        _exact_keys(item, _CASE_KEYS, label)
        case_id = _text(item["caseId"], f"{label} caseId")
        if case_id in case_ids:
            raise ClaimFixtureValidationError(f"duplicate caseId {case_id!r}")
        case_ids.add(case_id)
        interpretation_case_id = _text(
            item["interpretationCaseId"], f"{label} interpretationCaseId"
        )
        if not isinstance(item["claims"], list) or not isinstance(item["review"], list):
            raise ClaimFixtureValidationError(f"{label} claims and review must be lists")
        for claim_index, claim in enumerate(item["claims"]):
            _validate_claim(claim, f"{label} claim {claim_index}")
        for review_index, review in enumerate(item["review"]):
            _validate_review(review, f"{label} review {review_index}")
        _validate_expected(item["expected"], f"{label} expected")
        cases.append(
            ClaimFixtureCase(
                case_id=case_id,
                interpretation_case_id=interpretation_case_id,
                claims=tuple(_freeze(value) for value in item["claims"]),
                review=tuple(_freeze(value) for value in item["review"]),
                expected=_freeze(item["expected"]),
            )
        )

    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return ClaimFixtureCatalog(
        schema_version=CLAIM_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        manifest_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        cases=tuple(cases),
    )


__all__ = [
    "CLAIM_FIXTURE_SCHEMA_VERSION",
    "ClaimFixtureCase",
    "ClaimFixtureCatalog",
    "ClaimFixtureValidationError",
    "load_claim_fixture_catalog",
]
