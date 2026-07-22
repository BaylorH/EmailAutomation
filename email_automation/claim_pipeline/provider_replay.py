"""Pinned semantic adapter for no-effect provider claim replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from .contracts import EntityRef, EvidenceEnvelope
from .extraction import ClaimExtractionRequest, PREDICATE_OUTPUT_CONTRACTS
from .provider_quality_fixtures import SUPPORTED_REVIEW_CATEGORIES
from .replay import ProposalResponse, ProposalUsage


PINNED_PROVIDER_ID = "openai"
PINNED_MODEL_ID = "gpt-5.2-2025-12-11"
PINNED_PROMPT_ID = "sitesift-claim-proposal-2026-07-22-v3"
PINNED_PROMPT = f"""You are the read-only claim proposal stage for a commercial real-estate broker conversation.

Return one JSON object with exactly two arrays: claims and review. Follow outputSchema in the supplied request exactly. Use only supplied evidence, entities, prior claims, and resolution issues.

Rules:
- Every claim must quote an exact, contiguous evidenceText excerpt and use that evidence item's evidenceId.
- Bind property facts only to the one entity explicitly identified by the excerpt. Never borrow facts between target, alternate property, suite, or contact entities.
- Use the evidence actorRole. Do not turn questions, quoted history, signatures, hypotheticals, requirements, or uncertain references into asserted facts.
- A property that fails the user's requirements is not necessarily unavailable. Mark availability unavailable only when the evidence says the property itself is unavailable.
- Use normalized numbers and the units permitted by the schema. Do not infer missing units.
- Follow predicateContracts exactly for value type, enumerated value, unit, polarity, modality, effectiveAt, and correction requirements.
- For a direct explicit current broker statement use confidence 0.99. Lower confidence only when the evidence itself remains ambiguous; never manufacture precision.
- A correction must cite the correcting excerpt and supersede the exact prior claim only when the speaker, property, predicate, old value, and chronology all match.
- Requests, referrals, opt-outs, calls, tours, return dates, and information requests must stay scoped to the entity and wording that expressed them.
- Fresh evidence controls current claims. Do not create a review item merely because quoted, forwarded, or historical text differs from a fresh statement.
- When evidence cannot support a safe claim, omit it. Add a review item only when current evidence itself requires human review and bind it to the relevant evidenceId.
- review.reason must be exactly one of these category tokens: {", ".join(SUPPORTED_REVIEW_CATEGORIES)}.
- Use entity_ambiguity when current evidence or resolutionIssues cannot select one property or suite. Use insufficient_evidence when a current statement names a supported fact but omits a required unit, time basis, or other semantic basis. Do not review merely because quoted, forwarded, or historical text was omitted.
- Never include commentary, markdown, email drafts, actions, or fields outside the schema.
"""
PINNED_PROMPT_HASH = hashlib.sha256(
    (
        PINNED_PROMPT
        + json.dumps(
            PREDICATE_OUTPUT_CONTRACTS,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    ).encode("utf-8")
).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class ProviderTransportResult:
    model_output: object
    usage: ProposalUsage

    def __post_init__(self) -> None:
        if not isinstance(self.usage, ProposalUsage):
            raise TypeError("usage must be ProposalUsage")


class ProviderTransport(Protocol):
    provider_id: str
    model_id: str

    def invoke(
        self,
        *,
        case_id: str,
        instructions: str,
        payload: str,
    ) -> ProviderTransportResult: ...


class PinnedProviderProposalAdapter:
    provider_id = PINNED_PROVIDER_ID
    model_id = PINNED_MODEL_ID
    prompt_id = PINNED_PROMPT_ID
    prompt_hash = PINNED_PROMPT_HASH

    def __init__(self, transport: ProviderTransport):
        if getattr(transport, "provider_id", None) != self.provider_id:
            raise ValueError("provider transport identity is not pinned")
        if getattr(transport, "model_id", None) != self.model_id:
            raise ValueError("provider transport model is not pinned")
        self._transport = transport

    def propose(
        self,
        *,
        case_id: str,
        request: ClaimExtractionRequest,
        evidence: tuple[EvidenceEnvelope, ...],
        entities: tuple[EntityRef, ...],
    ) -> ProposalResponse:
        if request.evidence != tuple(sorted(evidence, key=lambda item: item.evidence_id)):
            raise ValueError("provider proposal evidence does not match the request")
        if request.entities != tuple(sorted(entities, key=lambda item: item.entity_id)):
            raise ValueError("provider proposal entities do not match the request")
        result = self._transport.invoke(
            case_id=case_id,
            instructions=PINNED_PROMPT,
            payload=_canonical_json(request.to_dict()),
        )
        if not isinstance(result, ProviderTransportResult):
            raise TypeError("provider transport returned an invalid result")
        return ProposalResponse(model_output=result.model_output, usage=result.usage)


__all__ = [
    "PINNED_MODEL_ID",
    "PINNED_PROMPT",
    "PINNED_PROMPT_HASH",
    "PINNED_PROMPT_ID",
    "PINNED_PROVIDER_ID",
    "PinnedProviderProposalAdapter",
    "ProviderTransport",
    "ProviderTransportResult",
]
