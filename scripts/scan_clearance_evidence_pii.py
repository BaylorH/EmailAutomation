#!/usr/bin/env python3
"""Fail-closed PII scan for committed production-clearance evidence."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
PROPERTY_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.-]+\s+){0,5}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Court|Ct|Parkway|Pkwy|Highway|Hwy|Way|Circle|Cir|Trail|Trl)\b\.?",
    re.IGNORECASE,
)
RAW_MESSAGE_KEYS = {
    "body",
    "inboundbody",
    "messagebody",
    "messagecontent",
    "messagetext",
    "outboundbody",
    "rawbody",
    "rawmessage",
    "rawmessagebody",
}


@dataclass(frozen=True, order=True)
class ScanFinding:
    source: str
    json_path: str
    kind: str

    def render(self) -> str:
        """Render only location and category; never echo the matched value."""
        return f"{self.source}:{self.json_path}: {self.kind}"


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _normalize_address(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _normalize_approved_addresses(values: Sequence[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        if not value.strip():
            continue
        normalized.add(_normalize_address(value))
        normalized.update(
            _normalize_address(match.group(0))
            for match in PROPERTY_ADDRESS_RE.finditer(value)
        )
    return frozenset(normalized)


def _contains_denied_name(value: str, denied_name: str) -> bool:
    normalized_value = " ".join(
        re.sub(r"[^\w]+", " ", value.casefold()).split()
    )
    normalized_name = " ".join(
        re.sub(r"[^\w]+", " ", denied_name.casefold()).split()
    )
    if not normalized_name:
        return False
    return f" {normalized_name} " in f" {normalized_value} "


def _string_pii_kinds(
    value: str,
    *,
    denied_names: tuple[str, ...],
    approved_addresses: frozenset[str],
) -> set[str]:
    kinds: set[str] = set()
    if EMAIL_RE.search(value):
        kinds.add("email_address")
    if any(_contains_denied_name(value, name) for name in denied_names):
        kinds.add("personal_name")
    if any(
        _normalize_address(match.group(0)) not in approved_addresses
        for match in PROPERTY_ADDRESS_RE.finditer(value)
    ):
        kinds.add("property_address")
    return kinds


def _child_path(parent: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=True)}]"


def _redacted_child_path(parent: str, key: str) -> str:
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{parent}.<redacted-key-{key_hash}>"


def _scan_node(
    node: Any,
    *,
    source: str,
    json_path: str,
    denied_names: tuple[str, ...],
    approved_addresses: frozenset[str],
    findings: set[ScanFinding],
) -> None:
    if isinstance(node, dict):
        for key in sorted(node):
            key_text = str(key)
            key_kinds = _string_pii_kinds(
                key_text,
                denied_names=denied_names,
                approved_addresses=approved_addresses,
            )
            child_path = (
                _redacted_child_path(json_path, key_text)
                if key_kinds
                else _child_path(json_path, key_text)
            )
            for kind in key_kinds:
                findings.add(ScanFinding(source, child_path, kind))
            if _normalize_key(key_text) in RAW_MESSAGE_KEYS:
                findings.add(ScanFinding(source, child_path, "raw_message_body"))
            _scan_node(
                node[key],
                source=source,
                json_path=child_path,
                denied_names=denied_names,
                approved_addresses=approved_addresses,
                findings=findings,
            )
        return

    if isinstance(node, list):
        for index, value in enumerate(node):
            _scan_node(
                value,
                source=source,
                json_path=f"{json_path}[{index}]",
                denied_names=denied_names,
                approved_addresses=approved_addresses,
                findings=findings,
            )
        return

    if not isinstance(node, str):
        return

    for kind in _string_pii_kinds(
        node,
        denied_names=denied_names,
        approved_addresses=approved_addresses,
    ):
        findings.add(ScanFinding(source, json_path, kind))


def _scan_json_document(
    document: Any,
    *,
    source: str,
    json_path: str,
    denied_names: tuple[str, ...],
    approved_addresses: frozenset[str],
    findings: set[ScanFinding],
) -> None:
    if not isinstance(document, dict):
        findings.add(ScanFinding(source, json_path, "parse_error"))
        return
    _scan_node(
        document,
        source=source,
        json_path=json_path,
        denied_names=denied_names,
        approved_addresses=approved_addresses,
        findings=findings,
    )


def scan_evidence_paths(
    paths: Sequence[Path],
    *,
    denied_names: Sequence[str] = (),
    approved_addresses: Sequence[str] = (),
) -> list[ScanFinding]:
    """Return deterministic findings; parse/read failures are findings."""
    normalized_names = tuple(
        sorted(
            {" ".join(name.split()) for name in denied_names if name.strip()},
            key=str.casefold,
        )
    )
    normalized_addresses = _normalize_approved_addresses(approved_addresses)
    findings: set[ScanFinding] = set()

    for raw_path in paths:
        path = Path(raw_path)
        source = str(path)
        try:
            if path.suffix == ".jsonl":
                with path.open(encoding="utf-8") as handle:
                    for line_number, raw_line in enumerate(handle, start=1):
                        if not raw_line.strip():
                            continue
                        line_path = f"$[line:{line_number}]"
                        try:
                            document = json.loads(
                                raw_line,
                                object_pairs_hook=_reject_duplicate_keys,
                            )
                        except (json.JSONDecodeError, ValueError):
                            findings.add(ScanFinding(source, line_path, "parse_error"))
                            continue
                        _scan_json_document(
                            document,
                            source=source,
                            json_path=line_path,
                            denied_names=normalized_names,
                            approved_addresses=normalized_addresses,
                            findings=findings,
                        )
            elif path.suffix == ".json":
                with path.open(encoding="utf-8") as handle:
                    try:
                        document = json.load(
                            handle,
                            object_pairs_hook=_reject_duplicate_keys,
                        )
                    except (json.JSONDecodeError, ValueError):
                        findings.add(ScanFinding(source, "$", "parse_error"))
                        continue
                _scan_json_document(
                    document,
                    source=source,
                    json_path="$",
                    denied_names=normalized_names,
                    approved_addresses=normalized_addresses,
                    findings=findings,
                )
            else:
                findings.add(ScanFinding(source, "$", "unsupported_file_type"))
        except OSError:
            findings.add(ScanFinding(source, "$", "read_error"))

    return sorted(findings)


def _load_runtime_list(path: Path | None) -> list[str]:
    if path is None:
        return []
    with path.open(encoding="utf-8") as handle:
        return [
            line.strip()
            for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reject PII in production-clearance JSON/JSONL evidence."
    )
    parser.add_argument(
        "--deny-names-file",
        type=Path,
        help="runtime-only newline-delimited personal-name deny-list",
    )
    parser.add_argument(
        "--approved-addresses-file",
        type=Path,
        help="runtime-only newline-delimited approved property-address allow-list",
    )
    parser.add_argument("paths", nargs="+", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        denied_names = _load_runtime_list(args.deny_names_file)
        approved_addresses = _load_runtime_list(args.approved_addresses_file)
    except (OSError, UnicodeError):
        print(
            "clearance evidence scan failed: runtime list could not be read",
            file=sys.stderr,
        )
        return 2

    findings = scan_evidence_paths(
        args.paths,
        denied_names=denied_names,
        approved_addresses=approved_addresses,
    )
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
