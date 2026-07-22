"""Strict, sanitized fixtures for no-effect legacy proposal comparison."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .policy_fixtures import PolicyFixtureCatalog


LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION = 1
LEGACY_SHADOW_PROVENANCE_KINDS = frozenset(
    {"historical_probe", "legacy_test_contract", "synthetic_boundary"}
)
LEGACY_SHADOW_DISPOSITIONS = frozenset(
    {
        "equivalent",
        "expected_improvement",
        "deferred_surface",
        "legacy_safety_risk",
        "new_policy_gap",
    }
)
LEGACY_SHADOW_SEVERITIES = frozenset(
    {"none", "info", "warning", "release_blocker"}
)
LEGACY_SHADOW_DISCREPANCY_CODES = frozenset(
    {
        "legacy_automatic_outbound_during_review",
        "legacy_bypasses_recipient_approval",
        "legacy_bypasses_required_review",
        "legacy_market_fit_conflation",
        "legacy_missing_alternate_property_review",
        "legacy_missing_call_request",
        "legacy_missing_policy_fact",
        "legacy_missing_terminal_freeze",
        "legacy_missing_terminal_status",
        "legacy_outbound_after_optout",
        "legacy_terminalizes_nonterminal",
        "legacy_unapproved_recipient_change",
        "legacy_unplanned_fact_mutation",
        "outbound_surface_deferred",
        "policy_adds_waiting_state",
        "row_move_surface_deferred",
        "unclassified_difference",
    }
)

_ROOT_KEYS = frozenset({"schemaVersion", "catalogId", "cases"})
_CASE_KEYS = frozenset(
    {"caseId", "policyCaseId", "provenance", "bindings", "legacyProposal", "expected"}
)
_PROVENANCE_KEYS = frozenset({"kind", "sourceRef"})
_BINDING_KEYS = frozenset(
    {"currentEntity", "eventEntities", "recipientRelation"}
)
_PROPOSAL_KEYS = frozenset(
    {"updates", "events", "responseDraft", "skipResponse"}
)
_UPDATE_KEYS = frozenset({"column", "valueKind"})
_EVENT_KEYS = frozenset({"type", "reason"})
_EXPECTED_KEYS = frozenset(
    {"disposition", "severity", "discrepancyCodes", "discrepancyEntities"}
)
_VALUE_KINDS = frozenset({"text", "number", "boolean"})
_RECIPIENT_RELATIONS = frozenset({"absent", "same", "different"})
_EVENT_TYPES = frozenset(
    {
        "call_requested",
        "property_unavailable",
        "new_property",
        "close_conversation",
        "needs_user_input",
        "contact_optout",
        "wrong_contact",
        "property_issue",
        "tour_requested",
    }
)
_COLUMN_NAMES = frozenset(
    {
        "Availability",
        "Asking Status",
        "Transaction Type",
        "Total SF",
        "Office SF",
        "Rent/SF/Yr",
        "Ops Ex / SF",
        "Power",
        "Clear Height",
        "Drive Ins",
        "Docks",
        "Occupancy Date",
        "Term",
    }
)
_REPORT_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,159}$")


class LegacyShadowFixtureValidationError(ValueError):
    """Raised when a legacy shadow fixture cannot be trusted."""


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyShadowFixtureValidationError(f"{label} must be non-empty text")
    return value.strip()


def _report_safe(value: Any, label: str) -> str:
    cleaned = _required_text(value, label)
    if not _REPORT_SAFE.fullmatch(cleaned):
        raise LegacyShadowFixtureValidationError(
            f"{label} must be a report-safe identifier"
        )
    return cleaned


def _exact_keys(value: Any, expected: frozenset[str], label: str) -> None:
    if not isinstance(value, dict):
        raise LegacyShadowFixtureValidationError(f"{label} must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        raise LegacyShadowFixtureValidationError(
            f"{label} missing keys: {sorted(missing)}"
        )
    if unknown:
        raise LegacyShadowFixtureValidationError(
            f"{label} has unknown keys: {sorted(unknown)}"
        )


def _choice(value: Any, choices: frozenset[str], label: str) -> str:
    cleaned = _required_text(value, label)
    if cleaned not in choices:
        raise LegacyShadowFixtureValidationError(
            f"{label} has unsupported value {cleaned!r}"
        )
    return cleaned


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LegacyShadowFixtureValidationError(f"{label} must be boolean")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LegacyShadowFixtureValidationError(f"{label} must be a list")
    return tuple(_required_text(item, f"{label} item") for item in value)


@dataclass(frozen=True)
class LegacyShadowProvenance:
    kind: str
    source_ref: str


@dataclass(frozen=True)
class LegacyShadowBindings:
    current_entity: str
    event_entities: tuple[str, ...]
    recipient_relation: str


@dataclass(frozen=True)
class LegacyShadowProposal:
    updates: tuple[Mapping[str, Any], ...]
    events: tuple[Mapping[str, Any], ...]
    response_draft: bool
    skip_response: bool


@dataclass(frozen=True)
class LegacyShadowExpectation:
    disposition: str
    severity: str
    discrepancy_codes: tuple[str, ...]
    discrepancy_entities: tuple[str, ...]


@dataclass(frozen=True)
class LegacyShadowFixtureCase:
    case_id: str
    policy_case_id: str
    provenance: LegacyShadowProvenance
    bindings: LegacyShadowBindings
    legacy_proposal: LegacyShadowProposal
    expected: LegacyShadowExpectation


@dataclass(frozen=True)
class LegacyShadowFixtureCatalog:
    schema_version: int
    catalog_id: str
    cases: tuple[LegacyShadowFixtureCase, ...]
    manifest_hash: str


def _validate_case(
    raw: Any,
    index: int,
    *,
    policy_cases: Mapping[str, Any],
) -> LegacyShadowFixtureCase:
    _exact_keys(raw, _CASE_KEYS, f"case {index}")
    case_id = _report_safe(raw["caseId"], f"case {index} caseId")
    policy_case_id = _report_safe(
        raw["policyCaseId"], f"case {case_id} policyCaseId"
    )
    policy_case = policy_cases.get(policy_case_id)
    if policy_case is None:
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} references unknown policy case"
        )
    entity_keys = {str(item["key"]) for item in policy_case.entities}

    provenance_raw = raw["provenance"]
    _exact_keys(provenance_raw, _PROVENANCE_KEYS, f"case {case_id} provenance")
    provenance_kind = _choice(
        provenance_raw["kind"],
        LEGACY_SHADOW_PROVENANCE_KINDS,
        f"case {case_id} provenance kind",
    )
    source_ref = _report_safe(
        provenance_raw["sourceRef"], f"case {case_id} provenance sourceRef"
    )
    if provenance_kind == "synthetic_boundary" and not source_ref.startswith(
        "synthetic/"
    ):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} synthetic sourceRef must start with 'synthetic/'"
        )
    if provenance_kind != "synthetic_boundary" and not source_ref.startswith(
        "tests/"
    ):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} repository sourceRef must start with 'tests/'"
        )

    bindings_raw = raw["bindings"]
    _exact_keys(bindings_raw, _BINDING_KEYS, f"case {case_id} bindings")
    current_entity = _required_text(
        bindings_raw["currentEntity"], f"case {case_id} currentEntity"
    )
    if current_entity not in entity_keys:
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} currentEntity references unknown policy entity"
        )
    event_entities = _string_list(
        bindings_raw["eventEntities"], f"case {case_id} eventEntities"
    )
    for entity in event_entities:
        if entity not in entity_keys:
            raise LegacyShadowFixtureValidationError(
                f"case {case_id} eventEntities references unknown policy entity"
            )
    recipient_relation = _choice(
        bindings_raw["recipientRelation"],
        _RECIPIENT_RELATIONS,
        f"case {case_id} recipientRelation",
    )

    proposal_raw = raw["legacyProposal"]
    _exact_keys(proposal_raw, _PROPOSAL_KEYS, f"case {case_id} legacyProposal")
    updates_raw = proposal_raw["updates"]
    if not isinstance(updates_raw, list):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} updates must be a list"
        )
    updates = []
    for update_index, update in enumerate(updates_raw):
        label = f"case {case_id} update {update_index}"
        _exact_keys(update, _UPDATE_KEYS, label)
        _choice(update["column"], _COLUMN_NAMES, f"{label} column")
        _choice(update["valueKind"], _VALUE_KINDS, f"{label} valueKind")
        updates.append(_freeze(update))

    events_raw = proposal_raw["events"]
    if not isinstance(events_raw, list):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} events must be a list"
        )
    if len(event_entities) != len(events_raw):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} eventEntities must match events length"
        )
    events = []
    for event_index, event in enumerate(events_raw):
        label = f"case {case_id} event {event_index}"
        _exact_keys(event, _EVENT_KEYS, label)
        _choice(event["type"], _EVENT_TYPES, f"{label} type")
        _report_safe(event["reason"], f"{label} reason")
        events.append(_freeze(event))

    response_draft = _boolean(
        proposal_raw["responseDraft"], f"case {case_id} responseDraft"
    )
    skip_response = _boolean(
        proposal_raw["skipResponse"], f"case {case_id} skipResponse"
    )

    expected_raw = raw["expected"]
    _exact_keys(expected_raw, _EXPECTED_KEYS, f"case {case_id} expected")
    disposition = _choice(
        expected_raw["disposition"],
        LEGACY_SHADOW_DISPOSITIONS,
        f"case {case_id} disposition",
    )
    severity = _choice(
        expected_raw["severity"],
        LEGACY_SHADOW_SEVERITIES,
        f"case {case_id} severity",
    )
    discrepancy_codes = _string_list(
        expected_raw["discrepancyCodes"],
        f"case {case_id} discrepancyCodes",
    )
    if tuple(sorted(discrepancy_codes)) != discrepancy_codes:
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} discrepancyCodes must be sorted"
        )
    discrepancy_entities = _string_list(
        expected_raw["discrepancyEntities"],
        f"case {case_id} discrepancyEntities",
    )
    if len(discrepancy_entities) != len(discrepancy_codes):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} discrepancyEntities must match discrepancyCodes length"
        )
    for entity in discrepancy_entities:
        if entity not in entity_keys:
            raise LegacyShadowFixtureValidationError(
                f"case {case_id} discrepancyEntities references unknown policy entity"
            )
    if len(set(zip(discrepancy_codes, discrepancy_entities))) != len(
        discrepancy_codes
    ):
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} expected discrepancy signatures contain duplicates"
        )
    unknown_codes = set(discrepancy_codes) - LEGACY_SHADOW_DISCREPANCY_CODES
    if unknown_codes:
        raise LegacyShadowFixtureValidationError(
            f"case {case_id} has unknown discrepancy codes: {sorted(unknown_codes)}"
        )

    return LegacyShadowFixtureCase(
        case_id=case_id,
        policy_case_id=policy_case_id,
        provenance=LegacyShadowProvenance(provenance_kind, source_ref),
        bindings=LegacyShadowBindings(
            current_entity=current_entity,
            event_entities=event_entities,
            recipient_relation=recipient_relation,
        ),
        legacy_proposal=LegacyShadowProposal(
            updates=tuple(updates),
            events=tuple(events),
            response_draft=response_draft,
            skip_response=skip_response,
        ),
        expected=LegacyShadowExpectation(
            disposition=disposition,
            severity=severity,
            discrepancy_codes=discrepancy_codes,
            discrepancy_entities=discrepancy_entities,
        ),
    )


def load_legacy_shadow_fixture_catalog(
    path: Path | str,
    *,
    policy_catalog: PolicyFixtureCatalog,
) -> LegacyShadowFixtureCatalog:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyShadowFixtureValidationError(
            f"fixture catalog cannot be read: {exc}"
        ) from exc
    _exact_keys(payload, _ROOT_KEYS, "fixture catalog")
    if payload["schemaVersion"] != LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION:
        raise LegacyShadowFixtureValidationError(
            "unsupported legacy shadow fixture schemaVersion"
        )
    catalog_id = _report_safe(payload["catalogId"], "catalogId")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise LegacyShadowFixtureValidationError(
            "fixture catalog cases must be non-empty"
        )
    policy_cases = {case.case_id: case for case in policy_catalog.cases}
    cases = tuple(
        _validate_case(raw, index, policy_cases=policy_cases)
        for index, raw in enumerate(payload["cases"])
    )
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise LegacyShadowFixtureValidationError(
            "fixture catalog has duplicate caseId"
        )
    manifest_hash = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return LegacyShadowFixtureCatalog(
        schema_version=LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION,
        catalog_id=catalog_id,
        cases=cases,
        manifest_hash=manifest_hash,
    )
