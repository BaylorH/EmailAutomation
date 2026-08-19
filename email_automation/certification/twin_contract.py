"""The locked candidate/twin normalized-difference matrix.

The certification twin must be the SAME artifact as the production candidate in
every way that could change behaviour, and different in a short, NAMED list of
ways that make it unable to cause an effect. Everything else being equal is the
whole point: a twin differing in a model name, a send cap, or a budget would be
certifying different software than the one being shipped.

The comparison technique is the load-bearing part.

Differences are never DELETED before comparison. Each approved asymmetry is
replaced on both sides by the same PAIRED SENTINEL, and only after the exact
expected value has been validated. Deleting an asymmetric key would hide the
approved difference and, with it, any unapproved difference that happened to
share the name. Normalising before validating would make any two values look
equal, which is the same failure wearing a nicer hat.

Every difference is therefore either approved-and-paired or a failure. There is
deliberately no third category, because that is exactly where a real one would
hide.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Mapping, Tuple

SENTINEL_PREFIX = "«paired:"

def _sentinel(name: str) -> str:
    return f"{SENTINEL_PREFIX}{name}»"


# Present on the candidate, and their PRESENCE on the twin is a hard failure.
# Each one is a capability to cause a real effect.
FORBIDDEN_ON_TWIN = (
    "FIREBASE_BUCKET",
    "AZURE_API_APP_ID",
    "AZURE_API_CLIENT_SECRET",
    "FIREBASE_API_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "PROCESS_USER_AUTH",
    "SITESIFT_AUTO_REPLY_ALLOWLIST",
    "SITESIFT_TOUR_ACTION_ALLOWLIST",
)

# Must exist ONLY on the twin, with exactly these shapes.
TWIN_ONLY = ("K_SERVICE", "FIRESTORE_DATABASE", "CERTIFICATION_FIXTURE_CONFIG")

EXPECTED_CERTIFICATION_DATABASE = "sitesift-certification"
EXPECTED_TWIN_SERVICE = "process-user-certification"
FIXTURE_CONFIG_SECRET = "sitesift-certification-fixture-config"

# A positive decimal with no leading zero. `latest` is an alias that can be
# repointed, `0` is not a version, and `07` is a different string for the same
# number -- which would give one deployment two spellings of its own identity.
_SECRET_VERSION = re.compile(r"^[1-9][0-9]*$")

_TWIN_RUNTIME_SA = re.compile(r"^sitesift-certification-runtime@[^@]+$")
_IMAGE_BY_DIGEST = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")


class TwinContractError(RuntimeError):
    """Malformed input. Distinct from "the two specs differ"."""


def _env(spec: Mapping[str, Any]) -> Dict[str, str]:
    env = spec.get("env")
    if not isinstance(env, Mapping):
        raise TwinContractError("spec has no env map")
    return {str(k): str(v) for k, v in env.items()}


def _validate(candidate: Mapping[str, Any],
              twin: Mapping[str, Any]) -> List[str]:
    """Every rule that must hold before anything may be normalised."""
    problems: List[str] = []
    candidate_env, twin_env = _env(candidate), _env(twin)

    # -- image: identical, and pinned by digest --------------------------
    for label, spec in (("candidate", candidate), ("twin", twin)):
        if not _IMAGE_BY_DIGEST.match(str(spec.get("image", ""))):
            problems.append(f"{label} image is not pinned by digest: {spec.get('image')}")
    if candidate.get("image") != twin.get("image"):
        problems.append("image differs between candidate and twin; the twin would "
                        "certify a different build")

    # -- fields that must be byte-equal ----------------------------------
    for field in ("containerConcurrency", "timeoutSeconds"):
        if candidate.get(field) != twin.get(field):
            problems.append(f"{field} differs: {candidate.get(field)} vs {twin.get(field)}")

    # -- the twin is never a production traffic target -------------------
    if int(twin.get("trafficPercent", 0)) != 0:
        problems.append("twin carries production traffic; it may never be a target")

    # -- credentials must be ABSENT from the twin ------------------------
    for name in FORBIDDEN_ON_TWIN:
        if name in twin_env:
            problems.append(f"{name} is present on the twin; it must be absent")

    # -- twin-only fields -------------------------------------------------
    for name in TWIN_ONLY:
        if name not in twin_env:
            problems.append(f"{name} is missing from the twin")
        if name in candidate_env:
            problems.append(f"{name} must not appear on the candidate")

    if twin_env.get("K_SERVICE") not in (None, EXPECTED_TWIN_SERVICE):
        problems.append("twin K_SERVICE is not the expected certification service")
    if twin_env.get("FIRESTORE_DATABASE") not in (None, EXPECTED_CERTIFICATION_DATABASE):
        problems.append("twin FIRESTORE_DATABASE is not the certification database")

    reference = twin_env.get("CERTIFICATION_FIXTURE_CONFIG")
    if reference is not None:
        secret, _, version = reference.partition(":")
        if secret != FIXTURE_CONFIG_SECRET:
            problems.append("CERTIFICATION_FIXTURE_CONFIG names the wrong secret")
        elif not _SECRET_VERSION.match(version):
            problems.append(
                "CERTIFICATION_FIXTURE_CONFIG must pin a positive decimal version; "
                f"{version!r} is an alias, zero, or non-canonical"
            )

    # -- identities that must differ, with exact expected values ---------
    if twin.get("serviceName") != EXPECTED_TWIN_SERVICE:
        problems.append("twin serviceName is not the expected certification service")
    if candidate.get("serviceName") == twin.get("serviceName"):
        problems.append("candidate and twin share a service name")
    if not _TWIN_RUNTIME_SA.match(str(twin.get("serviceAccount", ""))):
        problems.append("twin does not run as the certification runtime service account")
    if candidate.get("serviceAccount") == twin.get("serviceAccount"):
        problems.append("twin runs as the production service account")

    # -- symmetric env: everything else must match exactly ---------------
    approved = set(FORBIDDEN_ON_TWIN) | set(TWIN_ONLY)
    for name in sorted(set(candidate_env) | set(twin_env)):
        if name in approved:
            continue
        if name not in candidate_env:
            # Not classified as twin-only, not on the candidate: nobody approved
            # this asymmetry, and an unclassified difference is where a real one
            # would hide.
            problems.append(f"{name} exists only on the twin and is unclassified")
        elif name not in twin_env:
            problems.append(f"{name} exists only on the candidate and is unclassified")
        elif candidate_env[name] != twin_env[name]:
            problems.append(f"{name} differs between candidate and twin")
    return problems


def normalize(candidate: Mapping[str, Any],
              twin: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Replace each APPROVED asymmetry with the same paired sentinel on both sides."""
    left, right = copy.deepcopy(dict(candidate)), copy.deepcopy(dict(twin))
    left["env"], right["env"] = _env(candidate), _env(twin)

    for field in ("serviceName", "serviceAccount"):
        left[field] = right[field] = _sentinel(field)

    for name in FORBIDDEN_ON_TWIN:
        # Inserted on BOTH sides rather than deleted from the candidate, so the
        # key still exists to be compared and a stray twin value cannot vanish.
        left["env"][name] = right["env"][name] = _sentinel(f"candidate-only:{name}")
    for name in TWIN_ONLY:
        left["env"][name] = right["env"][name] = _sentinel(f"twin-only:{name}")
    return left, right


def compare(candidate: Mapping[str, Any], twin: Mapping[str, Any]) -> List[str]:
    """Validate first, then normalize, then deep-compare. Order is the contract."""
    problems = _validate(candidate, twin)
    if problems:
        return problems

    left, right = normalize(candidate, twin)
    residual: List[str] = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            residual.append(f"{key}: unpaired difference after normalization")
    return residual
