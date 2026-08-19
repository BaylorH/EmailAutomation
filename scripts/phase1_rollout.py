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
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request


# --- the candidate/twin classification, IMPORTED and never restated ----------
#
# This module and email_automation/certification/twin_contract.py enforce ONE
# classification of how a certification twin may differ from the production
# candidate. They used to hold two copies of it, and the copies drifted: the
# contract widened to the eight twin-only fields that
# scripts/deploy_certification_twin.sh really sets while this side still named
# three, so the rollout comparator would have refused EVERY twin the deploy
# script can produce -- at promotion time, under the lock, after the queue was
# already paused. A duplicated allowlist with nothing binding the two copies is
# how one classification became two files in the first place.
#
# Loaded BY PATH rather than as a package import, so this script keeps the
# stdlib-only isolation the docstring above promises: nothing is added to
# sys.path, no package __init__ runs, and twin_contract itself imports only
# copy/re/typing. A missing or unreadable contract raises here, at import, which
# is the fail-closed direction -- a rollout that cannot read the classification
# must not run with a guessed one.
_TWIN_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "email_automation"
    / "certification"
    / "twin_contract.py"
)
_TWIN_CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "phase1_rollout_twin_contract", _TWIN_CONTRACT_PATH
)
if _TWIN_CONTRACT_SPEC is None or _TWIN_CONTRACT_SPEC.loader is None:
    raise ImportError(f"twin contract not loadable from {_TWIN_CONTRACT_PATH}")
twin_contract = importlib.util.module_from_spec(_TWIN_CONTRACT_SPEC)
_TWIN_CONTRACT_SPEC.loader.exec_module(twin_contract)


ACCOUNT = "bp21harrison@gmail.com"
PROJECT = "email-automation-cache"
PROJECT_NUMBER = "248289505828"
REGION = "us-central1"
SERVICE = "process-user"
QUEUE = "graph-process-user"
BRANCH = "feat/native-image-attachment-ingestion-20260816"
IMAGE_REPOSITORY = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    "cloud-run-source-deploy/process-user"
)
OLD_REVISION = "process-user-stage-9491133f15d5"
OLD_IMAGE = (
    IMAGE_REPOSITORY
    + "@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
)
NATIVE_IMAGE_GATE_NAME = "SITESIFT_NATIVE_IMAGE_INGESTION"
NATIVE_IMAGE_GATE_VALUE = "false"
# The release stamp binds a serving revision to one reviewed source commit and
# one built artifact. Sorted, because the canonical comparison is on a sorted
# list and one spelling of the expected value is the whole point of a stamp.
RELEASE_STAMP_DIGEST_NAME = "SITESIFT_IMAGE_DIGEST"
RELEASE_STAMP_SOURCE_NAME = "SITESIFT_SOURCE_REVISION"
RELEASE_STAMP_NAMES = (RELEASE_STAMP_DIGEST_NAME, RELEASE_STAMP_SOURCE_NAME)
RULES_HASH = "7acf2bdbe2a7a42221efaa1ae15c2b406e4d6bef6b2c4131b3b0a6b5de8f8ee8"
HOSTING_VERSION = "a3758fb175d427f5"
INDEX_HASH = "33a041852c11a578b5d4836c64e76b7208afbbf20ccac2208d1b2fc10e0182c0"
JS_PATH = "static/js/main.e628d195.js"
JS_HASH = "7858189175c50bed17581c6f206988a6ba5918dbaab636b2ea2673f43de73ea9"
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
LOCK_DOCUMENT_ID = "processUserPhase1"
LOCK_DOCUMENT_NAME = (
    f"projects/{PROJECT}/databases/(default)/documents/"
    f"releaseLocks/{LOCK_DOCUMENT_ID}"
)
LOCK_COLLECTION_URL = (
    f"https://firestore.googleapis.com/v1/projects/{PROJECT}/"
    "databases/(default)/documents/releaseLocks"
)
LOCK_DOCUMENT_URL = (
    f"https://firestore.googleapis.com/v1/{LOCK_DOCUMENT_NAME}"
)
LOCK_PERMISSION_URL = (
    f"https://cloudresourcemanager.googleapis.com/v1/projects/{PROJECT}:"
    "testIamPermissions"
)
LOCK_PERMISSIONS = (
    "datastore.entities.create",
    "datastore.entities.delete",
    "datastore.entities.get",
)
TAG_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
NONCE_RE = re.compile(r"[0-9a-f]{64}")
UPDATE_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z"
)
EXPECTED_IAM = {
    "roles/run.invoker": (
        "serviceAccount:248289505828-compute@developer.gserviceaccount.com",
    ),
}
# --- the certification twin -------------------------------------------------
#
# The twin exists to run the SAME artifact under fixtures. It is IAM-private,
# carries no production traffic, and holds a service account with no mailbox,
# send, queue, or production-data authority. Everything below is an allowlist:
# a difference that is not named here is a failure, never a third category.
TWIN_SERVICE = "process-user-certification"
TWIN_RUNTIME_SERVICE_ACCOUNT = (
    f"sitesift-certification-runtime@{PROJECT}.iam.gserviceaccount.com"
)
TWIN_OPERATOR_SERVICE_ACCOUNT = (
    f"sitesift-certification-operator@{PROJECT}.iam.gserviceaccount.com"
)
TWIN_FIRESTORE_DATABASE = "sitesift-certification"
TWIN_FIXTURE_CONFIG_NAME = "CERTIFICATION_FIXTURE_CONFIG"
TWIN_FIXTURE_CONFIG_SECRET = "sitesift-certification-fixture-config"
# A positive decimal with no leading zero. `latest` is an alias that can be
# repointed after review, `0` is not a version, and `07` is a second spelling of
# the same number -- which would give one deployment two names for its own
# identity.
SECRET_VERSION_RE = re.compile(r"[1-9][0-9]*")
# The twin's OWN url. An OIDC audience naming some other service is the
# confused-deputy shape audience verification exists to stop. Cloud Run mints
# `https://<service>-<suffix>.<zone>.run.app`, and the service component has to
# be THIS service. Deliberately the same shape the contract enforces: two
# regexes for one fact is how two spellings of it drift apart.
TWIN_AUDIENCE_RE = re.compile(
    r"https://"
    + re.escape(TWIN_SERVICE)
    + r"-[A-Za-z0-9][A-Za-z0-9-]*\.[A-Za-z0-9-]+\.run\.app"
)
# The operator service account's numeric uniqueId. An address can be reassigned
# to a new principal; the numeric subject is what actually pins the identity, so
# a non-numeric one pins nothing.
OPERATOR_SUB_RE = re.compile(r"[0-9]+")
# Each of these is a capability to cause a real effect, so its PRESENCE on the
# twin is the failure, independent of value. Both tuples come from the contract
# rather than being restated -- see the loader at the top of this module.
TWIN_FORBIDDEN_ENV = twin_contract.FORBIDDEN_ON_TWIN
TWIN_ONLY_ENV = twin_contract.TWIN_ONLY
TWIN_EXPECTED_IAM = {
    "roles/run.invoker": (f"serviceAccount:{TWIN_OPERATOR_SERVICE_ACCOUNT}",),
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


class RolloutLockLost(RolloutError):
    """The durable rollout fencing record is absent, changed, or unreadable."""


class RolloutLockHeld(RolloutError):
    """Another exact invocation owns the durable rollout lock."""


class FirestoreExchangeError(RolloutError):
    """A Firestore HTTP exchange ended without an authoritative response."""

    def __init__(self, status: int | None) -> None:
        super().__init__("Firestore lock exchange failed")
        self.status = status


@dataclass(frozen=True)
class RolloutLock:
    owner_nonce: str
    head_sha: str
    update_time: str


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


def parse_lock_document(value: Any) -> RolloutLock:
    value = _object(value, "rollout lock document")
    if set(value) != {"name", "fields", "createTime", "updateTime"}:
        raise RolloutError("rollout lock document shape is not closed")
    if value.get("name") != LOCK_DOCUMENT_NAME:
        raise RolloutError("rollout lock document name is wrong")
    fields = _object(value.get("fields"), "rollout lock fields")
    if set(fields) != {"schemaVersion", "service", "headSha", "ownerNonce"}:
        raise RolloutError("rollout lock ownership packet is wrong")
    if (
        fields.get("schemaVersion") != {"integerValue": "1"}
        or fields.get("service") != {"stringValue": SERVICE}
    ):
        raise RolloutError("rollout lock ownership packet is wrong")
    head_value = fields.get("headSha")
    nonce_value = fields.get("ownerNonce")
    if not isinstance(head_value, dict) or not isinstance(nonce_value, dict):
        raise RolloutError("rollout lock identity fields are invalid")
    head_sha = head_value.get("stringValue")
    owner_nonce = nonce_value.get("stringValue")
    if (
        set(head_value) != {"stringValue"}
        or set(nonce_value) != {"stringValue"}
        or not isinstance(head_sha, str)
        or SHA_RE.fullmatch(head_sha) is None
        or not isinstance(owner_nonce, str)
        or NONCE_RE.fullmatch(owner_nonce) is None
    ):
        raise RolloutError("rollout lock identity is invalid")
    create_time = value.get("createTime")
    observed_update_time = value.get("updateTime")
    if (
        not isinstance(create_time, str)
        or UPDATE_TIME_RE.fullmatch(create_time) is None
        or not isinstance(observed_update_time, str)
        or UPDATE_TIME_RE.fullmatch(observed_update_time) is None
    ):
        raise RolloutError("rollout lock timestamps are invalid")
    return RolloutLock(owner_nonce, head_sha, observed_update_time)


def validate_lock_document(
    value: Any,
    *,
    owner_nonce: str,
    head_sha: str,
    update_time: str | None = None,
) -> RolloutLock:
    observed = parse_lock_document(value)
    if (
        observed.owner_nonce != owner_nonce
        or observed.head_sha != head_sha
        or (update_time is not None and observed.update_time != update_time)
    ):
        raise RolloutError("rollout lock ownership or updateTime changed")
    return observed


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


def release_stamp(expected_image: str, expected_source_revision: str) -> dict[str, str]:
    """The exact stamp a revision built from this commit and image must carry."""
    _, separator, digest = expected_image.partition("@")
    if not separator or DIGEST_RE.fullmatch(digest) is None:
        raise RolloutError("expected image is not pinned by an exact digest")
    if SHA_RE.fullmatch(expected_source_revision) is None:
        raise RolloutError("expected source revision is not an exact lowercase SHA")
    return {
        RELEASE_STAMP_DIGEST_NAME: digest,
        RELEASE_STAMP_SOURCE_NAME: expected_source_revision,
    }


def _canonical_revision_spec(
    value: Any,
    *,
    require_native_image_gate: bool,
    expected_stamp: Mapping[str, str] | None,
) -> dict[str, Any]:
    result = copy.deepcopy(_object(value, "revision spec"))
    containers = result.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("revision must have exactly one container")
    container = _object(containers[0], "revision container")
    containers[0] = container
    environment = container.get("env")
    if not isinstance(environment, list):
        raise RolloutError("revision environment is not a list")
    gate_entries = []
    stamp_entries = []
    retained_environment = []
    for raw_entry in environment:
        entry = _object(raw_entry, "revision environment entry")
        if not isinstance(entry.get("name"), str) or not entry.get("name"):
            raise RolloutError("revision environment entry name is invalid")
        if entry.get("name") == NATIVE_IMAGE_GATE_NAME:
            gate_entries.append(entry)
        elif entry.get("name") in RELEASE_STAMP_NAMES:
            stamp_entries.append(entry)
        else:
            retained_environment.append(entry)
    expected_gate_entries = (
        [{"name": NATIVE_IMAGE_GATE_NAME, "value": NATIVE_IMAGE_GATE_VALUE}]
        if require_native_image_gate
        else []
    )
    if gate_entries != expected_gate_entries:
        raise RolloutError("native image gate is not the exact dark contract")
    # Validate the stamp against its exact expected value BEFORE pairing it
    # away. Dropping an approved difference first would make any stamp -- a
    # forged one, or one from a different build -- compare equal to a baseline
    # that carries none.
    expected_stamp_entries = (
        [{"name": name, "value": expected_stamp[name]} for name in RELEASE_STAMP_NAMES]
        if expected_stamp is not None
        else []
    )
    if sorted(
        stamp_entries, key=lambda entry: entry["name"]
    ) != expected_stamp_entries:
        raise RolloutError("release stamp is not the exact source and image binding")
    container["env"] = retained_environment
    container.pop("image", None)
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


def validate_candidate_dark_identity(
    candidate: Any,
    expected_name: str,
    expected_image: str,
    *,
    expected_source_revision: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = _object(candidate, "candidate revision")
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
    containers = spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("candidate container contract is invalid")
    container = _object(containers[0], "candidate container")
    if container.get("image") != expected_image:
        raise RolloutError("candidate image does not match immutable digest")
    status = _object(candidate.get("status"), "candidate status")
    if status.get("imageDigest") != expected_image:
        raise RolloutError("candidate status image digest is wrong")
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        raise RolloutError("candidate conditions are not a list")
    ready = [
        row
        for row in conditions
        if isinstance(row, dict) and row.get("type") == "Ready"
    ]
    if len(ready) != 1 or ready[0].get("status") != "True":
        raise RolloutError("candidate is not exactly Ready")
    _canonical_revision_spec(
        spec,
        require_native_image_gate=True,
        expected_stamp=release_stamp(expected_image, expected_source_revision),
    )
    return metadata, spec


def _validate_candidate_parity(
    metadata: Any,
    spec: Any,
    baseline: Any,
    *,
    expected_stamp: Mapping[str, str],
) -> None:
    baseline = _object(baseline, "baseline revision")
    baseline_spec = _object(baseline.get("spec"), "baseline spec")
    if _canonical_revision_spec(
        spec, require_native_image_gate=True, expected_stamp=expected_stamp
    ) != _canonical_revision_spec(
        baseline_spec, require_native_image_gate=False, expected_stamp=None
    ):
        raise RolloutError("candidate config differs from baseline beyond image")
    if _canonical_revision_metadata(metadata) != _canonical_revision_metadata(
        _object(baseline.get("metadata"), "baseline metadata")
    ):
        raise RolloutError("candidate functional metadata differs from baseline")


def validate_candidate(
    candidate: Any,
    baseline: Any,
    expected_name: str,
    expected_image: str,
    *,
    expected_source_revision: str,
) -> None:
    metadata, spec = validate_candidate_dark_identity(
        candidate,
        expected_name,
        expected_image,
        expected_source_revision=expected_source_revision,
    )
    _validate_candidate_parity(
        metadata,
        spec,
        baseline,
        expected_stamp=release_stamp(expected_image, expected_source_revision),
    )


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


def _environment_by_name(container: Any, label: str) -> dict[str, dict[str, Any]]:
    container = _object(container, f"{label} container")
    environment = container.get("env")
    if not isinstance(environment, list):
        raise RolloutError(f"{label} environment is not a list")
    by_name: dict[str, dict[str, Any]] = {}
    for raw_entry in environment:
        entry = _object(raw_entry, f"{label} environment entry")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise RolloutError(f"{label} environment entry name is invalid")
        if name in by_name:
            raise RolloutError(f"{label} environment duplicates {name}")
        by_name[name] = entry
    return by_name


def _validate_twin_iam(policy: Any) -> None:
    policy = _object(policy, "twin IAM policy")
    bindings = policy.get("bindings")
    if not isinstance(bindings, list):
        raise RolloutError("twin IAM bindings are missing")
    normalized: dict[str, tuple[str, ...]] = {}
    for row in bindings:
        row = _object(row, "twin IAM binding")
        if set(row) != {"role", "members"}:
            raise RolloutError("twin IAM binding contains unexpected fields")
        role = row.get("role")
        members = row.get("members")
        if (
            not isinstance(role, str)
            or role in normalized
            or not isinstance(members, list)
            or not all(isinstance(member, str) for member in members)
        ):
            raise RolloutError("twin IAM binding shape is invalid")
        normalized[role] = tuple(sorted(members))
    if normalized != TWIN_EXPECTED_IAM:
        raise RolloutError("twin IAM policy is not the exact private contract")


def _validate_twin_fixture_secret(entry: Mapping[str, Any]) -> None:
    if "value" in entry:
        raise RolloutError("twin fixture config is a literal, not a secret reference")
    reference = entry.get("valueFrom")
    if not isinstance(reference, dict) or set(reference) != {"secretKeyRef"}:
        raise RolloutError("twin fixture config reference shape is invalid")
    secret = reference.get("secretKeyRef")
    if not isinstance(secret, dict) or set(secret) != {"name", "key"}:
        raise RolloutError("twin fixture config secret reference shape is invalid")
    if secret.get("name") != TWIN_FIXTURE_CONFIG_SECRET:
        raise RolloutError("twin fixture config names the wrong secret")
    version = secret.get("key")
    if not isinstance(version, str) or SECRET_VERSION_RE.fullmatch(version) is None:
        raise RolloutError(
            "twin fixture config must pin a positive decimal secret version"
        )


def _twin_only_literal(twin_env: Mapping[str, Any], name: str) -> str:
    """The literal value of a twin-only field, or a refusal.

    Allowlisted: only a PLAIN literal is readable. A secret reference here would
    make the value unknowable at validation time, and a value the comparator has
    to guess at is not a value it may approve. Extra keys are refused for the
    same reason -- an entry carrying both a literal and a reference has two
    answers, and the comparator may not pick one.
    """
    entry = twin_env[name]
    value = entry.get("value")
    if set(entry) != {"name", "value"} or not isinstance(value, str) or not value:
        raise RolloutError(f"twin {name} is not a plain literal value")
    return value


def _validate_twin_certification_identity(
    twin_env: Mapping[str, Any], *, expected_candidate_revision: str
) -> None:
    """The five twin-only fields the deploy script sets, held to exact values.

    Classifying a name is not the same as excusing it. The allowlist above
    proves only that the NAME is approved; without these rules a twin could
    carry an audience naming another service, an operator nobody authorised, or
    a fixture version disagreeing with the secret it actually mounted, and the
    comparator would report nothing at all.

    Each rule raises its OWN sentence, distinct from the generic
    "exists only on the twin and is unclassified" refusal. A rule whose only
    evidence is a message an adjacent generic check also produces is decorative:
    it goes on passing after the rule is deleted. Two rules in the sibling
    comparator were found to be exactly that.
    """
    if TAG_RE.fullmatch(expected_candidate_revision) is None:
        raise RolloutError("expected candidate revision is not a revision name")

    revision = _twin_only_literal(
        twin_env, "SITESIFT_PRODUCTION_CANDIDATE_REVISION"
    )
    # Checked BEFORE the equality below because the twin's own service name has
    # the candidate's certification prefix: a twin naming a revision of itself
    # is certifying itself, and that deserves its own refusal rather than the
    # generic "not the candidate" one.
    if revision == TWIN_SERVICE or revision.startswith(f"{TWIN_SERVICE}-"):
        raise RolloutError(
            "twin production candidate revision names the twin's own service"
        )
    if revision != expected_candidate_revision:
        raise RolloutError(
            "twin does not name the production candidate under certification"
        )

    # The second spelling of the fixture version. The deploy script sets this
    # and the secret reference from ONE variable, so disagreement means the run
    # cannot say which fixture it executed against.
    version = _twin_only_literal(
        twin_env, "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION"
    )
    if SECRET_VERSION_RE.fullmatch(version) is None:
        raise RolloutError(
            "twin fixture config secret version is not a positive decimal"
        )
    mounted = (
        twin_env[TWIN_FIXTURE_CONFIG_NAME]
        .get("valueFrom", {})
        .get("secretKeyRef", {})
        .get("key")
    )
    if version != mounted:
        raise RolloutError(
            "twin fixture config version and secret reference disagree"
        )

    audience = _twin_only_literal(twin_env, "SITESIFT_CERTIFICATION_AUDIENCE")
    if TWIN_AUDIENCE_RE.fullmatch(audience) is None:
        raise RolloutError("twin certification audience is not the twin's own URL")

    operator = _twin_only_literal(
        twin_env, "SITESIFT_CERTIFICATION_OPERATOR_EMAIL"
    )
    if operator != TWIN_OPERATOR_SERVICE_ACCOUNT:
        raise RolloutError(
            "twin certification operator is not the approved operator account"
        )

    subject = _twin_only_literal(twin_env, "SITESIFT_CERTIFICATION_OPERATOR_SUB")
    if OPERATOR_SUB_RE.fullmatch(subject) is None:
        raise RolloutError(
            "twin certification operator subject is not a numeric uniqueId"
        )


def validate_twin_stamp(
    service_value: Any,
    policy: Any,
    *,
    candidate_spec: Any,
    expected_image: str,
    expected_source_revision: str,
    expected_candidate_revision: str,
    production: Topology,
) -> None:
    """Prove the twin is the same artifact with none of the authority.

    Approved asymmetries are named and checked against their exact expected
    values. Anything else -- an extra name, a missing name, a differing value --
    is a failure, because an unclassified difference is exactly where a real one
    would hide.
    """
    stamp = release_stamp(expected_image, expected_source_revision)
    service_value = _object(service_value, "twin service")
    metadata = _object(service_value.get("metadata"), "twin metadata")
    if metadata.get("name") != TWIN_SERVICE:
        raise RolloutError("twin service identity is not exact")
    annotations = metadata.get("annotations", {})
    if not isinstance(annotations, dict) or annotations.get(
        "run.googleapis.com/ingress"
    ) != "internal":
        raise RolloutError("twin ingress is not internal")

    spec = _object(service_value.get("spec"), "twin spec")
    template = _object(spec.get("template"), "twin template")
    template_spec = _object(template.get("spec"), "twin template spec")
    if template_spec.get("serviceAccountName") != TWIN_RUNTIME_SERVICE_ACCOUNT:
        raise RolloutError("twin does not run as the certification runtime account")
    candidate_spec = _object(candidate_spec, "candidate spec")
    if template_spec.get("serviceAccountName") == candidate_spec.get(
        "serviceAccountName"
    ):
        raise RolloutError("twin runs as the production service account")

    containers = template_spec.get("containers")
    if not isinstance(containers, list) or len(containers) != 1:
        raise RolloutError("twin must have exactly one container")
    if containers[0].get("image") != expected_image:
        raise RolloutError("twin does not run the exact candidate artifact")

    # The twin is never a traffic target -- neither its own routes pointing at a
    # production revision, nor a production route pointing at the twin.
    twin_revisions: set[str] = set()
    for surface in ("spec", "status"):
        rows = _object(service_value.get(surface), f"twin {surface}").get("traffic")
        positive, tags, _ = _parse_traffic(rows, f"twin {surface}")
        twin_revisions.update(positive)
        twin_revisions.update(tags.values())
    for name in twin_revisions:
        if not name.startswith(f"{TWIN_SERVICE}-"):
            raise RolloutError("twin routes traffic to a revision it does not own")
    production_revisions = {production.positive_revision, *production.tags.values()}
    for name in production_revisions:
        if name == TWIN_SERVICE or name.startswith(f"{TWIN_SERVICE}-"):
            raise RolloutError("the twin is a production traffic target")

    twin_env = _environment_by_name(containers[0], "twin")
    candidate_containers = candidate_spec.get("containers")
    if not isinstance(candidate_containers, list) or len(candidate_containers) != 1:
        raise RolloutError("candidate must have exactly one container")
    candidate_env = _environment_by_name(candidate_containers[0], "candidate")

    for name in TWIN_FORBIDDEN_ENV:
        if name in twin_env:
            raise RolloutError(f"twin carries production capability {name}")
    for name in TWIN_ONLY_ENV:
        if name not in twin_env:
            raise RolloutError(f"twin is missing required certification field {name}")
        if name in candidate_env:
            raise RolloutError(f"{name} must not appear on the candidate")
    if twin_env["K_SERVICE"].get("value") != TWIN_SERVICE:
        raise RolloutError("twin K_SERVICE is not the certification service")
    if twin_env["FIRESTORE_DATABASE"].get("value") != TWIN_FIRESTORE_DATABASE:
        raise RolloutError("twin FIRESTORE_DATABASE is not the certification database")
    _validate_twin_fixture_secret(twin_env[TWIN_FIXTURE_CONFIG_NAME])
    _validate_twin_certification_identity(
        twin_env, expected_candidate_revision=expected_candidate_revision
    )

    for name, value in stamp.items():
        if twin_env.get(name, {}).get("value") != value:
            raise RolloutError(f"twin release stamp {name} is not this build")

    approved = set(TWIN_FORBIDDEN_ENV) | set(TWIN_ONLY_ENV)
    for name in sorted(set(twin_env) | set(candidate_env)):
        if name in approved:
            continue
        if name not in candidate_env:
            raise RolloutError(f"{name} exists only on the twin and is unclassified")
        if name not in twin_env:
            raise RolloutError(
                f"{name} exists only on the candidate and is unclassified"
            )
        if twin_env[name] != candidate_env[name]:
            raise RolloutError(f"{name} differs between candidate and twin")

    _validate_twin_iam(policy)


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
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if SHA_RE.fullmatch(head_sha) is None:
            raise RolloutError("HEAD is not an exact lowercase SHA")
        self.ops = ops
        self.head_sha = head_sha
        self.short_sha = head_sha[:12]
        self.candidate = f"{SERVICE}-stage-{self.short_sha}"
        self.cert_tag = f"phase1-cert-{self.short_sha}"
        self.sleeper = sleeper
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(32))
        self.task_observed = False

    def dry_run(self) -> str:
        return (
            f"dry-run: zero gcloud or HTTP commands; candidate={self.candidate}; "
            "queue remains RUNNING; traffic and tags remain unchanged"
        )

    def verify_staging_prerequisites(self) -> None:
        """Prove the approved rules -> UI -> backend order before any build."""
        self.ops.preflight()
        self.ops.verify_lock_permissions()
        self.ops.verify_rules_ui_switches()
        topology = validate_topology(
            self.ops.get_service(),
            expected_positive=OLD_REVISION,
            expected_release=OLD_REVISION,
            expected_aux=AUX_TAGS,
        )
        self.ops.verify_service_access(topology)
        validate_old_revision(self.ops.get_revision(OLD_REVISION))
        _validate_legacy_health(
            self.ops.legacy_health_get(topology.service_url, topology.service_url)
        )
        _validate_legacy_health(
            self.ops.legacy_health_get(
                topology.tag_urls["release-a"], topology.service_url
            )
        )
        validate_queue(self.ops.get_queue(), "RUNNING")
        if not self._tasks_are_empty():
            raise RolloutError("staging prerequisite tasks are not empty")

    def clear_orphan_lock_old_state(self, lock: RolloutLock) -> None:
        if lock.head_sha != self.head_sha:
            raise RolloutError("orphan lock HEAD does not equal checkout HEAD")
        self._baseline()
        if not self._tasks_are_empty():
            raise RolloutError("orphan lock tasks are not empty")
        self.ops.assert_lock(lock)
        self._baseline()
        if not self._tasks_are_empty():
            raise RolloutError("orphan lock tasks are not empty")
        self.ops.assert_lock(lock)
        self.ops.release_lock(lock)

    def _baseline(self) -> tuple[Any, Any, str]:
        self.ops.preflight()
        self.ops.verify_lock_permissions()
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
        validate_candidate(
            candidate,
            old,
            self.candidate,
            image,
            expected_source_revision=self.head_sha,
        )
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

    def _held_lock(self, lock: RolloutLock | None) -> RolloutLock:
        """The held lock, or a refusal.

        Everything below exists to provide one property: this step happened
        while the rollout lock was held. A ``None`` lock would make that
        property VACUOUS rather than violated -- ``assert_lock`` would never
        run, the step would proceed, and nothing would error. Vacuous is the
        harder failure to notice, so the invariant is stated here rather than
        inferred from the fact that every caller happens to sit downstream of
        ``acquire_lock``.

        ``apply`` currently guarantees this by coupling: ``lock`` is ``None``
        only before ``acquire_lock`` returns, and ``pause_attempted`` -- the
        flag that admits anything to the cleanup path -- is set only after it
        does. That coupling is real but it lives in two distant assignments,
        and it is not the kind of thing that should have to be reconstructed by
        a reader of the under-lock slice.
        """
        if lock is None:
            raise RolloutLockLost("rollout step attempted without a held lock")
        return lock

    def _locked_mutation(
        self, lock: RolloutLock | None, operation: Callable[[], None]
    ) -> None:
        lock = self._held_lock(lock)
        self.ops.assert_lock(lock)
        operation()
        self.ops.assert_lock(lock)

    def _validate_locked_pre_promotion(self, lock: RolloutLock | None) -> None:
        lock = self._held_lock(lock)
        self.ops.assert_lock(lock)
        image = self.ops.artifact_image()
        if not isinstance(image, str) or not image.startswith(IMAGE_REPOSITORY + "@"):
            raise RolloutError("artifact image is invalid")
        candidate = self.ops.get_revision(self.candidate)
        metadata, spec = validate_candidate_dark_identity(
            candidate,
            self.candidate,
            image,
            expected_source_revision=self.head_sha,
        )
        rollback = self.ops.get_revision(OLD_REVISION)
        validate_old_revision(rollback)
        _validate_candidate_parity(
            metadata,
            spec,
            rollback,
            expected_stamp=release_stamp(image, self.head_sha),
        )
        self.ops.verify_rules_ui_switches()
        topology = validate_topology(
            self.ops.get_service(),
            expected_positive=OLD_REVISION,
            expected_release=OLD_REVISION,
            expected_aux=AUX_TAGS,
        )
        self.ops.verify_service_access(topology)
        # Under the same lock, and before any traffic change: the certification
        # twin must still be the same artifact with none of the authority. A
        # twin proved outside this lock is a twin something can replace between
        # the proof and the promotion that relies on it.
        validate_twin_stamp(
            self.ops.get_twin_service(),
            self.ops.get_twin_iam_policy(),
            candidate_spec=spec,
            expected_image=image,
            expected_source_revision=self.head_sha,
            expected_candidate_revision=self.candidate,
            production=topology,
        )
        validate_queue(self.ops.get_queue(), "PAUSED")
        if not self._tasks_are_empty():
            raise RolloutError("task appeared before promotion")
        self.ops.assert_lock(lock)

    def _cleanup_failure(
        self,
        *,
        pause_attempted: bool,
        tag_attempted: bool,
        traffic_attempted: bool,
        lock: RolloutLock | None,
    ) -> bool:
        if not pause_attempted:
            # Nothing was mutated, so there is nothing to undo -- and this is
            # the ONLY branch on which a caller may still be holding no lock.
            return True
        # Past this point every step goes through _locked_mutation, which
        # refuses a None lock by name. Restating that requirement here would
        # read as a second control while being unable to fail independently of
        # the first, and a check that cannot be killed is decoration.
        cleanup_ok = True
        try:
            self._locked_mutation(lock, self.ops.pause_queue)
            validate_queue(self.ops.get_queue(), "PAUSED")
        except RolloutLockLost:
            raise
        except BaseException:
            raise RolloutError("MANUAL_RECOVERY: queue state unverified")
        if tag_attempted:
            try:
                self._locked_mutation(
                    lock, lambda: self.ops.remove_cert_tag(self.cert_tag)
                )
            except RolloutLockLost:
                raise
            except BaseException:
                cleanup_ok = False
        if traffic_attempted:
            try:
                self._locked_mutation(lock, self.ops.pause_queue)
                validate_queue(self.ops.get_queue(), "PAUSED")
                self._locked_mutation(
                    lock, lambda: self.ops.rollback(OLD_REVISION, self.candidate)
                )
            except RolloutLockLost:
                raise
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
                self._locked_mutation(lock, self.ops.pause_queue)
                validate_queue(self.ops.get_queue(), "PAUSED")
                self._locked_mutation(lock, self.ops.resume_queue)
                validate_queue(self.ops.get_queue(), "RUNNING")
            except RolloutLockLost:
                raise
            except BaseException:
                try:
                    self._locked_mutation(lock, self.ops.pause_queue)
                    validate_queue(self.ops.get_queue(), "PAUSED")
                except RolloutLockLost:
                    raise
                except BaseException as error:
                    raise RolloutError(
                        "MANUAL_RECOVERY: queue state unverified"
                    ) from error
                return False
        else:
            try:
                self._locked_mutation(lock, self.ops.pause_queue)
                validate_queue(self.ops.get_queue(), "PAUSED")
            except RolloutLockLost:
                raise
            except BaseException as error:
                raise RolloutError(
                    "MANUAL_RECOVERY: queue state unverified"
                ) from error
        return cleanup_ok

    def apply(self) -> None:
        pause_attempted = False
        tag_attempted = False
        traffic_attempted = False
        lock: RolloutLock | None = None
        release_lock = False
        try:
            self._baseline()
            nonce = self.nonce_factory()
            if not isinstance(nonce, str) or NONCE_RE.fullmatch(nonce) is None:
                raise RolloutError("rollout lock nonce is invalid")
            prior_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
            )
            try:
                lock = self.ops.acquire_lock(self.head_sha, nonce)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
            self.ops.assert_lock(lock)
            self._baseline()
            self.ops.assert_lock(lock)
            pause_attempted = True
            self._locked_mutation(lock, self.ops.pause_queue)
            validate_queue(self.ops.get_queue(), "PAUSED")
            for index in range(3):
                validate_queue(self.ops.get_queue(), "PAUSED")
                if not self._tasks_are_empty():
                    raise RolloutError("queue is not drained")
                if index < 2:
                    self.sleeper(5)

            tag_attempted = True
            self._locked_mutation(
                lock, lambda: self.ops.add_cert_tag(self.cert_tag, self.candidate)
            )
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
            _validate_legacy_health(
                self.ops.legacy_health_get(cert_url, tagged.service_url)
            )

            self._locked_mutation(
                lock, lambda: self.ops.remove_cert_tag(self.cert_tag)
            )
            validate_topology(
                self.ops.get_service(),
                expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION,
                expected_aux=AUX_TAGS,
            )
            tag_attempted = False
            self._validate_locked_pre_promotion(lock)
            traffic_attempted = True
            self._locked_mutation(
                lock, lambda: self.ops.promote(self.candidate, OLD_REVISION)
            )
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
                expected_source_revision=self.head_sha,
            )
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared after promotion")
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
            self._locked_mutation(lock, self.ops.pause_queue)
            validate_queue(self.ops.get_queue(), "PAUSED")
            if not self._tasks_are_empty():
                raise RolloutError("task appeared immediately before queue resume")
            self._locked_mutation(lock, self.ops.resume_queue)
            validate_queue(self.ops.get_queue(), "RUNNING")
            self.ops.verify_rules_ui_switches()
            self.ops.assert_lock(lock)
            release_lock = True
        except RolloutLockLost as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock ownership lost; queue state unverified"
            ) from error
        except BaseException as error:
            if lock is not None:
                try:
                    self.ops.assert_lock(lock)
                except RolloutLockLost as lock_error:
                    raise RolloutError(
                        "MANUAL_RECOVERY: rollout lock ownership lost; "
                        "queue state unverified"
                    ) from lock_error
            try:
                cleanup_ok = self._cleanup_failure(
                    pause_attempted=pause_attempted,
                    tag_attempted=tag_attempted,
                    traffic_attempted=traffic_attempted,
                    lock=lock,
                )
            except RolloutLockLost as lock_error:
                raise RolloutError(
                    "MANUAL_RECOVERY: rollout lock ownership lost; "
                    "queue state unverified"
                ) from lock_error
            except RolloutError:
                raise
            if not cleanup_ok:
                raise RolloutError("MANUAL_RECOVERY: queue left paused") from error
            release_lock = lock is not None
            if isinstance(error, RolloutError):
                raise
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise RolloutError("rollout interrupted safely") from error
            raise RolloutError("rollout failed safely") from error
        finally:
            if lock is not None and release_lock:
                try:
                    self.ops.release_lock(lock)
                except BaseException as error:
                    raise RolloutError(
                        "MANUAL_RECOVERY: rollout lock release unverified"
                    ) from error


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

    def _firestore_exchange(
        self, method: str, url: str, body: bytes | None = None
    ) -> tuple[int, bytes]:
        if method not in {"GET", "POST", "DELETE"}:
            raise RolloutError("Firestore lock method is invalid")
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Cache-Control": "no-cache",
            "x-goog-user-project": PROJECT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, headers=headers, data=body, method=method
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=20
            ) as response:
                if response.geturl() != url:
                    raise FirestoreExchangeError(response.status)
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (OSError, urllib.error.URLError) as error:
            raise FirestoreExchangeError(None) from error

    @staticmethod
    def _decode_firestore_document(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RolloutError("Firestore lock document is invalid JSON") from error

    @staticmethod
    def _is_google_error(
        status: int, raw: bytes, *, code: int, name: str
    ) -> bool:
        if status != code:
            return False
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(value, dict) or set(value) != {"error"}:
            return False
        error = value.get("error")
        return (
            isinstance(error, dict)
            and set(error) == {"code", "message", "status"}
            and error.get("code") == code
            and isinstance(error.get("message"), str)
            and error.get("status") == name
        )

    def verify_lock_permissions(self) -> None:
        body = json.dumps(
            {"permissions": list(LOCK_PERMISSIONS)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            status, raw = self._firestore_exchange(
                "POST", LOCK_PERMISSION_URL, body
            )
            value = json.loads(raw) if status == 200 else None
        except (FirestoreExchangeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RolloutError("rollout lock permissions are unverified") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"permissions"}
            or not isinstance(value.get("permissions"), list)
            or len(value["permissions"]) != len(LOCK_PERMISSIONS)
            or not all(isinstance(item, str) for item in value["permissions"])
            or set(value["permissions"]) != set(LOCK_PERMISSIONS)
        ):
            raise RolloutError("rollout lock permissions are not exact")

    def _observe_lock(
        self, *, owner_nonce: str, head_sha: str
    ) -> RolloutLock | None:
        try:
            status, raw = self._firestore_exchange("GET", LOCK_DOCUMENT_URL)
        except BaseException as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            ) from error
        if self._is_google_error(status, raw, code=404, name="NOT_FOUND"):
            return None
        if status != 200:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            )
        try:
            observed = parse_lock_document(self._decode_firestore_document(raw))
        except RolloutError as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            ) from error
        if observed.owner_nonce == owner_nonce and observed.head_sha == head_sha:
            return observed
        raise RolloutLockHeld("rollout lock is held by another invocation")

    def _resolve_ambiguous_lock_create(
        self,
        *,
        body: bytes,
        create_url: str,
        owner_nonce: str,
        head_sha: str,
    ) -> RolloutLock:
        try:
            observed = self._observe_lock(
                owner_nonce=owner_nonce, head_sha=head_sha
            )
        except RolloutLockHeld as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            ) from error
        if observed is not None:
            return observed
        try:
            status, raw = self._firestore_exchange("POST", create_url, body)
        except BaseException:
            status, raw = -1, b""
        if status == 200:
            try:
                return validate_lock_document(
                    self._decode_firestore_document(raw),
                    owner_nonce=owner_nonce,
                    head_sha=head_sha,
                )
            except RolloutError:
                pass
        elif self._is_google_error(status, raw, code=409, name="ALREADY_EXISTS"):
            pass
        try:
            observed = self._observe_lock(
                owner_nonce=owner_nonce, head_sha=head_sha
            )
        except RolloutLockHeld as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            ) from error
        if observed is not None:
            return observed
        raise RolloutError("MANUAL_RECOVERY: rollout lock acquisition unverified")

    def _reconcile_interrupted_lock_create(
        self,
        *,
        body: bytes,
        create_url: str,
        owner_nonce: str,
        head_sha: str,
    ) -> None:
        lock = self._resolve_ambiguous_lock_create(
            body=body,
            create_url=create_url,
            owner_nonce=owner_nonce,
            head_sha=head_sha,
        )
        try:
            self.release_lock(lock)
        except BaseException as error:
            raise RolloutError(
                "MANUAL_RECOVERY: rollout lock acquisition unverified"
            ) from error
        raise RolloutError("rollout interrupted safely")

    def acquire_lock(self, head_sha: str, owner_nonce: str) -> RolloutLock:
        if SHA_RE.fullmatch(head_sha) is None or NONCE_RE.fullmatch(owner_nonce) is None:
            raise RolloutError("rollout lock identity is invalid")
        body = json.dumps(
            {
                "fields": {
                    "schemaVersion": {"integerValue": "1"},
                    "service": {"stringValue": SERVICE},
                    "headSha": {"stringValue": head_sha},
                    "ownerNonce": {"stringValue": owner_nonce},
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        create_url = LOCK_COLLECTION_URL + "?" + urllib.parse.urlencode(
            {"documentId": LOCK_DOCUMENT_ID}
        )
        try:
            status, raw = self._firestore_exchange("POST", create_url, body)
            if status == 200:
                try:
                    return validate_lock_document(
                        self._decode_firestore_document(raw),
                        owner_nonce=owner_nonce,
                        head_sha=head_sha,
                    )
                except RolloutError:
                    return self._resolve_ambiguous_lock_create(
                        body=body,
                        create_url=create_url,
                        owner_nonce=owner_nonce,
                        head_sha=head_sha,
                    )
            if self._is_google_error(status, raw, code=409, name="ALREADY_EXISTS"):
                raise RolloutLockHeld("rollout lock is held by another invocation")
            if status in {400, 401, 403, 404} and self._is_google_error(
                status,
                raw,
                code=status,
                name={
                    400: "INVALID_ARGUMENT",
                    401: "UNAUTHENTICATED",
                    403: "PERMISSION_DENIED",
                    404: "NOT_FOUND",
                }[status],
            ):
                raise RolloutError("rollout lock create was rejected")
            return self._resolve_ambiguous_lock_create(
                body=body,
                create_url=create_url,
                owner_nonce=owner_nonce,
                head_sha=head_sha,
            )
        except (KeyboardInterrupt, SystemExit):
            self._reconcile_interrupted_lock_create(
                body=body,
                create_url=create_url,
                owner_nonce=owner_nonce,
                head_sha=head_sha,
            )
            raise AssertionError("interrupted lock reconciliation returned")
        except FirestoreExchangeError:
            return self._resolve_ambiguous_lock_create(
                body=body,
                create_url=create_url,
                owner_nonce=owner_nonce,
                head_sha=head_sha,
            )

    def assert_lock(self, lock: RolloutLock) -> None:
        try:
            status, raw = self._firestore_exchange("GET", LOCK_DOCUMENT_URL)
            if status != 200:
                raise RolloutLockLost("rollout lock is absent or unreadable")
            validate_lock_document(
                self._decode_firestore_document(raw),
                owner_nonce=lock.owner_nonce,
                head_sha=lock.head_sha,
                update_time=lock.update_time,
            )
        except (FirestoreExchangeError, RolloutError) as error:
            if isinstance(error, RolloutLockLost):
                raise
            raise RolloutLockLost("rollout lock is absent, changed, or unreadable") from error

    def release_lock(self, lock: RolloutLock) -> None:
        self.assert_lock(lock)
        delete_url = LOCK_DOCUMENT_URL + "?" + urllib.parse.urlencode(
            {"currentDocument.updateTime": lock.update_time}
        )
        for attempt in range(2):
            try:
                status, raw = self._firestore_exchange("DELETE", delete_url)
            except BaseException:
                status, raw = -1, b""
            if status not in {200, -1} and not self._is_google_error(
                status, raw, code=404, name="NOT_FOUND"
            ):
                raise RolloutError(
                    "MANUAL_RECOVERY: rollout lock release unverified"
                )
            try:
                read_status, read_raw = self._firestore_exchange(
                    "GET", LOCK_DOCUMENT_URL
                )
            except BaseException as error:
                raise RolloutError(
                    "MANUAL_RECOVERY: rollout lock release unverified"
                ) from error
            if self._is_google_error(
                read_status, read_raw, code=404, name="NOT_FOUND"
            ):
                return
            if read_status != 200:
                raise RolloutError(
                    "MANUAL_RECOVERY: rollout lock release unverified"
                )
            try:
                validate_lock_document(
                    self._decode_firestore_document(read_raw),
                    owner_nonce=lock.owner_nonce,
                    head_sha=lock.head_sha,
                    update_time=lock.update_time,
                )
            except RolloutError as error:
                raise RolloutError(
                    "MANUAL_RECOVERY: rollout lock release unverified"
                ) from error
            if attempt == 1:
                break
        raise RolloutError("MANUAL_RECOVERY: rollout lock release unverified")

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

    def get_twin_service(self) -> Any:
        return self._json_command(
            ["run", "services", "describe", TWIN_SERVICE, "--region", REGION], 60
        )

    def get_twin_iam_policy(self) -> Any:
        return self._json_command(
            ["run", "services", "get-iam-policy", TWIN_SERVICE, "--region", REGION],
            60,
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
    clear_mode = len(argv) == 4 and argv[0] == "--clear-orphan-lock-old-state"
    if argv not in (
        ["--dry-run"],
        ["--apply"],
        ["--verify-staging-prerequisites"],
        [],
    ) and not clear_mode:
        print(
            "Usage: rollout_process_user_phase1.sh [--dry-run|--apply|"
            "--verify-staging-prerequisites|"
            "--clear-orphan-lock-old-state HEAD NONCE UPDATE_TIME]",
            file=sys.stderr,
        )
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
        if mode == "--verify-staging-prerequisites":
            rollout.verify_staging_prerequisites()
            print(
                "Phase 1 staging prerequisites verified: rules=exact; "
                "hosting=exact; switches=false,false; queue=RUNNING; tasks=0"
            )
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
            if clear_mode:
                lock = RolloutLock(
                    owner_nonce=argv[2],
                    head_sha=argv[1],
                    update_time=argv[3],
                )
                if (
                    NONCE_RE.fullmatch(lock.owner_nonce) is None
                    or SHA_RE.fullmatch(lock.head_sha) is None
                    or UPDATE_TIME_RE.fullmatch(lock.update_time) is None
                ):
                    raise RolloutError("orphan lock arguments are invalid")
                rollout.clear_orphan_lock_old_state(lock)
            else:
                rollout.apply()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        if clear_mode:
            print(
                "Phase 1 orphan lock cleared after exact old-state proof; "
                "queue=RUNNING; switches=false,false; provider-canary=not-run"
            )
        else:
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
