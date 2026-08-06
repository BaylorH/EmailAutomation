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
        return json.load(handle, object_pairs_hook=_reject_duplicate_keys)


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
    if root.get("schemaVersion") != 1:
        raise ValueError("variant fixture schemaVersion must be 1")
    variants = root.get("scenarioFamilies")
    if not isinstance(variants, list) or not variants:
        raise ValueError("variant fixture scenarioFamilies must be a non-empty list")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for index, record in enumerate(variants):
        _validate_selection_record(record, index)
        if set(record) != EXPECTED_VARIANT_KEYS:
            raise ValueError(f"variant {index} has an unexpected schema")
        if not isinstance(record.get("body"), str) or not record["body"]:
            raise ValueError(f"variant {index} has invalid body")
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
