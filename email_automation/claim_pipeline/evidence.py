"""Deterministic, read-only normalization of message evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .contracts import (
    Actor,
    ActorRole,
    Direction,
    EvidenceEnvelope,
    EvidenceFreshness,
    EvidenceSource,
)


_FORWARDED_DIVIDER = re.compile(
    r"^(?:-{2,}\s*(?:begin\s+)?forwarded message\s*-{2,}|"
    r"begin forwarded message:)$",
    re.IGNORECASE,
)
_ORIGINAL_DIVIDER = re.compile(
    r"^-{2,}\s*original message\s*-{2,}$",
    re.IGNORECASE,
)
_GMAIL_QUOTE_START = re.compile(r"^On\s.+\swrote:\s*$", re.IGNORECASE)
_OUTLOOK_FROM = re.compile(r"^From:\s*.+", re.IGNORECASE)
_OUTLOOK_SENT = re.compile(r"^Sent:\s*.+", re.IGNORECASE)
_FORWARDED_SUBJECT = re.compile(
    r"^(?:(?:re)\s*:\s*)*(?:fw|fwd)\s*:",
    re.IGNORECASE,
)
_HISTORY_ACTOR = re.compile(
    r"^From:\s*(?P<name>.*?)\s*<(?P<email>[^>\s]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_GMAIL_HISTORY_ACTOR = re.compile(
    r"^On\s+.+?\b(?:AM|PM)\s+(?P<name>[^<\n]+?)\s*"
    r"<(?P<email>[^>\s]+)>\s+wrote:\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _require_text(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    return cleaned


def _optional_text(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value.strip()


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


@dataclass(frozen=True)
class ExternalEvidenceInput:
    """Already-extracted attachment or link text, or an explicit failure."""

    source_kind: EvidenceSource
    location: str
    content: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.source_kind not in (EvidenceSource.ATTACHMENT, EvidenceSource.LINK):
            raise ValueError("external evidence must be attachment or link content")
        object.__setattr__(self, "location", _require_text("location", self.location))
        object.__setattr__(self, "content", _optional_text("content", self.content))
        object.__setattr__(self, "error", _optional_text("error", self.error))
        if bool(self.content) == bool(self.error):
            raise ValueError("external evidence requires exactly one of content or error")


@dataclass(frozen=True)
class RawMessageEvidence:
    """Side-effect-free inputs accepted by the evidence normalizer."""

    tenant_id: str
    campaign_id: str
    message_id: str
    direction: Direction
    actor: Actor
    observed_at: str
    subject: str = ""
    body: str = ""
    signature: str = ""
    external: Tuple[ExternalEvidenceInput, ...] = ()

    def __post_init__(self) -> None:
        for label in ("tenant_id", "campaign_id", "message_id", "observed_at"):
            object.__setattr__(self, label, _require_text(label, getattr(self, label)))
        if not isinstance(self.direction, Direction):
            raise ValueError("direction must be a Direction value")
        if not isinstance(self.actor, Actor):
            raise TypeError("actor must be an Actor")
        for label in ("subject", "body", "signature"):
            value = getattr(self, label)
            if not isinstance(value, str):
                raise TypeError(f"{label} must be a string")
        if not isinstance(self.external, (list, tuple)):
            raise TypeError("external must be a sequence of ExternalEvidenceInput values")
        if not all(isinstance(item, ExternalEvidenceInput) for item in self.external):
            raise TypeError("external must contain only ExternalEvidenceInput values")
        object.__setattr__(self, "external", tuple(self.external))


@dataclass(frozen=True)
class EvidenceFailure:
    failure_id: str
    tenant_id: str
    campaign_id: str
    message_id: str
    source_kind: EvidenceSource
    location: str
    reason: str
    parent_evidence_id: Optional[str] = None

    def __post_init__(self) -> None:
        for label in (
            "failure_id",
            "tenant_id",
            "campaign_id",
            "message_id",
            "location",
            "reason",
        ):
            _require_text(label, getattr(self, label))
        if self.source_kind not in (EvidenceSource.ATTACHMENT, EvidenceSource.LINK):
            raise ValueError("failure source must be attachment or link")
        expected = self._identity(
            tenant_id=self.tenant_id,
            campaign_id=self.campaign_id,
            message_id=self.message_id,
            source_kind=self.source_kind,
            location=self.location,
            reason=self.reason,
            parent_evidence_id=self.parent_evidence_id,
        )
        if self.failure_id != expected:
            raise ValueError("failure identity does not match its source fields")

    @staticmethod
    def _identity(
        *,
        tenant_id: str,
        campaign_id: str,
        message_id: str,
        source_kind: EvidenceSource,
        location: str,
        reason: str,
        parent_evidence_id: Optional[str],
    ) -> str:
        return _stable_id(
            "evidence_failure",
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "message_id": message_id,
                "source_kind": source_kind.value,
                "location": location,
                "reason": reason,
                "parent_evidence_id": parent_evidence_id,
            },
        )

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        campaign_id: str,
        message_id: str,
        source_kind: EvidenceSource,
        location: str,
        reason: str,
        parent_evidence_id: Optional[str] = None,
    ) -> "EvidenceFailure":
        return cls(
            failure_id=cls._identity(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                message_id=message_id,
                source_kind=source_kind,
                location=location,
                reason=reason,
                parent_evidence_id=parent_evidence_id,
            ),
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            message_id=message_id,
            source_kind=source_kind,
            location=location,
            reason=reason,
            parent_evidence_id=parent_evidence_id,
        )


@dataclass(frozen=True)
class EvidenceNormalizationResult:
    evidence: Tuple[EvidenceEnvelope, ...] = ()
    failures: Tuple[EvidenceFailure, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, (list, tuple)) or not all(
            isinstance(item, EvidenceEnvelope) for item in self.evidence
        ):
            raise TypeError("evidence must contain only EvidenceEnvelope values")
        if not isinstance(self.failures, (list, tuple)) or not all(
            isinstance(item, EvidenceFailure) for item in self.failures
        ):
            raise TypeError("failures must contain only EvidenceFailure values")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "failures", tuple(self.failures))


@dataclass(frozen=True)
class _BodyRegion:
    source_kind: EvidenceSource
    start_line: int
    end_line: int
    content: str


def _trimmed_lines(body: str) -> tuple[list[str], int]:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    last = len(lines)
    while last > first and not lines[last - 1].strip():
        last -= 1
    return lines[first:last], first + 1


def _body_regions(body: str, *, forwarded_hint: bool = False) -> tuple[_BodyRegion, ...]:
    lines, line_offset = _trimmed_lines(body)
    if not lines:
        return ()

    regions: list[_BodyRegion] = []
    current_kind = EvidenceSource.FRESH_BODY
    current_start = 0
    gmail_quote = False
    gmail_quote_content_seen = False

    def flush(end_index: int) -> None:
        nonlocal current_start
        start = current_start
        end = end_index
        while start < end and not lines[start].strip():
            start += 1
        while end > start and not lines[end - 1].strip():
            end -= 1
        if start == end:
            return
        regions.append(
            _BodyRegion(
                source_kind=current_kind,
                start_line=line_offset + start,
                end_line=line_offset + end - 1,
                content="\n".join(lines[start:end]),
            )
        )

    index = 0
    while index < len(lines):
        stripped = lines[index].strip()

        if current_kind is EvidenceSource.FORWARDED_BODY:
            index += 1
            continue
        if current_kind is EvidenceSource.QUOTED_BODY and not gmail_quote:
            index += 1
            continue

        if _FORWARDED_DIVIDER.match(stripped):
            flush(index)
            current_kind = EvidenceSource.FORWARDED_BODY
            current_start = index
            index += 1
            continue
        if _ORIGINAL_DIVIDER.match(stripped):
            flush(index)
            current_kind = (
                EvidenceSource.FORWARDED_BODY
                if forwarded_hint
                else EvidenceSource.QUOTED_BODY
            )
            current_start = index
            gmail_quote = False
            index += 1
            continue
        if (
            current_kind is EvidenceSource.FRESH_BODY
            and _OUTLOOK_FROM.match(stripped)
            and index + 1 < len(lines)
            and _OUTLOOK_SENT.match(lines[index + 1].strip())
        ):
            flush(index)
            current_kind = (
                EvidenceSource.FORWARDED_BODY
                if forwarded_hint
                else EvidenceSource.QUOTED_BODY
            )
            current_start = index
            gmail_quote = False
            index += 1
            continue
        if current_kind is EvidenceSource.FRESH_BODY and (
            _GMAIL_QUOTE_START.match(stripped) or stripped.startswith(">")
        ):
            flush(index)
            current_kind = EvidenceSource.QUOTED_BODY
            current_start = index
            gmail_quote = True
            gmail_quote_content_seen = stripped.startswith(">")
            index += 1
            continue
        if current_kind is EvidenceSource.QUOTED_BODY and gmail_quote:
            if stripped.startswith(">"):
                gmail_quote_content_seen = True
                index += 1
                continue
            if not stripped:
                index += 1
                continue
            if gmail_quote_content_seen:
                flush(index)
                current_kind = EvidenceSource.FRESH_BODY
                current_start = index
                gmail_quote = False
                gmail_quote_content_seen = False
                continue

        index += 1

    flush(len(lines))
    return tuple(regions)


def _freshness(source_kind: EvidenceSource) -> EvidenceFreshness:
    if source_kind is EvidenceSource.QUOTED_BODY:
        return EvidenceFreshness.QUOTED
    if source_kind is EvidenceSource.FORWARDED_BODY:
        return EvidenceFreshness.FORWARDED
    return EvidenceFreshness.FRESH


def _envelope(
    raw: RawMessageEvidence,
    *,
    source_kind: EvidenceSource,
    location: str,
    content: str,
    parent_evidence_id: Optional[str] = None,
    actor: Optional[Actor] = None,
    freshness: Optional[EvidenceFreshness] = None,
) -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        tenant_id=raw.tenant_id,
        message_id=raw.message_id,
        source_kind=source_kind,
        location=location,
        content=content,
        direction=raw.direction,
        actor=actor or raw.actor,
        observed_at=raw.observed_at,
        freshness=freshness or _freshness(source_kind),
        parent_evidence_id=parent_evidence_id,
        campaign_id=raw.campaign_id,
    )


def _history_actor(content: str, fallback: Actor) -> Actor:
    match = _HISTORY_ACTOR.search(content) or _GMAIL_HISTORY_ACTOR.search(content)
    if not match:
        return fallback
    email = match.group("email").strip().lower()
    name = match.group("name").strip().strip('"') or email
    return Actor(name=name, email=email, role=ActorRole.UNKNOWN)


def normalize_message_evidence(raw: RawMessageEvidence) -> EvidenceNormalizationResult:
    """Normalize supplied message text without fetching or mutating anything."""

    if not isinstance(raw, RawMessageEvidence):
        raise TypeError("raw must be a RawMessageEvidence")

    evidence: list[EvidenceEnvelope] = []
    if raw.subject.strip():
        subject_freshness = (
            EvidenceFreshness.FORWARDED
            if _FORWARDED_SUBJECT.match(raw.subject.strip())
            else EvidenceFreshness.FRESH
        )
        evidence.append(
            _envelope(
                raw,
                source_kind=EvidenceSource.SUBJECT,
                location="subject",
                content=raw.subject.strip(),
                freshness=subject_freshness,
            )
        )

    regions = _body_regions(
        raw.body,
        forwarded_hint=bool(_FORWARDED_SUBJECT.match(raw.subject.strip())),
    )
    for region in regions:
        evidence.append(
            _envelope(
                raw,
                source_kind=region.source_kind,
                location=f"body:lines-{region.start_line}-{region.end_line}",
                content=region.content,
                actor=(
                    _history_actor(region.content, raw.actor)
                    if region.source_kind
                    in (EvidenceSource.QUOTED_BODY, EvidenceSource.FORWARDED_BODY)
                    else raw.actor
                ),
            )
        )

    first_fresh = next(
        (item for item in evidence if item.source_kind is EvidenceSource.FRESH_BODY),
        None,
    )
    subject = next(
        (item for item in evidence if item.source_kind is EvidenceSource.SUBJECT),
        None,
    )
    parent_id = (first_fresh or subject).evidence_id if first_fresh or subject else None

    # Recreate historical regions once the deterministic parent is known.
    for index, item in enumerate(evidence):
        if item.source_kind in (EvidenceSource.QUOTED_BODY, EvidenceSource.FORWARDED_BODY):
            evidence[index] = _envelope(
                raw,
                source_kind=item.source_kind,
                location=item.location,
                content=item.content,
                parent_evidence_id=parent_id,
                actor=item.actor,
            )

    if raw.signature.strip():
        evidence.append(
            _envelope(
                raw,
                source_kind=EvidenceSource.SIGNATURE,
                location="signature",
                content=raw.signature.strip(),
                parent_evidence_id=parent_id,
            )
        )

    failures: list[EvidenceFailure] = []
    for item in raw.external:
        if item.content:
            evidence.append(
                _envelope(
                    raw,
                    source_kind=item.source_kind,
                    location=item.location,
                    content=item.content,
                    parent_evidence_id=parent_id,
                )
            )
        else:
            failures.append(
                EvidenceFailure.create(
                    tenant_id=raw.tenant_id,
                    campaign_id=raw.campaign_id,
                    message_id=raw.message_id,
                    source_kind=item.source_kind,
                    location=item.location,
                    reason=item.error,
                    parent_evidence_id=parent_id,
                )
            )

    return EvidenceNormalizationResult(evidence=tuple(evidence), failures=tuple(failures))


__all__ = [
    "EvidenceFailure",
    "EvidenceNormalizationResult",
    "ExternalEvidenceInput",
    "RawMessageEvidence",
    "normalize_message_evidence",
]
