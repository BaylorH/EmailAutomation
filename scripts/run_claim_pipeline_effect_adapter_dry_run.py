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
CANONICAL_OUTPUT_PATH = Path(
    "/tmp/sitesift-disabled-effect-adapter-report.json"
)
OWNED_TEMP_PATH = Path(
    "/tmp/.sitesift-disabled-effect-adapter-report.json.tmp"
)
_UNLINK_ATTEMPTS = 3
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


def _source_tree_hash() -> str:
    tree = _git(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        "HEAD",
        "--",
        *_SOURCE_PATHS,
    )
    if not tree:
        raise DryRunReportError("committed source surface is empty")
    digest = hashlib.sha256()
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
    try:
        expanded = os.path.expanduser(os.fspath(output_path))
        path = Path(os.path.abspath(expanded))
    except (TypeError, ValueError, OSError) as exc:
        raise DryRunReportError(
            "effect-adapter dry run requires the canonical report output path"
        ) from exc
    if path != CANONICAL_OUTPUT_PATH:
        raise DryRunReportError(
            "effect-adapter dry run requires the canonical report output path"
        )
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


def _unlink_exact_with_retry(path: Path) -> bool:
    last_error = None
    for _ in range(_UNLINK_ATTEMPTS):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            last_error = exc
    raise DryRunReportError(f"cleanup failed for {path}") from last_error


def _lstat_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DryRunReportError(
            f"cleanup inspection failed for {path}"
        ) from exc
    return True


def _cleanup_errors(paths: Sequence[Path]) -> tuple[str, ...]:
    errors = []
    for path in paths:
        try:
            _unlink_exact_with_retry(path)
        except DryRunReportError as exc:
            errors.append(str(exc))
    return tuple(errors)


def _raise_controlled_failure(
    primary: str,
    *,
    cause: BaseException,
    cleanup_paths: Sequence[Path],
    extra_context: Sequence[str] = (),
) -> None:
    contexts = list(extra_context)
    contexts.extend(_cleanup_errors(cleanup_paths))
    message = primary
    if contexts:
        message = f"{message}; cleanup context: {'; '.join(contexts)}"
    raise DryRunReportError(message) from cause


def _remove_stale_output_artifacts(output_path: Path) -> Path:
    output_path = _safe_output_path(output_path)
    errors = []
    try:
        _unlink_exact_with_retry(output_path)
    except DryRunReportError as exc:
        errors.append(str(exc))

    temporary_collision = False
    try:
        temporary_collision = _lstat_exists(OWNED_TEMP_PATH)
    except DryRunReportError as exc:
        errors.append(str(exc))
    if temporary_collision:
        try:
            _unlink_exact_with_retry(OWNED_TEMP_PATH)
        except DryRunReportError as exc:
            errors.append(str(exc))

    if errors:
        raise DryRunReportError(
            f"stale report cleanup failed; {'; '.join(errors)}"
        )
    if temporary_collision:
        raise DryRunReportError(
            f"temporary output collision cleared at {OWNED_TEMP_PATH}"
        )
    return output_path


def _fsync_parent_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        raise OSError("directory synchronization is unavailable")
    descriptor = os.open(directory, flags | directory_flag)
    fsync_error = None
    try:
        os.fsync(descriptor)
    except OSError as exc:
        fsync_error = exc
    try:
        os.close(descriptor)
    except OSError as close_error:
        if fsync_error is not None:
            raise DryRunReportError(
                "parent directory fsync failed; directory close failed"
            ) from fsync_error
        raise close_error
    if fsync_error is not None:
        raise fsync_error


def _atomic_write(output_path: Path, content: bytes) -> None:
    output_path = _safe_output_path(output_path)
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    if no_follow_flag is None:
        raise DryRunReportError(
            "exclusive no-follow temporary output is unavailable"
        )

    descriptor = None
    phase = "temporary output creation"
    try:
        descriptor = os.open(
            OWNED_TEMP_PATH,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | no_follow_flag,
            0o600,
        )
        phase = "temporary output write"
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("temporary output write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None

        phase = "report output write during replace"
        os.replace(OWNED_TEMP_PATH, output_path)
        phase = "parent directory fsync"
        _fsync_parent_directory(output_path.parent)
    except (OSError, DryRunReportError) as exc:
        close_context = []
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                close_context.append(
                    "temporary output descriptor close failed"
                )
        if isinstance(exc, FileExistsError):
            primary = "report temporary output collision"
        elif phase == "parent directory fsync":
            primary = "report parent directory fsync failed"
        else:
            primary = f"report output write failed during {phase}"
        _raise_controlled_failure(
            primary,
            cause=exc,
            cleanup_paths=(output_path, OWNED_TEMP_PATH),
            extra_context=close_context,
        )


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
    source_tree_hash = _source_tree_hash()
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
        or _source_tree_hash() != source_tree_hash
        or _file_hash(fixture_path) != CANONICAL_FIXTURE_SHA256
    ):
        raise DryRunReportError("source identity changed during dry run")

    serialized = f"{_canonical_json(report)}\n".encode("ascii")
    _atomic_write(output_path, serialized)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Run the fixed sanitized disabled effect-adapter proof. "
            "No service or production effect is available."
        )
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        action="append",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        _remove_stale_output_artifacts(CANONICAL_OUTPUT_PATH)
    except DryRunReportError as exc:
        print(f"{Path(__file__).name}: error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    raw_argv = tuple(
        sys.argv[1:] if argv is None else (str(item) for item in argv)
    )
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    if len(args.output) != 1:
        parser.error("--output must be supplied exactly once")
    try:
        output_path = _safe_output_path(args.output[0])
        report = run_dry_run(
            fixture_path=args.fixture,
            runs=args.runs,
            output_path=output_path,
        )
    except DryRunReportError as exc:
        parser.error(str(exc))
    print(_canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
