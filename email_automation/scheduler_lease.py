from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from google.cloud.firestore import SERVER_TIMESTAMP, transactional

from .clients import _fs


DEFAULT_LEASE_ID = "emailAutomation"
DEFAULT_TTL_SECONDS = 45 * 60

# Per-user (webhook) lease: a user-scoped mutex keyed schedulerLeases/
# emailAutomation:{uid}. TTL is SHORT because a single user's pipeline run
# takes seconds — 10 min just bounds how long a crashed/killed webhook run can
# wedge that one user before the lease self-expires and the next request (or a
# Cloud Tasks retry) can reclaim it. Kept well under the 45-min global batch TTL
# above; the two lease families never share a doc, so the GHA-cron global path
# is completely unaffected.
DEFAULT_USER_LEASE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    owner: Optional[str]
    lease_id: str
    expires_at: Optional[datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_owner() -> str:
    # A globally-unique owner is what makes the lease safe: two runners must
    # never resolve to the same string, or owner-checked release lets one free
    # the other's lease and both process concurrently. On Cloud Run the
    # container entrypoint is PID 1 and the hostname is not guaranteed unique,
    # so hostname:pid can collide on "<host>:1" across executions — prefer the
    # per-execution/per-task identifiers Cloud Run injects.
    run_id = os.getenv("GITHUB_RUN_ID") or os.getenv("RENDER_INSTANCE_ID")
    if run_id:
        return run_id
    cloud_run_execution = os.getenv("CLOUD_RUN_EXECUTION")
    if cloud_run_execution:
        task_index = os.getenv("CLOUD_RUN_TASK_INDEX", "0")
        return f"{cloud_run_execution}:{task_index}"
    return f"{socket.gethostname()}:{os.getpid()}"


def _lease_ref(fs_client, lease_id: str):
    return fs_client.collection("schedulerLeases").document(lease_id)


def _normalize_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def acquire_scheduler_lease(
    *,
    fs_client=None,
    lease_id: str = DEFAULT_LEASE_ID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> LeaseResult:
    fs_client = fs_client or _fs
    owner = owner or _default_owner()
    now = now or _utc_now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    doc_ref = _lease_ref(fs_client, lease_id)
    transaction = fs_client.transaction()

    @transactional
    def claim(transaction, ref):
        snapshot = ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        existing_owner = data.get("owner")
        existing_expiry = _normalize_datetime(data.get("expiresAt"))
        existing_status = data.get("status")

        if existing_status == "running" and existing_expiry and existing_expiry > now:
            return LeaseResult(False, existing_owner, lease_id, existing_expiry)

        transaction.set(ref, {
            "owner": owner,
            "status": "running",
            "startedAt": now,
            "expiresAt": expires_at,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
        return LeaseResult(True, owner, lease_id, expires_at)

    return claim(transaction, doc_ref)


def release_scheduler_lease(
    *,
    fs_client=None,
    lease_id: str = DEFAULT_LEASE_ID,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    fs_client = fs_client or _fs
    owner = owner or _default_owner()
    now = now or _utc_now()
    doc_ref = _lease_ref(fs_client, lease_id)
    transaction = fs_client.transaction()

    @transactional
    def release(transaction, ref):
        snapshot = ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if data.get("owner") != owner or data.get("status") != "running":
            return False
        transaction.update(ref, {
            "status": "released",
            "releasedAt": now,
            "updatedAt": SERVER_TIMESTAMP,
        })
        return True

    return release(transaction, doc_ref)


def run_with_scheduler_lease(
    callback: Callable[[], None],
    *,
    fs_client=None,
    lease_id: str = DEFAULT_LEASE_ID,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    owner: Optional[str] = None,
) -> bool:
    owner = owner or _default_owner()
    lease = acquire_scheduler_lease(
        fs_client=fs_client,
        lease_id=lease_id,
        ttl_seconds=ttl_seconds,
        owner=owner,
    )
    if not lease.acquired:
        print(
            f"⏭️ Scheduler lease held by {lease.owner}; "
            f"expires at {lease.expires_at}. Skipping this run."
        )
        return False

    try:
        callback()
        return True
    finally:
        release_scheduler_lease(
            fs_client=fs_client,
            lease_id=lease_id,
            owner=owner,
        )


# ---------------------------------------------------------------------------
# Per-user (webhook) lease
#
# The webhook path processes ONE user per HTTP request instead of iterating the
# whole batch, so it needs a mutex scoped to that user rather than the single
# global scheduler lease. These helpers reuse the exact transactional
# claim/refuse/release above (acquire_scheduler_lease / release_scheduler_lease)
# but key the Firestore doc per-uid and default to the short user TTL. The
# global-lease entry point (run_with_scheduler_lease) is left fully intact — the
# GHA cron keeps using it until cutover.
# ---------------------------------------------------------------------------


def user_lease_id(uid: str) -> str:
    """Firestore lease-doc id for a single user: ``emailAutomation:{uid}``."""
    return f"{DEFAULT_LEASE_ID}:{uid}"


def acquire_user_lease(
    uid: str,
    *,
    fs_client=None,
    ttl_seconds: int = DEFAULT_USER_LEASE_TTL_SECONDS,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> LeaseResult:
    """Claim the per-user lease. Same transactional semantics as the global
    lease (an unexpired ``running`` lease held by anyone refuses the claim),
    but namespaced to ``uid`` so distinct users never contend."""
    return acquire_scheduler_lease(
        fs_client=fs_client,
        lease_id=user_lease_id(uid),
        ttl_seconds=ttl_seconds,
        owner=owner,
        now=now,
    )


def release_user_lease(
    uid: str,
    *,
    fs_client=None,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Release the per-user lease (owner-checked, same as the global release)."""
    return release_scheduler_lease(
        fs_client=fs_client,
        lease_id=user_lease_id(uid),
        owner=owner,
        now=now,
    )


def run_with_user_lease(
    uid: str,
    callback: Callable[[], None],
    *,
    fs_client=None,
    ttl_seconds: int = DEFAULT_USER_LEASE_TTL_SECONDS,
    owner: Optional[str] = None,
) -> bool:
    """Run ``callback`` while holding the per-user lease for ``uid``.

    Mirrors ``run_with_scheduler_lease``: acquire the (per-user) lease, run the
    callback, release in a ``finally`` even on exception. Returns True if the
    lease was acquired and the callback ran, False if the user is already being
    processed (same-uid concurrent invocation is refused/skipped cleanly).
    Exceptions from ``callback`` propagate to the caller after release, so the
    HTTP layer can surface a 5xx (and the lease never stays wedged).
    """
    owner = owner or _default_owner()
    lease = acquire_user_lease(
        uid,
        fs_client=fs_client,
        ttl_seconds=ttl_seconds,
        owner=owner,
    )
    if not lease.acquired:
        print(
            f"⏭️ User lease for {uid} held by {lease.owner}; "
            f"expires at {lease.expires_at}. Skipping this request."
        )
        return False

    try:
        callback()
        return True
    finally:
        release_user_lease(uid, fs_client=fs_client, owner=owner)
