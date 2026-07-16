#!/usr/bin/env python3
"""Grade the synthetic broker-voice campaign without production side effects."""

import argparse
import json
import os
import re
import sys
import time
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
    "event_types_mismatch",
    "forbidden_term",
    "invalid_proposal",
    "placeholder",
    "reasks_supplied_field",
    "reasks_supplied_value",
    "response_missing",
    "sensitive_event_auto_response",
    "signature",
    "unexpected_response",
}

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
COMPLETE_CLOSE_ACK_PATTERN = re.compile(
    r"\b(?:thank(?:s| you)?|appreciat(?:e|ed)|acknowledg(?:e|ed)|received|got it|noted)\b",
    re.IGNORECASE,
)
COMPLETE_CLOSE_REVIEW_PATTERN = re.compile(
    r"\bwe(?:'ll| will| can| plan to)?\s+(?:review|follow up)\b",
    re.IGNORECASE,
)
COMPLETE_CLOSE_WELCOME_PATTERN = re.compile(
    r"\b(?:please\s+(?:send|share)|feel free(?:\s+to)?|you(?:'re| are) welcome to|"
    r"let us know|reach out|send over)\b",
    re.IGNORECASE,
)
COMPLETE_CLOSE_WELCOME_OBJECT_PATTERN = re.compile(
    r"\bquestions?\b|\b(?:other|additional|alternative|relevant)\s+"
    r"(?:properties|property|options|opportunities|fits)\b",
    re.IGNORECASE,
)


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


def _contains_placeholder(response: str) -> bool:
    return any(pattern.search(response) for pattern in PLACEHOLDER_PATTERNS)


def _contains_semantic_term(response: str, term: str) -> bool:
    words = re.escape(term.strip()).replace(r"\ ", r"\s+")
    return bool(
        words
        and re.search(rf"(?<!\w){words}(?!\w)", response, re.IGNORECASE)
    )


def _complete_close_has_required_quality(response: str) -> bool:
    return all(
        pattern.search(response or "")
        for pattern in (
            COMPLETE_CLOSE_ACK_PATTERN,
            COMPLETE_CLOSE_REVIEW_PATTERN,
            COMPLETE_CLOSE_WELCOME_PATTERN,
            COMPLETE_CLOSE_WELCOME_OBJECT_PATTERN,
        )
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


def _effective_response(expected: dict, raw_response_text: str) -> tuple[str, str]:
    response_mode = expected["response_mode"]
    fallback = expected.get("deterministic_fallback")
    fallback_text = fallback.strip() if isinstance(fallback, str) else ""

    if response_mode == "send":
        return raw_response_text, "raw_proposal" if raw_response_text else "none"
    if response_mode == "null":
        return "", "none"
    if response_mode == "send_or_deterministic_fallback":
        if raw_response_text:
            return raw_response_text, "raw_proposal"
        return (
            fallback_text,
            "deterministic_fallback" if fallback_text else "none",
        )
    if response_mode == "quality_gated_fallback":
        if raw_response_text and _complete_close_has_required_quality(
            raw_response_text
        ):
            return raw_response_text, "raw_proposal"
        return (
            fallback_text,
            "quality_gated_fallback" if fallback_text else "none",
        )
    return "", "none"


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


def grade_case(case: dict, proposal: dict) -> dict:
    """Return a deterministic 0-100 grade and named vetoes for one proposal."""
    vetoes = []

    def veto(name: str) -> None:
        if name not in vetoes:
            vetoes.append(name)

    if not isinstance(proposal, dict):
        proposal = {}
        veto("invalid_proposal")

    expected = case["expect"]
    expected_events = expected["event_types"]
    allowed_optional_events = expected.get("allowed_optional_event_types", [])
    actual_events = _event_types(proposal)
    if not _events_match(
        actual_events,
        expected_events,
        allowed_optional_events,
    ):
        veto("event_types_mismatch")

    raw_response = proposal.get("response_email")
    raw_response_text = (
        raw_response.strip() if isinstance(raw_response, str) else ""
    )
    response_mode = expected["response_mode"]
    fallback_response = expected.get("deterministic_fallback")
    fallback_response_text = (
        fallback_response.strip() if isinstance(fallback_response, str) else ""
    )

    response, effective_response_source = _effective_response(
        expected,
        raw_response_text,
    )

    if response_mode == "send" and not raw_response_text:
        veto("response_missing")
    if response_mode == "null" and raw_response_text:
        veto("unexpected_response")
    if response_mode in {
        "quality_gated_fallback",
        "send_or_deterministic_fallback",
    }:
        if not fallback_response_text:
            veto("response_missing")
    if (raw_response_text or response) and set(actual_events) & SENSITIVE_EVENTS:
        veto("sensitive_event_auto_response")

    if response:
        lowered = response.lower()
        required_terms = list(expected["must_mention"])
        if effective_response_source == "raw_proposal":
            required_terms.extend(expected.get("raw_must_mention", []))
        elif effective_response_source in {
            "deterministic_fallback",
            "quality_gated_fallback",
        }:
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
        "expected_event_types": expected_events,
        "allowed_optional_event_types": allowed_optional_events,
        "actual_event_types": actual_events,
        "response_mode": response_mode,
        "raw_response": raw_response,
        "effective_response": response or None,
        "effective_response_source": effective_response_source,
    }


def run_live_case(case: dict) -> dict:
    """Call only the dry-run proposal function after the safety gate passes."""
    enforce_safety()

    repository_root = str(Path(__file__).resolve().parent.parent)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

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
        grade = grade_case(case, proposal)
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
