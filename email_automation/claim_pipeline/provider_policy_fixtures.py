"""Strict cross-catalog fixtures for the no-effect provider-policy shadow."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import (
    ActionType,
    ApprovalClass,
    CompletenessState,
    ConversationState,
    EntityType,
    FitState,
    MarketState,
)
from .policy_fixtures import POLICY_REASON_CODES
from .provider_quality_fixtures import ProviderQualityFixtureCatalog


PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION = 1
SUPPORTED_PROVIDER_POLICY_GAPS = frozenset(
    {
        "information_request_action_missing",
        "tour_request_action_missing",
    }
)

_SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DIMENSION_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_ROOT_KEYS = frozenset(
    {"schemaVersion", "catalogId", "providerQualityFixtureHash", "cases"}
)
_CASE_KEYS = frozenset(
    {
        "caseId",
        "providerCaseId",
        "dimensions",
        "contract",
        "subjects",
        "currentState",
        "expected",
    }
)
_CONTRACT_KEYS = frozenset(
    {"requiredFields", "hardRequirements", "softPreferences"}
)
_SUBJECT_KEYS = frozenset(
    {"key", "entityType", "relationship", "suite"}
)
_CURRENT_STATE_KEYS = frozenset(
    {"facts", "conversationStates", "followupStates"}
)
_EXPECTED_KEYS = frozenset({"disposition", "gapCodes", "results"})
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
        "actionCount",
        "requiredActions",
        "forbiddenActions",
    }
)
_DISPOSITIONS = frozenset({"pass", "expected_gap"})


class ProviderPolicyFixtureValidationError(ValueError):
    """Raised when a provider-policy fixture cannot be trusted."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ProviderPolicyFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise ProviderPolicyFixtureValidationError(
            f"{label} missing keys: {sorted(missing)}"
        )
    if unknown:
        raise ProviderPolicyFixtureValidationError(
            f"{label} has unknown keys: {sorted(unknown)}"
        )


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ProviderPolicyFixtureValidationError(
            f"{label} must be a report-safe identifier"
        )
    return value


def _sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProviderPolicyFixtureValidationError(
            f"{label} must be a SHA-256 hash"
        )
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderPolicyFixtureValidationError(f"{label} must be non-empty text")
    return value.strip()


def _enum(value: Any, enum_type: type, label: str) -> str:
    cleaned = _required_text(value, label)
    try:
        enum_type(cleaned)
    except ValueError as exc:
        raise ProviderPolicyFixtureValidationError(
            f"{label} has invalid value {cleaned!r}"
        ) from exc
    return cleaned


def _sorted_unique_text(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProviderPolicyFixtureValidationError(f"{label} must be a list")
    cleaned = tuple(_required_text(item, f"{label} item") for item in value)
    if cleaned != tuple(sorted(set(cleaned))):
        raise ProviderPolicyFixtureValidationError(
            f"{label} must be sorted and unique"
        )
    return cleaned


def _action_signatures(value: Any, label: str) -> tuple[str, ...]:
    signatures = _sorted_unique_text(value, label)
    for signature in signatures:
        parts = signature.split(":")
        if len(parts) != 2:
            raise ProviderPolicyFixtureValidationError(
                f"{label} has malformed action signature"
            )
        _enum(parts[0], ActionType, f"{label} action type")
        _enum(parts[1], ApprovalClass, f"{label} approval class")
    return signatures


def _validate_contract(value: Any, label: str) -> Mapping[str, Any]:
    _exact_keys(value, _CONTRACT_KEYS, label)
    _sorted_unique_text(value["requiredFields"], f"{label} requiredFields")
    for key in ("hardRequirements", "softPreferences"):
        if not isinstance(value[key], dict):
            raise ProviderPolicyFixtureValidationError(
                f"{label} {key} must be an object"
            )
        if any(not isinstance(item, str) or not item for item in value[key]):
            raise ProviderPolicyFixtureValidationError(
                f"{label} {key} has an invalid field"
            )
    return _freeze(value)


def _validate_subjects(value: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not value:
        raise ProviderPolicyFixtureValidationError(f"{label} must be non-empty")
    subjects = []
    keys = set()
    selectors = set()
    for index, item in enumerate(value):
        item_label = f"{label} item {index}"
        _exact_keys(item, _SUBJECT_KEYS, item_label)
        key = _safe_id(item["key"], f"{item_label} key")
        entity_type = _enum(
            item["entityType"], EntityType, f"{item_label} entityType"
        )
        relationship = _required_text(
            item["relationship"], f"{item_label} relationship"
        )
        suite = item["suite"]
        if not isinstance(suite, str) or len(suite) > 16 or "@" in suite:
            raise ProviderPolicyFixtureValidationError(
                f"{item_label} suite is unsafe"
            )
        selector = (entity_type, relationship, suite.casefold())
        if key in keys:
            raise ProviderPolicyFixtureValidationError(f"{label} has duplicate key")
        if selector in selectors:
            raise ProviderPolicyFixtureValidationError(
                f"{label} has duplicate selector"
            )
        keys.add(key)
        selectors.add(selector)
        subjects.append(_freeze(item))
    return tuple(subjects)


def _validate_current_state(
    value: Any,
    *,
    subject_keys: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    _exact_keys(value, _CURRENT_STATE_KEYS, label)
    for field in _CURRENT_STATE_KEYS:
        items = value[field]
        if not isinstance(items, dict):
            raise ProviderPolicyFixtureValidationError(
                f"{label} {field} must be an object"
            )
        if set(items) - subject_keys:
            raise ProviderPolicyFixtureValidationError(
                f"{label} {field} references unknown subject"
            )
    for item in value["facts"].values():
        if not isinstance(item, dict):
            raise ProviderPolicyFixtureValidationError(
                f"{label} facts values must be objects"
            )
    for field in ("conversationStates", "followupStates"):
        if any(not isinstance(item, str) or not item for item in value[field].values()):
            raise ProviderPolicyFixtureValidationError(
                f"{label} {field} values must be text"
            )
    return _freeze(value)


def _validate_expected(
    value: Any,
    *,
    subject_keys: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    _exact_keys(value, _EXPECTED_KEYS, label)
    disposition = _required_text(value["disposition"], f"{label} disposition")
    if disposition not in _DISPOSITIONS:
        raise ProviderPolicyFixtureValidationError(
            f"{label} disposition is unsupported"
        )
    gaps = _sorted_unique_text(value["gapCodes"], f"{label} gapCodes")
    if set(gaps) - SUPPORTED_PROVIDER_POLICY_GAPS:
        raise ProviderPolicyFixtureValidationError(f"{label} has unsupported gap code")
    if (disposition == "expected_gap") != bool(gaps):
        raise ProviderPolicyFixtureValidationError(
            f"{label} disposition and gap codes disagree"
        )
    results = value["results"]
    if not isinstance(results, list) or not results:
        raise ProviderPolicyFixtureValidationError(f"{label} results must be non-empty")
    result_subjects = set()
    for index, result in enumerate(results):
        result_label = f"{label} result {index}"
        _exact_keys(result, _RESULT_KEYS, result_label)
        subject = _safe_id(result["subject"], f"{result_label} subject")
        if subject not in subject_keys or subject in result_subjects:
            raise ProviderPolicyFixtureValidationError(
                f"{result_label} has invalid subject"
            )
        result_subjects.add(subject)
        _enum(result["marketState"], MarketState, f"{result_label} marketState")
        _enum(result["fitState"], FitState, f"{result_label} fitState")
        _enum(
            result["completenessState"],
            CompletenessState,
            f"{result_label} completenessState",
        )
        _enum(
            result["conversationState"],
            ConversationState,
            f"{result_label} conversationState",
        )
        _enum(
            result["approvalClass"],
            ApprovalClass,
            f"{result_label} approvalClass",
        )
        reasons = _sorted_unique_text(
            result["reasonCodes"], f"{result_label} reasonCodes"
        )
        if set(reasons) - POLICY_REASON_CODES:
            raise ProviderPolicyFixtureValidationError(
                f"{result_label} has unknown reason code"
            )
        _sorted_unique_text(result["missingFields"], f"{result_label} missingFields")
        action_count = result["actionCount"]
        if (
            not isinstance(action_count, int)
            or isinstance(action_count, bool)
            or action_count < 0
        ):
            raise ProviderPolicyFixtureValidationError(
                f"{result_label} actionCount must be nonnegative"
            )
        required = _action_signatures(
            result["requiredActions"], f"{result_label} requiredActions"
        )
        forbidden = _action_signatures(
            result["forbiddenActions"], f"{result_label} forbiddenActions"
        )
        if set(required) & set(forbidden):
            raise ProviderPolicyFixtureValidationError(
                f"{result_label} action cannot be required and forbidden"
            )
    if result_subjects != set(subject_keys):
        raise ProviderPolicyFixtureValidationError(
            f"{label} results must cover every subject"
        )
    return _freeze(value)


@dataclass(frozen=True)
class ProviderPolicyFixtureCase:
    case_id: str
    provider_case_id: str
    dimensions: tuple[str, ...]
    contract: Mapping[str, Any]
    subjects: tuple[Mapping[str, Any], ...]
    current_state: Mapping[str, Any]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderPolicyFixtureCatalog:
    schema_version: int
    catalog_id: str
    provider_quality_fixture_hash: str
    cases: tuple[ProviderPolicyFixtureCase, ...]
    covered_dimensions: frozenset[str]
    manifest_hash: str


def load_provider_policy_fixture_catalog(
    path: Path | str,
    *,
    provider_quality_catalog: ProviderQualityFixtureCatalog,
) -> ProviderPolicyFixtureCatalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderPolicyFixtureValidationError(
            "provider-policy fixture JSON is unreadable"
        ) from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["schemaVersion"] != PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION:
        raise ProviderPolicyFixtureValidationError(
            "provider-policy fixture schema version is unsupported"
        )
    catalog_id = _safe_id(raw["catalogId"], "catalogId")
    provider_hash = _sha256(
        raw["providerQualityFixtureHash"], "providerQualityFixtureHash"
    )
    if provider_hash != provider_quality_catalog.manifest_hash:
        raise ProviderPolicyFixtureValidationError(
            "provider quality fixture hash does not match the catalog"
        )
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise ProviderPolicyFixtureValidationError("catalog cases must be non-empty")

    provider_ids = {item.case_id for item in provider_quality_catalog.cases}
    cases = []
    case_ids = set()
    used_provider_ids = set()
    for index, item in enumerate(raw["cases"]):
        label = f"case {index}"
        _exact_keys(item, _CASE_KEYS, label)
        case_id = _safe_id(item["caseId"], f"{label} caseId")
        provider_case_id = _safe_id(
            item["providerCaseId"], f"{label} providerCaseId"
        )
        if case_id in case_ids:
            raise ProviderPolicyFixtureValidationError("duplicate caseId")
        if provider_case_id in used_provider_ids:
            raise ProviderPolicyFixtureValidationError("duplicate providerCaseId")
        if provider_case_id not in provider_ids:
            raise ProviderPolicyFixtureValidationError(
                f"{label} references an unknown provider case"
            )
        case_ids.add(case_id)
        used_provider_ids.add(provider_case_id)
        dimensions = _sorted_unique_text(item["dimensions"], f"{label} dimensions")
        if not dimensions or any(
            not _DIMENSION_ID.fullmatch(value) for value in dimensions
        ):
            raise ProviderPolicyFixtureValidationError(
                f"{label} dimensions must be report-safe"
            )
        subjects = _validate_subjects(item["subjects"], f"{label} subjects")
        subject_keys = frozenset(str(subject["key"]) for subject in subjects)
        cases.append(
            ProviderPolicyFixtureCase(
                case_id=case_id,
                provider_case_id=provider_case_id,
                dimensions=dimensions,
                contract=_validate_contract(item["contract"], f"{label} contract"),
                subjects=subjects,
                current_state=_validate_current_state(
                    item["currentState"],
                    subject_keys=subject_keys,
                    label=f"{label} currentState",
                ),
                expected=_validate_expected(
                    item["expected"],
                    subject_keys=subject_keys,
                    label=f"{label} expected",
                ),
            )
        )

    normalized = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return ProviderPolicyFixtureCatalog(
        schema_version=PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        provider_quality_fixture_hash=provider_hash,
        cases=tuple(cases),
        covered_dimensions=frozenset(
            dimension for case in cases for dimension in case.dimensions
        ),
        manifest_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION",
    "SUPPORTED_PROVIDER_POLICY_GAPS",
    "ProviderPolicyFixtureCase",
    "ProviderPolicyFixtureCatalog",
    "ProviderPolicyFixtureValidationError",
    "load_provider_policy_fixture_catalog",
]
