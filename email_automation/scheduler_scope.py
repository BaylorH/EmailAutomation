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


def resolve_scheduler_user_ids(available_user_ids: Sequence[str] | None = None) -> SchedulerUserScope:
    """Resolve the user ids the scheduler is allowed to process for this run.

    Scheduled production runs keep the existing all-user behavior. Manual
    GitHub dispatches must be explicitly marked as the developer scoped
    scheduler and must request only allowlisted development users.
    """
    available = list(available_user_ids or [])
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    dev_scoped = os.getenv("SITESIFT_DEV_SCOPED_SCHEDULER") == "1"

    if event_name != "workflow_dispatch" and not dev_scoped:
        return SchedulerUserScope(mode="all", user_ids=available)

    if not dev_scoped:
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
