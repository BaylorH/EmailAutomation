"""Strict fixture catalog for broad claim-extraction behavior."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Tuple

from .contracts import ActorRole, ClaimModality, ClaimPolarity, ClaimPredicate


CLAIM_FIXTURE_SCHEMA_VERSION = 3
_REPORT_SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROOT_KEYS = frozenset(
    {"schemaVersion", "catalogId", "coverageContract", "cases"}
)
_CASE_KEYS = frozenset(
    {
        "caseId",
        "interpretationCaseId",
        "priorClaims",
        "claims",
        "review",
        "coverage",
        "expected",
    }
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
_PRIOR_CLAIM_KEYS = _CLAIM_KEYS | frozenset({"actorEmail", "observedAt"})
_SUBJECT_KEYS = frozenset({"relationship", "suite", "canonicalAddress"})
_REVIEW_KEYS = frozenset({"evidenceIndex", "reason"})
_EXPECTED_KEYS = frozenset(
    {"acceptedClaimIndexes", "acceptedClaimDigests", "issues"}
)
_EXPECTED_ISSUE_KEYS = frozenset(
    {"code", "candidateIndex", "evidenceIndexes", "entities"}
)
_COVERAGE_CONTRACT_KEYS = frozenset(
    {"requiredPredicateOutcomes", "requiredIncidentDimensions"}
)
_CASE_COVERAGE_KEYS = frozenset({"predicateOutcomes", "incidentDimensions"})
_PREDICATE_OUTCOME_KEYS = frozenset({"predicate", "outcome"})
_COVERAGE_OUTCOMES = frozenset(
    {"accepted", "rejected", "corrected", "wrong_entity", "ambiguous"}
)
REQUIRED_INCIDENT_DIMENSIONS = (
    "alternate_property",
    "attachment",
    "call_request",
    "continued_followup_hazard",
    "correction",
    "link",
    "multi_turn",
    "opt_out",
    "redirect",
    "repeated_question",
    "requirements_mismatch",
    "split_suite",
    "terminal_closeout",
    "tour_request",
)
_SPECIAL_REQUIRED_OUTCOMES = {
    ClaimPredicate.AVAILABILITY.value: ("ambiguous", "wrong_entity"),
    ClaimPredicate.IDENTITY.value: ("ambiguous", "wrong_entity"),
    ClaimPredicate.RENT.value: ("corrected",),
}
REQUIRED_PREDICATE_OUTCOMES = MappingProxyType(
    {
        predicate.value: tuple(
            sorted(
                {"accepted", "rejected"}
                | set(_SPECIAL_REQUIRED_OUTCOMES.get(predicate.value, ()))
            )
        )
        for predicate in ClaimPredicate
    }
)


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


def _report_safe_id(value: object, label: str) -> str:
    cleaned = _text(value, label)
    if not _REPORT_SAFE_ID.fullmatch(cleaned):
        raise ClaimFixtureValidationError(
            f"{label} must be a report-safe identifier"
        )
    return cleaned


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


def _validate_claim(
    raw: object,
    label: str,
    *,
    prior_claim_count: int = 0,
    allow_symbolic_supersession: bool = False,
) -> None:
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
    supersedes = raw["supersedesClaimId"]
    if isinstance(supersedes, str) and supersedes.startswith("prior:"):
        if not allow_symbolic_supersession:
            raise ClaimFixtureValidationError(
                f"{label} cannot use symbolic supersession"
            )
        try:
            prior_index = int(supersedes.removeprefix("prior:"))
        except ValueError as exc:
            raise ClaimFixtureValidationError(
                f"{label} supersedesClaimId is invalid"
            ) from exc
        if prior_index < 0 or prior_index >= prior_claim_count:
            raise ClaimFixtureValidationError(
                f"{label} supersedesClaimId references an unknown prior claim"
            )


def _validate_prior_claim(raw: object, label: str) -> None:
    _exact_keys(raw, _PRIOR_CLAIM_KEYS, label)
    candidate = {key: value for key, value in raw.items() if key in _CLAIM_KEYS}
    _validate_claim(candidate, label)
    _text(raw["actorEmail"], f"{label} actorEmail")
    _text(raw["observedAt"], f"{label} observedAt")


def _validate_review(raw: object, label: str) -> None:
    _exact_keys(raw, _REVIEW_KEYS, label)
    _index(raw["evidenceIndex"], f"{label} evidenceIndex")
    _text(raw["reason"], f"{label} reason")


def _validate_expected(raw: object, label: str, claim_count: int) -> None:
    _exact_keys(raw, _EXPECTED_KEYS, label)
    accepted = raw["acceptedClaimIndexes"]
    if not isinstance(accepted, list):
        raise ClaimFixtureValidationError(
            f"{label} acceptedClaimIndexes must be a list"
        )
    accepted_indexes = tuple(
        _index(item, f"{label} acceptedClaimIndexes {index}")
        for index, item in enumerate(accepted)
    )
    if accepted_indexes != tuple(sorted(set(accepted_indexes))):
        raise ClaimFixtureValidationError(
            f"{label} acceptedClaimIndexes must be sorted and unique"
        )
    if any(index >= claim_count for index in accepted_indexes):
        raise ClaimFixtureValidationError(
            f"{label} acceptedClaimIndexes references an unknown claim"
        )
    digests = raw["acceptedClaimDigests"]
    if not isinstance(digests, list) or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in digests
    ):
        raise ClaimFixtureValidationError(
            f"{label} acceptedClaimDigests must contain SHA-256 digests"
        )
    if digests != sorted(set(digests)) or len(digests) != len(accepted_indexes):
        raise ClaimFixtureValidationError(
            f"{label} acceptedClaimDigests must be sorted, unique, and complete"
        )

    issues = raw["issues"]
    if not isinstance(issues, list):
        raise ClaimFixtureValidationError(f"{label} issues must be a list")
    for index, item in enumerate(issues):
        item_label = f"{label} issue {index}"
        _exact_keys(item, _EXPECTED_ISSUE_KEYS, item_label)
        _text(item["code"], f"{item_label} code")
        if item["candidateIndex"] is not None:
            _index(item["candidateIndex"], f"{item_label} candidateIndex")
        evidence_indexes = item["evidenceIndexes"]
        if not isinstance(evidence_indexes, list):
            raise ClaimFixtureValidationError(
                f"{item_label} evidenceIndexes must be a list"
            )
        normalized_evidence = tuple(
            _index(value, f"{item_label} evidenceIndexes {evidence_index}")
            for evidence_index, value in enumerate(evidence_indexes)
        )
        if normalized_evidence != tuple(sorted(set(normalized_evidence))):
            raise ClaimFixtureValidationError(
                f"{item_label} evidenceIndexes must be sorted and unique"
            )
        entities = item["entities"]
        if not isinstance(entities, list):
            raise ClaimFixtureValidationError(f"{item_label} entities must be a list")
        for entity_index, entity in enumerate(entities):
            _validate_subject(entity, f"{item_label} entity {entity_index}")


def _string_list(raw: object, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ClaimFixtureValidationError(f"{label} must be a list")
    values = tuple(_text(item, f"{label} item") for item in raw)
    if values != tuple(sorted(set(values))):
        raise ClaimFixtureValidationError(f"{label} must be sorted and unique")
    return values


def _predicate_outcomes(
    raw: object,
    label: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, list):
        raise ClaimFixtureValidationError(f"{label} must be a list")
    values = []
    for index, item in enumerate(raw):
        item_label = f"{label} item {index}"
        _exact_keys(item, _PREDICATE_OUTCOME_KEYS, item_label)
        _enum(item["predicate"], ClaimPredicate, f"{item_label} predicate")
        outcome = _text(item["outcome"], f"{item_label} outcome")
        if outcome not in _COVERAGE_OUTCOMES:
            raise ClaimFixtureValidationError(
                f"{item_label} outcome has an invalid value"
            )
        values.append((item["predicate"], outcome))
    normalized = tuple(sorted(set(values)))
    if tuple(values) != normalized:
        raise ClaimFixtureValidationError(f"{label} must be sorted and unique")
    return normalized


def _case_proves_predicate_outcome(
    *,
    claims: list[object],
    expected: Mapping[str, object],
    predicate: str,
    outcome: str,
) -> bool:
    accepted = set(expected["acceptedClaimIndexes"])
    matching = {
        index
        for index, claim in enumerate(claims)
        if claim["predicate"] == predicate
    }
    issue_by_candidate: dict[int, set[str]] = {}
    for issue in expected["issues"]:
        candidate_index = issue["candidateIndex"]
        if candidate_index is not None:
            issue_by_candidate.setdefault(candidate_index, set()).add(issue["code"])
    if outcome == "accepted":
        return bool(matching & accepted)
    if outcome == "rejected":
        return any(
            index not in accepted and index in issue_by_candidate for index in matching
        )
    if outcome == "corrected":
        return any(
            index in accepted
            and claims[index]["modality"] == ClaimModality.CORRECTED.value
            and isinstance(claims[index]["supersedesClaimId"], str)
            and claims[index]["supersedesClaimId"].startswith("prior:")
            for index in matching
        )
    required_issue = {
        "wrong_entity": "subject_evidence_mismatch",
        "ambiguous": "unresolved_entity_context",
    }[outcome]
    return any(
        index not in accepted and required_issue in issue_by_candidate.get(index, set())
        for index in matching
    )


def _validate_case_coverage(
    raw: object,
    *,
    label: str,
    claims: list[object],
    prior_claims: list[object],
    expected: Mapping[str, object],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    _exact_keys(raw, _CASE_COVERAGE_KEYS, label)
    predicate_outcomes = _predicate_outcomes(
        raw["predicateOutcomes"], f"{label} predicateOutcomes"
    )
    dimensions = _string_list(raw["incidentDimensions"], f"{label} incidentDimensions")
    for predicate, outcome in predicate_outcomes:
        if not _case_proves_predicate_outcome(
            claims=claims,
            expected=expected,
            predicate=predicate,
            outcome=outcome,
        ):
            raise ClaimFixtureValidationError(
                f"{label} does not prove {outcome} {predicate}"
            )

    accepted = set(expected["acceptedClaimIndexes"])
    accepted_claims = [claims[index] for index in accepted]
    if "multi_turn" in dimensions and not prior_claims:
        raise ClaimFixtureValidationError(
            f"{label} multi_turn coverage requires priorClaims"
        )
    if "correction" in dimensions and not any(
        claim["modality"] == ClaimModality.CORRECTED.value
        and isinstance(claim["supersedesClaimId"], str)
        and claim["supersedesClaimId"].startswith("prior:")
        for claim in accepted_claims
    ):
        raise ClaimFixtureValidationError(
            f"{label} correction coverage requires an accepted correction"
        )
    predicate_dimensions = {
        "call_request": ClaimPredicate.CALL_REQUEST.value,
        "opt_out": ClaimPredicate.OPT_OUT.value,
        "redirect": ClaimPredicate.REFERRAL.value,
        "tour_request": ClaimPredicate.TOUR_REQUEST.value,
    }
    for dimension, predicate in predicate_dimensions.items():
        if dimension in dimensions and not any(
            claim["predicate"] == predicate for claim in accepted_claims
        ):
            raise ClaimFixtureValidationError(
                f"{label} {dimension} coverage requires accepted {predicate}"
            )
    if "requirements_mismatch" in dimensions and not any(
        issue["code"] == "predicate_evidence_mismatch"
        for issue in expected["issues"]
    ):
        raise ClaimFixtureValidationError(
            f"{label} requirements_mismatch coverage requires a mismatch issue"
        )
    if "alternate_property" in dimensions and not any(
        "alternate" in claim["subject"]["relationship"] for claim in claims
    ):
        raise ClaimFixtureValidationError(
            f"{label} alternate_property coverage requires an alternate subject"
        )
    if "split_suite" in dimensions and len(
        {
            claim["subject"]["suite"]
            for claim in claims
            if claim["subject"]["suite"]
        }
    ) < 2:
        raise ClaimFixtureValidationError(
            f"{label} split_suite coverage requires multiple suites"
        )
    return predicate_outcomes, dimensions


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
    prior_claims: Tuple[Mapping[str, Any], ...]
    claims: Tuple[Mapping[str, Any], ...]
    review: Tuple[Mapping[str, Any], ...]
    predicate_outcomes: Tuple[Tuple[str, str], ...]
    incident_dimensions: Tuple[str, ...]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class ClaimFixtureCatalog:
    schema_version: int
    catalog_id: str
    manifest_hash: str
    required_predicate_outcomes: Mapping[str, Tuple[str, ...]]
    required_incident_dimensions: Tuple[str, ...]
    cases: Tuple[ClaimFixtureCase, ...]


def validate_claim_fixture_coverage(catalog: ClaimFixtureCatalog) -> None:
    """Fail when a required behavior has no semantically proven fixture."""

    observed_predicate_outcomes = {
        item
        for case in catalog.cases
        for item in case.predicate_outcomes
    }
    required_predicate_outcomes = {
        (predicate, outcome)
        for predicate, outcomes in catalog.required_predicate_outcomes.items()
        for outcome in outcomes
    }
    missing_predicates = sorted(
        required_predicate_outcomes - observed_predicate_outcomes
    )
    if missing_predicates:
        rendered = ", ".join(
            f"{predicate}:{outcome}" for predicate, outcome in missing_predicates
        )
        raise ClaimFixtureValidationError(
            f"missing required predicate coverage: {rendered}"
        )
    observed_dimensions = {
        dimension
        for case in catalog.cases
        for dimension in case.incident_dimensions
    }
    missing_dimensions = sorted(
        set(catalog.required_incident_dimensions) - observed_dimensions
    )
    if missing_dimensions:
        raise ClaimFixtureValidationError(
            "missing required incident coverage: " + ", ".join(missing_dimensions)
        )


def load_claim_fixture_catalog(path: Path) -> ClaimFixtureCatalog:
    """Load an exact, immutable claim fixture catalog."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaimFixtureValidationError("claim fixture JSON is unreadable") from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["schemaVersion"] != CLAIM_FIXTURE_SCHEMA_VERSION:
        raise ClaimFixtureValidationError("claim fixture schema version is unsupported")
    catalog_id = _report_safe_id(raw["catalogId"], "catalogId")
    coverage_contract = raw["coverageContract"]
    _exact_keys(coverage_contract, _COVERAGE_CONTRACT_KEYS, "coverageContract")
    raw_required_predicates = coverage_contract["requiredPredicateOutcomes"]
    if not isinstance(raw_required_predicates, dict) or not raw_required_predicates:
        raise ClaimFixtureValidationError(
            "coverageContract requiredPredicateOutcomes must be an object"
        )
    required_predicate_outcomes = {}
    for predicate, outcomes in raw_required_predicates.items():
        _enum(predicate, ClaimPredicate, "required predicate")
        normalized_outcomes = _string_list(
            outcomes, f"required predicate {predicate} outcomes"
        )
        if not normalized_outcomes or not set(normalized_outcomes).issubset(
            _COVERAGE_OUTCOMES
        ):
            raise ClaimFixtureValidationError(
                f"required predicate {predicate} outcomes are invalid"
            )
        required_predicate_outcomes[predicate] = normalized_outcomes
    required_incident_dimensions = _string_list(
        coverage_contract["requiredIncidentDimensions"],
        "coverageContract requiredIncidentDimensions",
    )
    if required_predicate_outcomes != dict(REQUIRED_PREDICATE_OUTCOMES):
        raise ClaimFixtureValidationError(
            "coverageContract must name every supported predicate and required outcome"
        )
    if required_incident_dimensions != REQUIRED_INCIDENT_DIMENSIONS:
        raise ClaimFixtureValidationError(
            "coverageContract must name every required incident dimension"
        )
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise ClaimFixtureValidationError("cases must be a non-empty list")

    cases = []
    case_ids = set()
    for index, item in enumerate(raw["cases"]):
        label = f"case {index}"
        _exact_keys(item, _CASE_KEYS, label)
        case_id = _report_safe_id(item["caseId"], f"{label} caseId")
        if case_id in case_ids:
            raise ClaimFixtureValidationError(f"duplicate caseId {case_id!r}")
        case_ids.add(case_id)
        interpretation_case_id = _report_safe_id(
            item["interpretationCaseId"], f"{label} interpretationCaseId"
        )
        if (
            not isinstance(item["priorClaims"], list)
            or not isinstance(item["claims"], list)
            or not isinstance(item["review"], list)
        ):
            raise ClaimFixtureValidationError(
                f"{label} priorClaims, claims, and review must be lists"
            )
        for prior_index, claim in enumerate(item["priorClaims"]):
            _validate_prior_claim(claim, f"{label} prior claim {prior_index}")
        for claim_index, claim in enumerate(item["claims"]):
            _validate_claim(
                claim,
                f"{label} claim {claim_index}",
                prior_claim_count=len(item["priorClaims"]),
                allow_symbolic_supersession=True,
            )
        for review_index, review in enumerate(item["review"]):
            _validate_review(review, f"{label} review {review_index}")
        _validate_expected(item["expected"], f"{label} expected", len(item["claims"]))
        predicate_outcomes, incident_dimensions = _validate_case_coverage(
            item["coverage"],
            label=f"{label} coverage",
            claims=item["claims"],
            prior_claims=item["priorClaims"],
            expected=item["expected"],
        )
        cases.append(
            ClaimFixtureCase(
                case_id=case_id,
                interpretation_case_id=interpretation_case_id,
                prior_claims=tuple(_freeze(value) for value in item["priorClaims"]),
                claims=tuple(_freeze(value) for value in item["claims"]),
                review=tuple(_freeze(value) for value in item["review"]),
                predicate_outcomes=predicate_outcomes,
                incident_dimensions=incident_dimensions,
                expected=_freeze(item["expected"]),
            )
        )

    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    catalog = ClaimFixtureCatalog(
        schema_version=CLAIM_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        manifest_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        required_predicate_outcomes=MappingProxyType(
            dict(sorted(required_predicate_outcomes.items()))
        ),
        required_incident_dimensions=required_incident_dimensions,
        cases=tuple(cases),
    )
    validate_claim_fixture_coverage(catalog)
    return catalog


__all__ = [
    "CLAIM_FIXTURE_SCHEMA_VERSION",
    "REQUIRED_INCIDENT_DIMENSIONS",
    "REQUIRED_PREDICATE_OUTCOMES",
    "ClaimFixtureCase",
    "ClaimFixtureCatalog",
    "ClaimFixtureValidationError",
    "load_claim_fixture_catalog",
    "validate_claim_fixture_coverage",
]
