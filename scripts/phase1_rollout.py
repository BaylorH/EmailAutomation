#!/usr/bin/env python3
"""Closed Phase 1 rollout for the process-user Cloud Run service.

This module deliberately exposes pure validators and an injected operations
boundary so the queue/tag/traffic recovery state machine can be tested without
cloud effects.  The command-line adapter uses argument arrays only and never
invokes the worker endpoint.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request


ACCOUNT = "bp21harrison@gmail.com"
PROJECT = "email-automation-cache"
PROJECT_NUMBER = "248289505828"
REGION = "us-central1"
SERVICE = "process-user"
QUEUE = "graph-process-user"
BRANCH = "codex/policy-blocked-reply-review-release-20260812"
IMAGE_REPOSITORY = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    "cloud-run-source-deploy/process-user"
)
OLD_REVISION = "process-user-00097-yus"
OLD_IMAGE = (
    IMAGE_REPOSITORY
    + "@sha256:cd49af55848b7d9fe481d501e087626240d9dc273d0dee663f5c82e04fb62780"
)
RULES_HASH = "7acf2bdbe2a7a42221efaa1ae15c2b406e4d6bef6b2c4131b3b0a6b5de8f8ee8"
HOSTING_VERSION = "33dd8acbe4e909c8"
INDEX_HASH = "687c3f827d2cb7f797b7a9aaf286dc3ed4d8137c4d47ad378f171dfa4eab6f15"
JS_PATH = "static/js/main.96bc0645.js"
JS_HASH = "20db7edcde967438955b85cd071203a1e2531175ee098b1c37efda42b32dd19b"
CSS_PATH = "static/css/main.aad5f62b.css"
CSS_HASH = "43bd2f02d0f3de9ba18fce0c638b94b0e84c9f7a13542f3b3747a90736a54d22"
DOMAINS = ("email-automation-cache.web.app", "sitesiftai.com")
AUX_TAGS = {
    "jill-one": "process-user-jill-one-202608020520",
    "lock": "process-user-lock-0837727b",
    "rollback-door": "process-user-door-294b7599f1",
}
QUEUE_NAME = (
    "projects/email-automation-cache/locations/us-central1/queues/graph-process-user"
)
TAG_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
EXPECTED_IAM = {
    "roles/run.invoker": (
        "serviceAccount:248289505828-compute@developer.gserviceaccount.com",
    ),
}
AUTH_OVERRIDE_ENV = (
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_CORE_ACCOUNT",
    "CLOUDSDK_CORE_PROJECT",
)
AUTH_OVERRIDE_PROPERTIES = (
    "auth/impersonate_service_account",
    "auth/access_token_file",
    "auth/credential_file_override",
)
REVISION_NONFUNCTIONAL_ANNOTATIONS = frozenset({
    "run.googleapis.com/operation-id",
})
REVISION_NONFUNCTIONAL_LABELS = frozenset({
    "serving.knative.dev/configurationGeneration",
    "serving.knative.dev/route",
})


class RolloutError(RuntimeError):
    """A content-free, operator-safe rollout failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        del request, fp, code, message, headers, new_url
        return None


def validate_auth_environment(environment: Mapping[str, str]) -> None:
    if environment.get("GCLOUD_ACCOUNT") != ACCOUNT:
        raise RolloutError("GCLOUD_ACCOUNT is not the approved account")
    if any(environment.get(name) not in (None, "") for name in AUTH_OVERRIDE_ENV):
        raise RolloutError("Cloud SDK authentication environment override is set")


def validate_rules_source(source: Any) -> None:
    source = _object(source, "Firestore rules source")
    if set(source) != {"files"}:
        raise RolloutError("Firestore rules source contains unexpected fields")
    files = source.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise RolloutError("Firestore rules source is not exactly one file")
    row = _object(files[0], "Firestore rules file")
    if set(row) != {"name", "content"} or row.get("name") != "firestore.rules":
        raise RolloutError("Firestore rules file identity or shape is wrong")
    content = row.get("content")
    if not isinstance(content, str) or hashlib.sha256(content.encode()).hexdigest() != RULES_HASH:
        raise RolloutError("live Firestore rules hash is wrong")


@dataclass(frozen=True)
class Topology:
    positive_revision: str
    tags: Mapping[str, str]
    tag_urls: Mapping[str, str]
    service_url: str


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RolloutError(f"{label} is not an object")
    return value


def _cloud_run_base_url(
    value: Any, *, tag: str | None = None, allow_tagged: bool = False
) -> str:
    if not isinstance(value, str):
        raise RolloutError("Cloud Run URL is missing")
    parsed = urllib.parse.urlsplit(value)
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not isinstance(hostname, str)
        or parsed.netloc != hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not hostname.endswith(".run.app")
    ):
        raise RolloutError("Cloud Run URL is outside the closed HTTPS origin")
    required_prefix = f"{tag}---{SERVICE}-" if tag else f"{SERVICE}-"
    tagged_service_fragment = f"---{SERVICE}-"
    if not hostname.startswith(required_prefix) and not (
        allow_tagged and tagged_service_fragment in hostname
    ):
        raise RolloutError("Cloud Run URL does not match the service or tag")
    return value.rstrip("/")


def _parse_traffic(rows: Any, label: str) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
    if not isinstance(rows, list) or not rows:
        raise RolloutError(f"{label} traffic is missing")
    positive: dict[str, float] = {}
    tags: dict[str, str] = {}
    urls: dict[str, str] = {}
    for row in rows:
        row = _object(row, f"{label} traffic row")
        if row.get("latestRevision") not in (None, False):
            raise RolloutError(f"{label} contains LATEST")
        revision = row.get("revisionName")
        if not isinstance(revision, str) or not revision:
            raise RolloutError(f"{label} contains an implicit revision")
        percent = row.get("percent", 0)
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise RolloutError(f"{label} percent is invalid")
        if percent < 0 or percent > 100:
            raise RolloutError(f"{label} percent is outside 0..100")
        if percent:
            if revision in positive:
                raise RolloutError(f"{label} duplicates a positive revision")
            positive[revision] = percent
        tag = row.get("tag")
        if tag is not None:
            if not isinstance(tag, str) or TAG_RE.fullmatch(tag) is None or tag in tags:
                raise RolloutError(f"{label} tag is invalid or duplicated")
            tags[tag] = revision
            url = row.get("url")
            if url is not None:
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise RolloutError(f"{label} tag URL is invalid")
                urls[tag] = url.rstrip("/")
    return positive, tags, urls


def validate_topology(
    service: Any,
    *,
    expected_positive: str,
    expected_release: str,
    expected_aux: Mapping[str, str],
    expected_extra: Mapping[str, str] | None = None,
) -> Topology:
    service = _object(service, "service")
    spec = _object(service.get("spec"), "service spec")
    status = _object(service.get("status"), "service status")
    metadata = _object(service.get("metadata"), "service metadata")
    if metadata.get("name") != SERVICE:
        raise RolloutError("service identity is not exact")
    spec_positive, spec_tags, _ = _parse_traffic(spec.get("traffic"), "spec")
    status_positive, status_tags, status_urls = _parse_traffic(
        status.get("traffic"), "status"
    )
    if spec_positive != {expected_positive: 100} or status_positive != spec_positive:
        raise RolloutError("positive traffic is not the exact sole 100 percent target")
    expected_tags = dict(expected_aux)
    expected_tags["release-a"] = expected_release
    if expected_extra:
        for key, value in expected_extra.items():
            if key in expected_tags:
                raise RolloutError("expected tag contract is duplicated")
            expected_tags[key] = value
    if spec_tags != expected_tags or status_tags != expected_tags:
        raise RolloutError("traffic tag mapping is not exact")
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict) or annotations.get(
        "run.googleapis.com/maxScale"
    ) != "20":
        raise RolloutError("service-wide maxScale is not 20")
    service_url = _cloud_run_base_url(status.get("url"))
    service_hostname = urllib.parse.urlsplit(service_url).hostname
    if not isinstance(service_hostname, str):
        raise RolloutError("service URL hostname is missing")
    for tag in expected_tags:
        if tag not in status_urls:
            raise RolloutError("tag URL is missing from status")
        status_urls[tag] = _cloud_run_base_url(status_urls[tag], tag=tag)
        tag_hostname = urllib.parse.urlsplit(status_urls[tag]).hostname
        if tag_hostname != f"{tag}---{service_hostname}":
            raise RolloutError("tag URL does not belong to the canonical service")
    return Topology(expected_positive, dict(status_tags), dict(status_urls), service_url)


def validate_queue(value: Any, expected_state: str) -> None:
    value = _object(value, "queue")
    if value.get("name") != QUEUE_NAME or value.get("state") != expected_state:
        raise RolloutError("queue identity or state is not exact")
    if value.get("rateLimits") != {
        "maxBurstSize": 10,
        "maxConcurrentDispatches": 1,
        "maxDispatchesPerSecond": 1.0,
    }:
        raise RolloutError("queue rate limits drifted")
    if value.get("retryConfig") != {
        "maxAttempts": 15,
        "maxBackoff": "300s",
        "maxDoublings": 4,
        "minBackoff": "30s",
    }:
        raise RolloutError("queue retry configuration drifted")
    if value.get("httpTarget") not in (None, {}):
        raise RolloutError("queue HTTP target override is present")
    if value.get("appEngineRoutingOverride") is not None:
        raise RolloutError("queue App Engine override is present")


def validate_iam(value: Any) -> None:
    value = _object(value, "service IAM policy")
    bindings = value.get("bindings")
    if not isinstance(bindings, list):
        raise RolloutError("service IAM bindings are missing")
    normalized: dict[str, tuple[str, ...]] = {}
    for row in bindings:
        row = _object(row, "service IAM binding")
        if set(row) != {"role", "members"}:
            raise RolloutError("service IAM binding contains unexpected fields")
        role = row.get("role")
        members = row.get("members")
        if (
            not isinstance(role, str)
            or role in normalized
            or not isinstance(members, list)
            or not all(isinstance(member, str) for member in members)
        ):
            raise RolloutError("service IAM binding shape is invalid")
        normalized[role] = tuple(sorted(members))
    if normalized != EXPECTED_IAM:
        raise RolloutError("service IAM policy is not the exact private contract")


def validate_project_iam(value: Any) -> None:
    value = _object(value, "project IAM policy")
    bindings = value.get("bindings")
    if not isinstance(bindings, list):
        raise RolloutError("project IAM bindings are missing")
    invokers: list[str] = []
    for row in bindings:
        row = _object(row, "project IAM binding")
        role = row.get("role")
        members = row.get("members")
        if (
            not isinstance(role, str)
            or not isinstance(members, list)
            or not all(isinstance(member, str) for member in members)
        ):
            raise RolloutError("project IAM binding shape is invalid")
        if "allUsers" in members or "allAuthenticatedUsers" in members:
            raise RolloutError("project IAM contains a broad principal")
        if role == "roles/run.invoker":
            if set(row) != {"role", "members"}:
                raise RolloutError("project invoker binding contains unexpected fields")
            invokers.extend(members)
    if tuple(sorted(invokers)) != EXPECTED_IAM["roles/run.invoker"]:
        raise RolloutError("project invoker policy is not the exact private contract")


def _canonical_revision_spec(value: Any) -> dict[str, Any]:
    result = copy.deepcopy(_object(value, "revision spec"))
    containers = result.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("revision must have exactly one container")
    containers[0] = _object(containers[0], "revision container")
    containers[0].pop("image", None)
    return result


def _canonical_revision_metadata(value: Any) -> dict[str, dict[str, str]]:
    metadata = _object(value, "revision metadata")
    result: dict[str, dict[str, str]] = {}
    for field, ignored in (
        ("annotations", REVISION_NONFUNCTIONAL_ANNOTATIONS),
        ("labels", REVISION_NONFUNCTIONAL_LABELS),
    ):
        raw = metadata.get(field, {})
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in raw.items()
        ):
            raise RolloutError(f"revision {field} shape is invalid")
        result[field] = {
            key: item for key, item in raw.items() if key not in ignored
        }
    return result


def validate_candidate(
    candidate: Any,
    baseline: Any,
    expected_name: str,
    expected_image: str,
) -> None:
    candidate = _object(candidate, "candidate revision")
    baseline = _object(baseline, "baseline revision")
    metadata = _object(candidate.get("metadata"), "candidate metadata")
    if metadata.get("name") != expected_name:
        raise RolloutError("candidate revision identity is wrong")
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict) or annotations.get(
        "autoscaling.knative.dev/maxScale"
    ) != "10" or annotations.get("autoscaling.knative.dev/minScale") not in (
        None,
        "0",
    ):
        raise RolloutError("candidate scaling contract drifted")
    spec = _object(candidate.get("spec"), "candidate spec")
    baseline_spec = _object(baseline.get("spec"), "baseline spec")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("candidate container contract is invalid")
    container = _object(containers[0], "candidate container")
    if container.get("image") != expected_image:
        raise RolloutError("candidate image does not match immutable digest")
    status = _object(candidate.get("status"), "candidate status")
    if status.get("imageDigest") != expected_image:
        raise RolloutError("candidate status image digest is wrong")
    ready = [
        row
        for row in status.get("conditions", [])
        if isinstance(row, dict) and row.get("type") == "Ready"
    ]
    if len(ready) != 1 or str(ready[0].get("status")).lower() != "true":
        raise RolloutError("candidate is not exactly Ready")
    if _canonical_revision_spec(spec) != _canonical_revision_spec(baseline_spec):
        raise RolloutError("candidate config differs from baseline beyond image")
    if _canonical_revision_metadata(metadata) != _canonical_revision_metadata(
        _object(baseline.get("metadata"), "baseline metadata")
    ):
        raise RolloutError("candidate functional metadata differs from baseline")


def validate_old_revision(value: Any) -> None:
    value = _object(value, "old revision")
    if value.get("metadata", {}).get("name") != OLD_REVISION:
        raise RolloutError("old revision identity is wrong")
    spec = _object(value.get("spec"), "old revision spec")
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("old revision container contract is invalid")
    if containers[0].get("image") != OLD_IMAGE or value.get("status", {}).get(
        "imageDigest"
    ) != OLD_IMAGE:
        raise RolloutError("old revision digest is wrong")


def _validate_identity(value: Any, candidate: str) -> None:
    if value != {"status": "ok", "service": SERVICE, "revision": candidate}:
        raise RolloutError("health identity does not match candidate")


def _validate_legacy_health(value: Any) -> None:
    if value != {"status": "ok"}:
        raise RolloutError("legacy health response is not exact")


class Phase1Rollout:
    def __init__(
        self,
        *,
        ops: Any,
        head_sha: str,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if SHA_RE.fullmatch(head_sha) is None:
            raise RolloutError("HEAD is not an exact lowercase SHA")
        self.ops = ops
        self.head_sha = head_sha
        self.short_sha = head_sha[:12]
        self.candidate = f"{SERVICE}-stage-{self.short_sha}"
        self.cert_tag = f"phase1-cert-{self.short_sha}"
        self.sleeper = sleeper
        self.task_observed = False

    def dry_run(self) -> str:
        return (
            f"dry-run: zero gcloud or HTTP commands; candidate={self.candidate}; "
            "queue remains RUNNING; traffic and tags remain unchanged"
        )

    def _baseline(self) -> tuple[Any, Any, str]:
        self.ops.preflight()
        self.ops.verify_rules_ui_switches()
        image = self.ops.artifact_image()
        if not isinstance(image, str) or not image.startswith(IMAGE_REPOSITORY + "@"):
            raise RolloutError("artifact image is invalid")
        service_value = self.ops.get_service()
        topology = validate_topology(
            service_value,
            expected_positive=OLD_REVISION,
            expected_release=OLD_REVISION,
            expected_aux=AUX_TAGS,
        )
        self.ops.verify_service_access(topology)
        old = self.ops.get_revision(OLD_REVISION)
        validate_old_revision(old)
        _validate_legacy_health(
            self.ops.legacy_health_get(topology.service_url, topology.service_url)
        )
        _validate_legacy_health(
            self.ops.legacy_health_get(
                topology.tag_urls["release-a"], topology.service_url
            )
        )
        candidate = self.ops.get_revision(self.candidate)
        validate_candidate(candidate, old, self.candidate, image)
        validate_queue(self.ops.get_queue(), "RUNNING")
        return old, candidate, image

    def _tasks_are_empty(self) -> bool:
        tasks = self.ops.list_tasks()
        if not isinstance(tasks, list):
            raise RolloutError("task list is not a JSON list")
        if tasks:
            self.task_observed = True
            return False
        return True

    def _cleanup_failure(
        self,
        *,
        pause_attempted: bool,
        tag_attempted: bool,
        traffic_attempted: bool,
    ) -> bool:
        if not pause_attempted:
            return True
        cleanup_ok = True
        try:
            self.ops.pause_queue()
            validate_queue(self.ops.get_queue(), "PAUSED")
        except BaseException:
            raise RolloutError("MANUAL_RECOVERY: queue state unverified")
        if tag_attempted:
            try:
                self.ops.remove_cert_tag(self.cert_tag)
            except BaseException:
                cleanup_ok = False
        if traffic_attempted:
            try:
                self.ops.pause_queue()
                validate_queue(self.ops.get_queue(), "PAUSED")
                self.ops.rollback(OLD_REVISION, self.candidate)
            except BaseException:
                cleanup_ok = False
        try:
            topology = validate_topology(
                self.ops.get_service(),
                expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION,
                expected_aux=AUX_TAGS,
            )
            self.ops.verify_service_access(topology)
            self.ops.verify_rules_ui_switches()
            validate_old_revision(self.ops.get_revision(OLD_REVISION))
            _validate_legacy_health(
                self.ops.legacy_health_get(topology.service_url, topology.service_url)
            )
            _validate_legacy_health(
                self.ops.legacy_health_get(
                    topology.tag_urls["release-a"], topology.service_url
                )
            )
            if not self._tasks_are_empty():
                cleanup_ok = False
            validate_queue(self.ops.get_queue(), "PAUSED")
        except BaseException:
            cleanup_ok = False
        if self.task_observed:
            cleanup_ok = False
        if cleanup_ok:
            try:
                self.ops.pause_queue()
                validate_queue(self.ops.get_queue(), "PAUSED")
                self.ops.resume_queue()
                validate_queue(self.ops.get_queue(), "RUNNING")
            except BaseException:
                try:
                    self.ops.pause_queue()
                    validate_queue(self.ops.get_queue(), "PAUSED")
                except BaseException as error:
                    raise RolloutError(
                        "MANUAL_RECOVERY: queue state unverified"
                    ) from error
                return False
        else:
            try:
                self.ops.pause_queue()
                validate_queue(self.ops.get_queue(), "PAUSED")
            except BaseException as error:
                raise RolloutError(
                    "MANUAL_RECOVERY: queue state unverified"
                ) from error
        return cleanup_ok

    def apply(self) -> None:
        pause_attempted = False
        tag_attempted = False
        traffic_attempted = False
        try:
            self._baseline()
            pause_attempted = True
            self.ops.pause_queue()
            validate_queue(self.ops.get_queue(), "PAUSED")
            for index in range(3):
                validate_queue(self.ops.get_queue(), "PAUSED")
                if not self._tasks_are_empty():
                    raise RolloutError("queue is not drained")
                if index < 2:
                    self.sleeper(5)

            tag_attempted = True
            self.ops.add_cert_tag(self.cert_tag, self.candidate)
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared while queue was paused")
            tagged = validate_topology(
                self.ops.get_service(),
                expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION,
                expected_aux=AUX_TAGS,
                expected_extra={self.cert_tag: self.candidate},
            )
            cert_url = tagged.tag_urls[self.cert_tag]
            if self.ops.unauthenticated_status(cert_url, "/health") != 403:
                raise RolloutError("temporary tag is not protected by Cloud Run IAM")
            _validate_legacy_health(
                self.ops.legacy_health_get(cert_url, tagged.service_url)
            )
            _validate_identity(
                self.ops.identity_get(cert_url, tagged.service_url), self.candidate
            )

            self.ops.remove_cert_tag(self.cert_tag)
            validate_topology(
                self.ops.get_service(),
                expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION,
                expected_aux=AUX_TAGS,
            )
            tag_attempted = False
            if not self._tasks_are_empty():
                raise RolloutError("task appeared before promotion")
            self.ops.verify_rules_ui_switches()
            self.ops.pause_queue()
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared immediately before promotion")
            traffic_attempted = True
            self.ops.promote(self.candidate, OLD_REVISION)
            promoted = validate_topology(
                self.ops.get_service(),
                expected_positive=self.candidate,
                expected_release=self.candidate,
                expected_aux=AUX_TAGS,
            )
            self.ops.verify_service_access(promoted)
            validate_candidate(
                self.ops.get_revision(self.candidate),
                self.ops.get_revision(OLD_REVISION),
                self.candidate,
                self.ops.artifact_image(),
            )
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared after promotion")
            _validate_identity(
                self.ops.identity_get(promoted.service_url, promoted.service_url),
                self.candidate,
            )
            _validate_identity(
                self.ops.identity_get(
                    promoted.tag_urls["release-a"], promoted.service_url
                ),
                self.candidate,
            )
            _validate_legacy_health(
                self.ops.legacy_health_get(
                    promoted.service_url, promoted.service_url
                )
            )
            _validate_legacy_health(
                self.ops.legacy_health_get(
                    promoted.tag_urls["release-a"], promoted.service_url
                )
            )
            if not self._tasks_are_empty():
                raise RolloutError("task appeared before queue resume")
            self.ops.verify_rules_ui_switches()
            self.ops.pause_queue()
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared immediately before queue resume")
            self.ops.resume_queue()
            validate_queue(self.ops.get_queue(), "RUNNING")
            self.ops.verify_rules_ui_switches()
        except BaseException as error:
            try:
                cleanup_ok = self._cleanup_failure(
                    pause_attempted=pause_attempted,
                    tag_attempted=tag_attempted,
                    traffic_attempted=traffic_attempted,
                )
            except RolloutError:
                raise
            if not cleanup_ok:
                raise RolloutError("MANUAL_RECOVERY: queue left paused") from error
            if isinstance(error, RolloutError):
                raise
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise RolloutError("rollout interrupted safely") from error
            raise RolloutError("rollout failed safely") from error


class SubprocessOps:
    """Production adapter. Outputs are captured and never include tokens."""

    def __init__(self, repo_root: Path, head_sha: str) -> None:
        self.repo_root = repo_root
        self.head_sha = head_sha
        self.short_sha = head_sha[:12]
        self.candidate = f"{SERVICE}-stage-{self.short_sha}"
        self._access_token: str | None = None

    def _run(self, args: list[str], timeout: int = 600) -> str:
        try:
            result = subprocess.run(
                args,
                cwd=self.repo_root,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as error:
            family = " ".join(args[:3])
            raise RolloutError(f"command failed: {family}") from error
        return result.stdout.strip()

    def _gcloud(self, args: list[str], timeout: int = 600) -> str:
        return self._run(
            ["gcloud", *args, "--account", ACCOUNT, "--project", PROJECT], timeout
        )

    def _json_command(self, args: list[str], timeout: int = 600) -> Any:
        raw = self._gcloud([*args, "--format=json"], timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise RolloutError("command returned invalid JSON") from error

    def _token(self) -> str:
        if self._access_token is None:
            token = self._gcloud(["auth", "print-access-token"], 60)
            if not token or any(character.isspace() for character in token):
                raise RolloutError("access token response is invalid")
            self._access_token = token
        return self._access_token

    def _validate_gcloud_auth_config(self) -> None:
        for property_name in AUTH_OVERRIDE_PROPERTIES:
            value = self._gcloud(["config", "get-value", property_name], 30)
            if value not in ("", "(unset)"):
                raise RolloutError("Cloud SDK authentication config override is set")

    def _http_bytes(
        self, url: str, *, token: str | None = None, timeout: int = 20
    ) -> bytes:
        headers = {"Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["x-goog-user-project"] = PROJECT
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            opener = (
                urllib.request.build_opener(_NoRedirect())
                if token
                else urllib.request.build_opener()
            )
            with opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RolloutError("HTTP read did not return 200")
                if response.geturl() != url:
                    raise RolloutError("HTTP read changed origin or URL")
                return response.read()
        except (OSError, urllib.error.URLError) as error:
            raise RolloutError("HTTP read failed") from error

    def _http_json(self, url: str, *, token: str | None = None) -> Any:
        try:
            return json.loads(self._http_bytes(url, token=token))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RolloutError("HTTP read returned invalid JSON") from error

    def preflight(self) -> None:
        validate_auth_environment(os.environ)
        if self._run(["git", "rev-parse", "HEAD"], 30) != self.head_sha:
            raise RolloutError("checkout HEAD changed")
        if self._run(["git", "status", "--porcelain=v1"], 30):
            raise RolloutError("deployment checkout is dirty")
        if self._run(["git", "rev-parse", "@{upstream}"], 30) != self.head_sha:
            raise RolloutError("upstream does not equal HEAD")
        remote = self._run(
            ["git", "ls-remote", "origin", f"refs/heads/{BRANCH}"], 60
        ).split()
        if not remote or remote[0] != self.head_sha:
            raise RolloutError("remote branch does not equal HEAD")
        self._validate_gcloud_auth_config()
        accounts = self._json_command(["auth", "list"], 30)
        active = [
            row.get("account")
            for row in accounts
            if isinstance(row, dict) and row.get("status") == "ACTIVE"
        ]
        if active != [ACCOUNT]:
            raise RolloutError("approved gcloud account is not uniquely active")
        project = self._json_command(["projects", "describe", PROJECT], 30)
        if str(project.get("projectNumber")) != PROJECT_NUMBER or project.get(
            "lifecycleState"
        ) != "ACTIVE" or project.get("parent") not in (None, {}):
            raise RolloutError("gcloud project identity is wrong")

    def verify_rules_ui_switches(self) -> None:
        token = self._token()
        rules_release = self._http_json(
            f"https://firebaserules.googleapis.com/v1/projects/{PROJECT}/releases/cloud.firestore",
            token=token,
        )
        ruleset = rules_release.get("rulesetName")
        if not isinstance(ruleset, str):
            raise RolloutError("Firestore rules release is invalid")
        rules = self._http_json(
            f"https://firebaserules.googleapis.com/v1/{ruleset}", token=token
        )
        validate_rules_source(rules.get("source"))

        releases = self._http_json(
            "https://firebasehosting.googleapis.com/v1beta1/projects/-/sites/"
            f"{PROJECT}/channels/live/releases?pageSize=1",
            token=token,
        )
        latest = releases.get("releases", [])
        if len(latest) != 1 or latest[0].get("version", {}).get("name", "").split(
            "/"
        )[-1] != HOSTING_VERSION:
            raise RolloutError("live Hosting version is wrong")
        version_name = latest[0]["version"]["name"]
        version = self._http_json(
            f"https://firebasehosting.googleapis.com/v1beta1/{version_name}",
            token=token,
        )
        if version.get("status") != "FINALIZED":
            raise RolloutError("live Hosting version is not finalized")
        for domain in DOMAINS:
            nonce = urllib.parse.quote(self.short_sha)
            index = self._http_bytes(f"https://{domain}/index.html?phase1={nonce}")
            if hashlib.sha256(index).hexdigest() != INDEX_HASH:
                raise RolloutError("served index hash is wrong")
            if hashlib.sha256(
                self._http_bytes(f"https://{domain}/{JS_PATH}?phase1={nonce}")
            ).hexdigest() != JS_HASH:
                raise RolloutError("served JavaScript hash is wrong")
            if hashlib.sha256(
                self._http_bytes(f"https://{domain}/{CSS_PATH}?phase1={nonce}")
            ).hexdigest() != CSS_HASH:
                raise RolloutError("served stylesheet hash is wrong")

        switches = self._http_json(
            "https://firestore.googleapis.com/v1/projects/email-automation-cache/"
            "databases/(default)/documents/systemConfig/campaignAccess?"
            "mask.fieldPaths=creationEnabled&mask.fieldPaths=automationEnabled",
            token=token,
        )
        fields = switches.get("fields")
        if not isinstance(fields, dict) or set(fields) != {
            "creationEnabled",
            "automationEnabled",
        }:
            raise RolloutError("campaign switch readback shape is wrong")
        if fields["creationEnabled"].get("booleanValue") is not False or fields[
            "automationEnabled"
        ].get("booleanValue") is not False:
            raise RolloutError("global campaign switches are not both false")

    def verify_service_access(self, topology: Topology) -> None:
        validate_project_iam(
            self._json_command(["projects", "get-iam-policy", PROJECT], 60)
        )
        validate_iam(
            self._json_command(
                [
                    "run",
                    "services",
                    "get-iam-policy",
                    SERVICE,
                    "--region",
                    REGION,
                ],
                60,
            )
        )
        for base_url in (topology.service_url, *topology.tag_urls.values()):
            if self.unauthenticated_status(base_url, "/health") != 403:
                raise RolloutError("Cloud Run endpoint is not IAM-protected")

    def unauthenticated_status(self, base_url: str, path: str) -> int:
        request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=20
            ) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except (OSError, urllib.error.URLError) as error:
            raise RolloutError("unauthenticated HTTP probe failed") from error

    def artifact_image(self) -> str:
        digest = self._gcloud(
            [
                "artifacts",
                "docker",
                "images",
                "describe",
                f"{IMAGE_REPOSITORY}:{self.short_sha}",
                "--format=value(image_summary.digest)",
            ],
            60,
        )
        if DIGEST_RE.fullmatch(digest) is None:
            raise RolloutError("Artifact Registry digest is invalid")
        return f"{IMAGE_REPOSITORY}@{digest}"

    def get_service(self) -> Any:
        return self._json_command(
            ["run", "services", "describe", SERVICE, "--region", REGION], 60
        )

    def get_revision(self, name: str) -> Any:
        return self._json_command(
            ["run", "revisions", "describe", name, "--region", REGION], 60
        )

    def get_queue(self) -> Any:
        return self._json_command(
            ["tasks", "queues", "describe", QUEUE, "--location", REGION], 60
        )

    def list_tasks(self) -> Any:
        return self._json_command(
            [
                "tasks",
                "list",
                "--queue",
                QUEUE,
                "--location",
                REGION,
                "--limit=1000",
            ],
            60,
        )

    def pause_queue(self) -> None:
        self._gcloud(["tasks", "queues", "pause", QUEUE, "--location", REGION], 60)

    def resume_queue(self) -> None:
        self._gcloud(["tasks", "queues", "resume", QUEUE, "--location", REGION], 60)

    def add_cert_tag(self, tag: str, candidate: str) -> None:
        self._gcloud(
            [
                "run",
                "services",
                "update-traffic",
                SERVICE,
                "--region",
                REGION,
                "--update-tags",
                f"{tag}={candidate}",
            ],
            300,
        )

    def remove_cert_tag(self, tag: str) -> None:
        self._gcloud(
            [
                "run",
                "services",
                "update-traffic",
                SERVICE,
                "--region",
                REGION,
                "--remove-tags",
                tag,
            ],
            300,
        )

    def promote(self, candidate: str, old: str) -> None:
        self._gcloud(
            [
                "run",
                "services",
                "update-traffic",
                SERVICE,
                "--region",
                REGION,
                "--update-tags",
                f"release-a={candidate}",
                "--to-revisions",
                f"{candidate}=100,{old}=0",
            ],
            300,
        )

    def rollback(self, old: str, candidate: str) -> None:
        self._gcloud(
            [
                "run",
                "services",
                "update-traffic",
                SERVICE,
                "--region",
                REGION,
                "--update-tags",
                f"release-a={old}",
                "--to-revisions",
                f"{old}=100,{candidate}=0",
            ],
            300,
        )

    def _health_get(self, base_url: str, audience: str, path: str) -> Any:
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise RolloutError("health base URL is invalid")
        if not isinstance(audience, str) or not audience.startswith("https://"):
            raise RolloutError("health audience is invalid")
        audience = _cloud_run_base_url(audience)
        target = _cloud_run_base_url(base_url, allow_tagged=True)
        audience_host = urllib.parse.urlsplit(audience).hostname
        target_host = urllib.parse.urlsplit(target).hostname
        if not isinstance(audience_host, str) or not isinstance(target_host, str):
            raise RolloutError("health target or audience hostname is missing")
        if target_host != audience_host and not target_host.endswith(
            "---" + audience_host
        ):
            raise RolloutError("health target does not belong to the audience service")
        identity_token = self._gcloud(["auth", "print-identity-token"], 60)
        if not identity_token or any(character.isspace() for character in identity_token):
            raise RolloutError("identity token response is invalid")
        return self._http_json(base_url.rstrip("/") + path, token=identity_token)

    def identity_get(self, base_url: str, audience: str) -> Any:
        return self._health_get(base_url, audience, "/health/identity/v1")

    def legacy_health_get(self, base_url: str, audience: str) -> Any:
        return self._health_get(base_url, audience, "/health")


def _current_head(repo_root: Path) -> str:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RolloutError("could not resolve rollout HEAD") from error
    if SHA_RE.fullmatch(value) is None:
        raise RolloutError("rollout HEAD is invalid")
    return value


def main(argv: list[str]) -> int:
    if argv not in (["--dry-run"], ["--apply"], []):
        print("Usage: rollout_process_user_phase1.sh [--dry-run|--apply]", file=sys.stderr)
        return 64
    mode = argv[0] if argv else "--dry-run"
    repo_root = Path(__file__).resolve().parents[1]
    try:
        head = _current_head(repo_root)
        ops = SubprocessOps(repo_root, head)
        rollout = Phase1Rollout(ops=ops, head_sha=head)
        if mode == "--dry-run":
            print(rollout.dry_run())
            return 0
        previous_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }

        def interrupt_rollout(signum: int, frame: Any) -> None:
            del signum, frame
            raise KeyboardInterrupt()

        try:
            for signum in previous_handlers:
                signal.signal(signum, interrupt_rollout)
            rollout.apply()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        print(
            f"Phase 1 rollout verified: candidate={rollout.candidate}; "
            "queue=RUNNING; switches=false,false; provider-canary=not-run"
        )
        return 0
    except RolloutError as error:
        print(str(error), file=sys.stderr)
        return 78 if "MANUAL_RECOVERY" in str(error) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
