#!/usr/bin/env python3
"""Validate immutable production release provenance without mutating infrastructure."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
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
    "trafficPercent",
    "rollbackRevision",
    "observedDarkDeployment",
}
FRONTEND_KEYS = {
    "productionSha",
    "observedCandidateSha",
    "functionRevision",
    "functionCommitMapping",
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
    traffic = backend["trafficPercent"]
    if type(traffic) is not int or not 0 <= traffic <= 100:
        raise ValueError("backend.trafficPercent must be an integer from 0 through 100")
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
    if mapping == "BLOCKED_PROVENANCE":
        if workflow_state != "BLOCKED_PROVENANCE":
            raise ValueError("unresolved Function provenance requires BLOCKED_PROVENANCE")
    else:
        _require_sha(mapping, "frontend.functionCommitMapping")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="production release manifest JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        validate_manifest(manifest)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"release manifest invalid: {exc}", file=sys.stderr)
        return 1
    print(f"release manifest valid: {args.manifest} ({manifest['workflowState']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
