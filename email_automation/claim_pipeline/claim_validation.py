"""Predicate-level, side-effect-free validation for extracted claims."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import datetime

from .contracts import (
    ActorRole,
    Claim,
    ClaimModality,
    ClaimPredicate,
    EntityRef,
    EntityType,
    EvidenceEnvelope,
    EvidenceFreshness,
    EvidenceSource,
)


class CandidateValidationError(ValueError):
    """A model candidate cannot safely become an accepted claim."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_HOSTILE_TEXT = re.compile(
    r"\b(?:ignore|disregard|override)\s+(?:all\s+)?(?:previous|prior|system)\s+instructions?\b|"
    r"\b(?:system\s+prompt|developer\s+message|tool\s+call)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
_UNAVAILABLE_WORDS = re.compile(
    r"\b(?:unavailable|not\s+available|no\s+longer\s+available|leased|sold)\b",
    re.IGNORECASE,
)
_FIT_ONLY_WORDS = re.compile(
    r"\b(?:not\s+(?:a\s+)?fit|does(?:n't|\s+not)\s+meet|too\s+(?:small|large)|"
    r"outside\s+(?:your|the)\s+(?:range|requirement)|space\s+requirement)\b",
    re.IGNORECASE,
)
_RENT_WORDS = re.compile(r"\b(?:rent|rental|asking|base\s+rate)\b", re.IGNORECASE)
_OPEX_WORDS = re.compile(
    r"\b(?:op\s*ex|opex|operating\s+expenses?|cam|nnn)\b", re.IGNORECASE
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
_SF_WORDS = re.compile(r"(?:\bSF\b|sq\.?\s*ft|square\s+feet|square\s+foot)", re.IGNORECASE)
_YEAR_WORDS = re.compile(r"(?:/\s*yr\b|per\s+year|annual(?:ly)?|\byearly\b)", re.IGNORECASE)
_MONTH_WORDS = re.compile(r"(?:/\s*mo(?:nth)?\b|per\s+month|\bmonthly\b)", re.IGNORECASE)
_REQUEST_CUES = {
    ClaimPredicate.OPT_OUT: re.compile(
        r"\b(?:stop|unsubscribe|remove\s+me|"
        r"do\s+not\s+(?:email|contact)|don't\s+(?:email|contact))\b",
        re.IGNORECASE,
    ),
    ClaimPredicate.CALL_REQUEST: re.compile(
        r"\b(?:(?:please|kindly)\s+(?:give\s+(?:me|us)\s+a\s+)?(?:call|phone)|"
        r"(?:can|could|would)\s+(?:you|we|i)\b.{0,40}\b(?:call|phone)|"
        r"(?:call|phone)\s+(?:me|us))\b",
        re.IGNORECASE,
    ),
    ClaimPredicate.TOUR_REQUEST: re.compile(
        r"\b(?:(?:please|kindly)\b.{0,40}\b(?:tour|showing|walkthrough|site\s+visit)|"
        r"(?:can|could|would)\s+(?:you|we|i)\b.{0,40}\b"
        r"(?:tour|showing|walkthrough|site\s+visit)|"
        r"(?:schedule|arrange|book|request)\b.{0,30}\b"
        r"(?:tour|showing|walkthrough|site\s+visit))\b",
        re.IGNORECASE,
    ),
    ClaimPredicate.INFORMATION_REQUEST: re.compile(
        r"\b(?:(?:please|kindly)\s+(?:send|share|provide|confirm)|"
        r"(?:can|could|would)\s+you\s+(?:send|share|provide|confirm)|"
        r"(?:send|share|provide)\s+(?:me|us)|"
        r"(?:what|who|when|where|which|how)\b.{0,120}\?)",
        re.IGNORECASE,
    ),
}

_FACT_PREDICATES = frozenset(
    {
        ClaimPredicate.AVAILABILITY,
        ClaimPredicate.ASKING_STATUS,
        ClaimPredicate.TRANSACTION_TYPE,
        ClaimPredicate.TOTAL_SF,
        ClaimPredicate.OFFICE_SF,
        ClaimPredicate.RENT,
        ClaimPredicate.OPERATING_EXPENSES,
        ClaimPredicate.POWER,
        ClaimPredicate.CLEAR_HEIGHT,
        ClaimPredicate.DRIVE_INS,
        ClaimPredicate.DOCKS,
        ClaimPredicate.OCCUPANCY_DATE,
        ClaimPredicate.TERM,
        ClaimPredicate.REMEDIATION,
        ClaimPredicate.RETURN_DATE,
    }
)
_REQUEST_PREDICATES = frozenset(
    {
        ClaimPredicate.OPT_OUT,
        ClaimPredicate.CALL_REQUEST,
        ClaimPredicate.TOUR_REQUEST,
        ClaimPredicate.INFORMATION_REQUEST,
    }
)
_CURRENCY_BASIS_UNITS = frozenset(
    {
        "usd_per_sf_year",
        "usd_per_sf_month",
        "usd_month",
        "usd_year",
    }
)


def _fail(code: str, message: str) -> None:
    raise CandidateValidationError(code, message)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_positive_number(value: object) -> None:
    if not _is_number(value) or float(value) <= 0:
        _fail("invalid_predicate_value", "Predicate requires a positive finite number.")


def _require_nonnegative_integer(value: object) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        _fail("invalid_predicate_value", "Predicate requires a non-negative integer.")


def _require_unit(claim: Claim, allowed: frozenset[str]) -> None:
    if claim.unit not in allowed:
        _fail("invalid_predicate_unit", "Predicate has a missing or unsupported unit basis.")


def _require_no_unit(claim: Claim) -> None:
    if claim.unit is not None:
        _fail("invalid_predicate_unit", "Predicate does not accept a unit.")


def _require_number_in_evidence(claim: Claim) -> None:
    expected = float(claim.value)
    observed = tuple(
        float(match.group(0).replace(",", ""))
        for match in _NUMBER.finditer(claim.evidence_text)
    )
    if not any(math.isclose(expected, value, rel_tol=1e-9, abs_tol=1e-9) for value in observed):
        _fail(
            "predicate_evidence_mismatch",
            "Numeric claim value is not explicitly present in its evidence excerpt.",
        )


def _require_number_bound_to_label(
    claim: Claim,
    own_label: re.Pattern[str],
    competing_label: re.Pattern[str] | None = None,
) -> None:
    numbers = tuple(
        (float(match.group(0).replace(",", "")), match.start())
        for match in _NUMBER.finditer(claim.evidence_text)
    )
    if len(numbers) <= 1:
        return
    own_positions = tuple(match.start() for match in own_label.finditer(claim.evidence_text))
    if not own_positions:
        _fail(
            "predicate_evidence_mismatch",
            "Numeric claim cannot be bound to its predicate label.",
        )
    expected = float(claim.value)
    expected_positions = tuple(
        position
        for value, position in numbers
        if math.isclose(expected, value, rel_tol=1e-9, abs_tol=1e-9)
    )
    other_positions = tuple(
        position
        for value, position in numbers
        if not math.isclose(expected, value, rel_tol=1e-9, abs_tol=1e-9)
    )
    competing_positions = (
        tuple(match.start() for match in competing_label.finditer(claim.evidence_text))
        if competing_label is not None
        else ()
    )
    for position in expected_positions:
        own_distance = min(abs(position - label) for label in own_positions)
        other_distance = min(
            (abs(other - label) for other in other_positions for label in own_positions),
            default=math.inf,
        )
        competing_distance = min(
            (abs(position - label) for label in competing_positions),
            default=math.inf,
        )
        if own_distance <= other_distance and own_distance < competing_distance:
            return
    _fail(
        "predicate_evidence_mismatch",
        "Numeric claim is closer to another value or predicate label.",
    )


def _valid_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return False
    year, month, day = (int(part) for part in value.split("-"))
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return day <= days[month]


def _require_currency_basis_in_evidence(claim: Claim) -> None:
    excerpt = claim.evidence_text
    unit = claim.unit or ""
    if "per_sf" in unit and not _SF_WORDS.search(excerpt):
        _fail("predicate_evidence_mismatch", "Claimed per-SF basis is absent from evidence.")
    if unit.endswith("_year") and not _YEAR_WORDS.search(excerpt):
        _fail("predicate_evidence_mismatch", "Claimed annual basis is absent from evidence.")
    if unit.endswith("_month") and not _MONTH_WORDS.search(excerpt):
        _fail("predicate_evidence_mismatch", "Claimed monthly basis is absent from evidence.")


def _validate_subject_binding(
    claim: Claim,
    entity: EntityRef,
    entities: tuple[EntityRef, ...],
) -> None:
    if claim.predicate in _FACT_PREDICATES and entity.entity_type not in {
        EntityType.TARGET_PROPERTY,
        EntityType.PROPERTY,
        EntityType.SUITE,
    }:
        _fail(
            "subject_evidence_mismatch",
            "Property fact must be bound to a property or suite subject.",
        )
    bound = tuple(
        item
        for item in entities
        if claim.evidence_id in item.evidence_ids
        and item.entity_type is not EntityType.CONTACT
    )
    bound_suites = tuple(item for item in bound if item.entity_type is EntityType.SUITE)
    if entity.relationship == "target" and bound_suites:
        _fail(
            "subject_evidence_mismatch",
            "Suite-bound evidence cannot be assigned to the whole target property.",
        )
    if len(bound) > 1:
        normalized_excerpt = re.sub(
            r"[^a-z0-9]+", " ", claim.evidence_text.casefold()
        ).strip()

        def mentioned(item: EntityRef) -> bool:
            candidates = [item.label, item.canonical_address]
            if item.suite:
                candidates.extend(
                    (f"suite {item.suite}", f"ste {item.suite}", f"unit {item.suite}")
                )
            normalized = {
                re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                for value in candidates
                if value.strip()
            }
            return any(value and value in normalized_excerpt for value in normalized)

        matches = tuple(item for item in bound if mentioned(item))
        suite_matches = tuple(
            item for item in matches if item.entity_type is EntityType.SUITE
        )
        if suite_matches:
            matches = suite_matches
        if len(matches) != 1 or matches[0].entity_id != entity.entity_id:
            _fail(
                "subject_evidence_mismatch",
                "Claim excerpt does not uniquely name its subject entity.",
            )
    if claim.evidence_id in entity.evidence_ids:
        return
    if entity.relationship == "target" and not bound:
        return
    _fail(
        "subject_evidence_mismatch",
        "Claim subject is not supported by the named evidence.",
    )


def _validate_correction(
    claim: Claim,
    prior_claims: Mapping[str, Claim],
    evidence_content: str,
) -> None:
    is_correction = (
        claim.modality is ClaimModality.CORRECTED
        or claim.predicate is ClaimPredicate.CORRECTION
        or claim.supersedes_claim_id is not None
    )
    if not is_correction:
        return
    if claim.modality is not ClaimModality.CORRECTED or not claim.supersedes_claim_id:
        _fail("invalid_correction", "Correction must name the claim it supersedes.")
    prior = prior_claims.get(claim.supersedes_claim_id)
    if prior is None:
        _fail("invalid_correction", "Correction references an unknown earlier claim.")
    if prior.tenant_id != claim.tenant_id or prior.subject_entity_id != claim.subject_entity_id:
        _fail("invalid_correction", "Correction crosses tenant or subject identity.")
    if prior.campaign_id != claim.campaign_id:
        _fail("invalid_correction", "Correction crosses campaign identity.")
    if not prior.actor_email or prior.actor_email != claim.actor_email:
        _fail("invalid_correction", "Correction crosses authoritative speaker identity.")
    try:
        prior_time = datetime.fromisoformat(prior.observed_at.replace("Z", "+00:00"))
        claim_time = datetime.fromisoformat(claim.observed_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        _fail("invalid_correction", "Correction chronology is missing or invalid.")
    if prior_time.tzinfo is None or claim_time.tzinfo is None:
        _fail("invalid_correction", "Correction chronology must include a timezone.")
    if prior_time >= claim_time:
        _fail("invalid_correction", "Correction does not follow the superseded claim.")
    if claim.predicate is not ClaimPredicate.CORRECTION and prior.predicate is not claim.predicate:
        _fail("invalid_correction", "Correction changes predicate identity.")
    if _is_number(prior.value):
        names_old_value = False
        for match in _NUMBER.finditer(evidence_content):
            value = float(match.group(0).replace(",", ""))
            if not math.isclose(
                float(prior.value), value, rel_tol=1e-9, abs_tol=1e-9
            ):
                continue
            before = evidence_content[max(0, match.start() - 32) : match.start()]
            after = evidence_content[match.end() : match.end() + 24]
            if re.search(
                r"(?:\bnot|\bfrom|\binstead\s+of|\brather\s+than|"
                r"\bprevious(?:ly)?|\bwas)\s*[$:]?\s*$",
                before,
                re.IGNORECASE,
            ) or re.search(
                r"^\s*(?:was|is)?\s*(?:wrong|incorrect|outdated)\b",
                after,
                re.IGNORECASE,
            ):
                names_old_value = True
                break
        if not names_old_value:
            _fail(
                "invalid_correction",
                "Numeric correction does not identify the value it supersedes.",
            )


def _validate_predicate(claim: Claim) -> None:
    predicate = claim.predicate
    value = claim.value

    if predicate is ClaimPredicate.AVAILABILITY:
        _require_no_unit(claim)
        if not isinstance(value, str) or value not in {
            "available",
            "unavailable",
            "conditional",
        }:
            _fail("invalid_predicate_value", "Availability value is unsupported.")
        excerpt = claim.evidence_text
        if value == "available" and _UNAVAILABLE_WORDS.search(excerpt):
            _fail(
                "predicate_evidence_mismatch",
                "Negative availability evidence cannot support an available claim.",
            )
        if value == "available" and not re.search(r"\bavailable\b", excerpt, re.IGNORECASE):
            _fail(
                "predicate_evidence_mismatch",
                "Available claim lacks explicit availability evidence.",
            )
        if value == "unavailable" and not _UNAVAILABLE_WORDS.search(excerpt):
            _fail(
                "predicate_evidence_mismatch",
                "Unavailable claim lacks explicit unavailable evidence.",
            )
        if value == "conditional" and not (
            re.search(r"\bavailable\b", excerpt, re.IGNORECASE)
            and re.search(
                r"\b(?:conditional(?:ly)?|contingent|subject\s+to|may|might|potentially)\b",
                excerpt,
                re.IGNORECASE,
            )
        ):
            _fail(
                "predicate_evidence_mismatch",
                "Conditional availability lacks explicit conditional availability evidence.",
            )
        if (
            value == "unavailable"
            and _FIT_ONLY_WORDS.search(excerpt)
            and not _UNAVAILABLE_WORDS.search(excerpt)
        ):
            _fail(
                "predicate_evidence_mismatch",
                "Requirements mismatch evidence does not establish unavailability.",
            )
        expected_polarity = "negative" if value == "unavailable" else "positive"
        if claim.polarity.value != expected_polarity:
            _fail("invalid_polarity", "Availability polarity contradicts its value.")
        return

    if predicate is ClaimPredicate.ASKING_STATUS:
        _require_no_unit(claim)
        if not isinstance(value, str) or value not in {
            "asking",
            "negotiable",
            "not_asking",
        }:
            _fail("invalid_predicate_value", "Asking-status value is unsupported.")
        cue = {
            "asking": r"\basking\b",
            "negotiable": r"\bnegotiable\b",
            "not_asking": r"\b(?:not|no)\s+asking\b",
        }[value]
        if not re.search(cue, claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Asking status is absent from evidence.")
        negative_asking = re.search(
            r"\b(?:not|no)\s+asking\b", claim.evidence_text, re.IGNORECASE
        )
        if value == "asking" and negative_asking:
            _fail(
                "predicate_evidence_mismatch",
                "Negative asking evidence cannot support a positive asking status.",
            )
        expected_polarity = "negative" if value == "not_asking" else "positive"
        if claim.polarity.value != expected_polarity:
            _fail("invalid_polarity", "Asking-status polarity contradicts its value.")
        return

    if predicate is ClaimPredicate.TRANSACTION_TYPE:
        _require_no_unit(claim)
        if not isinstance(value, str) or value not in {"lease", "sublease", "sale"}:
            _fail("invalid_predicate_value", "Transaction type is unsupported.")
        if not re.search(rf"\b{re.escape(value)}\b", claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Transaction type is absent from evidence.")
        return

    if predicate in {ClaimPredicate.TOTAL_SF, ClaimPredicate.OFFICE_SF}:
        _require_positive_number(value)
        _require_unit(claim, frozenset({"sf"}))
        _require_number_in_evidence(claim)
        if not _SF_WORDS.search(claim.evidence_text):
            _fail("predicate_evidence_mismatch", "Area unit is absent from evidence.")
        if re.search(
            r"(?:\$|\bUSD\b|\brent\b|\bopex\b|operating\s+expense)",
            claim.evidence_text,
            re.IGNORECASE,
        ):
            _fail("predicate_evidence_mismatch", "Pricing evidence cannot support an area claim.")
        if predicate is ClaimPredicate.OFFICE_SF and not re.search(
            r"\boffice\b", claim.evidence_text, re.IGNORECASE
        ):
            _fail("predicate_evidence_mismatch", "Office-area label is absent from evidence.")
        own_label = (
            re.compile(r"\boffice\b", re.IGNORECASE)
            if predicate is ClaimPredicate.OFFICE_SF
            else re.compile(r"\b(?:total|building|space|available)\b", re.IGNORECASE)
        )
        competing = (
            re.compile(r"\b(?:total|building|space|available)\b", re.IGNORECASE)
            if predicate is ClaimPredicate.OFFICE_SF
            else re.compile(r"\boffice\b", re.IGNORECASE)
        )
        _require_number_bound_to_label(claim, own_label, competing)
        return

    if predicate in {ClaimPredicate.RENT, ClaimPredicate.OPERATING_EXPENSES}:
        _require_positive_number(value)
        _require_unit(claim, _CURRENCY_BASIS_UNITS)
        _require_number_in_evidence(claim)
        if not re.search(r"(?:\$|\bUSD\b)", claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Currency is absent from pricing evidence.")
        _require_currency_basis_in_evidence(claim)
        if (
            predicate is ClaimPredicate.RENT
            and _OPEX_WORDS.search(claim.evidence_text)
            and not _RENT_WORDS.search(claim.evidence_text)
        ):
            _fail("predicate_evidence_mismatch", "Operating-expense evidence cannot support rent.")
        if (
            predicate is ClaimPredicate.OPERATING_EXPENSES
            and _RENT_WORDS.search(claim.evidence_text)
            and not _OPEX_WORDS.search(claim.evidence_text)
        ):
            _fail("predicate_evidence_mismatch", "Rent evidence cannot support operating expenses.")
        own_label = _RENT_WORDS if predicate is ClaimPredicate.RENT else _OPEX_WORDS
        competing = _OPEX_WORDS if predicate is ClaimPredicate.RENT else _RENT_WORDS
        _require_number_bound_to_label(claim, own_label, competing)
        return

    if predicate is ClaimPredicate.POWER:
        _require_positive_number(value)
        _require_unit(claim, frozenset({"amps", "volts", "kva"}))
        _require_number_in_evidence(claim)
        unit_words = {
            "amps": r"\b(?:amps?|amperes?)\b",
            "volts": r"\b(?:volts?|v)\b",
            "kva": r"\bkva\b",
        }
        if not re.search(
            unit_words[claim.unit or ""], claim.evidence_text, re.IGNORECASE
        ):
            _fail("predicate_evidence_mismatch", "Power unit is absent from evidence.")
        own_label = re.compile(unit_words[claim.unit or ""], re.IGNORECASE)
        competing_units = "|".join(
            pattern
            for unit, pattern in unit_words.items()
            if unit != claim.unit
        )
        _require_number_bound_to_label(
            claim,
            own_label,
            re.compile(competing_units, re.IGNORECASE),
        )
        return

    if predicate is ClaimPredicate.CLEAR_HEIGHT:
        _require_positive_number(value)
        _require_unit(claim, frozenset({"ft"}))
        _require_number_in_evidence(claim)
        if not re.search(r"\b(?:clear|ceiling)\b", claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Clear-height label is absent from evidence.")
        if not re.search(r"\b(?:ft|feet|foot)\b", claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Clear-height unit is absent from evidence.")
        _require_number_bound_to_label(
            claim,
            re.compile(r"\b(?:clear|ceiling)\b", re.IGNORECASE),
            re.compile(r"\b(?:roof|peak|max(?:imum)?)\b", re.IGNORECASE),
        )
        return

    if predicate in {ClaimPredicate.DRIVE_INS, ClaimPredicate.DOCKS}:
        _require_nonnegative_integer(value)
        _require_unit(claim, frozenset({"count"}))
        _require_number_in_evidence(claim)
        cue = (
            r"\bdrive(?:-|\s)?ins?\b"
            if predicate is ClaimPredicate.DRIVE_INS
            else r"\bdocks?\b"
        )
        if not re.search(cue, claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Count predicate label is absent from evidence.")
        own_label = re.compile(cue, re.IGNORECASE)
        competing = re.compile(
            r"\bdocks?\b" if predicate is ClaimPredicate.DRIVE_INS else r"\bdrive(?:-|\s)?ins?\b",
            re.IGNORECASE,
        )
        _require_number_bound_to_label(claim, own_label, competing)
        return

    if predicate in {ClaimPredicate.OCCUPANCY_DATE, ClaimPredicate.RETURN_DATE}:
        _require_no_unit(claim)
        if not _valid_iso_date(value):
            _fail("invalid_predicate_value", "Date predicate requires ISO YYYY-MM-DD.")
        if value not in claim.evidence_text:
            _fail("predicate_evidence_mismatch", "Date value is absent from its evidence excerpt.")
        return

    if predicate is ClaimPredicate.TERM:
        _require_positive_number(value)
        _require_unit(claim, frozenset({"months", "years"}))
        _require_number_in_evidence(claim)
        cue = (
            re.compile(r"\bmonths?\b", re.IGNORECASE)
            if claim.unit == "months"
            else re.compile(r"\byears?\b", re.IGNORECASE)
        )
        if not cue.search(claim.evidence_text):
            _fail("predicate_evidence_mismatch", "Term unit is absent from evidence.")
        if not re.search(r"\bterm\b", claim.evidence_text, re.IGNORECASE):
            _fail("predicate_evidence_mismatch", "Term label is absent from evidence.")
        _require_number_bound_to_label(
            claim,
            re.compile(r"\bterm\b", re.IGNORECASE),
            re.compile(r"\b(?:option|renewal)\b", re.IGNORECASE),
        )
        return

    if predicate in _REQUEST_PREDICATES:
        _require_no_unit(claim)
        if value is not True:
            _fail("invalid_predicate_value", "Request predicate requires boolean true.")
        if claim.modality is not ClaimModality.REQUESTED:
            _fail("invalid_modality", "Request predicate requires requested modality.")
        if not _REQUEST_CUES[predicate].search(claim.evidence_text):
            _fail("predicate_evidence_mismatch", "Request intent is absent from evidence.")
        return

    if predicate in {ClaimPredicate.IDENTITY, ClaimPredicate.REFERRAL}:
        _require_no_unit(claim)
        if not isinstance(value, (str, Mapping)) or not value:
            _fail("invalid_predicate_value", "Identity predicate requires explicit identity data.")
        if isinstance(value, Mapping):
            allowed_keys = (
                frozenset({"name", "email", "phone", "address"})
                if predicate is ClaimPredicate.IDENTITY
                else frozenset({"name", "email", "phone"})
            )
            if set(value) - allowed_keys:
                _fail("invalid_predicate_value", "Identity data contains unsupported keys.")
        explicit_values = (
            (value,)
            if isinstance(value, str)
            else tuple(item for item in value.values() if isinstance(item, str) and item.strip())
        )
        if not explicit_values or not all(
            item.casefold() in claim.evidence_text.casefold() for item in explicit_values
        ):
            _fail("predicate_evidence_mismatch", "Identity value is absent from evidence.")
        return

    if predicate in {ClaimPredicate.REMEDIATION, ClaimPredicate.CORRECTION}:
        _require_no_unit(claim)
        if not isinstance(value, str) or not value.strip():
            _fail("invalid_predicate_value", "Predicate requires non-empty text.")
        if value.strip().casefold() not in claim.evidence_text.casefold():
            _fail("predicate_evidence_mismatch", "Claim value is absent from evidence.")
        return

    _fail("invalid_predicate_value", "Predicate has no deterministic validator.")


def validate_claim_semantics(claim: Claim) -> None:
    """Validate a claim's predicate/value/excerpt contract without message context."""

    if not isinstance(claim, Claim):
        raise TypeError("claim must be a Claim")
    _validate_predicate(claim)


def validate_claim_subject_binding(
    claim: Claim,
    entity: EntityRef,
    entities: Iterable[EntityRef],
) -> None:
    """Validate only a claim's subject/evidence binding contract."""

    if not isinstance(claim, Claim) or not isinstance(entity, EntityRef):
        raise TypeError("claim and entity must use claim-pipeline contracts")
    entity_tuple = tuple(entities)
    if not all(isinstance(item, EntityRef) for item in entity_tuple):
        raise TypeError("entities must contain EntityRef values")
    _validate_subject_binding(claim, entity, entity_tuple)


def is_fit_only_availability_evidence(value: str) -> bool:
    """Return whether text describes fit requirements without unavailability."""

    if not isinstance(value, str):
        raise TypeError("value must be text")
    return bool(_FIT_ONLY_WORDS.search(value) and not _UNAVAILABLE_WORDS.search(value))


def validate_extracted_claim(
    claim: Claim,
    *,
    evidence: EvidenceEnvelope,
    entity: EntityRef,
    entities: Iterable[EntityRef],
    prior_claims: Mapping[str, Claim],
) -> None:
    """Validate one constructed claim without changing state."""

    entity_tuple = tuple(entities)
    if claim.campaign_id != evidence.campaign_id:
        _fail("context_scope_mismatch", "Claim campaign does not match evidence campaign.")
    if claim.actor_email != evidence.actor.email.strip().casefold():
        _fail("actor_authority_mismatch", "Claim actor identity does not match evidence actor.")
    if claim.observed_at != evidence.observed_at:
        _fail("context_scope_mismatch", "Claim chronology does not match evidence chronology.")
    if claim.actor_role is not evidence.actor.role:
        _fail("actor_authority_mismatch", "Claim actor does not match evidence actor.")
    if evidence.freshness is not EvidenceFreshness.FRESH:
        _fail("historical_instruction", "Historical evidence cannot become a current claim.")
    if claim.confidence < 0.8:
        _fail("low_confidence", "Low-confidence claim requires review.")
    if claim.actor_role in {ActorRole.UNKNOWN, ActorRole.SYSTEM}:
        _fail("unauthorized_actor", "Actor is not authorized to assert this claim.")
    if claim.predicate in _FACT_PREDICATES and claim.actor_role is not ActorRole.BROKER:
        _fail("unauthorized_actor", "Only broker evidence may assert property facts.")
    if claim.predicate in _FACT_PREDICATES and claim.modality is ClaimModality.REQUESTED:
        _fail("invalid_modality", "A question cannot be accepted as an asserted property fact.")
    if _HOSTILE_TEXT.search(evidence.content):
        _fail("hostile_evidence", "Instruction-like untrusted evidence requires review.")
    if evidence.source_kind is EvidenceSource.SIGNATURE and claim.predicate not in {
        ClaimPredicate.IDENTITY,
        ClaimPredicate.REFERRAL,
    }:
        _fail(
            "invalid_source_for_predicate",
            "Signature evidence cannot assert a property or workflow claim.",
        )
    if evidence.source_kind is EvidenceSource.SUBJECT and claim.predicate in _REQUEST_PREDICATES:
        _fail(
            "invalid_source_for_predicate",
            "Subject text cannot assert a current workflow request.",
        )
    if claim.effective_at is not None and not _valid_iso_date(claim.effective_at):
        _fail("invalid_predicate_value", "Claim effective date is invalid.")
    _validate_subject_binding(claim, entity, entity_tuple)
    _validate_predicate(claim)
    _validate_correction(claim, prior_claims, evidence.content)


__all__ = [
    "CandidateValidationError",
    "is_fit_only_availability_evidence",
    "validate_claim_semantics",
    "validate_claim_subject_binding",
    "validate_extracted_claim",
]
