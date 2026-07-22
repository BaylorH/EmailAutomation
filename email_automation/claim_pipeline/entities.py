"""Deterministic entity resolution for normalized broker evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .contracts import EntityRef, EntityType, EvidenceEnvelope, EvidenceSource


_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+"
    r"(?:(?!(?:Street|Avenue|Boulevard|Parkway|Highway|Road|Drive|Lane|Court|"
    r"St|Ave|Blvd|Pkwy|Hwy|Rd|Dr|Ln|Ct|Way)\b)"
    r"(?!\d{1,6}\b)[A-Za-z0-9][A-Za-z0-9.'-]*\s+){0,6}"
    r"(?:Street|Avenue|Boulevard|Parkway|Highway|Road|Drive|Lane|Court|Way|"
    r"St|Ave|Blvd|Pkwy|Hwy|Rd|Dr|Ln|Ct)"
    r"(?:\s+(?:North|South|East|West|Northeast|Northwest|Southeast|Southwest|"
    r"NE|NW|SE|SW|N|S|E|W))?\b",
    re.IGNORECASE,
)
_SUITE_RE = re.compile(
    r"\b(?:Suite|Ste|Unit|Space)\b\s*#?\s*([A-Za-z0-9][A-Za-z0-9-]*)\b",
    re.IGNORECASE,
)
_NON_ADDRESS_WORDS = frozenset(
    {
        "acres",
        "are",
        "available",
        "docks",
        "feet",
        "has",
        "have",
        "includes",
        "is",
        "on",
        "parking",
        "sf",
        "spaces",
        "square",
        "with",
    }
)
_NON_IDENTIFIERS = frozenset(
    {
        "ARE",
        "AVAILABLE",
        "HAS",
        "HAVE",
        "INCLUDE",
        "INCLUDES",
        "IS",
        "REMAINS",
        "WAS",
        "WERE",
    }
)
_AMBIGUOUS_ALTERNATE_RE = re.compile(
    r"\b(?:the\s+(?:other|alternate)|an\s+(?:other|alternate)|another)"
    r"\s+(?:building|property|space)\b",
    re.IGNORECASE,
)
_SUFFIXES = {
    "n": "north",
    "north": "north",
    "s": "south",
    "south": "south",
    "e": "east",
    "east": "east",
    "w": "west",
    "west": "west",
    "ne": "northeast",
    "northeast": "northeast",
    "nw": "northwest",
    "northwest": "northwest",
    "se": "southeast",
    "southeast": "southeast",
    "sw": "southwest",
    "southwest": "southwest",
    "st": "street",
    "street": "street",
    "ave": "avenue",
    "avenue": "avenue",
    "rd": "road",
    "road": "road",
    "blvd": "boulevard",
    "boulevard": "boulevard",
    "dr": "drive",
    "drive": "drive",
    "ln": "lane",
    "lane": "lane",
    "ct": "court",
    "court": "court",
    "pkwy": "parkway",
    "parkway": "parkway",
    "hwy": "highway",
    "highway": "highway",
    "way": "way",
}


def _require_text(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    return cleaned


def _string_tuple(label: str, values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{label} must be a sequence of strings")
    if not all(isinstance(value, str) for value in values):
        raise TypeError(f"{label} must contain only strings")
    return tuple(_require_text(f"{label} item", value) for value in values)


def _stable_id(prefix: str, payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def canonicalize_address(value: str) -> str:
    """Return a conservative comparison form for a US-style street address."""

    if not isinstance(value, str):
        raise TypeError("address must be a string")
    cleaned = re.sub(r"[^A-Za-z0-9#-]+", " ", value).strip().lower()
    words = cleaned.split()
    return " ".join(_SUFFIXES.get(word, word) for word in words)


def extract_addresses(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise TypeError("evidence content must be a string")
    seen: set[str] = set()
    addresses: list[str] = []
    for match in _ADDRESS_RE.finditer(value):
        canonical = canonicalize_address(match.group(0))
        if _NON_ADDRESS_WORDS.intersection(canonical.split()[1:]):
            continue
        if canonical and canonical not in seen:
            seen.add(canonical)
            addresses.append(canonical)
    return tuple(addresses)


def extract_suites(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise TypeError("evidence content must be a string")
    seen: set[str] = set()
    suites: list[str] = []
    for match in _SUITE_RE.finditer(value):
        suite = match.group(1).upper()
        if suite in _NON_IDENTIFIERS or (suite.isalpha() and len(suite) > 4):
            continue
        if suite not in seen:
            seen.add(suite)
            suites.append(suite)
    return tuple(suites)


@dataclass(frozen=True)
class EntitySeed:
    entity_type: EntityType
    label: str
    canonical_address: str = ""
    suite: str = ""
    relationship: str = "target"
    aliases: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, EntityType):
            raise ValueError("entity_type must be an EntityType value")
        object.__setattr__(self, "label", _require_text("label", self.label))
        extracted_addresses = extract_addresses(self.canonical_address)
        object.__setattr__(
            self,
            "canonical_address",
            (
                extracted_addresses[0]
                if extracted_addresses
                else canonicalize_address(self.canonical_address)
            ),
        )
        if not isinstance(self.suite, str):
            raise TypeError("suite must be a string")
        object.__setattr__(self, "suite", self.suite.strip().upper())
        object.__setattr__(
            self,
            "relationship",
            _require_text("relationship", self.relationship),
        )
        if self.relationship == "target" and self.entity_type is not EntityType.TARGET_PROPERTY:
            raise ValueError("only a target_property seed may have target relationship")
        if self.entity_type is EntityType.TARGET_PROPERTY and self.relationship != "target":
            raise ValueError("target_property seeds must have target relationship")
        object.__setattr__(self, "aliases", _string_tuple("aliases", self.aliases))


@dataclass(frozen=True)
class EntityMatch:
    evidence_id: str
    entity_id: str
    match_kind: str
    confidence: float

    def __post_init__(self) -> None:
        for label in ("evidence_id", "entity_id", "match_kind"):
            _require_text(label, getattr(self, label))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ResolutionIssue:
    issue_id: str
    code: str
    message: str
    evidence_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        for label in ("issue_id", "code", "message"):
            _require_text(label, getattr(self, label))
        object.__setattr__(
            self,
            "evidence_ids",
            _string_tuple("evidence_ids", self.evidence_ids),
        )
        expected = self._identity(self.code, self.message, self.evidence_ids)
        if self.issue_id != expected:
            raise ValueError("resolution issue identity does not match its fields")

    @staticmethod
    def _identity(code: str, message: str, evidence_ids: tuple[str, ...]) -> str:
        return _stable_id(
            "resolution_issue",
            {
                "code": code,
                "message": message,
                "evidence_ids": list(evidence_ids),
            },
        )

    @classmethod
    def create(
        cls,
        *,
        code: str,
        message: str,
        evidence_ids: tuple[str, ...],
    ) -> "ResolutionIssue":
        normalized_ids = tuple(sorted(set(evidence_ids)))
        return cls(
            issue_id=cls._identity(code, message, normalized_ids),
            code=code,
            message=message,
            evidence_ids=normalized_ids,
        )


@dataclass(frozen=True)
class EntityResolutionResult:
    entities: Tuple[EntityRef, ...] = ()
    matches: Tuple[EntityMatch, ...] = ()
    issues: Tuple[ResolutionIssue, ...] = ()

    def __post_init__(self) -> None:
        requirements = (
            ("entities", self.entities, EntityRef),
            ("matches", self.matches, EntityMatch),
            ("issues", self.issues, ResolutionIssue),
        )
        for label, values, expected_type in requirements:
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(item, expected_type) for item in values
            ):
                raise TypeError(f"{label} contains an invalid value")
            object.__setattr__(self, label, tuple(values))


@dataclass
class _EntityAggregate:
    entity_type: EntityType
    label: str
    canonical_address: str
    suite: str
    relationship: str
    evidence_ids: set[str]

    def ref(self, tenant_id: str, campaign_id: str) -> EntityRef:
        return EntityRef.create(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            entity_type=self.entity_type,
            label=self.label,
            canonical_address=self.canonical_address,
            suite=self.suite,
            relationship=self.relationship,
            evidence_ids=tuple(sorted(self.evidence_ids)),
        )


def _confidence(evidence: EvidenceEnvelope) -> float:
    return {
        EvidenceSource.QUOTED_BODY: 0.6,
        EvidenceSource.FORWARDED_BODY: 0.5,
        EvidenceSource.SIGNATURE: 0.9,
    }.get(evidence.source_kind, 1.0)


def resolve_entities(
    *,
    tenant_id: str,
    campaign_id: str,
    seeds: tuple[EntitySeed, ...],
    evidence: tuple[EvidenceEnvelope, ...],
) -> EntityResolutionResult:
    """Bind normalized evidence to explicit entities without policy decisions."""

    tenant_id = _require_text("tenant_id", tenant_id)
    campaign_id = _require_text("campaign_id", campaign_id)
    if not isinstance(seeds, (list, tuple)) or not all(
        isinstance(item, EntitySeed) for item in seeds
    ):
        raise TypeError("seeds must contain only EntitySeed values")
    if not isinstance(evidence, (list, tuple)) or not all(
        isinstance(item, EvidenceEnvelope) for item in evidence
    ):
        raise TypeError("evidence must contain only EvidenceEnvelope values")
    seeds = tuple(seeds)
    evidence = tuple(evidence)
    foreign_tenants = sorted(
        {item.tenant_id for item in evidence if item.tenant_id != tenant_id}
    )
    if foreign_tenants:
        raise ValueError("evidence tenant_id must match resolver tenant_id")
    foreign_campaigns = sorted(
        {item.campaign_id for item in evidence if item.campaign_id != campaign_id}
    )
    if foreign_campaigns:
        raise ValueError("evidence campaign_id must match resolver campaign_id")

    aggregates: dict[str, _EntityAggregate] = {}
    matches: list[EntityMatch] = []
    issues: list[ResolutionIssue] = []
    issue_keys: set[tuple[str, tuple[str, ...]]] = set()

    def register(
        *,
        entity_type: EntityType,
        label: str,
        canonical_address: str = "",
        suite: str = "",
        relationship: str,
        evidence_id: Optional[str] = None,
    ) -> EntityRef:
        provisional = EntityRef.create(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            entity_type=entity_type,
            label=label,
            canonical_address=canonical_address,
            suite=suite,
            relationship=relationship,
        )
        aggregate = aggregates.setdefault(
            provisional.entity_id,
            _EntityAggregate(
                entity_type=entity_type,
                label=label,
                canonical_address=canonical_address,
                suite=suite,
                relationship=relationship,
                evidence_ids=set(),
            ),
        )
        if evidence_id:
            aggregate.evidence_ids.add(evidence_id)
        return provisional

    def add_issue(code: str, message: str, evidence_ids: tuple[str, ...]) -> None:
        normalized_ids = tuple(sorted(set(evidence_ids)))
        key = (code, normalized_ids)
        if key in issue_keys:
            return
        issue_keys.add(key)
        issues.append(
            ResolutionIssue.create(
                code=code,
                message=message,
                evidence_ids=normalized_ids,
            )
        )

    target_candidates: list[tuple[EntitySeed, EntityRef, set[str]]] = []
    for seed in seeds:
        ref = register(
            entity_type=seed.entity_type,
            label=seed.label,
            canonical_address=seed.canonical_address,
            suite=seed.suite,
            relationship=seed.relationship,
        )
        aliases: set[str] = set()
        for value in (seed.label, seed.canonical_address, *seed.aliases):
            if not value.strip():
                continue
            extracted = extract_addresses(value)
            aliases.update(extracted or (canonicalize_address(value),))
        if seed.entity_type is EntityType.TARGET_PROPERTY or seed.relationship == "target":
            target_candidates.append((seed, ref, aliases))

    if len(target_candidates) != 1:
        raise ValueError("resolver requires exactly one target_property seed")
    target = target_candidates[0]
    alternate_addresses: dict[str, set[str]] = {}
    property_context: dict[str, tuple[EntityRef, ...]] = {}

    for item in evidence:
        confidence = _confidence(item)

        if item.actor.email.strip():
            email = item.actor.email.strip().lower()
            contact = register(
                entity_type=EntityType.CONTACT,
                label=email,
                canonical_address=email,
                relationship="contact",
                evidence_id=item.evidence_id,
            )
            matches.append(
                EntityMatch(
                    evidence_id=item.evidence_id,
                    entity_id=contact.entity_id,
                    match_kind="actor_contact",
                    confidence=confidence,
                )
            )

        addresses = extract_addresses(item.content)
        address_refs: list[EntityRef] = []
        for address in addresses:
            matched_target = next(
                (
                    (seed, ref)
                    for seed, ref, aliases in target_candidates
                    if address in aliases
                ),
                None,
            )
            if matched_target:
                seed, ref = matched_target
                register(
                    entity_type=seed.entity_type,
                    label=seed.label,
                    canonical_address=seed.canonical_address,
                    suite=seed.suite,
                    relationship=seed.relationship,
                    evidence_id=item.evidence_id,
                )
                address_refs.append(ref)
                matches.append(
                    EntityMatch(
                        evidence_id=item.evidence_id,
                        entity_id=ref.entity_id,
                        match_kind="target_exact",
                        confidence=confidence,
                    )
                )
                continue

            alternate = register(
                entity_type=EntityType.PROPERTY,
                label=" ".join(part.capitalize() for part in address.split()),
                canonical_address=address,
                relationship="alternate",
                evidence_id=item.evidence_id,
            )
            address_refs.append(alternate)
            alternate_addresses.setdefault(address, set()).add(item.evidence_id)
            matches.append(
                EntityMatch(
                    evidence_id=item.evidence_id,
                    entity_id=alternate.entity_id,
                    match_kind="alternate_address",
                    confidence=confidence,
                )
            )

        property_context[item.evidence_id] = tuple(address_refs)
        suites = extract_suites(item.content)
        suite_parent: Optional[EntityRef] = None
        if len(address_refs) == 1:
            suite_parent = address_refs[0]
        elif not address_refs and item.parent_evidence_id:
            parent_refs = property_context.get(item.parent_evidence_id, ())
            if len(parent_refs) == 1:
                suite_parent = parent_refs[0]
            elif suites:
                add_issue(
                    "unbound_suite",
                    "A child suite reference has no unique parent property.",
                    (item.evidence_id,),
                )
        elif not address_refs:
            suite_parent = target[1]
        elif suites:
            add_issue(
                "unbound_suite",
                "A suite reference cannot be bound to one property.",
                (item.evidence_id,),
            )

        if suite_parent:
            parent_is_target = suite_parent.entity_type is EntityType.TARGET_PROPERTY
            for suite in suites:
                suite_ref = register(
                    entity_type=EntityType.SUITE,
                    label=f"{suite_parent.label} - Suite {suite}",
                    canonical_address=suite_parent.canonical_address,
                    suite=suite,
                    relationship=(
                        "suite_of_target" if parent_is_target else "suite_of_alternate"
                    ),
                    evidence_id=item.evidence_id,
                )
                matches.append(
                    EntityMatch(
                        evidence_id=item.evidence_id,
                        entity_id=suite_ref.entity_id,
                        match_kind="suite_reference",
                        confidence=confidence,
                    )
                )

        if not addresses and _AMBIGUOUS_ALTERNATE_RE.search(item.content):
            add_issue(
                "ambiguous_alternate",
                "Alternate-property language has no explicit address.",
                (item.evidence_id,),
            )

    if len(alternate_addresses) > 1:
        add_issue(
            "multiple_property_candidates",
            "Evidence contains multiple alternate property addresses.",
            tuple(
                evidence_id
                for evidence_ids in alternate_addresses.values()
                for evidence_id in evidence_ids
            ),
        )

    entities = tuple(
        aggregates[entity_id].ref(tenant_id, campaign_id)
        for entity_id in sorted(aggregates)
    )
    unique_matches = {
        (match.evidence_id, match.entity_id, match.match_kind): match for match in matches
    }
    ordered_matches = tuple(unique_matches[key] for key in sorted(unique_matches))
    return EntityResolutionResult(
        entities=entities,
        matches=ordered_matches,
        issues=tuple(sorted(issues, key=lambda issue: issue.issue_id)),
    )


__all__ = [
    "EntityMatch",
    "EntityResolutionResult",
    "EntitySeed",
    "ResolutionIssue",
    "canonicalize_address",
    "extract_addresses",
    "extract_suites",
    "resolve_entities",
]
