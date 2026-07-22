"""Strict fixtures for deterministic policy and action evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import (
    ActionType,
    ApprovalClass,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    CompletenessState,
    ConversationState,
    EntityType,
    FitState,
    MarketState,
)


POLICY_FIXTURE_SCHEMA_VERSION = 1
REQUIRED_POLICY_DIMENSIONS = frozenset(
    {
        "market_state",
        "fit_state",
        "completeness",
        "conversation_state",
        "entity_isolation",
        "approval_boundary",
        "claim_conflict",
        "order_independence",
    }
)
POLICY_REASON_CODES = frozenset(
    {
        "broker_confirmed_available",
        "broker_confirmed_unavailable",
        "accepting_backup_offers",
        "hard_occupancy_after_deadline",
        "hard_term_below_minimum",
        "hard_drive_ins_below_minimum",
        "hard_requirement_unproven",
        "definite_remediation_before_deadline",
        "tentative_remediation_requires_review",
        "required_facts_complete",
        "required_facts_missing",
        "contact_opted_out",
        "broker_return_date",
        "redirect_requires_approval",
        "alternate_property_requires_approval",
        "call_requires_approval",
        "conflicting_active_claims",
        "unsupported_hard_requirement",
        "market_state_unknown",
    }
)

_ROOT_KEYS = frozenset({"schemaVersion", "catalogId", "cases"})
_CASE_KEYS = frozenset(
    {"caseId", "dimensions", "contract", "entities", "claims", "currentState", "expected"}
)
_ENTITY_KEYS = frozenset({"key", "type", "relationship"})
_CLAIM_KEYS = frozenset(
    {"key", "subject", "predicate", "value", "polarity", "modality", "supersedes"}
)
_CLAIM_REQUIRED_KEYS = _CLAIM_KEYS - {"supersedes"}
_CURRENT_STATE_KEYS = frozenset({"facts", "conversationStates", "followupStates"})
_EXPECTED_KEYS = frozenset({"results", "effectPolicy"})
_RESULT_KEYS = frozenset(
    {
        "subject",
        "marketState",
        "fitState",
        "completenessState",
        "conversationState",
        "approvalClass",
        "reasonCodes",
        "missingFields",
        "requiredActions",
        "forbiddenActions",
    }
)


class PolicyFixtureValidationError(ValueError):
    """Raised when a policy fixture cannot be trusted."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyFixtureValidationError(f"{label} must be non-empty text")
    return value.strip()


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise PolicyFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise PolicyFixtureValidationError(f"{label} missing keys: {sorted(missing)}")
    if unknown:
        raise PolicyFixtureValidationError(f"{label} has unknown keys: {sorted(unknown)}")


def _enum(value: Any, enum_type: type, label: str) -> None:
    cleaned = _required_text(value, label)
    try:
        enum_type(cleaned)
    except ValueError as exc:
        raise PolicyFixtureValidationError(f"{label} has invalid value {cleaned!r}") from exc


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PolicyFixtureValidationError(f"{label} must be a list")
    cleaned = tuple(_required_text(item, f"{label} item") for item in value)
    if len(cleaned) != len(set(cleaned)):
        raise PolicyFixtureValidationError(f"{label} contains duplicates")
    return cleaned


def _validate_action_signatures(values: Any, label: str) -> None:
    for signature in _string_list(values, label):
        parts = signature.split(":")
        if len(parts) != 2:
            raise PolicyFixtureValidationError(f"{label} has malformed action signature")
        _enum(parts[0], ActionType, f"{label} action type")
        _enum(parts[1], ApprovalClass, f"{label} approval class")


def _validate_case(raw: Any, index: int) -> "PolicyFixtureCase":
    _exact_keys(raw, _CASE_KEYS, f"case {index}")
    case_id = _required_text(raw["caseId"], f"case {index} caseId")
    dimensions = _string_list(raw["dimensions"], f"case {case_id} dimensions")

    contract = raw["contract"]
    if not isinstance(contract, dict):
        raise PolicyFixtureValidationError(f"case {case_id} contract must be an object")
    if not isinstance(contract.get("version", 1), int) or isinstance(
        contract.get("version", 1), bool
    ) or contract.get("version", 1) < 1:
        raise PolicyFixtureValidationError(f"case {case_id} contract version must be positive")

    entities = raw["entities"]
    if not isinstance(entities, list) or not entities:
        raise PolicyFixtureValidationError(f"case {case_id} entities must be non-empty")
    entity_keys = set()
    for entity_index, entity in enumerate(entities):
        label = f"case {case_id} entity {entity_index}"
        _exact_keys(entity, _ENTITY_KEYS, label)
        key = _required_text(entity["key"], f"{label} key")
        if key in entity_keys:
            raise PolicyFixtureValidationError(f"case {case_id} duplicate entity key")
        entity_keys.add(key)
        _enum(entity["type"], EntityType, f"{label} type")
        _required_text(entity["relationship"], f"{label} relationship")

    claims = raw["claims"]
    if not isinstance(claims, list):
        raise PolicyFixtureValidationError(f"case {case_id} claims must be a list")
    claim_keys = set()
    for claim_index, claim in enumerate(claims):
        label = f"case {case_id} claim {claim_index}"
        if not isinstance(claim, dict):
            raise PolicyFixtureValidationError(f"{label} must be an object")
        missing = _CLAIM_REQUIRED_KEYS - set(claim)
        unknown = set(claim) - _CLAIM_KEYS
        if missing:
            raise PolicyFixtureValidationError(f"{label} missing keys: {sorted(missing)}")
        if unknown:
            raise PolicyFixtureValidationError(f"{label} has unknown keys: {sorted(unknown)}")
        key = _required_text(claim["key"], f"{label} key")
        if key in claim_keys:
            raise PolicyFixtureValidationError(f"case {case_id} duplicate claim key")
        claim_keys.add(key)
        if claim["subject"] not in entity_keys:
            raise PolicyFixtureValidationError(f"{label} references unknown subject")
        _enum(claim["predicate"], ClaimPredicate, f"{label} predicate")
        _enum(claim["polarity"], ClaimPolarity, f"{label} polarity")
        _enum(claim["modality"], ClaimModality, f"{label} modality")
        supersedes = claim.get("supersedes")
        if supersedes is not None and supersedes not in claim_keys:
            raise PolicyFixtureValidationError(f"{label} supersedes unknown prior claim")

    current_state = raw["currentState"]
    _exact_keys(current_state, _CURRENT_STATE_KEYS, f"case {case_id} currentState")
    for key in _CURRENT_STATE_KEYS:
        if not isinstance(current_state[key], dict):
            raise PolicyFixtureValidationError(
                f"case {case_id} currentState {key} must be an object"
            )
        if set(current_state[key]) - entity_keys:
            raise PolicyFixtureValidationError(
                f"case {case_id} currentState {key} references unknown entity"
            )

    expected = raw["expected"]
    _exact_keys(expected, _EXPECTED_KEYS, f"case {case_id} expected")
    if expected["effectPolicy"] != "no_side_effect":
        raise PolicyFixtureValidationError(
            f"case {case_id} effectPolicy must be no_side_effect"
        )
    results = expected["results"]
    if not isinstance(results, list) or not results:
        raise PolicyFixtureValidationError(f"case {case_id} expected results must be non-empty")
    result_subjects = set()
    for result_index, result in enumerate(results):
        label = f"case {case_id} result {result_index}"
        _exact_keys(result, _RESULT_KEYS, label)
        subject = result["subject"]
        if subject not in entity_keys or subject in result_subjects:
            raise PolicyFixtureValidationError(f"{label} has invalid subject")
        result_subjects.add(subject)
        _enum(result["marketState"], MarketState, f"{label} marketState")
        _enum(result["fitState"], FitState, f"{label} fitState")
        _enum(result["completenessState"], CompletenessState, f"{label} completenessState")
        _enum(result["conversationState"], ConversationState, f"{label} conversationState")
        _enum(result["approvalClass"], ApprovalClass, f"{label} approvalClass")
        for reason in _string_list(result["reasonCodes"], f"{label} reasonCodes"):
            if reason not in POLICY_REASON_CODES:
                raise PolicyFixtureValidationError(f"{label} has unknown reason code {reason!r}")
        _string_list(result["missingFields"], f"{label} missingFields")
        _validate_action_signatures(result["requiredActions"], f"{label} requiredActions")
        _validate_action_signatures(result["forbiddenActions"], f"{label} forbiddenActions")

    return PolicyFixtureCase(
        case_id=case_id,
        dimensions=dimensions,
        contract=_freeze(contract),
        entities=tuple(_freeze(item) for item in entities),
        claims=tuple(_freeze(item) for item in claims),
        current_state=_freeze(current_state),
        expected=_freeze(expected),
    )


@dataclass(frozen=True)
class PolicyFixtureCase:
    case_id: str
    dimensions: tuple[str, ...]
    contract: Mapping[str, Any]
    entities: tuple[Mapping[str, Any], ...]
    claims: tuple[Mapping[str, Any], ...]
    current_state: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class PolicyFixtureCatalog:
    schema_version: int
    catalog_id: str
    cases: tuple[PolicyFixtureCase, ...]
    covered_dimensions: frozenset[str]
    manifest_hash: str


def load_policy_fixture_catalog(path: Path | str) -> PolicyFixtureCatalog:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyFixtureValidationError(f"fixture catalog cannot be read: {exc}") from exc
    _exact_keys(payload, _ROOT_KEYS, "fixture catalog")
    if payload["schemaVersion"] != POLICY_FIXTURE_SCHEMA_VERSION:
        raise PolicyFixtureValidationError("unsupported policy fixture schemaVersion")
    catalog_id = _required_text(payload["catalogId"], "catalogId")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise PolicyFixtureValidationError("fixture catalog cases must be non-empty")

    cases = tuple(_validate_case(raw, index) for index, raw in enumerate(payload["cases"]))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise PolicyFixtureValidationError("fixture catalog has duplicate caseId")
    covered = frozenset(dimension for case in cases for dimension in case.dimensions)
    missing_dimensions = REQUIRED_POLICY_DIMENSIONS - covered
    if missing_dimensions:
        raise PolicyFixtureValidationError(
            f"fixture catalog missing dimensions: {sorted(missing_dimensions)}"
        )
    manifest_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return PolicyFixtureCatalog(
        schema_version=POLICY_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        cases=cases,
        covered_dimensions=covered,
        manifest_hash=manifest_hash,
    )
