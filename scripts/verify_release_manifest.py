#!/usr/bin/env python3
"""Validate immutable production release provenance without mutating infrastructure."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse


ROOT_KEYS = {"schemaVersion", "observedAt", "workflowState", "backend", "frontend"}
BACKEND_KEYS = {
    "projectId",
    "region",
    "service",
    "productionSha",
    "candidateSha",
    "candidateCi",
    "receiptSha",
    "receiptCi",
    "artifactDigest",
    "artifactTag",
    "deployedRevision",
    "configHash",
    "configHashAlgorithm",
    "deploymentConfigHash",
    "deploymentConfigHashAlgorithm",
    "trafficPercent",
    "rollbackRevision",
    "observedDarkDeployment",
}
FRONTEND_KEYS = {
    "productionSha",
    "observedCandidateSha",
    "functionRevision",
    "functionCommitMapping",
    "hostingReleaseId",
    "hostingRollbackReleaseId",
    "hostingCommitMapping",
}
CI_KEYS = {"runId", "url", "headSha", "status", "conclusion"}
DARK_DEPLOYMENT_KEYS = {
    "sourceSha",
    "artifactDigest",
    "artifactTag",
    "revision",
    "trafficPercent",
    "outboundMode",
    "coordinatorMode",
}
WORKFLOW_STATES = {
    "BLOCKED_PROVENANCE",
    "CI_VERIFIED",
    "DEPLOYED_DARK",
    "READY_FOR_TRAFFIC",
    "PRODUCTION",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TAG_RE = re.compile(r"^[0-9a-f]{12}$")
REVISION_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PRODUCTION_CLEARANCE_WORKFLOW_DATABASE_ID = 327317922


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant: {value}")


def load_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(
            handle,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    return value


def _require_exact_keys(value: Any, expected: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{field} has an unexpected schema")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _require_sha(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if not SHA_RE.fullmatch(value):
        raise ValueError(f"{field} must be an exact 40-character lowercase SHA")
    return value


def _require_digest(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if not DIGEST_RE.fullmatch(value):
        raise ValueError(f"{field} must be an immutable sha256 digest")
    return value


def _require_revision(value: Any, field: str) -> str:
    value = _require_string(value, field)
    if not REVISION_RE.fullmatch(value):
        raise ValueError(f"{field} must be a concrete revision name")
    return value


def _validate_ci(value: Any, expected_sha: str, field: str) -> None:
    ci = _require_exact_keys(value, CI_KEYS, field)
    run_id = ci["runId"]
    if type(run_id) is not int or run_id <= 0:
        raise ValueError(f"{field}.runId must be a positive integer")
    if _require_sha(ci["headSha"], f"{field}.headSha") != expected_sha:
        raise ValueError(f"{field}.headSha must equal its exact candidate SHA")
    if ci["status"] != "completed" or ci["conclusion"] != "success":
        raise ValueError(f"{field} must record a completed successful run")
    url = _require_string(ci["url"], f"{field}.url")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path != f"/BaylorH/EmailAutomation/actions/runs/{run_id}"
    ):
        raise ValueError(f"{field}.url must identify the recorded GitHub Actions run")


def _read_github_run(run_id: int) -> dict[str, Any]:
    command = [
        "gh",
        "run",
        "view",
        str(run_id),
        "--repo",
        "BaylorH/EmailAutomation",
        "--json",
        (
            "databaseId,url,headSha,status,conclusion,workflowName,"
            "workflowDatabaseId,event"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError("GitHub CI attestation command could not run") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"gh exited {result.returncode}"
        raise ValueError(f"GitHub CI attestation readback failed: {detail}")
    try:
        value = json.loads(
            result.stdout,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("GitHub CI attestation returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("GitHub CI attestation returned a non-object")
    return value


def verify_github_attestations(manifest: dict[str, Any]) -> None:
    for field in ("candidateCi", "receiptCi"):
        expected = manifest["backend"][field]
        actual = _read_github_run(expected["runId"])
        attested = {
            "runId": actual.get("databaseId"),
            "url": actual.get("url"),
            "headSha": actual.get("headSha"),
            "status": actual.get("status"),
            "conclusion": actual.get("conclusion"),
        }
        if (
            actual.get("workflowName") != "Production Clearance CI"
            or actual.get("workflowDatabaseId")
            != PRODUCTION_CLEARANCE_WORKFLOW_DATABASE_ID
            or actual.get("event") != "push"
            or attested != expected
        ):
            raise ValueError(f"GitHub CI attestation mismatch for backend.{field}")


def verify_controller_attestation(controller_sha: str, run_id: int) -> None:
    expected_sha = _require_sha(controller_sha, "controllerSha")
    if type(run_id) is not int or run_id <= 0:
        raise ValueError("controllerCiRunId must be a positive integer")
    actual = _read_github_run(run_id)
    expected = {
        "databaseId": run_id,
        "url": (
            "https://github.com/BaylorH/EmailAutomation/actions/runs/"
            f"{run_id}"
        ),
        "headSha": expected_sha,
        "status": "completed",
        "conclusion": "success",
        "workflowName": "Production Clearance CI",
        "workflowDatabaseId": PRODUCTION_CLEARANCE_WORKFLOW_DATABASE_ID,
        "event": "push",
    }
    if actual != expected:
        raise ValueError("GitHub CI attestation mismatch for release controller")


def validate_manifest(manifest: Any) -> None:
    root = _require_exact_keys(manifest, ROOT_KEYS, "root")
    if type(root["schemaVersion"]) is not int or root["schemaVersion"] != 1:
        raise ValueError("schemaVersion must be integer 1")

    observed_at = _require_string(root["observedAt"], "observedAt")
    if not TIMESTAMP_RE.fullmatch(observed_at):
        raise ValueError("observedAt must be an RFC3339 UTC timestamp")
    try:
        datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("observedAt must be a valid RFC3339 UTC timestamp") from exc

    workflow_state = _require_string(root["workflowState"], "workflowState")
    if workflow_state not in WORKFLOW_STATES:
        raise ValueError("workflowState is not recognized")

    backend = _require_exact_keys(root["backend"], BACKEND_KEYS, "backend")
    if backend["projectId"] != "email-automation-cache":
        raise ValueError("backend.projectId must identify the approved production project")
    if backend["region"] != "us-central1":
        raise ValueError("backend.region must identify the approved production region")
    if backend["service"] != "process-user":
        raise ValueError("backend.service must identify the approved production service")
    production_sha = _require_sha(backend["productionSha"], "backend.productionSha")
    candidate_sha = _require_sha(backend["candidateSha"], "backend.candidateSha")
    receipt_sha = _require_sha(backend["receiptSha"], "backend.receiptSha")
    _validate_ci(backend["candidateCi"], candidate_sha, "backend.candidateCi")
    _validate_ci(backend["receiptCi"], receipt_sha, "backend.receiptCi")
    _require_digest(backend["artifactDigest"], "backend.artifactDigest")
    artifact_tag = _require_string(backend["artifactTag"], "backend.artifactTag")
    if not TAG_RE.fullmatch(artifact_tag) or not production_sha.startswith(artifact_tag):
        raise ValueError("backend.artifactTag must be the production SHA's 12-character tag")
    _require_revision(backend["deployedRevision"], "backend.deployedRevision")
    _require_digest(backend["configHash"], "backend.configHash")
    if backend["configHashAlgorithm"] != "sha256:canonical-json(spec)":
        raise ValueError("backend.configHashAlgorithm is unsupported")
    _require_digest(
        backend["deploymentConfigHash"],
        "backend.deploymentConfigHash",
    )
    if (
        backend["deploymentConfigHashAlgorithm"]
        != "sha256:canonical-json(spec,image=IMAGE_DIGEST_BOUND_AT_DEPLOY)"
    ):
        raise ValueError("backend.deploymentConfigHashAlgorithm is unsupported")
    traffic = backend["trafficPercent"]
    if type(traffic) is not int or traffic != 100:
        raise ValueError("backend.trafficPercent must be exactly 100")
    _require_revision(backend["rollbackRevision"], "backend.rollbackRevision")

    dark = _require_exact_keys(
        backend["observedDarkDeployment"],
        DARK_DEPLOYMENT_KEYS,
        "backend.observedDarkDeployment",
    )
    dark_sha = _require_sha(
        dark["sourceSha"],
        "backend.observedDarkDeployment.sourceSha",
    )
    _require_digest(
        dark["artifactDigest"],
        "backend.observedDarkDeployment.artifactDigest",
    )
    dark_tag = _require_string(
        dark["artifactTag"],
        "backend.observedDarkDeployment.artifactTag",
    )
    if not TAG_RE.fullmatch(dark_tag) or not dark_sha.startswith(dark_tag):
        raise ValueError("dark artifactTag must be the dark source SHA's 12-character tag")
    _require_revision(dark["revision"], "backend.observedDarkDeployment.revision")
    if type(dark["trafficPercent"]) is not int or dark["trafficPercent"] != 0:
        raise ValueError("dark deployment must remain at zero percent traffic")
    if dark["outboundMode"] != "paused":
        raise ValueError("dark deployment outboundMode must be paused")
    if dark["coordinatorMode"] != "disabled":
        raise ValueError("dark deployment coordinatorMode must be disabled")

    frontend = _require_exact_keys(root["frontend"], FRONTEND_KEYS, "frontend")
    _require_sha(frontend["productionSha"], "frontend.productionSha")
    _require_sha(frontend["observedCandidateSha"], "frontend.observedCandidateSha")
    _require_revision(frontend["functionRevision"], "frontend.functionRevision")
    mapping = frontend["functionCommitMapping"]
    if mapping != "BLOCKED_PROVENANCE":
        raise ValueError(
            "schemaVersion 1 cannot attest a Function commit; use BLOCKED_PROVENANCE"
        )
    for field in (
        "hostingReleaseId",
        "hostingRollbackReleaseId",
        "hostingCommitMapping",
    ):
        if frontend[field] != "BLOCKED_PROVENANCE":
            raise ValueError(f"frontend.{field} must remain BLOCKED_PROVENANCE")
    if workflow_state != "BLOCKED_PROVENANCE":
        raise ValueError("unresolved Firebase provenance requires BLOCKED_PROVENANCE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="production release manifest JSON")
    parser.add_argument(
        "--verify-github",
        action="store_true",
        help="read back the recorded GitHub Actions runs and compare exact metadata",
    )
    parser.add_argument(
        "--expected-candidate-sha",
        help="require backend.candidateSha to equal this exact commit",
    )
    parser.add_argument(
        "--controller-sha",
        help="exact release-controller commit to attest remotely",
    )
    parser.add_argument(
        "--controller-ci-run-id",
        type=int,
        help="successful Production Clearance CI run for --controller-sha",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
        if args.expected_candidate_sha is not None:
            expected_sha = _require_sha(
                args.expected_candidate_sha,
                "expectedCandidateSha",
            )
            if manifest["backend"]["candidateSha"] != expected_sha:
                raise ValueError(
                    "backend.candidateSha does not match expected candidate SHA"
                )
        if args.verify_github:
            verify_github_attestations(manifest)
        controller_args = (args.controller_sha, args.controller_ci_run_id)
        if (controller_args[0] is None) != (controller_args[1] is None):
            raise ValueError(
                "--controller-sha and --controller-ci-run-id must be provided together"
            )
        if controller_args[0] is not None:
            verify_controller_attestation(*controller_args)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"release manifest invalid: {exc}", file=sys.stderr)
        return 1
    suffix_parts = []
    if args.verify_github:
        suffix_parts.append("GitHub attestations verified")
    if args.controller_sha is not None:
        suffix_parts.append("release controller attested")
    suffix = f" {'; '.join(suffix_parts)}" if suffix_parts else ""
    print(
        f"release manifest valid: {args.manifest} "
        f"({manifest['workflowState']}){suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
