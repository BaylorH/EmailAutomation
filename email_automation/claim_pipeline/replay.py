"""Deterministic, read-only replay for the isolated claim pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .claim_fixtures import ClaimFixtureCatalog
from .claim_validation import (
    CandidateValidationError,
    is_fit_only_availability_evidence,
    validate_claim_semantics,
    validate_claim_subject_binding,
)
from .contracts import (
    ActorRole,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    EntityRef,
    EvidenceEnvelope,
)
from .entities import resolve_entities
from .evidence import normalize_message_evidence
from .extraction import (
    CLAIM_EXTRACTION_SCHEMA_VERSION,
    ClaimExtractionRequest,
    build_claim_extraction_request,
    extract_claims,
)
from .interpretation_fixtures import InterpretationFixtureCatalog
from .provider_quality_fixtures import (
    SUPPORTED_REVIEW_CATEGORIES,
    ProviderQualityFixtureCatalog,
    ProviderReviewExpectation,
)


MAX_REPLAY_REPEATS = 10
MAX_REPLAY_CALLS = 2_560
RECORDED_PROVIDER_ID = "recorded"
RECORDED_MODEL_ID = "fixture-output-v1"
RECORDED_PROMPT_ID = "recorded-claim-proposal-v1"
RECORDED_PROMPT_HASH = hashlib.sha256(
    b"SiteSift recorded claim fixture materialization v1"
).hexdigest()
_REPORT_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_EVALUATION_PROFILES = frozenset({"candidate_validation", "provider_quality"})


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _report_safe_id(value: object, label: str) -> str:
    cleaned = _text(value, label)
    if not _REPORT_SAFE_ID.fullmatch(cleaned):
        raise ValueError(f"{label} must be a report-safe identifier")
    return cleaned


def _sha256(value: object, label: str) -> str:
    cleaned = _text(value, label).lower()
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise ValueError(f"{label} must be a SHA-256 hexadecimal digest")
    return cleaned


def _git_revision(value: object) -> str:
    cleaned = _text(value, "code_revision").lower()
    if len(cleaned) != 40 or any(
        character not in "0123456789abcdef" for character in cleaned
    ):
        raise ValueError("code_revision must be a 40-character hexadecimal revision")
    return cleaned


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ReplayIdentity:
    identity_id: str
    code_revision: str
    source_tree_hash: str
    source_tree_dirty: bool
    python_version: str
    dependency_lock_hash: str
    interpretation_fixture_hash: str
    claim_fixture_hash: str
    evaluation_fixture_hash: str
    extraction_schema_version: int
    provider_id: str
    model_id: str
    prompt_id: str
    prompt_hash: str
    evaluation_profile: str
    repeats: int
    case_count: int
    interpretation_case_count: int
    planned_calls: int
    planned_interpretations: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_tree_dirty, bool):
            raise ValueError("source_tree_dirty must be boolean")
        normalized = {
            "code_revision": _git_revision(self.code_revision),
            "source_tree_hash": _sha256(self.source_tree_hash, "source_tree_hash"),
            "source_tree_dirty": self.source_tree_dirty,
            "python_version": _report_safe_id(self.python_version, "python_version"),
            "dependency_lock_hash": _sha256(
                self.dependency_lock_hash, "dependency_lock_hash"
            ),
            "interpretation_fixture_hash": _sha256(
                self.interpretation_fixture_hash, "interpretation_fixture_hash"
            ),
            "claim_fixture_hash": _sha256(
                self.claim_fixture_hash, "claim_fixture_hash"
            ),
            "evaluation_fixture_hash": _sha256(
                self.evaluation_fixture_hash, "evaluation_fixture_hash"
            ),
            "extraction_schema_version": _positive_int(
                self.extraction_schema_version, "extraction_schema_version"
            ),
            "provider_id": _report_safe_id(self.provider_id, "provider_id"),
            "model_id": _report_safe_id(self.model_id, "model_id"),
            "prompt_id": _report_safe_id(self.prompt_id, "prompt_id"),
            "prompt_hash": _sha256(self.prompt_hash, "prompt_hash"),
            "evaluation_profile": _report_safe_id(
                self.evaluation_profile, "evaluation_profile"
            ),
            "repeats": _positive_int(self.repeats, "repeats"),
            "case_count": _positive_int(self.case_count, "case_count"),
            "interpretation_case_count": _positive_int(
                self.interpretation_case_count, "interpretation_case_count"
            ),
        }
        if normalized["repeats"] > MAX_REPLAY_REPEATS:
            raise ValueError(f"repeats cannot exceed {MAX_REPLAY_REPEATS}")
        if normalized["evaluation_profile"] not in _EVALUATION_PROFILES:
            raise ValueError("evaluation_profile is unsupported")
        planned_calls = normalized["repeats"] * normalized["case_count"]
        planned_interpretations = (
            normalized["repeats"] * normalized["interpretation_case_count"]
        )
        if planned_calls > MAX_REPLAY_CALLS:
            raise ValueError(f"planned calls cannot exceed {MAX_REPLAY_CALLS}")
        if planned_interpretations > MAX_REPLAY_CALLS:
            raise ValueError(
                f"planned interpretations cannot exceed {MAX_REPLAY_CALLS}"
            )
        if self.planned_calls != planned_calls:
            raise ValueError("planned_calls does not match replay identity fields")
        if self.planned_interpretations != planned_interpretations:
            raise ValueError(
                "planned_interpretations does not match replay identity fields"
            )
        identity_fields = {
            **normalized,
            "planned_calls": planned_calls,
            "planned_interpretations": planned_interpretations,
        }
        if self.identity_id != _stable_id("replay_identity", identity_fields):
            raise ValueError("replay identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        code_revision: str,
        source_tree_hash: str,
        source_tree_dirty: bool,
        python_version: str,
        dependency_lock_hash: str,
        interpretation_fixture_hash: str,
        claim_fixture_hash: str,
        evaluation_fixture_hash: str,
        extraction_schema_version: int,
        provider_id: str,
        model_id: str,
        prompt_id: str,
        prompt_hash: str,
        repeats: int,
        case_count: int,
        interpretation_case_count: int,
        evaluation_profile: str = "candidate_validation",
    ) -> "ReplayIdentity":
        if not isinstance(source_tree_dirty, bool):
            raise ValueError("source_tree_dirty must be boolean")
        normalized = {
            "code_revision": _git_revision(code_revision),
            "source_tree_hash": _sha256(source_tree_hash, "source_tree_hash"),
            "source_tree_dirty": source_tree_dirty,
            "python_version": _report_safe_id(python_version, "python_version"),
            "dependency_lock_hash": _sha256(
                dependency_lock_hash, "dependency_lock_hash"
            ),
            "interpretation_fixture_hash": _sha256(
                interpretation_fixture_hash, "interpretation_fixture_hash"
            ),
            "claim_fixture_hash": _sha256(claim_fixture_hash, "claim_fixture_hash"),
            "evaluation_fixture_hash": _sha256(
                evaluation_fixture_hash, "evaluation_fixture_hash"
            ),
            "extraction_schema_version": _positive_int(
                extraction_schema_version, "extraction_schema_version"
            ),
            "provider_id": _report_safe_id(provider_id, "provider_id"),
            "model_id": _report_safe_id(model_id, "model_id"),
            "prompt_id": _report_safe_id(prompt_id, "prompt_id"),
            "prompt_hash": _sha256(prompt_hash, "prompt_hash"),
            "evaluation_profile": _report_safe_id(
                evaluation_profile, "evaluation_profile"
            ),
            "repeats": _positive_int(repeats, "repeats"),
            "case_count": _positive_int(case_count, "case_count"),
            "interpretation_case_count": _positive_int(
                interpretation_case_count, "interpretation_case_count"
            ),
        }
        if normalized["repeats"] > MAX_REPLAY_REPEATS:
            raise ValueError(f"repeats cannot exceed {MAX_REPLAY_REPEATS}")
        if normalized["evaluation_profile"] not in _EVALUATION_PROFILES:
            raise ValueError("evaluation_profile is unsupported")
        planned_calls = normalized["repeats"] * normalized["case_count"]
        if planned_calls > MAX_REPLAY_CALLS:
            raise ValueError(f"planned calls cannot exceed {MAX_REPLAY_CALLS}")
        planned_interpretations = (
            normalized["repeats"] * normalized["interpretation_case_count"]
        )
        if planned_interpretations > MAX_REPLAY_CALLS:
            raise ValueError(
                f"planned interpretations cannot exceed {MAX_REPLAY_CALLS}"
            )
        identity_fields = {
            **normalized,
            "planned_calls": planned_calls,
            "planned_interpretations": planned_interpretations,
        }
        return cls(
            identity_id=_stable_id("replay_identity", identity_fields),
            planned_calls=planned_calls,
            planned_interpretations=planned_interpretations,
            **normalized,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identityId": self.identity_id,
            "codeRevision": self.code_revision,
            "sourceTreeHash": self.source_tree_hash,
            "sourceTreeDirty": self.source_tree_dirty,
            "pythonVersion": self.python_version,
            "dependencyLockHash": self.dependency_lock_hash,
            "interpretationFixtureHash": self.interpretation_fixture_hash,
            "claimFixtureHash": self.claim_fixture_hash,
            "evaluationFixtureHash": self.evaluation_fixture_hash,
            "extractionSchemaVersion": self.extraction_schema_version,
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "promptId": self.prompt_id,
            "promptHash": self.prompt_hash,
            "evaluationProfile": self.evaluation_profile,
            "repeats": self.repeats,
            "caseCount": self.case_count,
            "interpretationCaseCount": self.interpretation_case_count,
            "plannedCalls": self.planned_calls,
            "plannedInterpretations": self.planned_interpretations,
        }


@dataclass(frozen=True)
class ProposalUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_microusd: int = 0
    provider_calls: int = 0
    provider_billed: bool = False
    usage_complete: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
            "provider_calls",
        ):
            _nonnegative_int(getattr(self, field_name), field_name)
        if not isinstance(self.provider_billed, bool):
            raise ValueError("provider_billed must be boolean")
        if not isinstance(self.usage_complete, bool):
            raise ValueError("usage_complete must be boolean")
        if self.provider_billed and self.provider_calls == 0:
            raise ValueError("provider_billed usage requires a provider call")
        if self.cost_microusd and not self.provider_billed:
            raise ValueError("non-billed usage cannot report provider cost")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "latencyMs": self.latency_ms,
            "costMicrousd": self.cost_microusd,
            "providerCalls": self.provider_calls,
            "providerBilled": self.provider_billed,
            "usageComplete": self.usage_complete,
        }


@dataclass(frozen=True)
class ProviderTelemetrySnapshot:
    """Aggregate transport observations, independent of proposal semantics."""

    attempts: int = 0
    billed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_microusd: int = 0
    incomplete_attempts: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "attempts",
            "billed_calls",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
            "incomplete_attempts",
        ):
            _nonnegative_int(getattr(self, field_name), field_name)
        if self.billed_calls > self.attempts:
            raise ValueError("billed_calls cannot exceed attempts")
        if self.incomplete_attempts > self.attempts:
            raise ValueError("incomplete_attempts cannot exceed attempts")

    def delta_usage(self, earlier: "ProviderTelemetrySnapshot") -> ProposalUsage:
        if not isinstance(earlier, ProviderTelemetrySnapshot):
            raise TypeError("earlier telemetry must be a ProviderTelemetrySnapshot")
        fields = (
            "attempts",
            "billed_calls",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_microusd",
            "incomplete_attempts",
        )
        delta = {
            field_name: getattr(self, field_name) - getattr(earlier, field_name)
            for field_name in fields
        }
        if any(value < 0 for value in delta.values()):
            raise ValueError("provider telemetry cannot move backwards")
        return ProposalUsage(
            input_tokens=delta["input_tokens"],
            output_tokens=delta["output_tokens"],
            latency_ms=delta["latency_ms"],
            cost_microusd=delta["cost_microusd"],
            provider_calls=delta["attempts"],
            provider_billed=(
                delta["attempts"] > 0
                and delta["billed_calls"] == delta["attempts"]
            ),
            usage_complete=(
                delta["incomplete_attempts"] == 0
                and delta["billed_calls"] in {0, delta["attempts"]}
            ),
        )


class ProviderTelemetry(Protocol):
    def snapshot(self) -> ProviderTelemetrySnapshot: ...


@dataclass(frozen=True)
class ProposalResponse:
    model_output: object
    usage: ProposalUsage

    def __post_init__(self) -> None:
        if not isinstance(self.usage, ProposalUsage):
            raise TypeError("usage must be ProposalUsage")


class ProposalAdapter(Protocol):
    provider_id: str
    model_id: str
    prompt_id: str
    prompt_hash: str

    def propose(
        self,
        *,
        case_id: str,
        request: ClaimExtractionRequest,
        evidence: tuple[EvidenceEnvelope, ...],
        entities: tuple[EntityRef, ...],
    ) -> ProposalResponse: ...


class RecordedProposalAdapter:
    """Materialize saved, sanitized fixture proposals without a provider call."""

    provider_id = RECORDED_PROVIDER_ID
    model_id = RECORDED_MODEL_ID
    prompt_id = RECORDED_PROMPT_ID
    prompt_hash = RECORDED_PROMPT_HASH

    def __init__(self, catalog: ClaimFixtureCatalog):
        if not isinstance(catalog, ClaimFixtureCatalog):
            raise TypeError("catalog must be a ClaimFixtureCatalog")
        self._cases = {case.case_id: case for case in catalog.cases}

    def propose(
        self,
        *,
        case_id: str,
        request: ClaimExtractionRequest,
        evidence: tuple[EvidenceEnvelope, ...],
        entities: tuple[EntityRef, ...],
    ) -> ProposalResponse:
        case = self._cases.get(case_id)
        if case is None:
            raise ValueError("recorded proposal case is unknown")
        if request.evidence != tuple(sorted(evidence, key=lambda item: item.evidence_id)):
            raise ValueError("recorded proposal evidence does not match the request")
        if request.entities != tuple(sorted(entities, key=lambda item: item.entity_id)):
            raise ValueError("recorded proposal entities do not match the request")

        fixture_prior_claims = tuple(
            _prior_claim_from_fixture(raw, evidence, entities)
            for raw in case.prior_claims
        )
        if tuple(sorted(item.claim_id for item in request.prior_claims)) != tuple(
            sorted(item.claim_id for item in fixture_prior_claims)
        ):
            raise ValueError("recorded proposal prior claims do not match the request")

        entity_by_key = {
            (item.relationship, item.suite, item.canonical_address): item
            for item in entities
        }
        claims = []
        for raw in case.claims:
            subject = raw["subject"]
            entity = entity_by_key[
                (
                    subject["relationship"],
                    subject["suite"],
                    subject["canonicalAddress"],
                )
            ]
            claim = {
                key: _plain_json(value)
                for key, value in raw.items()
                if key not in {"evidenceIndex", "subject"}
            }
            supersedes = claim["supersedesClaimId"]
            if isinstance(supersedes, str) and supersedes.startswith("prior:"):
                claim["supersedesClaimId"] = fixture_prior_claims[
                    int(supersedes.removeprefix("prior:"))
                ].claim_id
            claim.update(
                {
                    "evidenceId": evidence[raw["evidenceIndex"]].evidence_id,
                    "subjectEntityId": entity.entity_id,
                }
            )
            claims.append(claim)
        review = [
            {
                "evidenceId": evidence[item["evidenceIndex"]].evidence_id,
                "reason": item["reason"],
            }
            for item in case.review
        ]
        return ProposalResponse(
            model_output={"claims": claims, "review": review},
            usage=ProposalUsage(provider_billed=False),
        )


@dataclass(frozen=True)
class ReplayCaseResult:
    case_id: str
    repeat_index: int
    request_id: str
    proposal_digest: str
    outcome_digest: str
    quality_outcome_digest: str
    accepted_claim_count: int
    accepted_predicate_counts: tuple[tuple[str, int], ...]
    claim_mismatch_field_counts: tuple[tuple[str, int], ...]
    rejected_predicate_counts: tuple[tuple[str, int], ...]
    issue_codes: tuple[str, ...]
    quality_mismatch_codes: tuple[str, ...]
    passed: bool
    error_code: str
    usage: ProposalUsage

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "repeatIndex": self.repeat_index,
            "requestId": self.request_id,
            "proposalDigest": self.proposal_digest,
            "outcomeDigest": self.outcome_digest,
            "qualityOutcomeDigest": self.quality_outcome_digest,
            "acceptedClaimCount": self.accepted_claim_count,
            "acceptedPredicateCounts": dict(self.accepted_predicate_counts),
            "claimMismatchFieldCounts": dict(self.claim_mismatch_field_counts),
            "rejectedPredicateCounts": dict(self.rejected_predicate_counts),
            "issueCodes": list(self.issue_codes),
            "qualityMismatchCodes": list(self.quality_mismatch_codes),
            "passed": self.passed,
            "errorCode": self.error_code,
            "usage": self.usage.to_dict(),
        }


@dataclass(frozen=True)
class InterpretationReplayResult:
    case_id: str
    repeat_index: int
    outcome_digest: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "repeatIndex": self.repeat_index,
            "outcomeDigest": self.outcome_digest,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ReplayReport:
    identity: ReplayIdentity
    evaluation_passed: bool
    passed: bool
    interpretation_results: tuple[InterpretationReplayResult, ...]
    results: tuple[ReplayCaseResult, ...]
    interpretation_variance_case_ids: tuple[str, ...]
    proposal_variance_case_ids: tuple[str, ...]
    outcome_variance_case_ids: tuple[str, ...]
    quality_outcome_variance_case_ids: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    provider_calls: int
    provider_billed_calls: int
    usage_complete: bool
    error_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "evaluationPassed": self.evaluation_passed,
            "passed": self.passed,
            "summary": {
                "interpretationResultCount": len(self.interpretation_results),
                "passedInterpretationResultCount": sum(
                    item.passed for item in self.interpretation_results
                ),
                "resultCount": len(self.results),
                "passedResultCount": sum(item.passed for item in self.results),
                "errorCount": self.error_count,
                "interpretationVarianceCaseIds": list(
                    self.interpretation_variance_case_ids
                ),
                "proposalVarianceCaseIds": list(self.proposal_variance_case_ids),
                "outcomeVarianceCaseIds": list(self.outcome_variance_case_ids),
                "qualityOutcomeVarianceCaseIds": list(
                    self.quality_outcome_variance_case_ids
                ),
                "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens,
                "totalTokens": self.input_tokens + self.output_tokens,
                "latencyMs": self.latency_ms,
                "costMicrousd": self.cost_microusd,
                "providerCalls": self.provider_calls,
                "providerBilledCalls": self.provider_billed_calls,
                "usageComplete": self.usage_complete,
            },
            "interpretationResults": [
                item.to_dict() for item in self.interpretation_results
            ],
            "results": [item.to_dict() for item in self.results],
        }


def _validate_replay_inputs(
    *,
    interpretation_catalog: InterpretationFixtureCatalog,
    claim_catalog: ClaimFixtureCatalog,
    provider_quality_catalog: ProviderQualityFixtureCatalog | None,
    adapter: ProposalAdapter,
    identity: ReplayIdentity,
) -> dict[str, object]:
    _report_safe_id(interpretation_catalog.catalog_id, "interpretation catalog_id")
    _report_safe_id(claim_catalog.catalog_id, "claim catalog_id")
    for case in interpretation_catalog.cases:
        _report_safe_id(case.case_id, "interpretation case_id")
    for case in claim_catalog.cases:
        _report_safe_id(case.case_id, "claim case_id")
        _report_safe_id(case.interpretation_case_id, "interpretation case_id")
    if identity.interpretation_fixture_hash != interpretation_catalog.manifest_hash:
        raise ValueError("replay identity interpretation fixture hash does not match")
    if identity.claim_fixture_hash != claim_catalog.manifest_hash:
        raise ValueError("replay identity claim fixture hash does not match")
    if identity.extraction_schema_version != CLAIM_EXTRACTION_SCHEMA_VERSION:
        raise ValueError("replay identity extraction schema does not match")
    if identity.evaluation_profile == "provider_quality":
        if provider_quality_catalog is None:
            raise ValueError("provider quality replay requires its expectation catalog")
        _report_safe_id(
            provider_quality_catalog.catalog_id,
            "provider quality catalog_id",
        )
        expected_evaluation_hash = provider_quality_catalog.manifest_hash
        expected_case_count = len(provider_quality_catalog.cases)
    else:
        if provider_quality_catalog is not None:
            raise ValueError("candidate replay cannot use a provider quality catalog")
        expected_evaluation_hash = claim_catalog.manifest_hash
        expected_case_count = len(claim_catalog.cases)
    if identity.evaluation_fixture_hash != expected_evaluation_hash:
        raise ValueError("replay identity evaluation fixture hash does not match")
    if identity.case_count != expected_case_count:
        raise ValueError("replay identity case count does not match")
    if identity.interpretation_case_count != len(interpretation_catalog.cases):
        raise ValueError("replay identity interpretation case count does not match")
    adapter_identity = (
        adapter.provider_id,
        adapter.model_id,
        adapter.prompt_id,
        adapter.prompt_hash,
    )
    expected_adapter_identity = (
        identity.provider_id,
        identity.model_id,
        identity.prompt_id,
        identity.prompt_hash,
    )
    if adapter_identity != expected_adapter_identity:
        raise ValueError("replay identity adapter fields do not match")
    interpretation_by_id = {
        case.case_id: case for case in interpretation_catalog.cases
    }
    missing = sorted(
        {
            case.interpretation_case_id
            for case in claim_catalog.cases
            if case.interpretation_case_id not in interpretation_by_id
        }
    )
    if missing:
        raise ValueError("claim fixtures reference unknown interpretation cases")
    for case in claim_catalog.cases:
        source = interpretation_by_id[case.interpretation_case_id]
        evidence = normalize_message_evidence(source.message).evidence
        resolved_entities = resolve_entities(
            tenant_id=source.message.tenant_id,
            campaign_id=source.campaign_id,
            seeds=source.seeds,
            evidence=evidence,
        ).entities
        evidence_sources = tuple(item.source_kind.value for item in evidence)
        bound_evidence_indexes = {
            item["evidenceIndex"]
            for item in (*case.claims, *case.prior_claims, *case.review)
        }
        accepted = tuple(
            case.claims[index]
            for index in case.expected["acceptedClaimIndexes"]
        )
        accepted_predicates = {item["predicate"] for item in accepted}
        dimensions = set(case.incident_dimensions)
        try:
            source_time = datetime.fromisoformat(
                source.message.observed_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(
                f"{case.case_id} current message chronology is invalid"
            ) from exc
        for prior_index, raw_prior in enumerate(case.prior_claims):
            envelope = evidence[raw_prior["evidenceIndex"]]
            if raw_prior["evidenceText"].casefold() not in envelope.content.casefold():
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} excerpt is absent from evidence"
                )
            if raw_prior["actorEmail"].casefold() != envelope.actor.email.casefold():
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} actor is not evidence-bound"
                )
            try:
                prior_time = datetime.fromisoformat(
                    raw_prior["observedAt"].replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} chronology is invalid"
                ) from exc
            if prior_time.tzinfo is None:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} chronology is invalid"
                )
            if source_time.tzinfo is None or prior_time >= source_time:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} must precede the current message"
                )
            if raw_prior["observedAt"] not in envelope.content:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} chronology is not evidence-bound"
                )
            prior_claim = _prior_claim_from_fixture(
                raw_prior,
                evidence,
                resolved_entities,
            )
            try:
                validate_claim_semantics(prior_claim)
            except CandidateValidationError as exc:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} predicate does not match its evidence"
                ) from exc
            prior_entity = next(
                item
                for item in resolved_entities
                if item.entity_id == prior_claim.subject_entity_id
            )
            try:
                validate_claim_subject_binding(
                    prior_claim,
                    prior_entity,
                    resolved_entities,
                )
            except CandidateValidationError as exc:
                raise ValueError(
                    f"{case.case_id} prior claim {prior_index} subject is not evidence-bound"
                ) from exc
        for dimension in ("attachment", "link"):
            if dimension in dimensions and dimension not in evidence_sources:
                raise ValueError(
                    f"{case.case_id} {dimension} coverage requires {dimension} evidence"
                )
            if dimension in dimensions and not any(
                index < len(evidence_sources)
                and evidence_sources[index] == dimension
                for index in bound_evidence_indexes
            ):
                raise ValueError(
                    f"{case.case_id} {dimension} coverage requires a claim or review "
                    f"bound to {dimension} evidence"
                )
        if "repeated_question" in dimensions and not (
            any(
                item["predicate"] == ClaimPredicate.INFORMATION_REQUEST.value
                for item in case.prior_claims
            )
            and ClaimPredicate.INFORMATION_REQUEST.value in accepted_predicates
        ):
            raise ValueError(
                f"{case.case_id} repeated_question coverage requires prior and current requests"
            )
        if "requirements_mismatch" in dimensions and not any(
            item["predicate"] == ClaimPredicate.AVAILABILITY.value
            and item["value"] == "unavailable"
            and is_fit_only_availability_evidence(item["evidenceText"])
            and any(
                issue["candidateIndex"] == index
                and issue["code"] == "predicate_evidence_mismatch"
                for issue in case.expected["issues"]
            )
            for index, item in enumerate(case.claims)
        ):
            raise ValueError(
                f"{case.case_id} requirements_mismatch coverage requires "
                "fit-only availability evidence"
            )
        if "terminal_closeout" in dimensions and not any(
            item["predicate"] == ClaimPredicate.AVAILABILITY.value
            and item["value"] == "unavailable"
            and item["subject"]["relationship"] == "target"
            for item in accepted
        ):
            raise ValueError(
                f"{case.case_id} terminal_closeout coverage requires target unavailability"
            )
        if "continued_followup_hazard" in dimensions and not (
            ClaimPredicate.OPT_OUT.value in accepted_predicates
            or any(
                item["predicate"] == ClaimPredicate.AVAILABILITY.value
                and item["value"] == "unavailable"
                and item["subject"]["relationship"] == "target"
                for item in accepted
            )
        ):
            raise ValueError(
                f"{case.case_id} continued_followup_hazard coverage requires a stop signal"
            )
    return interpretation_by_id


def _claim_from_fixture(
    raw: Mapping[str, object],
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
    prior_claims: tuple[Claim, ...] = (),
) -> Claim:
    envelope = evidence[raw["evidenceIndex"]]
    subject = raw["subject"]
    entity = next(
        item
        for item in entities
        if (
            item.relationship,
            item.suite,
            item.canonical_address,
        )
        == (
            subject["relationship"],
            subject["suite"],
            subject["canonicalAddress"],
        )
    )
    supersedes = raw["supersedesClaimId"]
    if isinstance(supersedes, str) and supersedes.startswith("prior:"):
        supersedes = prior_claims[int(supersedes.removeprefix("prior:"))].claim_id
    return Claim.create(
        tenant_id=envelope.tenant_id,
        campaign_id=envelope.campaign_id,
        evidence_id=envelope.evidence_id,
        subject_entity_id=entity.entity_id,
        predicate=ClaimPredicate(raw["predicate"]),
        value=raw["value"],
        evidence_text=raw["evidenceText"],
        actor_role=ActorRole(raw["actorRole"]),
        polarity=ClaimPolarity(raw["polarity"]),
        modality=ClaimModality(raw["modality"]),
        confidence=raw["confidence"],
        unit=raw["unit"],
        effective_at=raw["effectiveAt"],
        supersedes_claim_id=supersedes,
        actor_email=envelope.actor.email,
        observed_at=envelope.observed_at,
    )


def _prior_claim_from_fixture(
    raw: Mapping[str, object],
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
) -> Claim:
    envelope = evidence[raw["evidenceIndex"]]
    subject = raw["subject"]
    entity = next(
        item
        for item in entities
        if (
            item.relationship,
            item.suite,
            item.canonical_address,
        )
        == (
            subject["relationship"],
            subject["suite"],
            subject["canonicalAddress"],
        )
    )
    return Claim.create(
        tenant_id=envelope.tenant_id,
        campaign_id=envelope.campaign_id,
        evidence_id=envelope.evidence_id,
        subject_entity_id=entity.entity_id,
        predicate=ClaimPredicate(raw["predicate"]),
        value=raw["value"],
        evidence_text=raw["evidenceText"],
        actor_role=ActorRole(raw["actorRole"]),
        polarity=ClaimPolarity(raw["polarity"]),
        modality=ClaimModality(raw["modality"]),
        confidence=raw["confidence"],
        unit=raw["unit"],
        effective_at=raw["effectiveAt"],
        supersedes_claim_id=raw["supersedesClaimId"],
        actor_email=raw["actorEmail"],
        observed_at=raw["observedAt"],
    )


def _claim_oracle(case) -> tuple[str, ...]:
    return tuple(case.expected["acceptedClaimDigests"])


def _string_counts(values: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return tuple(sorted(counts.items()))


def _accepted_predicate_counts(claims: tuple[Claim, ...]) -> tuple[tuple[str, int], ...]:
    return _string_counts(
        tuple(item.predicate.value for item in claims)
    )


def _provider_expected_predicate_counts(
    evaluation_case: object,
    claim_by_id: Mapping[str, object],
) -> tuple[tuple[str, int], ...]:
    predicates_by_digest = {}
    for source_case_id in evaluation_case.source_claim_case_ids:
        source_case = claim_by_id[source_case_id]
        for index in source_case.expected["acceptedClaimIndexes"]:
            raw = source_case.claims[index]
            predicates_by_digest[_digest(_plain_json(raw))] = raw["predicate"]
    expected_digests = set(evaluation_case.expected_claim_digests)
    return _string_counts(
        tuple(
            predicate
            for digest, predicate in predicates_by_digest.items()
            if digest in expected_digests
        )
    )


def _provider_expected_claim_items(
    evaluation_case: object,
    claim_by_id: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    items_by_digest = {}
    for source_case_id in evaluation_case.source_claim_case_ids:
        source_case = claim_by_id[source_case_id]
        for index in source_case.expected["acceptedClaimIndexes"]:
            item = _plain_json(source_case.claims[index])
            items_by_digest[_digest(item)] = item
    return tuple(
        sorted(
            (
                item
                for digest, item in items_by_digest.items()
                if digest in set(evaluation_case.expected_claim_digests)
            ),
            key=_canonical_json,
        )
    )


def _actual_claim_items(
    claims: tuple[Claim, ...],
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
    prior_claims: tuple[Claim, ...],
) -> tuple[dict[str, object], ...]:
    evidence_indexes = {item.evidence_id: index for index, item in enumerate(evidence)}
    entities_by_id = {item.entity_id: item for item in entities}
    prior_indexes = {
        item.claim_id: f"prior:{index}" for index, item in enumerate(prior_claims)
    }
    items = []
    for claim in claims:
        entity = entities_by_id[claim.subject_entity_id]
        supersedes = prior_indexes.get(
            claim.supersedes_claim_id,
            claim.supersedes_claim_id,
        )
        items.append(
            {
                "evidenceIndex": evidence_indexes[claim.evidence_id],
                "subject": {
                    "relationship": entity.relationship,
                    "suite": entity.suite,
                    "canonicalAddress": entity.canonical_address,
                },
                "predicate": claim.predicate.value,
                "value": _plain_json(claim.value),
                "evidenceText": claim.evidence_text,
                "actorRole": claim.actor_role.value,
                "polarity": claim.polarity.value,
                "modality": claim.modality.value,
                "confidence": claim.confidence,
                "unit": claim.unit,
                "effectiveAt": claim.effective_at,
                "supersedesClaimId": supersedes,
            }
        )
    return tuple(sorted(items, key=_canonical_json))


def _actual_claim_outcome(
    claims: tuple[Claim, ...],
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
    prior_claims: tuple[Claim, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _digest(item)
            for item in _actual_claim_items(claims, evidence, entities, prior_claims)
        )
    )


def _provider_quality_claim_outcome(
    items: tuple[dict[str, object], ...],
) -> tuple[str, ...]:
    def semantic_json(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): semantic_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [semantic_json(item) for item in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    def semantic_claim(item: dict[str, object]) -> object:
        normalized = {
            key: value
            for key, value in item.items()
            if key not in {"evidenceText", "confidence"}
        }
        if normalized.get("predicate") in {"remediation", "correction"}:
            normalized["value"] = "validated_text_span"
        return semantic_json(normalized)

    return tuple(
        sorted(
            _digest(semantic_claim(item))
            for item in items
        )
    )


def _provider_quality_outcome_digest(
    items: tuple[dict[str, object], ...],
    reviews: tuple[ProviderReviewExpectation, ...],
) -> str:
    return _digest(
        {
            "claims": list(_provider_quality_claim_outcome(items)),
            "reviews": [
                {
                    "category": item.category,
                    "evidenceIndex": item.evidence_index,
                }
                for item in reviews
            ],
        }
    )


def _provider_claim_mismatch_field_counts(
    expected_items: tuple[dict[str, object], ...],
    actual_items: tuple[dict[str, object], ...],
) -> tuple[tuple[str, int], ...]:
    fields = (
        "evidenceIndex",
        "subject",
        "value",
        "evidenceText",
        "actorRole",
        "polarity",
        "modality",
        "confidence",
        "unit",
        "effectiveAt",
        "supersedesClaimId",
    )
    counts = {}
    predicates = sorted(
        {item["predicate"] for item in expected_items}
        | {item["predicate"] for item in actual_items}
    )
    for predicate in predicates:
        expected = tuple(
            item for item in expected_items if item["predicate"] == predicate
        )
        actual = tuple(item for item in actual_items if item["predicate"] == predicate)
        if len(expected) != len(actual):
            continue
        for field in (
            field
            for expected_item, actual_item in zip(expected, actual)
            for field in fields
            if expected_item[field] != actual_item[field]
        ):
            counts[field] = counts.get(field, 0) + 1
    return tuple(sorted(counts.items()))


def _issue_outcome(
    issues: tuple[object, ...],
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
) -> tuple[dict[str, object], ...]:
    evidence_indexes = {item.evidence_id: index for index, item in enumerate(evidence)}
    entities_by_id = {item.entity_id: item for item in entities}
    return tuple(
        sorted(
            (
                {
                    "code": item.code,
                    "candidateIndex": item.candidate_index,
                    "evidenceIndexes": sorted(
                        evidence_indexes[value] for value in item.evidence_ids
                    ),
                    "entities": sorted(
                        (
                            {
                                "relationship": entities_by_id[value].relationship,
                                "suite": entities_by_id[value].suite,
                                "canonicalAddress": entities_by_id[
                                    value
                                ].canonical_address,
                            }
                            for value in item.entity_ids
                        ),
                        key=_canonical_json,
                    ),
                }
                for item in issues
            ),
            key=_canonical_json,
        )
    )


def _expected_issue_outcome(case) -> tuple[dict[str, object], ...]:
    return tuple(
        sorted(
            (_plain_json(item) for item in case.expected["issues"]),
            key=_canonical_json,
        )
    )


def _provider_review_outcome(
    model_output: object,
    evidence: tuple[EvidenceEnvelope, ...],
) -> tuple[tuple[ProviderReviewExpectation, ...], tuple[str, ...]]:
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError:
            return (), ("invalid_provider_output",)
    if not isinstance(model_output, Mapping):
        return (), ("invalid_provider_output",)
    raw_reviews = model_output.get("review")
    if not isinstance(raw_reviews, list):
        return (), ("invalid_provider_output",)
    evidence_indexes = {item.evidence_id: index for index, item in enumerate(evidence)}
    reviews = []
    mismatch_codes = set()
    for item in raw_reviews:
        if not isinstance(item, Mapping) or set(item) != {"evidenceId", "reason"}:
            mismatch_codes.add("invalid_provider_output")
            continue
        category = item.get("reason")
        if category not in SUPPORTED_REVIEW_CATEGORIES:
            mismatch_codes.add("invalid_review_category")
            continue
        evidence_index = evidence_indexes.get(item.get("evidenceId"))
        if evidence_index is None:
            mismatch_codes.add("review_binding_mismatch")
            continue
        reviews.append(
            ProviderReviewExpectation(
                category=category,
                evidence_index=evidence_index,
            )
        )
    return tuple(sorted(reviews)), tuple(sorted(mismatch_codes))


def _rejected_predicate_counts(
    model_output: object,
    issues: tuple[object, ...],
) -> tuple[tuple[str, int], ...]:
    if isinstance(model_output, str):
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError:
            return ()
    if not isinstance(model_output, Mapping):
        return ()
    raw_claims = model_output.get("claims")
    if not isinstance(raw_claims, list):
        return ()
    allowed_predicates = {item.value for item in ClaimPredicate}
    counts = {}
    for issue in issues:
        index = issue.candidate_index
        if (
            issue.code == "model_requested_review"
            or index is None
            or index < 0
            or index >= len(raw_claims)
        ):
            continue
        raw = raw_claims[index]
        if not isinstance(raw, Mapping):
            continue
        predicate = raw.get("predicate")
        if predicate not in allowed_predicates:
            continue
        key = f"{issue.code}:{predicate}"
        if not re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", key):
            continue
        counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(counts.items()))


def _provider_quality_mismatches(
    *,
    expected_claim_digests: tuple[str, ...],
    expected_reviews: tuple[ProviderReviewExpectation, ...],
    actual_claim_digests: tuple[str, ...],
    expected_predicate_counts: tuple[tuple[str, int], ...],
    actual_predicate_counts: tuple[tuple[str, int], ...],
    actual_reviews: tuple[ProviderReviewExpectation, ...],
    review_parse_mismatches: tuple[str, ...],
) -> tuple[str, ...]:
    mismatches = set(review_parse_mismatches)
    expected_claims = set(expected_claim_digests)
    actual_claims = set(actual_claim_digests)
    missing_claims = expected_claims - actual_claims
    if missing_claims:
        mismatches.add("missing_expected_claims")
    if missing_claims:
        if expected_predicate_counts == actual_predicate_counts:
            mismatches.add("claim_detail_mismatch")
        else:
            mismatches.add("claim_predicate_count_mismatch")

    expected_review_set = set(expected_reviews)
    actual_review_set = set(actual_reviews)
    expected_categories = sorted(item.category for item in expected_reviews)
    actual_categories = sorted(item.category for item in actual_reviews)
    if expected_categories == actual_categories and expected_review_set != actual_review_set:
        mismatches.add("review_binding_mismatch")
    else:
        if expected_review_set - actual_review_set:
            mismatches.add("missing_expected_reviews")
        if actual_review_set - expected_review_set:
            mismatches.add("unexpected_reviews")
    return tuple(sorted(mismatches))


def _proposal_digest(model_output: object) -> str:
    try:
        return _digest(_plain_json(model_output))
    except (TypeError, ValueError):
        return _digest({"invalidOutputType": type(model_output).__name__})


def _interpretation_outcome(case) -> dict[str, object]:
    normalized = normalize_message_evidence(case.message)
    resolved = resolve_entities(
        tenant_id=case.message.tenant_id,
        campaign_id=case.campaign_id,
        seeds=case.seeds,
        evidence=normalized.evidence,
    )
    evidence_indexes = {
        item.evidence_id: index for index, item in enumerate(normalized.evidence)
    }
    source_counts: dict[str, int] = {}
    for item in normalized.evidence:
        source = item.source_kind.value
        source_counts[source] = source_counts.get(source, 0) + 1
    entities = sorted(
        (
            {
                "entityType": item.entity_type.value,
                "label": item.label,
                "canonicalAddress": item.canonical_address,
                "suite": item.suite,
                "relationship": item.relationship,
                "evidenceIndexes": sorted(
                    evidence_indexes[value] for value in item.evidence_ids
                ),
            }
            for item in resolved.entities
        ),
        key=_canonical_json,
    )
    issues = sorted(
        (
            {
                "code": item.code,
                "evidenceIndexes": sorted(
                    evidence_indexes[value] for value in item.evidence_ids
                ),
            }
            for item in resolved.issues
        ),
        key=_canonical_json,
    )
    return {
        "sourceCounts": dict(sorted(source_counts.items())),
        "evidenceSequence": [
            {
                "sourceKind": item.source_kind.value,
                "freshness": item.freshness.value,
                "location": item.location,
                "content": item.content,
                "parentIndex": (
                    evidence_indexes[item.parent_evidence_id]
                    if item.parent_evidence_id
                    else None
                ),
                "actorEmail": item.actor.email.lower(),
                "actorRole": item.actor.role.value,
            }
            for item in normalized.evidence
        ],
        "failures": [
            {
                "sourceKind": item.source_kind.value,
                "location": item.location,
                "reason": item.reason,
                "parentIndex": (
                    evidence_indexes[item.parent_evidence_id]
                    if item.parent_evidence_id
                    else None
                ),
            }
            for item in normalized.failures
        ],
        "entities": entities,
        "issues": issues,
    }


def _expected_interpretation_outcome(case) -> dict[str, object]:
    expected = _plain_json(case.expected)
    expected["sourceCounts"] = dict(sorted(expected["sourceCounts"].items()))
    expected["entities"] = sorted(expected["entities"], key=_canonical_json)
    expected["issues"] = sorted(expected["issues"], key=_canonical_json)
    return expected


def _outcome_digest(claims: tuple[Claim, ...], issues: tuple[object, ...]) -> str:
    return _digest(
        {
            "claims": sorted(
                (_plain_json(item.to_dict()) for item in claims),
                key=lambda item: item["claim_id"],
            ),
            "issues": sorted(
                (
                    {
                        "issueId": item.issue_id,
                        "code": item.code,
                        "candidateIndex": item.candidate_index,
                        "evidenceIds": list(item.evidence_ids),
                        "entityIds": list(item.entity_ids),
                    }
                    for item in issues
                ),
                key=lambda item: item["issueId"],
            ),
        }
    )


def _adapter_failure_result(
    *,
    case_id: str,
    repeat_index: int,
    request_id: str,
    error_code: str,
    usage: ProposalUsage | None = None,
) -> ReplayCaseResult:
    digest = _digest({"errorCode": error_code})
    return ReplayCaseResult(
        case_id=case_id,
        repeat_index=repeat_index,
        request_id=request_id,
        proposal_digest=digest,
        outcome_digest=digest,
        quality_outcome_digest=digest,
        accepted_claim_count=0,
        accepted_predicate_counts=(),
        claim_mismatch_field_counts=(),
        rejected_predicate_counts=(),
        issue_codes=(),
        quality_mismatch_codes=(),
        passed=False,
        error_code=error_code,
        usage=usage
        or ProposalUsage(provider_billed=False, usage_complete=False),
    )


def _reconciled_case_usage(
    *,
    provider_id: str,
    telemetry: ProviderTelemetry | None,
    telemetry_before: ProviderTelemetrySnapshot | None,
    declared: ProposalUsage | None,
) -> tuple[ProposalUsage, str]:
    if provider_id == RECORDED_PROVIDER_ID:
        return declared or ProposalUsage(
            provider_billed=False,
            usage_complete=False,
        ), ""
    if telemetry is None or telemetry_before is None:
        return ProposalUsage(provider_billed=False, usage_complete=False), (
            "transport_telemetry_missing"
        )
    try:
        observed = telemetry.snapshot().delta_usage(telemetry_before)
    except (TypeError, ValueError):
        return ProposalUsage(provider_billed=False, usage_complete=False), (
            "transport_telemetry_invalid"
        )
    if observed.provider_calls != 1:
        return observed, "transport_attempt_count_mismatch"
    if declared is not None and declared != observed:
        return observed, "transport_usage_mismatch"
    return observed, ""


def run_claim_replay(
    *,
    interpretation_catalog: InterpretationFixtureCatalog,
    claim_catalog: ClaimFixtureCatalog,
    provider_quality_catalog: ProviderQualityFixtureCatalog | None = None,
    adapter: ProposalAdapter,
    identity: ReplayIdentity,
    telemetry: ProviderTelemetry | None = None,
) -> ReplayReport:
    """Run the current saved boundary catalogs without persistence or effects."""

    interpretation_by_id = _validate_replay_inputs(
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        provider_quality_catalog=provider_quality_catalog,
        adapter=adapter,
        identity=identity,
    )
    claim_by_id = {case.case_id: case for case in claim_catalog.cases}
    if identity.evaluation_profile == "provider_quality":
        assert provider_quality_catalog is not None
        replay_cases = tuple(
            (
                case,
                claim_by_id[case.source_claim_case_ids[0]],
            )
            for case in provider_quality_catalog.cases
        )
    else:
        replay_cases = tuple((case, case) for case in claim_catalog.cases)
    interpretation_results: list[InterpretationReplayResult] = []
    results: list[ReplayCaseResult] = []
    interpretation_digests: dict[str, set[str]] = {
        case.case_id: set() for case in interpretation_catalog.cases
    }
    proposal_digests: dict[str, set[str]] = {
        case.case_id: set() for case, _ in replay_cases
    }
    outcome_digests: dict[str, set[str]] = {
        case.case_id: set() for case, _ in replay_cases
    }
    quality_outcome_digests: dict[str, set[str]] = {
        case.case_id: set() for case, _ in replay_cases
    }

    for repeat_index in range(identity.repeats):
        for case in interpretation_catalog.cases:
            actual = _interpretation_outcome(case)
            outcome_digest = _digest(actual)
            interpretation_digests[case.case_id].add(outcome_digest)
            interpretation_results.append(
                InterpretationReplayResult(
                    case_id=case.case_id,
                    repeat_index=repeat_index,
                    outcome_digest=outcome_digest,
                    passed=actual == _expected_interpretation_outcome(case),
                )
            )
        for evaluation_case, source_case in replay_cases:
            case_id = evaluation_case.case_id
            source = interpretation_by_id[source_case.interpretation_case_id]
            normalized = normalize_message_evidence(source.message)
            resolved = resolve_entities(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                seeds=source.seeds,
                evidence=normalized.evidence,
            )
            prior_claims = tuple(
                _prior_claim_from_fixture(raw, normalized.evidence, resolved.entities)
                for raw in source_case.prior_claims
            )
            request = build_claim_extraction_request(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                evidence=normalized.evidence,
                entities=resolved.entities,
                prior_claims=prior_claims,
                resolution_issues=resolved.issues,
            )
            telemetry_before = telemetry.snapshot() if telemetry is not None else None
            try:
                response = adapter.propose(
                    case_id=case_id,
                    request=request,
                    evidence=normalized.evidence,
                    entities=resolved.entities,
                )
            except Exception as exc:
                safe_error = f"adapter_{type(exc).__name__}"
                observed_usage, telemetry_error = _reconciled_case_usage(
                    provider_id=identity.provider_id,
                    telemetry=telemetry,
                    telemetry_before=telemetry_before,
                    declared=None,
                )
                failed = _adapter_failure_result(
                    case_id=case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    error_code=telemetry_error or safe_error,
                    usage=observed_usage,
                )
                proposal_digests[case_id].add(failed.proposal_digest)
                outcome_digests[case_id].add(failed.outcome_digest)
                quality_outcome_digests[case_id].add(failed.quality_outcome_digest)
                results.append(failed)
                continue
            if not isinstance(response, ProposalResponse):
                observed_usage, telemetry_error = _reconciled_case_usage(
                    provider_id=identity.provider_id,
                    telemetry=telemetry,
                    telemetry_before=telemetry_before,
                    declared=None,
                )
                failed = _adapter_failure_result(
                    case_id=case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    error_code=telemetry_error or "adapter_invalid_response",
                    usage=observed_usage,
                )
                proposal_digests[case_id].add(failed.proposal_digest)
                outcome_digests[case_id].add(failed.outcome_digest)
                quality_outcome_digests[case_id].add(failed.quality_outcome_digest)
                results.append(failed)
                continue
            observed_usage, telemetry_error = _reconciled_case_usage(
                provider_id=identity.provider_id,
                telemetry=telemetry,
                telemetry_before=telemetry_before,
                declared=response.usage,
            )
            if telemetry_error:
                failed = _adapter_failure_result(
                    case_id=case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    error_code=telemetry_error,
                    usage=observed_usage,
                )
                proposal_digests[case_id].add(failed.proposal_digest)
                outcome_digests[case_id].add(failed.outcome_digest)
                quality_outcome_digests[case_id].add(failed.quality_outcome_digest)
                results.append(failed)
                continue
            extracted = extract_claims(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                evidence=normalized.evidence,
                entities=resolved.entities,
                prior_claims=request.prior_claims,
                resolution_issues=resolved.issues,
                model_output=response.model_output,
            )
            proposal_digest = _proposal_digest(response.model_output)
            outcome_digest = _outcome_digest(extracted.claims, extracted.issues)
            proposal_digests[case_id].add(proposal_digest)
            outcome_digests[case_id].add(outcome_digest)
            issue_codes = tuple(sorted(item.code for item in extracted.issues))
            rejected_predicate_counts = _rejected_predicate_counts(
                response.model_output,
                extracted.issues,
            )
            actual_predicate_counts = _accepted_predicate_counts(extracted.claims)
            actual_claim_items = _actual_claim_items(
                extracted.claims,
                normalized.evidence,
                resolved.entities,
                request.prior_claims,
            )
            actual_claim_outcome = tuple(
                sorted(_digest(item) for item in actual_claim_items)
            )
            if identity.evaluation_profile == "provider_quality":
                expected_claim_items = _provider_expected_claim_items(
                    evaluation_case,
                    claim_by_id,
                )
                actual_reviews, review_parse_mismatches = _provider_review_outcome(
                    response.model_output,
                    normalized.evidence,
                )
                quality_mismatch_codes = _provider_quality_mismatches(
                    expected_claim_digests=_provider_quality_claim_outcome(
                        expected_claim_items
                    ),
                    expected_reviews=evaluation_case.expected_reviews,
                    actual_claim_digests=_provider_quality_claim_outcome(
                        actual_claim_items
                    ),
                    expected_predicate_counts=_provider_expected_predicate_counts(
                        evaluation_case,
                        claim_by_id,
                    ),
                    actual_predicate_counts=actual_predicate_counts,
                    actual_reviews=actual_reviews,
                    review_parse_mismatches=review_parse_mismatches,
                )
                claim_mismatch_field_counts = (
                    _provider_claim_mismatch_field_counts(
                        expected_claim_items,
                        actual_claim_items,
                    )
                    if "claim_detail_mismatch" in quality_mismatch_codes
                    else ()
                )
                quality_outcome_digest = _provider_quality_outcome_digest(
                    actual_claim_items,
                    actual_reviews,
                )
                passed = not quality_mismatch_codes
            else:
                quality_mismatch_codes = ()
                claim_mismatch_field_counts = ()
                quality_outcome_digest = outcome_digest
                passed = (
                    _claim_oracle(source_case) == actual_claim_outcome
                    and _expected_issue_outcome(source_case)
                    == _issue_outcome(
                        extracted.issues,
                        normalized.evidence,
                        resolved.entities,
                    )
                )
            results.append(
                ReplayCaseResult(
                    case_id=case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    proposal_digest=proposal_digest,
                    outcome_digest=outcome_digest,
                    quality_outcome_digest=quality_outcome_digest,
                    accepted_claim_count=len(extracted.claims),
                    accepted_predicate_counts=actual_predicate_counts,
                    claim_mismatch_field_counts=claim_mismatch_field_counts,
                    rejected_predicate_counts=rejected_predicate_counts,
                    issue_codes=issue_codes,
                    quality_mismatch_codes=quality_mismatch_codes,
                    passed=passed,
                    error_code="",
                    usage=observed_usage,
                )
            )
            quality_outcome_digests[case_id].add(quality_outcome_digest)

    interpretation_variance = tuple(
        sorted(
            case_id
            for case_id, values in interpretation_digests.items()
            if len(values) > 1
        )
    )
    proposal_variance = tuple(
        sorted(case_id for case_id, values in proposal_digests.items() if len(values) > 1)
    )
    outcome_variance = tuple(
        sorted(case_id for case_id, values in outcome_digests.items() if len(values) > 1)
    )
    quality_outcome_variance = tuple(
        sorted(
            case_id
            for case_id, values in quality_outcome_digests.items()
            if len(values) > 1
        )
    )
    error_count = sum(bool(item.error_code) for item in results)
    provider_calls = sum(item.usage.provider_calls for item in results)
    provider_billed_calls = sum(
        item.usage.provider_calls if item.usage.provider_billed else 0
        for item in results
    )
    usage_complete = all(item.usage.usage_complete for item in results)
    if identity.provider_id == RECORDED_PROVIDER_ID:
        provider_usage_matches_identity = (
            provider_calls == 0
            and provider_billed_calls == 0
            and sum(item.usage.total_tokens for item in results) == 0
            and sum(item.usage.latency_ms for item in results) == 0
            and sum(item.usage.cost_microusd for item in results) == 0
        )
    else:
        provider_usage_matches_identity = (
            provider_calls == identity.planned_calls
            and provider_billed_calls == identity.planned_calls
        )
    if identity.evaluation_profile == "provider_quality":
        repeatability_passed = not quality_outcome_variance
    else:
        repeatability_passed = not proposal_variance and not outcome_variance
    evaluation_passed = (
        len(interpretation_results) == identity.planned_interpretations
        and all(item.passed for item in interpretation_results)
        and len(results) == identity.planned_calls
        and all(item.passed for item in results)
        and not interpretation_variance
        and repeatability_passed
        and error_count == 0
        and usage_complete
        and provider_usage_matches_identity
    )
    passed = evaluation_passed and not identity.source_tree_dirty
    return ReplayReport(
        identity=identity,
        evaluation_passed=evaluation_passed,
        passed=passed,
        interpretation_results=tuple(interpretation_results),
        results=tuple(results),
        interpretation_variance_case_ids=interpretation_variance,
        proposal_variance_case_ids=proposal_variance,
        outcome_variance_case_ids=outcome_variance,
        quality_outcome_variance_case_ids=quality_outcome_variance,
        input_tokens=sum(item.usage.input_tokens for item in results),
        output_tokens=sum(item.usage.output_tokens for item in results),
        latency_ms=sum(item.usage.latency_ms for item in results),
        cost_microusd=sum(item.usage.cost_microusd for item in results),
        provider_calls=provider_calls,
        provider_billed_calls=provider_billed_calls,
        usage_complete=usage_complete,
        error_count=error_count,
    )


__all__ = [
    "MAX_REPLAY_CALLS",
    "MAX_REPLAY_REPEATS",
    "ProposalAdapter",
    "ProposalResponse",
    "ProposalUsage",
    "ProviderTelemetry",
    "ProviderTelemetrySnapshot",
    "RECORDED_MODEL_ID",
    "RECORDED_PROMPT_HASH",
    "RECORDED_PROMPT_ID",
    "RECORDED_PROVIDER_ID",
    "RecordedProposalAdapter",
    "InterpretationReplayResult",
    "ReplayCaseResult",
    "ReplayIdentity",
    "ReplayReport",
    "run_claim_replay",
]
