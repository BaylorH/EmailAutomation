"""Strict, read-only boundary for evidence-backed model claim proposals."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .claim_validation import CandidateValidationError, validate_extracted_claim
from .contracts import (
    ActorRole,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    EntityRef,
    EvidenceEnvelope,
)
from .entities import ResolutionIssue
from .validation import ContractViolation, validate_claim_bundle


CLAIM_EXTRACTION_SCHEMA_VERSION = 1
MAX_CLAIM_CANDIDATES = 64
MAX_REVIEW_ITEMS = 64
MAX_MODEL_OUTPUT_CHARS = 1_000_000
MAX_EVIDENCE_ITEMS = 128
MAX_ENTITY_ITEMS = 256
MAX_PRIOR_CLAIMS = 512
MAX_RESOLUTION_ISSUES = 128
MAX_SINGLE_EVIDENCE_CHARS = 50_000
MAX_EVIDENCE_CONTENT_CHARS = 250_000
MAX_REQUEST_PAYLOAD_CHARS = 300_000
_ROOT_KEYS = frozenset({"claims", "review"})
_REVIEW_KEYS = frozenset({"evidenceId", "reason"})
_CANDIDATE_KEYS = frozenset(
    {
        "evidenceId",
        "subjectEntityId",
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


def _output_schema() -> dict[str, Any]:
    claim_properties = {
        "evidenceId": {"type": "string", "minLength": 1},
        "subjectEntityId": {"type": "string", "minLength": 1},
        "predicate": {"type": "string", "enum": [item.value for item in ClaimPredicate]},
        "value": {"type": ["string", "number", "boolean", "object"]},
        "evidenceText": {"type": "string", "minLength": 1},
        "actorRole": {"type": "string", "enum": [item.value for item in ActorRole]},
        "polarity": {"type": "string", "enum": [item.value for item in ClaimPolarity]},
        "modality": {"type": "string", "enum": [item.value for item in ClaimModality]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "unit": {"type": ["string", "null"]},
        "effectiveAt": {"type": ["string", "null"]},
        "supersedesClaimId": {"type": ["string", "null"]},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims", "review"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": MAX_CLAIM_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_CANDIDATE_KEYS),
                    "properties": claim_properties,
                },
            },
            "review": {
                "type": "array",
                "maxItems": MAX_REVIEW_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(_REVIEW_KEYS),
                    "properties": {
                        "evidenceId": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                },
            },
        },
    }


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_ready(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _text(value: object, label: str, *, optional: bool = False) -> Optional[str]:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _request_limit_problem(
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
    prior_claims: tuple[Claim, ...],
    resolution_issues: tuple[ResolutionIssue, ...],
) -> Optional[str]:
    limits = (
        (len(evidence), MAX_EVIDENCE_ITEMS, "evidence item"),
        (len(entities), MAX_ENTITY_ITEMS, "entity"),
        (len(prior_claims), MAX_PRIOR_CLAIMS, "prior claim"),
        (len(resolution_issues), MAX_RESOLUTION_ISSUES, "resolution issue"),
    )
    for observed, limit, label in limits:
        if observed > limit:
            return f"Extraction request exceeds the {label} limit."
    if any(len(item.content) > MAX_SINGLE_EVIDENCE_CHARS for item in evidence):
        return "Extraction request exceeds the per-evidence content limit."
    if sum(len(item.content) for item in evidence) > MAX_EVIDENCE_CONTENT_CHARS:
        return "Extraction request exceeds the total evidence content limit."
    serialized_context = {
        "evidence": [item.to_dict() for item in evidence],
        "entities": [item.to_dict() for item in entities],
        "priorClaims": [item.to_dict() for item in prior_claims],
        "resolutionIssues": [
            {
                "issueId": item.issue_id,
                "code": item.code,
                "evidenceIds": list(item.evidence_ids),
            }
            for item in resolution_issues
        ],
    }
    serialized_size = len(
        json.dumps(
            _json_ready(serialized_context),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )
    if serialized_size > MAX_REQUEST_PAYLOAD_CHARS:
        return "Extraction request exceeds the serialized payload limit."
    return None


@dataclass(frozen=True)
class ClaimExtractionRequest:
    request_id: str
    tenant_id: str
    campaign_id: str
    evidence: Tuple[EvidenceEnvelope, ...]
    entities: Tuple[EntityRef, ...]
    prior_claims: Tuple[Claim, ...] = ()
    resolution_issues: Tuple[ResolutionIssue, ...] = ()

    def __post_init__(self) -> None:
        tenant = _text(self.tenant_id, "tenant_id")
        campaign = _text(self.campaign_id, "campaign_id")
        collections = (
            ("evidence", self.evidence, EvidenceEnvelope),
            ("entities", self.entities, EntityRef),
            ("prior_claims", self.prior_claims, Claim),
            ("resolution_issues", self.resolution_issues, ResolutionIssue),
        )
        for label, values, expected_type in collections:
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(item, expected_type) for item in values
            ):
                raise TypeError(f"{label} contains an invalid value")
            object.__setattr__(self, label, tuple(values))
        limit_problem = _request_limit_problem(
            self.evidence,
            self.entities,
            self.prior_claims,
            self.resolution_issues,
        )
        if limit_problem:
            raise ValueError(limit_problem)
        expected = self._identity(
            tenant_id=tenant,
            campaign_id=campaign,
            evidence=self.evidence,
            entities=self.entities,
            prior_claims=self.prior_claims,
            resolution_issues=self.resolution_issues,
        )
        if self.request_id != expected:
            raise ValueError("claim extraction request identity does not match its fields")

    @staticmethod
    def _identity(
        *,
        tenant_id: str,
        campaign_id: str,
        evidence: tuple[EvidenceEnvelope, ...],
        entities: tuple[EntityRef, ...],
        prior_claims: tuple[Claim, ...],
        resolution_issues: tuple[ResolutionIssue, ...],
    ) -> str:
        return _stable_id(
            "claim_request",
            {
                "schema_version": CLAIM_EXTRACTION_SCHEMA_VERSION,
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "evidence_ids": [item.evidence_id for item in evidence],
                "entity_ids": [item.entity_id for item in entities],
                "prior_claim_ids": [item.claim_id for item in prior_claims],
                "resolution_issue_ids": [
                    item.issue_id for item in resolution_issues
                ],
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": CLAIM_EXTRACTION_SCHEMA_VERSION,
            "requestId": self.request_id,
            "tenantId": self.tenant_id,
            "campaignId": self.campaign_id,
            "evidence": [
                {
                    "evidenceId": item.evidence_id,
                    "sourceKind": item.source_kind.value,
                    "freshness": item.freshness.value,
                    "direction": item.direction.value,
                    "actorRole": item.actor.role.value,
                    "observedAt": item.observed_at,
                    "location": item.location,
                    "content": item.content,
                    "parentEvidenceId": item.parent_evidence_id,
                }
                for item in self.evidence
            ],
            "entities": [
                {
                    "entityId": item.entity_id,
                    "entityType": item.entity_type.value,
                    "label": item.label,
                    "canonicalAddress": item.canonical_address,
                    "suite": item.suite,
                    "relationship": item.relationship,
                    "evidenceIds": list(item.evidence_ids),
                }
                for item in self.entities
            ],
            "priorClaims": [item.to_dict() for item in self.prior_claims],
            "resolutionIssues": [
                {
                    "issueId": item.issue_id,
                    "code": item.code,
                    "evidenceIds": list(item.evidence_ids),
                }
                for item in self.resolution_issues
            ],
            "supportedPredicates": [item.value for item in ClaimPredicate],
            "outputSchema": _output_schema(),
        }


@dataclass(frozen=True)
class ClaimExtractionIssue:
    issue_id: str
    code: str
    message: str
    candidate_index: Optional[int] = None
    evidence_ids: Tuple[str, ...] = ()
    entity_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not isinstance(self.message, str):
            raise TypeError("claim extraction issue code and message must be text")
        if not self.code.strip() or not self.message.strip():
            raise ValueError("claim extraction issue requires code and message")
        if self.candidate_index is not None and (
            not isinstance(self.candidate_index, int)
            or isinstance(self.candidate_index, bool)
            or self.candidate_index < 0
        ):
            raise ValueError("claim extraction issue candidate index is invalid")
        for label in ("evidence_ids", "entity_ids"):
            values = getattr(self, label)
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(item, str) and item.strip() for item in values
            ):
                raise TypeError(f"claim extraction issue {label} is invalid")
            object.__setattr__(self, label, tuple(values))
        expected = self._identity(
            code=self.code,
            message=self.message,
            candidate_index=self.candidate_index,
            evidence_ids=self.evidence_ids,
            entity_ids=self.entity_ids,
        )
        if self.issue_id != expected:
            raise ValueError("claim extraction issue identity does not match its fields")

    @staticmethod
    def _identity(
        *,
        code: str,
        message: str,
        candidate_index: Optional[int],
        evidence_ids: tuple[str, ...],
        entity_ids: tuple[str, ...],
    ) -> str:
        return _stable_id(
            "claim_issue",
            {
                "code": code,
                "message": message,
                "candidate_index": candidate_index,
                "evidence_ids": list(evidence_ids),
                "entity_ids": list(entity_ids),
            },
        )

    @classmethod
    def create(
        cls,
        *,
        code: str,
        message: str,
        candidate_index: Optional[int] = None,
        evidence_ids: tuple[str, ...] = (),
        entity_ids: tuple[str, ...] = (),
    ) -> "ClaimExtractionIssue":
        normalized_evidence = tuple(sorted(set(evidence_ids)))
        normalized_entities = tuple(sorted(set(entity_ids)))
        return cls(
            issue_id=cls._identity(
                code=code,
                message=message,
                candidate_index=candidate_index,
                evidence_ids=normalized_evidence,
                entity_ids=normalized_entities,
            ),
            code=code,
            message=message,
            candidate_index=candidate_index,
            evidence_ids=normalized_evidence,
            entity_ids=normalized_entities,
        )


@dataclass(frozen=True)
class ClaimExtractionResult:
    claims: Tuple[Claim, ...] = ()
    issues: Tuple[ClaimExtractionIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.claims, (list, tuple)) or not all(
            isinstance(item, Claim) for item in self.claims
        ):
            raise TypeError("claims contains an invalid value")
        if not isinstance(self.issues, (list, tuple)) or not all(
            isinstance(item, ClaimExtractionIssue) for item in self.issues
        ):
            raise TypeError("issues contains an invalid value")
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "issues", tuple(self.issues))


def _scope_issue(message: str) -> ClaimExtractionResult:
    return ClaimExtractionResult(
        issues=(
            ClaimExtractionIssue.create(
                code="context_scope_mismatch",
                message=message,
            ),
        )
    )


def _check_scope(
    tenant_id: str,
    campaign_id: str,
    evidence: tuple[EvidenceEnvelope, ...],
    entities: tuple[EntityRef, ...],
    prior_claims: tuple[Claim, ...],
    resolution_issues: tuple[ResolutionIssue, ...],
) -> Optional[ClaimExtractionResult]:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        return _scope_issue("Extraction tenant identity is missing.")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        return _scope_issue("Extraction campaign identity is missing.")
    collections = (
        (evidence, EvidenceEnvelope, "evidence"),
        (entities, EntityRef, "entity"),
        (prior_claims, Claim, "prior claim"),
        (resolution_issues, ResolutionIssue, "resolution issue"),
    )
    for values, expected_type, label in collections:
        if not all(isinstance(item, expected_type) for item in values):
            return _scope_issue(f"Extraction context contains an invalid {label}.")
    identities = (
        ([item.evidence_id for item in evidence], "evidence"),
        ([item.entity_id for item in entities], "entity"),
        ([item.claim_id for item in prior_claims], "prior claim"),
        ([item.issue_id for item in resolution_issues], "resolution issue"),
    )
    for values, label in identities:
        if len(values) != len(set(values)):
            return _scope_issue(f"Extraction context contains duplicate {label} IDs.")
    if any(item.tenant_id != tenant_id or item.campaign_id != campaign_id for item in evidence):
        return _scope_issue("Evidence falls outside the extraction tenant or campaign.")
    if any(item.tenant_id != tenant_id or item.campaign_id != campaign_id for item in entities):
        return _scope_issue("Entity falls outside the extraction tenant or campaign.")
    if any(
        item.tenant_id != tenant_id or item.campaign_id != campaign_id
        for item in prior_claims
    ):
        return _scope_issue("Prior claim falls outside the extraction tenant or campaign.")
    if any(not item.actor_email or not item.observed_at for item in prior_claims):
        return _scope_issue("Prior claim lacks extraction authority or chronology metadata.")
    evidence_ids = {item.evidence_id for item in evidence}
    if any(
        evidence_id not in evidence_ids
        for issue in resolution_issues
        for evidence_id in issue.evidence_ids
    ):
        return _scope_issue("Resolution issue references unknown extraction evidence.")
    return None


def build_claim_extraction_request(
    *,
    tenant_id: str,
    campaign_id: str,
    evidence: Iterable[EvidenceEnvelope],
    entities: Iterable[EntityRef],
    prior_claims: Iterable[Claim] = (),
    resolution_issues: Iterable[ResolutionIssue] = (),
) -> ClaimExtractionRequest:
    """Build the deterministic, effect-free payload for one constrained model call."""

    tenant = _text(tenant_id, "tenant_id")
    campaign = _text(campaign_id, "campaign_id")
    evidence_tuple = tuple(sorted(evidence, key=lambda item: item.evidence_id))
    entity_tuple = tuple(sorted(entities, key=lambda item: item.entity_id))
    prior_tuple = tuple(sorted(prior_claims, key=lambda item: item.claim_id))
    resolution_tuple = tuple(
        sorted(resolution_issues, key=lambda item: item.issue_id)
    )
    scope_problem = _check_scope(
        tenant,
        campaign,
        evidence_tuple,
        entity_tuple,
        prior_tuple,
        resolution_tuple,
    )
    if scope_problem:
        raise ValueError(scope_problem.issues[0].message)
    limit_problem = _request_limit_problem(
        evidence_tuple,
        entity_tuple,
        prior_tuple,
        resolution_tuple,
    )
    if limit_problem:
        raise ValueError(limit_problem)
    return ClaimExtractionRequest(
        request_id=ClaimExtractionRequest._identity(
            tenant_id=tenant,
            campaign_id=campaign,
            evidence=evidence_tuple,
            entities=entity_tuple,
            prior_claims=prior_tuple,
            resolution_issues=resolution_tuple,
        ),
        tenant_id=tenant,
        campaign_id=campaign,
        evidence=evidence_tuple,
        entities=entity_tuple,
        prior_claims=prior_tuple,
        resolution_issues=resolution_tuple,
    )


def _issue_for_candidate(
    *,
    code: str,
    message: str,
    index: int,
    raw: object,
) -> ClaimExtractionIssue:
    evidence_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    if isinstance(raw, Mapping):
        evidence_id = raw.get("evidenceId")
        entity_id = raw.get("subjectEntityId")
        if isinstance(evidence_id, str) and evidence_id.strip():
            evidence_ids = (evidence_id.strip(),)
        if isinstance(entity_id, str) and entity_id.strip():
            entity_ids = (entity_id.strip(),)
    return ClaimExtractionIssue.create(
        code=code,
        message=message,
        candidate_index=index,
        evidence_ids=evidence_ids,
        entity_ids=entity_ids,
    )


def _parse_candidate(raw: object) -> Claim:
    if not isinstance(raw, Mapping) or set(raw) != _CANDIDATE_KEYS:
        raise CandidateValidationError(
            "invalid_candidate_schema",
            "Claim candidate does not match the exact output schema.",
        )
    confidence = raw["confidence"]
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise CandidateValidationError(
            "invalid_candidate_value",
            "Claim confidence must be a finite number between zero and one.",
        )
    try:
        unit = _text(raw["unit"], "unit", optional=True)
        effective_at = _text(raw["effectiveAt"], "effectiveAt", optional=True)
        supersedes = _text(
            raw["supersedesClaimId"], "supersedesClaimId", optional=True
        )
        return Claim.create(
            tenant_id="__pending_scope__",
            evidence_id=_text(raw["evidenceId"], "evidenceId"),
            subject_entity_id=_text(raw["subjectEntityId"], "subjectEntityId"),
            predicate=ClaimPredicate(_text(raw["predicate"], "predicate")),
            value=raw["value"],
            evidence_text=_text(raw["evidenceText"], "evidenceText"),
            actor_role=ActorRole(_text(raw["actorRole"], "actorRole")),
            polarity=ClaimPolarity(_text(raw["polarity"], "polarity")),
            modality=ClaimModality(_text(raw["modality"], "modality")),
            confidence=float(confidence),
            unit=unit,
            effective_at=effective_at,
            supersedes_claim_id=supersedes,
        )
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "invalid_candidate_value",
            "Claim candidate contains an invalid typed value.",
        ) from exc


def _with_evidence_context(claim: Claim, envelope: EvidenceEnvelope) -> Claim:
    return Claim.create(
        tenant_id=envelope.tenant_id,
        campaign_id=envelope.campaign_id,
        evidence_id=claim.evidence_id,
        subject_entity_id=claim.subject_entity_id,
        predicate=claim.predicate,
        value=claim.value,
        evidence_text=claim.evidence_text,
        actor_role=claim.actor_role,
        polarity=claim.polarity,
        modality=claim.modality,
        confidence=claim.confidence,
        unit=claim.unit,
        effective_at=claim.effective_at,
        supersedes_claim_id=claim.supersedes_claim_id,
        actor_email=envelope.actor.email,
        observed_at=envelope.observed_at,
    )


def extract_claims(
    *,
    tenant_id: str,
    campaign_id: str,
    evidence: Iterable[EvidenceEnvelope],
    entities: Iterable[EntityRef],
    model_output: object,
    prior_claims: Iterable[Claim] = (),
    resolution_issues: Iterable[ResolutionIssue] = (),
) -> ClaimExtractionResult:
    """Interpret one strict model response into accepted claims or review issues."""

    evidence_tuple = tuple(evidence)
    entity_tuple = tuple(entities)
    prior_tuple = tuple(prior_claims)
    resolution_tuple = tuple(resolution_issues)
    scope_problem = _check_scope(
        tenant_id,
        campaign_id,
        evidence_tuple,
        entity_tuple,
        prior_tuple,
        resolution_tuple,
    )
    if scope_problem:
        return scope_problem
    limit_problem = _request_limit_problem(
        evidence_tuple,
        entity_tuple,
        prior_tuple,
        resolution_tuple,
    )
    if limit_problem:
        return ClaimExtractionResult(
            issues=(
                ClaimExtractionIssue.create(
                    code="request_limit_exceeded",
                    message=limit_problem,
                ),
            )
        )

    if isinstance(model_output, str):
        if len(model_output) > MAX_MODEL_OUTPUT_CHARS:
            return ClaimExtractionResult(
                issues=(
                    ClaimExtractionIssue.create(
                        code="invalid_model_output",
                        message="Model output exceeds the extraction size limit.",
                    ),
                )
            )
        try:
            model_output = json.loads(model_output)
        except json.JSONDecodeError:
            model_output = None
    if not isinstance(model_output, Mapping) or set(model_output) != _ROOT_KEYS:
        return ClaimExtractionResult(
            issues=(
                ClaimExtractionIssue.create(
                    code="invalid_model_output",
                    message="Model output does not match the exact extraction schema.",
                ),
            )
        )
    raw_claims = model_output.get("claims")
    raw_review = model_output.get("review")
    if not isinstance(raw_claims, list) or not isinstance(raw_review, list):
        return ClaimExtractionResult(
            issues=(
                ClaimExtractionIssue.create(
                    code="invalid_model_output",
                    message="Model claims and review fields must be arrays.",
                ),
            )
        )
    if len(raw_claims) > MAX_CLAIM_CANDIDATES or len(raw_review) > MAX_REVIEW_ITEMS:
        return ClaimExtractionResult(
            issues=(
                ClaimExtractionIssue.create(
                    code="invalid_model_output",
                    message="Model output exceeds the extraction item limit.",
                ),
            )
        )

    evidence_by_id = {item.evidence_id: item for item in evidence_tuple}
    entity_by_id = {item.entity_id: item for item in entity_tuple}
    prior_by_id = {item.claim_id: item for item in prior_tuple}
    blocked_evidence_ids = {
        evidence_id
        for issue in resolution_tuple
        for evidence_id in issue.evidence_ids
    }
    issues: list[ClaimExtractionIssue] = []
    accepted: list[Claim] = []
    blocked_claim_evidence_ids: set[str] = set()
    blocked_claim_keys: set[tuple[str, str]] = set()
    block_all_claims = False

    for index, raw in enumerate(raw_review):
        if not isinstance(raw, Mapping) or set(raw) != _REVIEW_KEYS:
            block_all_claims = True
            issues.append(
                ClaimExtractionIssue.create(
                    code="invalid_model_output",
                    message="Model review item does not match the exact schema.",
                    candidate_index=index,
                )
            )
            continue
        evidence_id = raw.get("evidenceId")
        reason = raw.get("reason")
        if (
            not isinstance(evidence_id, str)
            or evidence_id not in evidence_by_id
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
        ):
            if isinstance(evidence_id, str) and evidence_id in evidence_by_id:
                blocked_claim_evidence_ids.add(evidence_id)
            else:
                block_all_claims = True
            issues.append(
                ClaimExtractionIssue.create(
                    code="invalid_model_output",
                    message="Model review item references invalid evidence or reason.",
                    candidate_index=index,
                )
            )
            continue
        blocked_claim_evidence_ids.add(evidence_id)
        issues.append(
            ClaimExtractionIssue.create(
                code="model_requested_review",
                message="Model reported evidence ambiguity requiring review.",
                candidate_index=index,
                evidence_ids=(evidence_id,),
            )
        )

    for index, raw in enumerate(raw_claims):
        try:
            pending = _parse_candidate(raw)
            envelope = evidence_by_id.get(pending.evidence_id)
            if envelope is None:
                raise CandidateValidationError(
                    "unknown_evidence", "Claim references unknown evidence."
                )
            claim = _with_evidence_context(pending, envelope)
            if claim.evidence_id in blocked_evidence_ids:
                raise CandidateValidationError(
                    "unresolved_entity_context",
                    "Claim evidence has an unresolved entity-resolution issue.",
                )
            entity = entity_by_id.get(claim.subject_entity_id)
            if entity is None:
                raise CandidateValidationError(
                    "unknown_entity", "Claim references an unknown subject entity."
                )
            if claim.evidence_text not in envelope.content:
                raise CandidateValidationError(
                    "evidence_span_mismatch",
                    "Claim excerpt is not exactly present in its evidence.",
                )
            if claim.actor_role is not envelope.actor.role:
                raise CandidateValidationError(
                    "actor_authority_mismatch",
                    "Claim actor does not match the evidence actor.",
                )
            validate_extracted_claim(
                claim,
                evidence=envelope,
                entity=entity,
                entities=entity_tuple,
                prior_claims=prior_by_id,
            )
            if claim.claim_id in prior_by_id:
                continue
            accepted.append(claim)
        except CandidateValidationError as exc:
            raw_evidence_id = raw.get("evidenceId") if isinstance(raw, Mapping) else None
            if isinstance(raw_evidence_id, str) and raw_evidence_id in evidence_by_id:
                blocked_claim_evidence_ids.add(raw_evidence_id)
            else:
                block_all_claims = True
            if isinstance(raw, Mapping):
                raw_entity_id = raw.get("subjectEntityId")
                raw_predicate = raw.get("predicate")
                normalized_entity_id = (
                    raw_entity_id.strip() if isinstance(raw_entity_id, str) else ""
                )
                normalized_predicate = (
                    raw_predicate.strip() if isinstance(raw_predicate, str) else ""
                )
                if (
                    normalized_entity_id in entity_by_id
                    and normalized_predicate
                    in {item.value for item in ClaimPredicate}
                ):
                    blocked_claim_keys.add(
                        (normalized_entity_id, normalized_predicate)
                    )
            issues.append(
                _issue_for_candidate(
                    code=exc.code,
                    message=exc.message,
                    index=index,
                    raw=raw,
                )
            )

    unique_claims = {item.claim_id: item for item in accepted}
    grouped: dict[tuple[str, ClaimPredicate], list[Claim]] = {}
    for claim in unique_claims.values():
        grouped.setdefault(
            (claim.subject_entity_id, claim.predicate), []
        ).append(claim)
    conflicted_ids: set[str] = set()
    for group in grouped.values():
        semantics = {
            json.dumps(
                {
                    "value": _json_ready(item.value),
                    "unit": item.unit,
                    "effective_at": item.effective_at,
                    "polarity": item.polarity.value,
                    "modality": item.modality.value,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in group
        }
        if len(semantics) <= 1:
            continue
        conflicted_ids.update(item.claim_id for item in group)
        issues.append(
            ClaimExtractionIssue.create(
                code="conflicting_claims",
                message="Explicit candidates conflict for one subject and predicate.",
                evidence_ids=tuple(item.evidence_id for item in group),
                entity_ids=tuple(item.subject_entity_id for item in group),
            )
        )
    final_claims = tuple(
        sorted(
            (
                item
                for item in unique_claims.values()
                if item.claim_id not in conflicted_ids
                and item.evidence_id not in blocked_claim_evidence_ids
                and (item.subject_entity_id, item.predicate.value)
                not in blocked_claim_keys
                and not block_all_claims
            ),
            key=lambda item: item.claim_id,
        )
    )

    if final_claims:
        try:
            validate_claim_bundle(
                tenant_id=tenant_id,
                evidence=evidence_tuple,
                entities=entity_tuple,
                claims=final_claims,
                known_claim_ids=tuple(prior_by_id),
            )
        except ContractViolation:
            final_claims = ()
            issues.append(
                ClaimExtractionIssue.create(
                    code="bundle_validation_failed",
                    message="Accepted claims failed final provenance validation.",
                )
            )

    return ClaimExtractionResult(
        claims=final_claims,
        issues=tuple(sorted(issues, key=lambda item: item.issue_id)),
    )


__all__ = [
    "CLAIM_EXTRACTION_SCHEMA_VERSION",
    "MAX_CLAIM_CANDIDATES",
    "MAX_ENTITY_ITEMS",
    "MAX_EVIDENCE_CONTENT_CHARS",
    "MAX_EVIDENCE_ITEMS",
    "MAX_PRIOR_CLAIMS",
    "MAX_RESOLUTION_ISSUES",
    "MAX_REQUEST_PAYLOAD_CHARS",
    "MAX_SINGLE_EVIDENCE_CHARS",
    "ClaimExtractionIssue",
    "ClaimExtractionRequest",
    "ClaimExtractionResult",
    "build_claim_extraction_request",
    "extract_claims",
]
