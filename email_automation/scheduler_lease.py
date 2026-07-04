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
