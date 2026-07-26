"""Strict, reproducible loader for claim-pipeline boundary fixtures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .contracts import (
    ActionType,
    ActorRole,
    ApprovalClass,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    CompletenessState,
    ConversationState,
    EntityType,
    EvidenceSource,
    FitState,
    MarketState,
)


FIXTURE_SCHEMA_VERSION = 2
REQUIRED_DIMENSIONS = frozenset(
    {
        "evidence_source",
        "actor_authority",
        "subject_binding",
        "qualification",
        "time",
        "effective_contract",
        "action_boundary",
        "commit_state",
    }
)
_ROOT_KEYS = frozenset({"schemaVersion", "catalogId", "cases"})
_CASE_KEYS = frozenset({"caseId", "dimensions", "contract", "evidence", "expected"})
_EXPECTED_KEYS = frozenset(
    {
        "evidenceCount",
        "entities",
        "claims",
        "decisions",
        "actions",
        "effectPolicy",
    }
)
_ENTITY_KEYS = frozenset({"entityKey", "entityType", "relationship"})
_CLAIM_KEYS = frozenset(
    {
        "subject",
        "predicate",
        "value",
        "polarity",
        "modality",
        "evidenceIndex",
        "supersedesClaimIndex",
    }
)
_CLAIM_REQUIRED_KEYS = _CLAIM_KEYS - {"supersedesClaimIndex"}
_DECISION_KEYS = frozenset(
    {
        "subject",
        "marketState",
        "fitState",
        "completenessState",
        "conversationState",
        "reviewClass",
    }
)
_ACTION_KEYS = frozenset({"actionType", "target", "approvalClass"})
_CONTRACT_KEYS = frozenset(
    {
        "version",
        "hardRequirements",
        "requiredFields",
        "suiteRepresentation",
        "outOfOfficePolicy",
        "redirectPolicy",
        "callPolicy",
        "alternatePolicy",
        "hostileResponsePolicy",
    }
)
_EVIDENCE_KEYS = frozenset({"sourceKind", "actorRole", "subject", "text"})


class FixtureValidationError(ValueError):
    """Raised when a fixture catalog cannot be trusted for replay."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise FixtureValidationError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise FixtureValidationError(f"{label} must be non-empty")
    return cleaned


def _exact_keys(
    value: Any,
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    label: str,
) -> None:
    if not isinstance(value, dict):
        raise FixtureValidationError(f"{label} must be an object")
    missing = required - set(value)
    unknown = set(value) - allowed
    if missing:
        raise FixtureValidationError(f"{label} is missing keys: {sorted(missing)}")
    if unknown:
        raise FixtureValidationError(f"{label} has unknown keys: {sorted(unknown)}")


def _enum_value(value: Any, enum_type: type, label: str) -> str:
    cleaned = _required_text(value, label)
    try:
        enum_type(cleaned)
    except ValueError as exc:
        raise FixtureValidationError(f"{label} has invalid value {cleaned!r}") from exc
    return cleaned


def _validate_expected(expected: Any, evidence_count: int, case_id: str) -> None:
    _exact_keys(
        expected,
        required=_EXPECTED_KEYS,
        allowed=_EXPECTED_KEYS,
        label=f"case {case_id} expected",
    )
    if expected["evidenceCount"] != evidence_count:
        raise FixtureValidationError(
            f"case {case_id} expected evidenceCount must match evidence inputs"
        )
    for key in ("entities", "claims", "decisions", "actions"):
        if not isinstance(expected[key], list):
            raise FixtureValidationError(f"case {case_id} expected {key} must be a list")
    if expected["effectPolicy"] != "no_side_effect":
        raise FixtureValidationError(
            f"case {case_id} effectPolicy must be 'no_side_effect'"
        )

    entity_keys = set()
    for index, entity in enumerate(expected["entities"]):
        label = f"case {case_id} entity {index}"
        _exact_keys(entity, required=_ENTITY_KEYS, allowed=_ENTITY_KEYS, label=label)
        entity_key = _required_text(entity["entityKey"], f"{label} entityKey")
        if entity_key in entity_keys:
            raise FixtureValidationError(f"case {case_id} has duplicate entityKey")
        entity_keys.add(entity_key)
        _enum_value(entity["entityType"], EntityType, f"{label} entityType")
        _required_text(entity["relationship"], f"{label} relationship")

    for index, claim in enumerate(expected["claims"]):
        label = f"case {case_id} claim {index}"
        _exact_keys(
            claim,
            required=_CLAIM_REQUIRED_KEYS,
            allowed=_CLAIM_KEYS,
            label=label,
        )
        if claim["subject"] not in entity_keys:
            raise FixtureValidationError(f"{label} references unknown subject")
        evidence_index = claim["evidenceIndex"]
        if not isinstance(evidence_index, int) or not 0 <= evidence_index < evidence_count:
            raise FixtureValidationError(f"{label} has invalid evidenceIndex")
        supersedes = claim.get("supersedesClaimIndex")
        if supersedes is not None and (
            not isinstance(supersedes, int) or not 0 <= supersedes < index
        ):
            raise FixtureValidationError(f"{label} has invalid supersedesClaimIndex")
        _enum_value(claim["predicate"], ClaimPredicate, f"{label} predicate")
        _enum_value(claim["polarity"], ClaimPolarity, f"{label} polarity")
        _enum_value(claim["modality"], ClaimModality, f"{label} modality")

    decision_subjects = set()
    for index, decision in enumerate(expected["decisions"]):
        label = f"case {case_id} decision {index}"
        _exact_keys(
            decision,
            required=_DECISION_KEYS,
            allowed=_DECISION_KEYS,
            label=label,
        )
        subject = decision["subject"]
        if subject not in entity_keys:
            raise FixtureValidationError(f"{label} references unknown subject")
        if subject in decision_subjects:
            raise FixtureValidationError(f"case {case_id} has duplicate decision subject")
        decision_subjects.add(subject)
        _enum_value(decision["marketState"], MarketState, f"{label} marketState")
        _enum_value(decision["fitState"], FitState, f"{label} fitState")
        _enum_value(
            decision["completenessState"],
            CompletenessState,
            f"{label} completenessState",
        )
        _enum_value(
            decision["conversationState"],
            ConversationState,
            f"{label} conversationState",
        )
        _enum_value(decision["reviewClass"], ApprovalClass, f"{label} reviewClass")

    for index, action in enumerate(expected["actions"]):
        label = f"case {case_id} action {index}"
        _exact_keys(action, required=_ACTION_KEYS, allowed=_ACTION_KEYS, label=label)
        if action["target"] not in entity_keys:
            raise FixtureValidationError(f"{label} references unknown target")
        _enum_value(action["actionType"], ActionType, f"{label} actionType")
        _enum_value(
            action["approvalClass"],
            ApprovalClass,
            f"{label} approvalClass",
        )


def _validate_inputs(contract: Any, evidence: Any, case_id: str) -> None:
    _exact_keys(
        contract,
        required=frozenset(),
        allowed=_CONTRACT_KEYS,
        label=f"case {case_id} contract",
    )
    version = contract.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise FixtureValidationError(f"case {case_id} contract version must be positive")
    for key in ("hardRequirements",):
        if key in contract and not isinstance(contract[key], dict):
            raise FixtureValidationError(f"case {case_id} contract {key} must be an object")
    if "requiredFields" in contract and (
        not isinstance(contract["requiredFields"], list)
        or not all(
            isinstance(value, str) and value.strip()
            for value in contract["requiredFields"]
        )
    ):
        raise FixtureValidationError(
            f"case {case_id} contract requiredFields must be a list of strings"
        )
    for key in _CONTRACT_KEYS - {"version", "hardRequirements", "requiredFields"}:
        if key in contract:
            _required_text(contract[key], f"case {case_id} contract {key}")

    if not isinstance(evidence, list) or not evidence:
        raise FixtureValidationError(
            f"case {case_id} evidence must be a non-empty list of objects"
        )
    for index, item in enumerate(evidence):
        label = f"case {case_id} evidence {index}"
        _exact_keys(
            item,
            required=_EVIDENCE_KEYS,
            allowed=_EVIDENCE_KEYS,
            label=label,
        )
        _enum_value(item["sourceKind"], EvidenceSource, f"{label} sourceKind")
        _enum_value(item["actorRole"], ActorRole, f"{label} actorRole")
        _required_text(item["subject"], f"{label} subject")
        _required_text(item["text"], f"{label} text")


@dataclass(frozen=True)
class FixtureCase:
    case_id: str
    dimensions: Tuple[str, ...]
    contract: Mapping[str, Any]
    evidence: Tuple[Mapping[str, Any], ...]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class FixtureCatalog:
    schema_version: int
    catalog_id: str
    cases: Tuple[FixtureCase, ...]
    covered_dimensions: frozenset[str]
    manifest_hash: str


def _validate_case(raw_case: Any, index: int) -> FixtureCase:
    if not isinstance(raw_case, dict):
        raise FixtureValidationError(f"case {index} must be an object")
    unknown_keys = set(raw_case) - _CASE_KEYS
    missing_keys = _CASE_KEYS - set(raw_case)
    if unknown_keys:
        raise FixtureValidationError(
            f"case {index} has unknown keys: {sorted(unknown_keys)}"
        )
    if missing_keys:
        raise FixtureValidationError(
            f"case {index} is missing keys: {sorted(missing_keys)}"
        )

    case_id = _required_text(raw_case["caseId"], f"case {index} caseId")
    dimensions = raw_case["dimensions"]
    if not isinstance(dimensions, list) or not dimensions:
        raise FixtureValidationError(f"case {case_id} dimensions must be a non-empty list")
    cleaned_dimensions = tuple(
        _required_text(value, f"case {case_id} dimension") for value in dimensions
    )
    if len(cleaned_dimensions) != len(set(cleaned_dimensions)):
        raise FixtureValidationError(f"case {case_id} has duplicate dimensions")

    contract = raw_case["contract"]
    evidence = raw_case["evidence"]
    expected = raw_case["expected"]
    _validate_inputs(contract, evidence, case_id)
    if not isinstance(expected, dict):
        raise FixtureValidationError(f"case {case_id} expected must be an object")
    _validate_expected(expected, len(evidence), case_id)

    return FixtureCase(
        case_id=case_id,
        dimensions=cleaned_dimensions,
        contract=_freeze(contract),
        evidence=tuple(_freeze(item) for item in evidence),
        expected=_freeze(expected),
    )


def load_fixture_catalog(path: Path | str) -> FixtureCatalog:
    source_path = Path(path)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureValidationError(f"fixture catalog cannot be read: {exc}") from exc

    if not isinstance(payload, dict):
        raise FixtureValidationError("fixture catalog root must be an object")
    unknown_root_keys = set(payload) - _ROOT_KEYS
    missing_root_keys = _ROOT_KEYS - set(payload)
    if unknown_root_keys:
        raise FixtureValidationError(
            f"fixture catalog has unknown root keys: {sorted(unknown_root_keys)}"
        )
    if missing_root_keys:
        raise FixtureValidationError(
            f"fixture catalog is missing root keys: {sorted(missing_root_keys)}"
        )
    if payload["schemaVersion"] != FIXTURE_SCHEMA_VERSION:
        raise FixtureValidationError(
            f"unsupported fixture schemaVersion {payload['schemaVersion']!r}"
        )
    catalog_id = _required_text(payload["catalogId"], "catalogId")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise FixtureValidationError("fixture cases must be a non-empty list")

    cases = tuple(
        _validate_case(raw_case, index)
        for index, raw_case in enumerate(payload["cases"])
    )
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise FixtureValidationError("fixture catalog contains duplicate caseId values")
    covered_dimensions = frozenset(
        dimension for case in cases for dimension in case.dimensions
    )
    missing_dimensions = REQUIRED_DIMENSIONS - covered_dimensions
    if missing_dimensions:
        raise FixtureValidationError(
            f"fixture catalog is missing dimensions: {sorted(missing_dimensions)}"
        )

    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return FixtureCatalog(
        schema_version=FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        cases=cases,
        covered_dimensions=covered_dimensions,
        manifest_hash=hashlib.sha256(canonical).hexdigest(),
    )
