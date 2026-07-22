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
PINNED_PROMPT_ID = "sitesift-claim-proposal-2026-07-22-v6"
PINNED_PROMPT = f"""You are the read-only claim proposal stage for a commercial real-estate broker conversation.

Return one JSON object with exactly two arrays: claims and review. Follow outputSchema in the supplied request exactly. Use only supplied evidence, entities, prior claims, and resolution issues.

Rules:
- Inspect all evidence before answering and emit every distinct supported claim. Do not stop after the first fact or intent.
- Every claim must quote an exact, contiguous evidenceText excerpt and use that evidence item's evidenceId.
- Bind property facts only to the one entity explicitly identified by the excerpt. Never borrow facts between target, alternate property, suite, or contact entities.
- Use the evidence actorRole. Do not turn questions, hypotheticals, requirements, or uncertain references into asserted facts.
- Do not emit any claim from quoted, forwarded, or historical evidence. Fresh broker evidence and fresh extracted attachment or link evidence are the only claim sources.
- Identity claims are allowed only from fresh attachment or link evidence that explicitly introduces one alternate property or suite. Use the resolved alternate or suite entity, never for the seeded target, a contact, or an action. Do not emit identity merely because the known target appears in a subject or body.
- For a suite identity, value must be the exact Suite plus its suite token from evidence, such as Suite C, and evidenceText must be that naming span. For an alternate property identity, value and evidenceText must be the explicit address span.
- Fresh resolved attachment or link evidence may also support property facts when the evidence and entity select exactly one subject. Do not request review solely because evidence came from an attachment or link.
- Never emit a claim from signature evidence. Signature names, contact details, addresses, and titles are context only.
- When resolutionIssues or current wording leave more than one possible property or suite, do not emit any claim from that ambiguous evidence; emit one entity_ambiguity review item bound to it.
- A property that fails the user's requirements is not necessarily unavailable. Mark availability unavailable only when the evidence says the property itself is unavailable.
- Use normalized numbers and the units permitted by the schema. Do not infer missing units.
- When a supported numeric fact lacks a required unit, time basis, or semantic basis, do not emit a candidate; emit one insufficient_evidence review item for that evidence.
- Follow predicateContracts exactly for value type, enumerated value, unit, polarity, modality, effectiveAt, and correction requirements.
- For a direct explicit current broker statement use confidence 0.99. Lower confidence only when the evidence itself remains ambiguous; never manufacture precision.
- A correction must cite the correcting excerpt and supersede the exact prior claim only when the speaker, property, predicate, old value, and chronology all match. supersedesClaimId must be the exact claimId from priorClaims, and both new claims use the same subjectEntityId as that prior claim. Emit both claims: the corrected domain claim carries the new normalized value, and the correction claim carries the exact phrase that negates or replaces the old value. Both use corrected modality and the same supersedesClaimId.
- Bind workflow claims to the property or suite they concern, not to a contact or action entity. opt_out, call_request, tour_request, and information_request use value true with requested modality. tour_request evidenceText must contain both the request cue and the tour term; when a clause requests a call and a tour, reuse the complete shared clause for both claims. referral uses an object containing only explicit name, email, or phone values with asserted modality. remediation value and evidenceText use the same exact repair-action clause, including the action such as repair, replace, or remediate, with asserted modality. return_date uses an ISO date and the same effectiveAt date with asserted modality. If one excerpt expresses multiple intents, emit each one as a separate claim.
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
