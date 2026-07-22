"""Deterministic, read-only replay for the isolated claim pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .claim_fixtures import ClaimFixtureCatalog
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


MAX_REPLAY_REPEATS = 10
MAX_REPLAY_CALLS = 2_560
RECORDED_PROVIDER_ID = "recorded"
RECORDED_MODEL_ID = "fixture-output-v1"
RECORDED_PROMPT_ID = "recorded-claim-proposal-v1"
RECORDED_PROMPT_HASH = hashlib.sha256(
    b"SiteSift recorded claim fixture materialization v1"
).hexdigest()


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


def _sha256(value: object, label: str) -> str:
    cleaned = _text(value, label).lower()
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise ValueError(f"{label} must be a SHA-256 hexadecimal digest")
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
    extraction_schema_version: int
    provider_id: str
    model_id: str
    prompt_id: str
    prompt_hash: str
    repeats: int
    case_count: int
    interpretation_case_count: int
    planned_calls: int
    planned_interpretations: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_tree_dirty, bool):
            raise ValueError("source_tree_dirty must be boolean")
        normalized = {
            "code_revision": _text(self.code_revision, "code_revision"),
            "source_tree_hash": _sha256(self.source_tree_hash, "source_tree_hash"),
            "source_tree_dirty": self.source_tree_dirty,
            "python_version": _text(self.python_version, "python_version"),
            "dependency_lock_hash": _sha256(
                self.dependency_lock_hash, "dependency_lock_hash"
            ),
            "interpretation_fixture_hash": _sha256(
                self.interpretation_fixture_hash, "interpretation_fixture_hash"
            ),
            "claim_fixture_hash": _sha256(
                self.claim_fixture_hash, "claim_fixture_hash"
            ),
            "extraction_schema_version": _positive_int(
                self.extraction_schema_version, "extraction_schema_version"
            ),
            "provider_id": _text(self.provider_id, "provider_id"),
            "model_id": _text(self.model_id, "model_id"),
            "prompt_id": _text(self.prompt_id, "prompt_id"),
            "prompt_hash": _sha256(self.prompt_hash, "prompt_hash"),
            "repeats": _positive_int(self.repeats, "repeats"),
            "case_count": _positive_int(self.case_count, "case_count"),
            "interpretation_case_count": _positive_int(
                self.interpretation_case_count, "interpretation_case_count"
            ),
        }
        if normalized["repeats"] > MAX_REPLAY_REPEATS:
            raise ValueError(f"repeats cannot exceed {MAX_REPLAY_REPEATS}")
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
        extraction_schema_version: int,
        provider_id: str,
        model_id: str,
        prompt_id: str,
        prompt_hash: str,
        repeats: int,
        case_count: int,
        interpretation_case_count: int,
    ) -> "ReplayIdentity":
        if not isinstance(source_tree_dirty, bool):
            raise ValueError("source_tree_dirty must be boolean")
        normalized = {
            "code_revision": _text(code_revision, "code_revision"),
            "source_tree_hash": _sha256(source_tree_hash, "source_tree_hash"),
            "source_tree_dirty": source_tree_dirty,
            "python_version": _text(python_version, "python_version"),
            "dependency_lock_hash": _sha256(
                dependency_lock_hash, "dependency_lock_hash"
            ),
            "interpretation_fixture_hash": _sha256(
                interpretation_fixture_hash, "interpretation_fixture_hash"
            ),
            "claim_fixture_hash": _sha256(claim_fixture_hash, "claim_fixture_hash"),
            "extraction_schema_version": _positive_int(
                extraction_schema_version, "extraction_schema_version"
            ),
            "provider_id": _text(provider_id, "provider_id"),
            "model_id": _text(model_id, "model_id"),
            "prompt_id": _text(prompt_id, "prompt_id"),
            "prompt_hash": _sha256(prompt_hash, "prompt_hash"),
            "repeats": _positive_int(repeats, "repeats"),
            "case_count": _positive_int(case_count, "case_count"),
            "interpretation_case_count": _positive_int(
                interpretation_case_count, "interpretation_case_count"
            ),
        }
        if normalized["repeats"] > MAX_REPLAY_REPEATS:
            raise ValueError(f"repeats cannot exceed {MAX_REPLAY_REPEATS}")
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
            "extractionSchemaVersion": self.extraction_schema_version,
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "promptId": self.prompt_id,
            "promptHash": self.prompt_hash,
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
    accepted_claim_count: int
    issue_codes: tuple[str, ...]
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
            "acceptedClaimCount": self.accepted_claim_count,
            "issueCodes": list(self.issue_codes),
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
    adapter: ProposalAdapter,
    identity: ReplayIdentity,
) -> dict[str, object]:
    if identity.interpretation_fixture_hash != interpretation_catalog.manifest_hash:
        raise ValueError("replay identity interpretation fixture hash does not match")
    if identity.claim_fixture_hash != claim_catalog.manifest_hash:
        raise ValueError("replay identity claim fixture hash does not match")
    if identity.extraction_schema_version != CLAIM_EXTRACTION_SCHEMA_VERSION:
        raise ValueError("replay identity extraction schema does not match")
    if identity.case_count != len(claim_catalog.cases):
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
    return interpretation_by_id


def _claim_from_fixture(
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
        actor_email=envelope.actor.email,
        observed_at=envelope.observed_at,
    )


def _claim_oracle(
    case,
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
) -> tuple[dict[str, object], ...]:
    return tuple(
        sorted(
            (
                _plain_json(
                    _claim_from_fixture(
                        case.claims[index], evidence, entities
                    ).to_dict()
                )
                for index in case.expected["acceptedClaimIndexes"]
            ),
            key=lambda item: item["claim_id"],
        )
    )


def _actual_claim_outcome(claims: tuple[Claim, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        sorted(
            (_plain_json(item.to_dict()) for item in claims),
            key=lambda item: item["claim_id"],
        )
    )


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
        accepted_claim_count=0,
        issue_codes=(),
        passed=False,
        error_code=error_code,
        usage=usage
        or ProposalUsage(provider_billed=False, usage_complete=False),
    )


def run_claim_replay(
    *,
    interpretation_catalog: InterpretationFixtureCatalog,
    claim_catalog: ClaimFixtureCatalog,
    adapter: ProposalAdapter,
    identity: ReplayIdentity,
) -> ReplayReport:
    """Run the current saved boundary catalogs without persistence or effects."""

    interpretation_by_id = _validate_replay_inputs(
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        adapter=adapter,
        identity=identity,
    )
    interpretation_results: list[InterpretationReplayResult] = []
    results: list[ReplayCaseResult] = []
    interpretation_digests: dict[str, set[str]] = {
        case.case_id: set() for case in interpretation_catalog.cases
    }
    proposal_digests: dict[str, set[str]] = {
        case.case_id: set() for case in claim_catalog.cases
    }
    outcome_digests: dict[str, set[str]] = {
        case.case_id: set() for case in claim_catalog.cases
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
        for case in claim_catalog.cases:
            source = interpretation_by_id[case.interpretation_case_id]
            normalized = normalize_message_evidence(source.message)
            resolved = resolve_entities(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                seeds=source.seeds,
                evidence=normalized.evidence,
            )
            request = build_claim_extraction_request(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                evidence=normalized.evidence,
                entities=resolved.entities,
                resolution_issues=resolved.issues,
            )
            try:
                response = adapter.propose(
                    case_id=case.case_id,
                    request=request,
                    evidence=normalized.evidence,
                    entities=resolved.entities,
                )
            except Exception as exc:
                safe_error = f"adapter_{type(exc).__name__}"
                failed = _adapter_failure_result(
                    case_id=case.case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    error_code=safe_error,
                )
                proposal_digests[case.case_id].add(failed.proposal_digest)
                outcome_digests[case.case_id].add(failed.outcome_digest)
                results.append(failed)
                continue
            if not isinstance(response, ProposalResponse):
                failed = _adapter_failure_result(
                    case_id=case.case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    error_code="adapter_invalid_response",
                )
                proposal_digests[case.case_id].add(failed.proposal_digest)
                outcome_digests[case.case_id].add(failed.outcome_digest)
                results.append(failed)
                continue
            extracted = extract_claims(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                evidence=normalized.evidence,
                entities=resolved.entities,
                resolution_issues=resolved.issues,
                model_output=response.model_output,
            )
            proposal_digest = _proposal_digest(response.model_output)
            outcome_digest = _outcome_digest(extracted.claims, extracted.issues)
            proposal_digests[case.case_id].add(proposal_digest)
            outcome_digests[case.case_id].add(outcome_digest)
            issue_codes = tuple(sorted(item.code for item in extracted.issues))
            passed = (
                _claim_oracle(case, normalized.evidence, resolved.entities)
                == _actual_claim_outcome(extracted.claims)
                and _expected_issue_outcome(case)
                == _issue_outcome(
                    extracted.issues, normalized.evidence, resolved.entities
                )
            )
            results.append(
                ReplayCaseResult(
                    case_id=case.case_id,
                    repeat_index=repeat_index,
                    request_id=request.request_id,
                    proposal_digest=proposal_digest,
                    outcome_digest=outcome_digest,
                    accepted_claim_count=len(extracted.claims),
                    issue_codes=issue_codes,
                    passed=passed,
                    error_code="",
                    usage=response.usage,
                )
            )

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
    evaluation_passed = (
        len(interpretation_results) == identity.planned_interpretations
        and all(item.passed for item in interpretation_results)
        and len(results) == identity.planned_calls
        and all(item.passed for item in results)
        and not interpretation_variance
        and not proposal_variance
        and not outcome_variance
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
