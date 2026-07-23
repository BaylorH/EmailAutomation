#!/usr/bin/env python3
"""Produce deterministic no-effect evidence from sanitized adapter fixtures."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from email_automation.claim_pipeline.effect_adapter_fixtures import (
    EffectAdapterFixtureValidationError,
    load_effect_adapter_fixture_catalog,
    run_effect_adapter_fixture_case,
)


PROFILE = "disabled-effect-adapter-dry-run-v1"
REQUIRED_RUNS = 3
_SOURCE_PATHS = (
    "email_automation/claim_pipeline",
    "scripts/run_claim_pipeline_effect_adapter_dry_run.py",
    "tests/fixtures/claim_pipeline_effect_adapter_cases.json",
    "tests/test_claim_pipeline_effect_adapter.py",
    "tests/test_claim_pipeline_effect_adapter_fixtures.py",
    "tests/test_claim_pipeline_effect_adapter_report.py",
)
_EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
_FORBIDDEN_REPORT_TOKENS = (
    "payload",
    "recipient",
    "evidencetext",
    "row-fixture",
)


class DryRunReportError(RuntimeError):
    """A privacy-safe fail-closed runner error."""


def _git(*args: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=REPO_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DryRunReportError("repository identity check failed") from exc
    return completed.stdout


def _source_tree_dirty() -> bool:
    return bool(
        _git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).strip()
    )


def _code_revision() -> str:
    try:
        revision = _git("rev-parse", "HEAD").decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise DryRunReportError("repository revision is invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DryRunReportError("repository revision is invalid")
    return revision


def _source_tree_hash(revision: str) -> str:
    tree = _git(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
        "--",
        *_SOURCE_PATHS,
    )
    if not tree:
        raise DryRunReportError("committed source surface is empty")
    digest = hashlib.sha256()
    revision_bytes = revision.encode("ascii")
    digest.update(len(revision_bytes).to_bytes(8, "big"))
    digest.update(revision_bytes)
    digest.update(len(tree).to_bytes(8, "big"))
    digest.update(tree)
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DryRunReportError("fixture catalog cannot be read") from exc
    return hashlib.sha256(content).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _receipt_digest(receipt_id: str) -> str:
    return hashlib.sha256(receipt_id.encode("utf-8")).hexdigest()


def _safe_output_path(output_path: Path) -> Path:
    path = output_path.expanduser().resolve(strict=False)
    if path.is_relative_to(REPO_ROOT):
        raise DryRunReportError("report output must be outside the repository")
    if not path.parent.is_dir():
        raise DryRunReportError("report output directory does not exist")
    return path


def _remove_output(output_path: Path) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError as exc:
        raise DryRunReportError("stale report output cannot be removed") from exc


def _atomic_write(output_path: Path, content: bytes) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise DryRunReportError("report output could not be written") from exc


def _assert_private_report(report: Mapping[str, Any]) -> None:
    encoded = _canonical_json(report)
    lowered = encoded.lower()
    if _EMAIL_LIKE.search(encoded) or any(
        token in lowered for token in _FORBIDDEN_REPORT_TOKENS
    ):
        raise DryRunReportError("report privacy validation failed")


def _build_results(catalog, runs: int):
    results = []
    receipt_digests = defaultdict(set)
    status_counts = Counter()
    reason_counts = Counter()
    try:
        for _ in range(runs):
            for case in catalog.cases:
                fixture_result = run_effect_adapter_fixture_case(case)
                if fixture_result.case_id != case.case_id:
                    raise DryRunReportError(
                        "fixture execution returned an unexpected identity"
                    )
                signatures = []
                for receipt in fixture_result.receipts:
                    status = receipt["status"]
                    reason = receipt["reason"]
                    signatures.append(f"{status}:{reason}")
                    status_counts[status] += 1
                    reason_counts[reason] += 1
                receipt_digest = _receipt_digest(fixture_result.receipt_id)
                receipt_digests[case.case_id].add(receipt_digest)
                results.append(
                    {
                        "caseId": fixture_result.case_id,
                        "passed": fixture_result.passed,
                        "statusReasonSignatures": signatures,
                        "receiptDigest": receipt_digest,
                    }
                )
    except DryRunReportError:
        raise
    except Exception as exc:
        raise DryRunReportError("fixture execution failed") from exc
    return results, receipt_digests, status_counts, reason_counts


def run_dry_run(
    *,
    fixture_path: Path,
    runs: int,
    output_path: Path,
) -> dict[str, Any]:
    if type(runs) is not int or runs != REQUIRED_RUNS:
        raise DryRunReportError("effect-adapter dry run requires exactly 3 runs")

    output_path = _safe_output_path(Path(output_path))
    _remove_output(output_path)
    if _source_tree_dirty():
        raise DryRunReportError(
            "effect-adapter dry run requires a clean committed source tree"
        )

    revision = _code_revision()
    source_tree_hash = _source_tree_hash(revision)
    fixture_path = Path(fixture_path).expanduser().resolve(strict=False)
    fixture_hash = _file_hash(fixture_path)
    try:
        catalog = load_effect_adapter_fixture_catalog(fixture_path)
    except EffectAdapterFixtureValidationError as exc:
        raise DryRunReportError("effect-adapter fixture catalog rejected") from exc

    results, receipt_digests, status_counts, reason_counts = _build_results(
        catalog,
        runs,
    )
    expected_result_count = len(catalog.cases) * runs
    if len(results) != expected_result_count:
        raise DryRunReportError("effect-adapter dry run is incomplete")
    if not all(item["passed"] for item in results):
        raise DryRunReportError("effect-adapter fixture expectation mismatch")

    variance_case_ids = [
        case.case_id
        for case in catalog.cases
        if len(receipt_digests[case.case_id]) != 1
    ]
    if variance_case_ids:
        raise DryRunReportError("effect-adapter receipt variance detected")

    report = {
        "identity": {
            "profile": PROFILE,
            "codeRevision": revision,
            "sourceTreeDirty": False,
            "sourceTreeHash": source_tree_hash,
            "fixtureHash": fixture_hash,
            "caseCount": len(catalog.cases),
            "runs": runs,
        },
        "summary": {
            "resultCount": len(results),
            "passedResultCount": sum(item["passed"] for item in results),
            "varianceCaseIds": variance_case_ids,
            "statusCounts": dict(sorted(status_counts.items())),
            "reasonCounts": dict(sorted(reason_counts.items())),
        },
        "results": results,
    }
    report["passed"] = (
        report["summary"]["resultCount"] == expected_result_count
        and report["summary"]["passedResultCount"] == len(results)
        and not variance_case_ids
    )
    if not report["passed"]:
        raise DryRunReportError("effect-adapter report did not pass")
    _assert_private_report(report)
    report["resultDigest"] = _digest(report)
    _assert_private_report(report)

    if (
        _source_tree_dirty()
        or _code_revision() != revision
        or _source_tree_hash(revision) != source_tree_hash
        or _file_hash(fixture_path) != fixture_hash
    ):
        raise DryRunReportError("source identity changed during dry run")

    serialized = f"{_canonical_json(report)}\n".encode("ascii")
    _atomic_write(output_path, serialized)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed sanitized disabled effect-adapter proof. "
            "No service or production effect is available."
        )
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs", type=int, choices=(REQUIRED_RUNS,), required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_dry_run(
            fixture_path=args.fixture,
            runs=args.runs,
            output_path=args.output,
        )
    except DryRunReportError as exc:
        parser.error(str(exc))
    print(_canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
