#!/usr/bin/env python3
"""Select one deterministic, never-before-used production browser variant."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "production_browser_conversation_variants.json"
)
EXPECTED_ROOT_KEYS = {
    "schemaVersion",
    "historicalSources",
    "scenarioFamilies",
    "productionUse",
}
EXPECTED_VARIANT_KEYS = {
    "variantId",
    "scenarioFamily",
    "sourceClass",
    "semanticFacts",
    "expectedEvents",
    "forbiddenEvents",
    "expectedReplyPolicy",
    "axes",
    "body",
    "bodySha256",
    "lastProductionUse",
}
EXPECTED_HISTORICAL_SOURCE_KEYS = {
    "sourceId",
    "sourceClass",
    "path",
    "sanitizationPolicy",
}
EXPECTED_AXIS_KEYS = {
    "tone",
    "register",
    "informationOrder",
    "quoteStyle",
    "attachmentBundle",
    "turnTiming",
}
SOURCE_CLASSES = {
    "production_report",
    "production_history",
    "production_model_misread",
    "synthetic_near_miss",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(
            handle,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {value}")


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_string_list(value: Any, field: str, index: int) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not _is_nonempty_string(item) for item in value)
    ):
        raise ValueError(f"variant {index} has invalid {field}")


def _validate_historical_source(source: Any, index: int) -> None:
    if not isinstance(source, dict) or set(source) != EXPECTED_HISTORICAL_SOURCE_KEYS:
        raise ValueError(f"historical source {index} has an unexpected schema")
    for field in ("sourceId", "path"):
        if not _is_nonempty_string(source[field]):
            raise ValueError(f"historical source {index} has invalid {field}")
    if (
        not _is_nonempty_string(source["sourceClass"])
        or source["sourceClass"] not in SOURCE_CLASSES
    ):
        raise ValueError(f"historical source {index} has invalid sourceClass")
    if source["sanitizationPolicy"] != "semantic_facts_only":
        raise ValueError(
            f"historical source {index} has invalid sanitizationPolicy"
        )


def _validate_complete_variant(record: Any, index: int) -> None:
    _validate_selection_record(record, index)
    if set(record) != EXPECTED_VARIANT_KEYS:
        raise ValueError(f"variant {index} has an unexpected schema")
    if (
        not _is_nonempty_string(record["sourceClass"])
        or record["sourceClass"] not in SOURCE_CLASSES
    ):
        raise ValueError(f"variant {index} has invalid sourceClass")
    if not isinstance(record["semanticFacts"], dict) or not record["semanticFacts"]:
        raise ValueError(f"variant {index} has invalid semanticFacts")
    _validate_string_list(record["expectedEvents"], "expectedEvents", index)
    _validate_string_list(record["forbiddenEvents"], "forbiddenEvents", index)
    if not _is_nonempty_string(record["expectedReplyPolicy"]):
        raise ValueError(f"variant {index} has invalid expectedReplyPolicy")
    axes = record["axes"]
    if (
        not isinstance(axes, dict)
        or set(axes) != EXPECTED_AXIS_KEYS
        or any(not _is_nonempty_string(value) for value in axes.values())
    ):
        raise ValueError(f"variant {index} has invalid axes")
    if not _is_nonempty_string(record["body"]):
        raise ValueError(f"variant {index} has invalid body")
    if record["lastProductionUse"] is not None:
        raise ValueError(f"variant {index} has invalid lastProductionUse")


def _validate_selection_record(record: Any, index: int) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"variant {index} must be a JSON object")
    for field in ("variantId", "scenarioFamily", "bodySha256"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"variant {index} has invalid {field}")
    if not SHA256_RE.fullmatch(record["bodySha256"]):
        raise ValueError(f"variant {index} has invalid bodySha256")


def load_variants(path: Path) -> list[dict[str, Any]]:
    """Load and validate the production variant fixture fail-closed."""
    root = _load_json(path)
    if not isinstance(root, dict) or set(root) != EXPECTED_ROOT_KEYS:
        raise ValueError("variant fixture has an unexpected root schema")
    if type(root.get("schemaVersion")) is not int or root["schemaVersion"] != 1:
        raise ValueError("variant fixture schemaVersion must be 1")
    historical_sources = root.get("historicalSources")
    if not isinstance(historical_sources, list) or not historical_sources:
        raise ValueError(
            "variant fixture historicalSources must be a non-empty list"
        )
    for index, source in enumerate(historical_sources):
        _validate_historical_source(source, index)
    if not isinstance(root.get("productionUse"), list):
        raise ValueError("variant fixture productionUse must be a list")
    variants = root.get("scenarioFamilies")
    if not isinstance(variants, list) or not variants:
        raise ValueError("variant fixture scenarioFamilies must be a non-empty list")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, record in enumerate(variants):
        _validate_complete_variant(record, index)
        expected_hash = hashlib.sha256(record["body"].encode("utf-8")).hexdigest()
        if record["bodySha256"] != expected_hash:
            raise ValueError(f"variant {index} bodySha256 does not match body")
        if record["variantId"] in seen_ids:
            raise ValueError(f"duplicate variantId: {record['variantId']}")
        if record["bodySha256"] in seen_hashes:
            raise ValueError("duplicate variant bodySha256")
        seen_ids.add(record["variantId"])
        seen_hashes.add(record["bodySha256"])

    return variants


def load_used_hashes(path: Path) -> set[str]:
    """Read exactBodyHashes from an immutable JSONL checkpoint history."""
    used_hashes: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                checkpoint = json.loads(
                    raw_line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonstandard_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"checkpoint history line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(checkpoint, dict):
                raise ValueError(f"checkpoint history line {line_number} must be an object")
            hashes = checkpoint.get("exactBodyHashes")
            if not isinstance(hashes, list):
                raise ValueError(
                    f"checkpoint history line {line_number} must contain exactBodyHashes"
                )
            for body_hash in hashes:
                if not isinstance(body_hash, str) or not SHA256_RE.fullmatch(body_hash):
                    raise ValueError(
                        f"checkpoint history line {line_number} has invalid body hash"
                    )
                used_hashes.add(body_hash)
    return used_hashes


def select_unused_variant(
    family: str,
    variants: Sequence[dict[str, Any]],
    used_hashes: set[str],
) -> dict[str, Any]:
    """Return the lexicographically first eligible variant for the family."""
    if not isinstance(family, str) or not family:
        raise ValueError("family must be a non-empty string")

    normalized_hashes = set(used_hashes)
    for body_hash in normalized_hashes:
        if not isinstance(body_hash, str) or not SHA256_RE.fullmatch(body_hash):
            raise ValueError("used_hashes contains an invalid SHA-256")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, record in enumerate(variants):
        _validate_selection_record(record, index)
        if record["variantId"] in seen_ids:
            raise ValueError(f"duplicate variantId: {record['variantId']}")
        if record["bodySha256"] in seen_hashes:
            raise ValueError("duplicate variant bodySha256")
        seen_ids.add(record["variantId"])
        seen_hashes.add(record["bodySha256"])

    eligible = [
        record
        for record in variants
        if record["scenarioFamily"] == family
        and record["bodySha256"] not in normalized_hashes
    ]
    if not eligible:
        raise RuntimeError(f"no unused production variant for {family}")
    return sorted(eligible, key=lambda record: record["variantId"])[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print one deterministic unused browser-conversation variant."
    )
    parser.add_argument("family", help="scenarioFamily to select")
    parser.add_argument("checkpoint_history", type=Path, help="immutable JSONL history")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="variant fixture JSON path",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        variants = load_variants(args.fixture)
        used_hashes = load_used_hashes(args.checkpoint_history)
        selected = select_unused_variant(args.family, variants, used_hashes)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"variant selection failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(selected, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
