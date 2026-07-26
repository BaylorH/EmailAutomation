"""Strict expectations for complete pinned-provider claim extraction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from .claim_fixtures import ClaimFixtureCatalog
from .interpretation_fixtures import InterpretationFixtureCatalog


PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION = 1
SUPPORTED_REVIEW_CATEGORIES = (
    "entity_ambiguity",
    "insufficient_evidence",
)
_REPORT_SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROOT_KEYS = frozenset(
    {"schemaVersion", "catalogId", "claimFixtureHash", "cases"}
)
_CASE_KEYS = frozenset(
    {
        "caseId",
        "interpretationCaseId",
        "sourceClaimCaseIds",
        "expectedClaimDigests",
        "expectedReviews",
    }
)
_REVIEW_KEYS = frozenset({"category", "evidenceIndex"})


class ProviderQualityFixtureValidationError(ValueError):
    """Raised when provider-quality expectations are incomplete or unsafe."""


def _exact_keys(value: object, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ProviderQualityFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise ProviderQualityFixtureValidationError(
            f"{label} is missing keys: {sorted(missing)}"
        )
    if unknown:
        raise ProviderQualityFixtureValidationError(
            f"{label} has unknown keys: {sorted(unknown)}"
        )


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REPORT_SAFE_ID.fullmatch(value):
        raise ProviderQualityFixtureValidationError(
            f"{label} must be a report-safe identifier"
        )
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProviderQualityFixtureValidationError(
            f"{label} must be a SHA-256 digest"
        )
    return value


def _sorted_unique_ids(value: object, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ProviderQualityFixtureValidationError(f"{label} must be a list")
    normalized = tuple(_safe_id(item, f"{label} item") for item in value)
    if not normalized or normalized != tuple(sorted(set(normalized))):
        raise ProviderQualityFixtureValidationError(
            f"{label} must be non-empty, sorted, and unique"
        )
    return normalized


def _sorted_unique_digests(value: object, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ProviderQualityFixtureValidationError(f"{label} must be a list")
    normalized = tuple(
        _digest(item, f"{label} item {index}") for index, item in enumerate(value)
    )
    if normalized != tuple(sorted(set(normalized))):
        raise ProviderQualityFixtureValidationError(
            f"{label} must be sorted and unique"
        )
    return normalized


@dataclass(frozen=True, order=True)
class ProviderReviewExpectation:
    category: str
    evidence_index: int


@dataclass(frozen=True)
class ProviderQualityFixtureCase:
    case_id: str
    interpretation_case_id: str
    source_claim_case_ids: Tuple[str, ...]
    expected_claim_digests: Tuple[str, ...]
    expected_reviews: Tuple[ProviderReviewExpectation, ...]


@dataclass(frozen=True)
class ProviderQualityFixtureCatalog:
    schema_version: int
    catalog_id: str
    claim_fixture_hash: str
    manifest_hash: str
    cases: Tuple[ProviderQualityFixtureCase, ...]


def load_provider_quality_fixture_catalog(
    path: Path,
    *,
    claim_catalog: ClaimFixtureCatalog,
    interpretation_catalog: InterpretationFixtureCatalog,
) -> ProviderQualityFixtureCatalog:
    """Load provider expectations and prove their source partition is complete."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderQualityFixtureValidationError(
            "provider quality fixture JSON is unreadable"
        ) from exc
    _exact_keys(raw, _ROOT_KEYS, "catalog")
    if raw["schemaVersion"] != PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION:
        raise ProviderQualityFixtureValidationError(
            "provider quality fixture schema version is unsupported"
        )
    catalog_id = _safe_id(raw["catalogId"], "catalogId")
    claim_fixture_hash = _digest(raw["claimFixtureHash"], "claimFixtureHash")
    if claim_fixture_hash != claim_catalog.manifest_hash:
        raise ProviderQualityFixtureValidationError(
            "provider quality claim fixture hash does not match the claim catalog"
        )
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise ProviderQualityFixtureValidationError("cases must be a non-empty list")

    claim_by_id = {case.case_id: case for case in claim_catalog.cases}
    interpretation_by_id = {
        case.case_id: case for case in interpretation_catalog.cases
    }
    cases = []
    provider_case_ids = set()
    used_source_ids = []
    used_interpretation_ids = set()
    for index, item in enumerate(raw["cases"]):
        label = f"case {index}"
        _exact_keys(item, _CASE_KEYS, label)
        case_id = _safe_id(item["caseId"], f"{label} caseId")
        if case_id in provider_case_ids:
            raise ProviderQualityFixtureValidationError(
                f"duplicate provider caseId {case_id!r}"
            )
        provider_case_ids.add(case_id)
        interpretation_case_id = _safe_id(
            item["interpretationCaseId"], f"{label} interpretationCaseId"
        )
        if interpretation_case_id in used_interpretation_ids:
            raise ProviderQualityFixtureValidationError(
                f"duplicate provider interpretation case {interpretation_case_id!r}"
            )
        used_interpretation_ids.add(interpretation_case_id)
        interpretation_case = interpretation_by_id.get(interpretation_case_id)
        if interpretation_case is None:
            raise ProviderQualityFixtureValidationError(
                f"{label} references an unknown interpretation case"
            )
        source_ids = _sorted_unique_ids(
            item["sourceClaimCaseIds"], f"{label} sourceClaimCaseIds"
        )
        try:
            source_cases = tuple(claim_by_id[value] for value in source_ids)
        except KeyError as exc:
            raise ProviderQualityFixtureValidationError(
                f"{label} references an unknown source claim case"
            ) from exc
        used_source_ids.extend(source_ids)
        if any(
            source.interpretation_case_id != interpretation_case_id
            or source.prior_claims != source_cases[0].prior_claims
            for source in source_cases
        ):
            raise ProviderQualityFixtureValidationError(
                f"{label} source claim cases are not request-equivalent"
            )

        expected_claim_digests = _sorted_unique_digests(
            item["expectedClaimDigests"], f"{label} expectedClaimDigests"
        )
        complete_union = tuple(
            sorted(
                {
                    digest
                    for source in source_cases
                    for digest in source.expected["acceptedClaimDigests"]
                }
            )
        )
        if expected_claim_digests != complete_union:
            raise ProviderQualityFixtureValidationError(
                f"{label} expected claims must equal the complete accepted claim union"
            )

        raw_reviews = item["expectedReviews"]
        if not isinstance(raw_reviews, list):
            raise ProviderQualityFixtureValidationError(
                f"{label} expectedReviews must be a list"
            )
        reviews = []
        evidence_count = len(interpretation_case.expected["evidenceSequence"])
        for review_index, review in enumerate(raw_reviews):
            review_label = f"{label} expected review {review_index}"
            _exact_keys(review, _REVIEW_KEYS, review_label)
            category = review["category"]
            if not isinstance(category, str) or category not in SUPPORTED_REVIEW_CATEGORIES:
                raise ProviderQualityFixtureValidationError(
                    f"{review_label} category is unsupported"
                )
            evidence_index = review["evidenceIndex"]
            if (
                not isinstance(evidence_index, int)
                or isinstance(evidence_index, bool)
                or evidence_index < 0
                or evidence_index >= evidence_count
            ):
                raise ProviderQualityFixtureValidationError(
                    f"{review_label} evidenceIndex is invalid"
                )
            reviews.append(
                ProviderReviewExpectation(
                    category=category,
                    evidence_index=evidence_index,
                )
            )
        expected_reviews = tuple(sorted(set(reviews)))
        if tuple(reviews) != expected_reviews:
            raise ProviderQualityFixtureValidationError(
                f"{label} expectedReviews must be sorted and unique"
            )
        cases.append(
            ProviderQualityFixtureCase(
                case_id=case_id,
                interpretation_case_id=interpretation_case_id,
                source_claim_case_ids=source_ids,
                expected_claim_digests=expected_claim_digests,
                expected_reviews=expected_reviews,
            )
        )

    if sorted(used_source_ids) != sorted(claim_by_id):
        raise ProviderQualityFixtureValidationError(
            "source claim cases must form an exact partition of the claim catalog"
        )
    if used_interpretation_ids != set(interpretation_by_id):
        raise ProviderQualityFixtureValidationError(
            "provider cases must cover every interpretation case"
        )
    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return ProviderQualityFixtureCatalog(
        schema_version=PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        claim_fixture_hash=claim_fixture_hash,
        manifest_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        cases=tuple(cases),
    )


__all__ = [
    "PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION",
    "SUPPORTED_REVIEW_CATEGORIES",
    "ProviderQualityFixtureCase",
    "ProviderQualityFixtureCatalog",
    "ProviderQualityFixtureValidationError",
    "ProviderReviewExpectation",
    "load_provider_quality_fixture_catalog",
]
