from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Sequence


BAYLOR_DEV_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"


class SchedulerScopeError(RuntimeError):
    """Raised when a manual scheduler run is not safely scoped."""


@dataclass(frozen=True)
class SchedulerUserScope:
    mode: str
    user_ids: List[str]


def _csv(value: str | None) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _ordered_intersection(available_user_ids: Sequence[str], requested_user_ids: Iterable[str]) -> List[str]:
    requested = set(requested_user_ids)
    return [uid for uid in available_user_ids if uid in requested]


def _is_cloud_run_runtime() -> bool:
    """True when running on Cloud Run — a Job (CLOUD_RUN_JOB / CLOUD_RUN_EXECUTION)
    or a Service (K_SERVICE / K_REVISION). Any of these injected vars suffices; a
    Service deployment must hit the fail-closed gate too, not fall through to the
    legacy all-user default."""
    return bool(
        os.getenv("CLOUD_RUN_JOB")
        or os.getenv("CLOUD_RUN_EXECUTION")
        or os.getenv("K_SERVICE")
        or os.getenv("K_REVISION")
    )


def _is_github_actions() -> bool:
    """Positive proof we are running under GitHub Actions, where the scope env is
    pinned in a git-reviewed workflow file. Only in that trusted runtime may the
    scheduler default to all-user processing without an explicit opt-in."""
    return os.getenv("GITHUB_ACTIONS") == "true" or bool(os.getenv("GITHUB_EVENT_NAME"))


def resolve_scheduler_user_ids(available_user_ids: Sequence[str] | None = None) -> SchedulerUserScope:
    """Resolve the user ids the scheduler is allowed to process for this run.

    Scheduled GitHub Actions production runs keep the existing all-user
    behavior. Manual GitHub dispatches must be explicitly marked as the
    developer scoped scheduler and must request only allowlisted development
    users.

    Cloud Run runtime (CLOUD_RUN_JOB / CLOUD_RUN_EXECUTION for Jobs, or
    K_SERVICE / K_REVISION for Services) is fail-closed: on GitHub Actions the
    scope env was pinned in a git-reviewed
    workflow file, but on Cloud Run it lives in mutable job config, so a
    dropped or mistyped SITESIFT_DEV_SCOPED_SCHEDULER must never silently
    widen to every live user. All-user processing on Cloud Run requires the
    explicit opt-in SITESIFT_SCHEDULER_ALLOW_ALL_USERS='1'; otherwise the run
    must be dev-scoped (SITESIFT_DEV_SCOPED_SCHEDULER='1' + allowlisted
    targets) or it raises SchedulerScopeError before any user is touched.
    """
    available = list(available_user_ids or [])
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    dev_scoped = os.getenv("SITESIFT_DEV_SCOPED_SCHEDULER") == "1"

    if _is_cloud_run_runtime() and not dev_scoped:
        if os.getenv("SITESIFT_SCHEDULER_ALLOW_ALL_USERS") == "1":
            return SchedulerUserScope(mode="all", user_ids=available)
        raise SchedulerScopeError(
            "Cloud Run scheduler scope is fail-closed: set "
            "SITESIFT_DEV_SCOPED_SCHEDULER='1' with allowlisted "
            "SITESIFT_SCHEDULER_TARGET_USER_IDS for a dev-scoped run, or "
            "explicitly opt in to all-user processing with "
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS='1'."
        )

    if not dev_scoped:
        if not _is_github_actions():
            # Unrecognized runtime — not GitHub Actions and not a detected Cloud
            # Run Job/Service. A locally-run image (e.g. `docker run` with prod
            # secrets) or any future host that doesn't inject the vars above must
            # NEVER silently process every live user; that is the same footgun
            # the Cloud Run gate closes. Require the explicit all-user opt-in or
            # fail closed before any user is touched.
            if os.getenv("SITESIFT_SCHEDULER_ALLOW_ALL_USERS") == "1":
                return SchedulerUserScope(mode="all", user_ids=available)
            raise SchedulerScopeError(
                "Unrecognized scheduler runtime is fail-closed: no GitHub Actions "
                "or Cloud Run context detected. Set SITESIFT_DEV_SCOPED_SCHEDULER='1' "
                "with allowlisted SITESIFT_SCHEDULER_TARGET_USER_IDS for a dev-scoped "
                "run, or explicitly opt in to all-user processing with "
                "SITESIFT_SCHEDULER_ALLOW_ALL_USERS='1'."
            )
        if event_name != "workflow_dispatch":
            return SchedulerUserScope(mode="all", user_ids=available)
        raise SchedulerScopeError(
            "Manual workflow_dispatch runs are disabled unless the dev-scoped scheduler guard is enabled."
        )

    allowed_user_ids = set(_csv(os.getenv("SITESIFT_SCHEDULER_ALLOWED_USER_IDS")) or [BAYLOR_DEV_UID])
    requested_user_ids = _csv(os.getenv("SITESIFT_SCHEDULER_TARGET_USER_IDS"))
    if not requested_user_ids:
        raise SchedulerScopeError("Dev-scoped scheduler requires SITESIFT_SCHEDULER_TARGET_USER_IDS.")

    disallowed = sorted(set(requested_user_ids) - allowed_user_ids)
    if disallowed:
        raise SchedulerScopeError(
            f"Requested scheduler user id(s) are not allowed for dev-scoped runs: {', '.join(disallowed)}"
        )

    resolved = _ordered_intersection(available, requested_user_ids)
    missing = sorted(set(requested_user_ids) - set(resolved))
    if missing:
        raise SchedulerScopeError(
            f"Requested scheduler user id(s) do not have available token caches: {', '.join(missing)}"
        )

    return SchedulerUserScope(mode="dev_scoped", user_ids=resolved)
