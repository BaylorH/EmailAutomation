#!/usr/bin/env python3
"""Grade the synthetic broker-voice campaign without production side effects."""

import argparse
import json
import os
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "broker_voice_large_campaign.json"
)
ALLOWED_PROJECTS = {"sitesift-test", "demo-sitesift"}
SENSITIVE_EVENTS = {
    "call_requested",
    "contact_optout",
    "needs_user_input",
    "property_issue",
    "tour_requested",
    "wrong_contact",
}
SAFETY_VETOES = {
    "event_payload_mismatch",
    "event_types_mismatch",
    "fabricated_update_value",
    "forbidden_term",
    "forbidden_update",
    "formula_update",
    "invalid_proposal",
    "missing_expected_update",
    "placeholder",
    "reasks_supplied_field",
    "reasks_supplied_value",
    "response_missing",
    "sensitive_event_auto_response",
    "signature",
    "unexpected_update",
    "unexpected_response",
}
IGNORABLE_EVENT_METADATA_FIELDS = {"schemaVersion"}
EVENT_CONDITION_KEYS = ("equals", "one_of", "must_mention_any")
OPTIONAL_EVENT_CONDITION_KEYS = (
    "optional_equals",
    "optional_one_of",
    "optional_must_mention_any",
)

FIELD_ALIASES = {
    "Total SF": ("total sf", "square footage", "building size"),
    "Rent/SF /Yr": ("asking rent", "asking rate", "rent"),
    "Ops Ex /SF": ("operating expenses", "ops ex", "nnn", "cam"),
    "Drive Ins": (
        "drive-in count",
        "drive in count",
        "drive-ins",
        "drive ins",
        "grade-level doors",
    ),
    "Docks": ("dock count", "dock doors", "docks"),
    "Ceiling Ht": ("clear height", "ceiling height"),
    "Power": ("power", "electrical service", "amperage", "voltage"),
    "Flyer / Link": ("flyer", "brochure"),
    "Floorplan": ("floor plan", "floorplan"),
}

PLACEHOLDER_PATTERNS = (
    re.compile(r"\[(?:TBD|TODO|[A-Z][A-Z0-9 _/-]{1,60})\]", re.IGNORECASE),
    re.compile(r"<[^>\n]{1,80}>"),
    re.compile(r"\{\{[^}\n]{1,80}\}\}"),
    re.compile(r"\b(?:TBD|TODO)\b", re.IGNORECASE),
)
SIGNATURE_PATTERN = re.compile(
    r"(?im)^\s*(?:best(?: regards)?|kind regards|regards|sincerely),?\s*$"
)
REQUEST_CUE_PATTERN = re.compile(
    r"\b(?:could|can|would|will)\s+you\b"
    r"|\bplease\b"
    r"|\b(?:share|send|provide|confirm|verify|tell me|let me know)\b",
    re.IGNORECASE,
)
WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
NUMBER_PATTERN = re.compile(r"(?<![\w@])\$?(\d[\d,]*(?:\.\d+)?)")
class SafetyError(RuntimeError):
    """Raised before application imports when the synthetic lane is unsafe."""


def safety_errors(environment: Mapping[str, str] = None) -> list[str]:
    environment = os.environ if environment is None else environment
    errors = []
    if not str(environment.get("FIRESTORE_EMULATOR_HOST", "")).strip():
        errors.append("FIRESTORE_EMULATOR_HOST must be present")
    project = environment.get("GOOGLE_CLOUD_PROJECT")
    if project not in ALLOWED_PROJECTS:
        errors.append(
            "GOOGLE_CLOUD_PROJECT must be exactly sitesift-test or demo-sitesift"
        )
    if environment.get("SITESIFT_DISABLE_GRAPH_SENDS") != "1":
        errors.append("SITESIFT_DISABLE_GRAPH_SENDS must equal 1")
    return errors


def enforce_safety(environment: Mapping[str, str] = None) -> None:
    errors = safety_errors(environment)
    if errors:
        raise SafetyError("; ".join(errors))


def _event_types(proposal: dict) -> list[str]:
    events = proposal.get("events", [])
    if not isinstance(events, list):
        return []
    return [
        event.get("type")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("type"), str)
    ]


def _normalize_column(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _normalize_value(value: object) -> str:
    normalized = str(value if value is not None else "").strip().casefold()
    normalized = normalized.replace("\u2019", "'")
    normalized = re.sub(r"(?<=\d),(?=\d)", "", normalized)
    return re.sub(r"\s+", " ", normalized)


def _normalize_power_value(value: object) -> str:
    text = _normalize_value(value)
    measurements = []
    patterns = (
        ("amps", r"\b(\d+(?:\.\d+)?)\s*(?:a|amps?|amperes?)\b"),
        ("volts", r"\b(\d+(?:\.\d+)?)\s*(?:v|volts?)\b"),
        ("kva", r"\b(\d+(?:\.\d+)?)\s*kva\b"),
        ("kw", r"\b(\d+(?:\.\d+)?)\s*kw\b"),
        ("phase", r"\b(\d+)\s*[- ]?phase\b"),
    )
    for unit, pattern in patterns:
        measurements.extend(
            f"{unit}:{_normalize_value(match)}"
            for match in re.findall(pattern, text, re.IGNORECASE)
        )
    if measurements:
        return "|".join(sorted(measurements))
    return re.sub(r"[^a-z0-9.]", "", text)


def _normalize_update_value(column: object, value: object) -> str:
    if _normalize_column(column) == "power":
        return _normalize_power_value(value)
    return _normalize_value(value)


def _runtime_skipped_asset_columns(case: dict) -> set[str]:
    mappings = case.get("column_config", {}).get("mappings", {})
    return {
        _normalize_column(mappings.get(canonical))
        for canonical in ("flyer_link", "floorplan")
        if mappings.get(canonical)
    }


def _grade_updates(case: dict, proposal: dict, veto) -> tuple[list, list]:
    expected_updates = case.get("expected_updates", [])
    raw_updates = proposal.get("updates", [])
    if not isinstance(raw_updates, list):
        veto("invalid_proposal")
        raw_updates = []

    expected_by_column = {
        _normalize_column(update.get("column")): update
        for update in expected_updates
        if isinstance(update, dict)
    }
    actual_by_column = {}
    runtime_skipped_asset_columns = _runtime_skipped_asset_columns(case)
    for update in raw_updates:
        if not isinstance(update, dict):
            veto("invalid_proposal")
            continue
        column_key = _normalize_column(update.get("column"))
        if not column_key:
            veto("invalid_proposal")
            continue
        if column_key in runtime_skipped_asset_columns:
            continue
        if column_key in actual_by_column:
            veto("unexpected_update")
        actual_by_column[column_key] = update

    formula_columns = {
        _normalize_column(case["column_config"].get("mappings", {}).get(canonical))
        for canonical in case["column_config"].get("formulaFields", [])
    }
    forbidden_columns = {
        _normalize_column(column)
        for column in case.get("forbidden_update_columns", [])
    }
    for column_key in actual_by_column:
        if column_key in formula_columns:
            veto("formula_update")
        if column_key in forbidden_columns:
            veto("forbidden_update")
        if column_key not in expected_by_column:
            veto("unexpected_update")

    for column_key, expected_update in expected_by_column.items():
        actual_update = actual_by_column.get(column_key)
        if actual_update is None:
            veto("missing_expected_update")
            continue
        if _normalize_update_value(
            expected_update.get("column"),
            actual_update.get("value"),
        ) != _normalize_update_value(
            expected_update.get("column"),
            expected_update.get("value"),
        ):
            veto("fabricated_update_value")

    return raw_updates, list(actual_by_column.values())


def _event_conditions_match(actual: dict, conditions: dict) -> bool:
    for field, expected_value in conditions.get("equals", {}).items():
        if _normalize_value(actual.get(field)) != _normalize_value(expected_value):
            return False

    for field, accepted_values in conditions.get("one_of", {}).items():
        actual_value = _normalize_value(actual.get(field))
        if actual_value not in {
            _normalize_value(value) for value in accepted_values
        }:
            return False

    for field, semantic_groups in conditions.get("must_mention_any", {}).items():
        actual_value = str(actual.get(field) or "")
        if any(
            not any(
                _contains_semantic_term(actual_value, alias)
                for alias in acceptable_terms
            )
            for acceptable_terms in semantic_groups
        ):
            return False

    for field, expected_value in conditions.get("optional_equals", {}).items():
        if _has_nonempty_payload_value(actual.get(field)) and (
            _normalize_value(actual.get(field)) != _normalize_value(expected_value)
        ):
            return False

    for field, accepted_values in conditions.get("optional_one_of", {}).items():
        if not _has_nonempty_payload_value(actual.get(field)):
            continue
        actual_value = _normalize_value(actual.get(field))
        if actual_value not in {
            _normalize_value(value) for value in accepted_values
        }:
            return False

    for field, semantic_groups in conditions.get(
        "optional_must_mention_any", {}
    ).items():
        if not _has_nonempty_payload_value(actual.get(field)):
            continue
        actual_value = str(actual.get(field) or "")
        if any(
            not any(
                _contains_semantic_term(actual_value, alias)
                for alias in acceptable_terms
            )
            for acceptable_terms in semantic_groups
        ):
            return False
    return True


def _event_contract_fields(expected: dict) -> set[str]:
    fields = {"type"}
    for conditions in [expected, *expected.get("any_of", [])]:
        for key in (*EVENT_CONDITION_KEYS, *OPTIONAL_EVENT_CONDITION_KEYS):
            fields.update(conditions.get(key, {}))
    fields.update(
        set(expected.get("ignored_fields", []))
        & IGNORABLE_EVENT_METADATA_FIELDS
    )
    return fields


def _alternative_field_matches(actual: dict, field: str, alternatives: list[dict]) -> bool:
    for conditions in alternatives:
        for condition_name in (*EVENT_CONDITION_KEYS, *OPTIONAL_EVENT_CONDITION_KEYS):
            field_conditions = conditions.get(condition_name, {})
            if field not in field_conditions:
                continue
            if _event_conditions_match(
                actual,
                {condition_name: {field: field_conditions[field]}},
            ):
                return True
    return False


def _has_nonempty_payload_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _event_payload_matches(actual: dict, expected: dict) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    if _normalize_value(actual.get("type")) != _normalize_value(expected.get("type")):
        return False
    if not _event_conditions_match(actual, expected):
        return False

    alternatives = expected.get("any_of", [])
    if alternatives and not any(
        _event_conditions_match(actual, conditions)
        for conditions in alternatives
    ):
        return False

    alternative_fields = {
        field
        for conditions in alternatives
        for condition_name in (*EVENT_CONDITION_KEYS, *OPTIONAL_EVENT_CONDITION_KEYS)
        for field in conditions.get(condition_name, {})
    }
    if any(
        _has_nonempty_payload_value(actual.get(field))
        and not _alternative_field_matches(actual, field, alternatives)
        for field in alternative_fields
    ):
        return False

    allowed_fields = _event_contract_fields(expected)
    return not any(
        field not in allowed_fields and _has_nonempty_payload_value(value)
        for field, value in actual.items()
    )


def _event_payloads_match(
    actual_events: list,
    expected_events: list,
    optional_events: list,
) -> bool:
    if len(actual_events) < len(expected_events):
        return False
    if not all(
        _event_payload_matches(actual, expected)
        for actual, expected in zip(actual_events, expected_events)
    ):
        return False

    for actual in actual_events[len(expected_events):]:
        matching_contracts = [
            expected
            for expected in optional_events
            if _normalize_value(expected.get("type"))
            == _normalize_value(actual.get("type"))
        ]
        if not any(
            _event_payload_matches(actual, expected)
            for expected in matching_contracts
        ):
            return False
    return True


def _contains_placeholder(response: str) -> bool:
    return any(pattern.search(response) for pattern in PLACEHOLDER_PATTERNS)


def _contains_semantic_term(response: str, term: str) -> bool:
    words = re.escape(term.strip()).replace(r"\ ", r"\s+")
    return bool(
        words
        and re.search(rf"(?<!\w){words}(?!\w)", response, re.IGNORECASE)
    )


def _events_match(
    actual_events: list[str],
    expected_events: list[str],
    allowed_optional_events: list[str],
) -> bool:
    optional_sequences = [[]]
    for event_type in allowed_optional_events:
        optional_sequences += [
            [*sequence, event_type] for sequence in optional_sequences
        ]
    return actual_events in [
        [*expected_events, *sequence] for sequence in optional_sequences
    ]


def _repository_root_on_path() -> None:
    repository_root = str(Path(__file__).resolve().parent.parent)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)


def _load_runtime_helpers(mode: str) -> dict:
    enforce_safety()
    _repository_root_on_path()
    if mode == "offline":
        os.environ.setdefault("E2E_TEST_MODE", "true")

    from email_automation.ai_processing import check_missing_required_fields
    from email_automation.processing import (
        _response_mentions_missing_fields,
        _select_automatic_response_body,
        _select_missing_fields_response_body,
        _select_tour_evidence_question,
        _select_tour_notification_suggested_email,
    )

    return {
        "check_missing_required_fields": check_missing_required_fields,
        "response_mentions_missing_fields": _response_mentions_missing_fields,
        "select_automatic_response_body": _select_automatic_response_body,
        "select_missing_fields_response_body": _select_missing_fields_response_body,
        "select_tour_evidence_question": _select_tour_evidence_question,
        "select_tour_notification_suggested_email": (
            _select_tour_notification_suggested_email
        ),
    }


def _runtime_effective_events(
    case: dict,
    proposal: dict,
    runtime_helpers: dict,
) -> list[dict]:
    raw_events = proposal.get("events", [])
    if not isinstance(raw_events, list):
        return []
    inbound_messages = [
        message for message in case.get("conversation", [])
        if message.get("direction") == "inbound"
    ]
    fresh_message = inbound_messages[-1].get("content", "") if inbound_messages else ""
    events = deepcopy(raw_events)
    for event in events:
        if not isinstance(event, dict) or _normalize_value(event.get("type")) != "tour_requested":
            continue
        question = runtime_helpers["select_tour_evidence_question"](
            fresh_message,
            event.get("question", ""),
        )
        event["question"] = question
        event["suggestedEmail"] = runtime_helpers[
            "select_tour_notification_suggested_email"
        ](
            event.get("suggestedEmail", ""),
            contact_name=case["row"]["contact_name"],
            recipient_email=case["row"]["recipient"],
            question=question,
        )
    return events


def _apply_proposed_updates(case: dict, proposal: dict) -> tuple[list, list, int]:
    cells = case["row"]["cells"]
    header = list(cells)
    row_values = list(cells.values())
    column_indexes = {
        _normalize_column(column): index for index, column in enumerate(header)
    }
    applied_columns = set()
    updates = proposal.get("updates", [])
    if not isinstance(updates, list):
        updates = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        column_key = _normalize_column(update.get("column"))
        if column_key in _runtime_skipped_asset_columns(case):
            continue
        index = column_indexes.get(column_key)
        if index is None:
            continue
        value = update.get("value")
        row_values[index] = "" if value is None else str(value)
        applied_columns.add(column_key)
    return header, row_values, len(applied_columns)


def _resolve_effective_response(
    case: dict,
    proposal: dict,
    runtime_helpers: dict,
) -> dict:
    raw_response = proposal.get("response_email")
    raw_response_text = (
        raw_response.strip() if isinstance(raw_response, str) else ""
    )
    actual_event_types = _event_types(proposal)
    header, row_values, applied_update_count = _apply_proposed_updates(
        case,
        proposal,
    )
    missing_fields = runtime_helpers["check_missing_required_fields"](
        row_values,
        header,
        case["column_config"],
    )

    evidence = {
        "effective_response": None,
        "effective_response_source": "none",
        "missing_fields": missing_fields,
        "applied_update_count": applied_update_count,
    }
    if proposal.get("skip_response"):
        evidence["effective_response_source"] = "skip_response"
        return evidence
    if set(actual_event_types) & SENSITIVE_EVENTS:
        evidence["effective_response_source"] = "sensitive_event"
        return evidence

    has_unavailable = "property_unavailable" in actual_event_types
    if has_unavailable:
        scenario = (
            "nonviable_with_alternative"
            if "new_property" in actual_event_types
            else "nonviable"
        )
        selected = runtime_helpers["select_automatic_response_body"](
            scenario,
            raw_response_text or None,
            case["column_config"],
            case["row"]["contact_name"],
        )
        evidence["effective_response"] = selected
        evidence["effective_response_source"] = (
            "production_automatic_model_copy"
            if raw_response_text and selected == raw_response_text
            else "production_automatic_fallback"
        )
        return evidence

    if missing_fields:
        selected = runtime_helpers["select_missing_fields_response_body"](
            raw_response_text or None,
            missing_fields,
            case["column_config"],
            case["row"]["contact_name"],
        )
        selected_model_copy = bool(
            raw_response_text
            and runtime_helpers["response_mentions_missing_fields"](
                raw_response_text,
                missing_fields,
                case["column_config"],
            )
        )
        evidence["effective_response"] = selected
        evidence["effective_response_source"] = (
            "production_missing_model_copy"
            if selected_model_copy
            else "production_missing_fallback"
        )
        return evidence

    selected = runtime_helpers["select_automatic_response_body"](
        "complete",
        raw_response_text or None,
        case["column_config"],
        case["row"]["contact_name"],
    )
    if raw_response_text and selected == raw_response_text:
        evidence["effective_response"] = selected
        evidence["effective_response_source"] = "production_complete_model_copy"
    elif applied_update_count:
        evidence["effective_response"] = selected
        evidence["effective_response_source"] = "production_complete_fallback"
    else:
        evidence["effective_response_source"] = (
            "zero_update_complete_no_fallback"
        )
    return evidence


def _reasks_supplied_field(response: str, supplied_fields: list[str]) -> bool:
    clauses = re.split(r"(?<=[?.!])\s+|\n+", response.lower())
    for clause in clauses:
        if not ("?" in clause or REQUEST_CUE_PATTERN.search(clause)):
            continue
        for field in supplied_fields:
            aliases = FIELD_ALIASES.get(field, (field.lower(),))
            if any(alias.lower() in clause for alias in aliases):
                return True
    return False


def _numeric_values(text: str) -> set[str]:
    values = set()
    for raw_value in NUMBER_PATTERN.findall(text or ""):
        try:
            value = Decimal(raw_value.replace(",", ""))
        except InvalidOperation:
            continue
        values.add(format(value.normalize(), "f"))
    return values


def _supplied_numeric_values(case: dict) -> set[str]:
    excluded_columns = {
        "Property Address",
        "City",
        "Email",
        "Gross Rent",
        "Flyer / Link",
        "Floorplan",
    }
    source_texts = [
        str(value)
        for column, value in case["row"]["cells"].items()
        if column not in excluded_columns and value not in (None, "")
    ]
    source_texts.extend(
        message.get("content", "")
        for message in case["conversation"]
        if message.get("direction") == "inbound"
    )
    source_texts.extend(
        document.get("text", "") for document in case.get("pdf_manifest", [])
    )
    return set().union(*(_numeric_values(text) for text in source_texts))


def _reasks_supplied_value(response: str, case: dict) -> bool:
    supplied_values = _supplied_numeric_values(case)
    clauses = re.split(r"(?<=[?.!])\s+|\n+", response.lower())
    return any(
        ("?" in clause or REQUEST_CUE_PATTERN.search(clause))
        and bool(_numeric_values(clause) & supplied_values)
        for clause in clauses
    )


def grade_case(
    case: dict,
    proposal: dict,
    runtime_helpers: dict = None,
) -> dict:
    """Return a deterministic 0-100 grade and named vetoes for one proposal."""
    vetoes = []

    def veto(name: str) -> None:
        if name not in vetoes:
            vetoes.append(name)

    if not isinstance(proposal, dict):
        proposal = {}
        veto("invalid_proposal")

    if runtime_helpers is None:
        runtime_helpers = _load_runtime_helpers("offline")

    raw_updates, _normalized_updates = _grade_updates(case, proposal, veto)

    expected = case["expect"]
    expected_events = expected["event_types"]
    allowed_optional_events = expected.get("allowed_optional_event_types", [])
    actual_event_payloads = _runtime_effective_events(
        case,
        proposal,
        runtime_helpers,
    )
    actual_events = _event_types({"events": actual_event_payloads})
    if not _events_match(
        actual_events,
        expected_events,
        allowed_optional_events,
    ):
        veto("event_types_mismatch")
    raw_events = proposal.get("events", [])
    if not isinstance(raw_events, list):
        veto("invalid_proposal")
    if not _event_payloads_match(
        actual_event_payloads,
        case.get("event_payloads", []),
        case.get("optional_event_payloads", []),
    ):
        veto("event_payload_mismatch")

    raw_response = proposal.get("response_email")
    raw_response_text = (
        raw_response.strip() if isinstance(raw_response, str) else ""
    )
    response_mode = expected["response_mode"]
    response_evidence = _resolve_effective_response(
        case,
        proposal,
        runtime_helpers,
    )
    response = response_evidence["effective_response"] or ""
    effective_response_source = response_evidence[
        "effective_response_source"
    ]

    if response_mode == "send" and not response:
        veto("response_missing")
    if response_mode == "null" and raw_response_text:
        veto("unexpected_response")
    if (raw_response_text or response) and set(actual_events) & SENSITIVE_EVENTS:
        veto("sensitive_event_auto_response")

    if response:
        lowered = response.lower()
        required_terms = list(expected["must_mention"])
        if effective_response_source.endswith("_model_copy"):
            required_terms.extend(expected.get("raw_must_mention", []))
        elif effective_response_source.endswith("_fallback"):
            required_terms.extend(expected.get("fallback_must_mention", []))
        if any(term.lower() not in lowered for term in required_terms):
            veto("missing_required_term")
        if any(
            not any(
                _contains_semantic_term(response, alias)
                for alias in acceptable_terms
            )
            for acceptable_terms in expected.get("must_mention_any", [])
        ):
            veto("missing_required_term")
        if any(term.lower() in lowered for term in expected["must_not_mention"]):
            veto("forbidden_term")
        if _contains_placeholder(response):
            veto("placeholder")
        if SIGNATURE_PATTERN.search(response):
            veto("signature")
        if _reasks_supplied_field(response, expected.get("supplied_fields", [])):
            veto("reasks_supplied_field")
        if _reasks_supplied_value(response, case):
            veto("reasks_supplied_value")
        if len(WORD_PATTERN.findall(response)) > expected["max_words"]:
            veto("word_limit")
        if response.count("!") > 1:
            veto("excessive_exclamation")

    score = max(0, 100 - (10 * len(vetoes)))
    return {
        "score": score,
        "vetoes": vetoes,
        "safety_vetoes": [name for name in vetoes if name in SAFETY_VETOES],
        "word_count": len(WORD_PATTERN.findall(response)),
        "raw_word_count": len(WORD_PATTERN.findall(raw_response_text)),
        "expected_event_types": expected_events,
        "allowed_optional_event_types": allowed_optional_events,
        "actual_event_types": actual_events,
        "actual_events": actual_event_payloads,
        "raw_updates": raw_updates,
        "response_mode": response_mode,
        "raw_response": raw_response,
        "effective_response": response or None,
        "effective_response_source": effective_response_source,
        "missing_fields": response_evidence["missing_fields"],
        "applied_update_count": response_evidence["applied_update_count"],
    }


def run_live_case(case: dict) -> dict:
    """Call only the dry-run proposal function after the safety gate passes."""
    enforce_safety()
    _repository_root_on_path()

    from email_automation.ai_processing import propose_sheet_updates

    row = case["row"]
    cells = row["cells"]
    column_config = case["column_config"]
    return propose_sheet_updates(
        uid="synthetic-eval-user",
        client_id="synthetic-eval-client",
        email=row["recipient"],
        sheet_id="synthetic-eval-sheet",
        header=list(cells),
        rownum=row["number"],
        rowvals=list(cells.values()),
        thread_id=f"synthetic-eval-{row['id']}",
        pdf_manifest=case.get("pdf_manifest"),
        contact_name=row["contact_name"],
        conversation=case["conversation"],
        column_config=column_config,
        extraction_fields=column_config["extractionFields"],
        dry_run=True,
    )


def _token_usage(_proposal: dict, mode: str) -> tuple[object, str]:
    if mode == "offline":
        return None, "Unavailable in offline mode because no model call was made."
    return (
        None,
        "Unavailable because propose_sheet_updates does not return provider usage "
        "metadata in dry-run mode.",
    )


def _load_cases() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _markdown_report(report: dict) -> str:
    summary = report["summary"]
    aggregate_token_text = json.dumps(summary["token_usage"], sort_keys=True)
    lines = [
        "# Broker Voice Large Campaign Report",
        "",
        f"- Mode: `{report['mode']}`",
        f"- Rows: {summary['row_count']}",
        f"- Aggregate score: {summary['aggregate_score']}",
        f"- Vetoes: {summary['veto_count']}",
        f"- Safety vetoes: {summary['safety_veto_count']}",
        f"- Runtime: {summary['runtime_ms']} ms",
        f"- Token usage: {aggregate_token_text}",
        f"- Token note: {summary['token_usage_note']}",
        "",
        "| Row | Scenario | Score | Vetoes | Runtime ms | Raw proposal response | Effective response source | Token usage |",
        "|---|---|---:|---|---:|---|---|---|",
    ]
    for row in report["rows"]:
        veto_text = ", ".join(row["vetoes"]) or "none"
        token_text = (
            json.dumps(row["token_usage"], sort_keys=True)
            if row["token_usage"] is not None
            else "null"
        )
        raw_response_text = "present" if row["raw_response"] else "null"
        lines.append(
            f"| {row['row_id']} | {row['scenario']} | {row['score']} | "
            f"{veto_text} | {row['runtime_ms']} | {raw_response_text} | "
            f"{row['effective_response_source']} | {token_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def run_campaign(mode: str, output: Path) -> tuple[dict, int]:
    enforce_safety()
    runtime_helpers = _load_runtime_helpers(mode)
    cases = _load_cases()
    campaign_started = time.perf_counter()
    rows = []

    for case in cases:
        started = time.perf_counter()
        proposal = (
            case["offline_proposal"]
            if mode == "offline"
            else run_live_case(case)
        )
        runtime_ms = round((time.perf_counter() - started) * 1000, 3)
        grade = grade_case(case, proposal, runtime_helpers)
        token_usage, token_note = _token_usage(proposal, mode)
        rows.append(
            {
                "row_id": case["row"]["id"],
                "scenario": case["scenario"],
                "recipient": case["row"]["recipient"],
                **grade,
                "runtime_ms": runtime_ms,
                "token_usage": token_usage,
                "token_usage_note": token_note,
            }
        )

    runtime_ms = round((time.perf_counter() - campaign_started) * 1000, 3)
    veto_count = sum(len(row["vetoes"]) for row in rows)
    safety_veto_count = sum(len(row["safety_vetoes"]) for row in rows)
    available_usage = [
        row["token_usage"] for row in rows if row["token_usage"] is not None
    ]
    aggregate_usage = available_usage if available_usage else None
    aggregate_token_note = (
        "Provider usage is listed per row."
        if available_usage
        else rows[0]["token_usage_note"] if rows else "No rows were graded."
    )
    report = {
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": str(FIXTURE_PATH),
        "safety": {
            "firestore_emulator_host": os.environ["FIRESTORE_EMULATOR_HOST"],
            "google_cloud_project": os.environ["GOOGLE_CLOUD_PROJECT"],
            "graph_sends_disabled": os.environ["SITESIFT_DISABLE_GRAPH_SENDS"] == "1",
        },
        "rows": rows,
        "summary": {
            "row_count": len(rows),
            "aggregate_score": (
                round(sum(row["score"] for row in rows) / len(rows), 2)
                if rows
                else 0
            ),
            "veto_count": veto_count,
            "safety_veto_count": safety_veto_count,
            "runtime_ms": runtime_ms,
            "token_usage": aggregate_usage,
            "token_usage_note": aggregate_token_note,
        },
    }

    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "report.md").write_text(_markdown_report(report), encoding="utf-8")
    return report, 0 if veto_count == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Grade the 22-row synthetic broker-voice campaign."
    )
    parser.add_argument(
        "--mode", choices=("offline", "live-model"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] = None) -> int:
    try:
        enforce_safety()
    except SafetyError as exc:
        print(f"SAFETY BLOCK: {exc}", file=sys.stderr)
        return 2

    args = build_parser().parse_args(argv)
    report, exit_code = run_campaign(args.mode, args.output)
    summary = report["summary"]
    print(
        f"Graded {summary['row_count']} rows; "
        f"aggregate={summary['aggregate_score']}; "
        f"vetoes={summary['veto_count']}; "
        f"safety_vetoes={summary['safety_veto_count']}"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
