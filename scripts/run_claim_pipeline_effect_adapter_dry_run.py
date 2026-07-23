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
    EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION,
    EffectAdapterFixtureValidationError,
    load_effect_adapter_fixture_catalog,
    run_effect_adapter_fixture_case,
)


PROFILE = "disabled-effect-adapter-dry-run-v1"
REQUIRED_RUNS = 3
CANONICAL_FIXTURE_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "claim_pipeline_effect_adapter_cases.json"
).resolve()
CANONICAL_FIXTURE_SHA256 = (
    "c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229"
)
CANONICAL_FIXTURE_SCHEMA_VERSION = (
    "claim-pipeline-effect-adapter-fixtures-v1"
)
# Report-visible fixture labels are closed here so arbitrary names cannot escape.
CANONICAL_CASE_IDS = (
    "automatic-fact-matching",
    "automatic-fact-stale-prior",
    "whole-plan-stale-snapshot",
    "whole-plan-stale-contract",
    "already-committed-effect",
    "human-action-no-approval",
    "human-action-exact-approval",
    "approval-for-other-action",
    "approval-wrong-plan",
    "forbidden-plan",
    "unsupported-actions",
    "terminal-outbound-draft",
    "terminal-followup-freeze",
    "dependency-chain-eligible",
    "dependency-chain-blocked",
    "dependency-construction-rejected",
    "scope-and-provenance-rejected",
    "input-order-byte-stable",
)
CANONICAL_CASE_COUNT = 18
CANONICAL_RESULT_COUNT = 54
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
_IDENTITY_KEYS = frozenset(
    {
        "profile",
        "codeRevision",
        "sourceTreeDirty",
        "sourceTreeHash",
        "fixtureHash",
        "caseCount",
        "runs",
    }
)
_SUMMARY_KEYS = frozenset(
    {
        "resultCount",
        "passedResultCount",
        "varianceCaseIds",
        "statusCounts",
        "reasonCounts",
    }
)
_RESULT_KEYS = frozenset(
    {
        "caseId",
        "passed",
        "statusReasonSignatures",
        "receiptDigest",
    }
)
_STATUS_KEYS = frozenset({"blocked", "skipped", "would_apply"})
_REASON_KEYS = frozenset(
    {
        "approval_required",
        "approval_scope_mismatch",
        "dependency_blocked",
        "eligible_automatic_action",
        "eligible_human_approved_action",
        "idempotency_key_already_committed",
        "plan_contract_violation",
        "prior_state_mismatch",
        "stale_contract",
        "stale_snapshot",
        "terminal_outbound_suppressed",
        "unsupported_action_type",
    }
)
_STATUS_REASON_SIGNATURES = frozenset(
    {
        "blocked:approval_scope_mismatch",
        "blocked:dependency_blocked",
        "blocked:plan_contract_violation",
        "blocked:prior_state_mismatch",
        "blocked:stale_contract",
        "blocked:stale_snapshot",
        "blocked:terminal_outbound_suppressed",
        "blocked:unsupported_action_type",
        "skipped:approval_required",
        "skipped:idempotency_key_already_committed",
        "would_apply:eligible_automatic_action",
        "would_apply:eligible_human_approved_action",
    }
)
_HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


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
    if not _HEX_40.fullmatch(revision):
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


def _canonical_fixture_path(fixture_path: Path) -> Path:
    path = Path(fixture_path).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise DryRunReportError(
            "effect-adapter dry run requires the canonical fixture path"
        ) from exc
    if path != CANONICAL_FIXTURE_PATH:
        raise DryRunReportError(
            "effect-adapter dry run requires the canonical fixture path"
        )
    return path


def _validated_runs(value: object) -> int:
    if type(value) is int:
        runs = value
    elif isinstance(value, str):
        try:
            runs = int(value, 10)
        except ValueError as exc:
            raise DryRunReportError(
                "effect-adapter dry run requires exactly 3 runs"
            ) from exc
    else:
        raise DryRunReportError(
            "effect-adapter dry run requires exactly 3 runs"
        )
    if runs != REQUIRED_RUNS:
        raise DryRunReportError(
            "effect-adapter dry run requires exactly 3 runs"
        )
    return runs


def _remove_output(output_path: Path) -> None:
    try:
        output_path.unlink(missing_ok=True)
    except OSError as exc:
        raise DryRunReportError("stale report output cannot be removed") from exc


def _remove_stale_output_artifacts(output_path: Path) -> Path:
    output_path = _safe_output_path(output_path)
    _remove_output(output_path)
    temporary_prefix = f".{output_path.name}."
    try:
        siblings = tuple(output_path.parent.iterdir())
        for sibling in siblings:
            if sibling.name.startswith(temporary_prefix):
                sibling.unlink(missing_ok=True)
    except OSError as exc:
        raise DryRunReportError(
            "stale report temporary output cannot be removed"
        ) from exc
    return output_path


def _preparse_output_paths(
    argv: Sequence[str],
) -> tuple[tuple[Path, ...], Optional[str]]:
    candidates = []
    output_occurrences = 0
    malformed = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token == "--output":
            output_occurrences += 1
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                malformed = True
                index += 1
                continue
            candidates.append(Path(argv[index + 1]))
            index += 2
            continue
        if token.startswith("--output="):
            output_occurrences += 1
            value = token.partition("=")[2]
            if value:
                candidates.append(Path(value))
            else:
                malformed = True
        index += 1

    if output_occurrences > 1:
        issue = "--output must be supplied exactly once"
    elif malformed:
        issue = "--output requires one unambiguous path"
    else:
        issue = None
    return tuple(candidates), issue


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
    _assert_report_allowlist(report)


def _exact_keys(
    value: object,
    expected: frozenset[str],
) -> bool:
    return isinstance(value, Mapping) and set(value) == expected


def _count_map_is_allowed(
    value: object,
    allowed_keys: frozenset[str],
) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == allowed_keys
        and all(type(count) is int and count >= 0 for count in value.values())
    )


def _assert_report_allowlist(report: Mapping[str, Any]) -> None:
    unsigned_keys = frozenset({"identity", "summary", "results", "passed"})
    signed_keys = unsigned_keys | {"resultDigest"}
    if set(report) not in (unsigned_keys, signed_keys):
        raise DryRunReportError("report field allowlist validation failed")

    identity = report.get("identity")
    summary = report.get("summary")
    results = report.get("results")
    if not _exact_keys(identity, _IDENTITY_KEYS) or not _exact_keys(
        summary,
        _SUMMARY_KEYS,
    ):
        raise DryRunReportError("report field allowlist validation failed")
    assert isinstance(identity, Mapping)
    assert isinstance(summary, Mapping)

    identity_allowed = (
        identity["profile"] == PROFILE
        and isinstance(identity["codeRevision"], str)
        and _HEX_40.fullmatch(identity["codeRevision"]) is not None
        and identity["sourceTreeDirty"] is False
        and isinstance(identity["sourceTreeHash"], str)
        and _HEX_64.fullmatch(identity["sourceTreeHash"]) is not None
        and identity["fixtureHash"] == CANONICAL_FIXTURE_SHA256
        and identity["caseCount"] == CANONICAL_CASE_COUNT
        and identity["runs"] == REQUIRED_RUNS
    )
    summary_allowed = (
        summary["resultCount"] == CANONICAL_RESULT_COUNT
        and summary["passedResultCount"] == CANONICAL_RESULT_COUNT
        and summary["varianceCaseIds"] == []
        and _count_map_is_allowed(summary["statusCounts"], _STATUS_KEYS)
        and _count_map_is_allowed(summary["reasonCounts"], _REASON_KEYS)
    )
    if not identity_allowed or not summary_allowed or report.get("passed") is not True:
        raise DryRunReportError("report value allowlist validation failed")

    if not isinstance(results, list) or len(results) != CANONICAL_RESULT_COUNT:
        raise DryRunReportError("report value allowlist validation failed")
    expected_case_ids = CANONICAL_CASE_IDS * REQUIRED_RUNS
    actual_case_ids = []
    for item in results:
        if not _exact_keys(item, _RESULT_KEYS):
            raise DryRunReportError("report field allowlist validation failed")
        assert isinstance(item, Mapping)
        signatures = item["statusReasonSignatures"]
        if (
            item["caseId"] not in CANONICAL_CASE_IDS
            or item["passed"] is not True
            or not isinstance(signatures, list)
            or not signatures
            or not all(
                isinstance(signature, str)
                and signature in _STATUS_REASON_SIGNATURES
                for signature in signatures
            )
            or not isinstance(item["receiptDigest"], str)
            or _HEX_64.fullmatch(item["receiptDigest"]) is None
        ):
            raise DryRunReportError("report value allowlist validation failed")
        actual_case_ids.append(item["caseId"])
    if tuple(actual_case_ids) != expected_case_ids:
        raise DryRunReportError("report value allowlist validation failed")

    if "resultDigest" in report and (
        not isinstance(report["resultDigest"], str)
        or _HEX_64.fullmatch(report["resultDigest"]) is None
    ):
        raise DryRunReportError("report value allowlist validation failed")


def _validate_canonical_catalog(catalog) -> None:
    if (
        EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION
        != CANONICAL_FIXTURE_SCHEMA_VERSION
        or catalog.schema_version != CANONICAL_FIXTURE_SCHEMA_VERSION
    ):
        raise DryRunReportError(
            "effect-adapter fixture schema version is not canonical"
        )
    if len(catalog.cases) != CANONICAL_CASE_COUNT:
        raise DryRunReportError(
            "effect-adapter fixture must contain exactly 18 cases"
        )
    case_ids = tuple(case.case_id for case in catalog.cases)
    if case_ids != CANONICAL_CASE_IDS:
        raise DryRunReportError(
            "effect-adapter fixture must use the canonical case IDs"
        )


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
    runs: object,
    output_path: Path,
) -> dict[str, Any]:
    output_path = _remove_stale_output_artifacts(Path(output_path))
    runs = _validated_runs(runs)
    fixture_path = _canonical_fixture_path(fixture_path)
    fixture_hash = _file_hash(fixture_path)
    if fixture_hash != CANONICAL_FIXTURE_SHA256:
        raise DryRunReportError(
            "effect-adapter canonical fixture hash mismatch"
        )
    if _source_tree_dirty():
        raise DryRunReportError(
            "effect-adapter dry run requires a clean committed source tree"
        )

    revision = _code_revision()
    source_tree_hash = _source_tree_hash(revision)
    try:
        catalog = load_effect_adapter_fixture_catalog(fixture_path)
    except EffectAdapterFixtureValidationError as exc:
        raise DryRunReportError("effect-adapter fixture catalog rejected") from exc
    _validate_canonical_catalog(catalog)

    results, receipt_digests, status_counts, reason_counts = _build_results(
        catalog,
        runs,
    )
    if len(results) != CANONICAL_RESULT_COUNT:
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
            "caseCount": CANONICAL_CASE_COUNT,
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
        report["summary"]["resultCount"] == CANONICAL_RESULT_COUNT
        and report["summary"]["passedResultCount"] == CANONICAL_RESULT_COUNT
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
        or _file_hash(fixture_path) != CANONICAL_FIXTURE_SHA256
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
    parser.add_argument("--runs", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    raw_argv = tuple(
        sys.argv[1:] if argv is None else (str(item) for item in argv)
    )
    output_paths, output_issue = _preparse_output_paths(raw_argv)
    cleanup_error = None
    for output_path in output_paths:
        try:
            _remove_stale_output_artifacts(output_path)
        except DryRunReportError as exc:
            if cleanup_error is None:
                cleanup_error = exc
    if cleanup_error is not None:
        parser.error(str(cleanup_error))
    if output_issue is not None:
        parser.error(output_issue)

    args = parser.parse_args(raw_argv)
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
