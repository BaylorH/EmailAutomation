from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


RENEWAL_RUNWAY = timedelta(hours=24)


@dataclass(frozen=True)
class WorkerMailboxReadiness:
    ready: bool
    reason: str


def _as_utc_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def read_worker_mailbox_readiness(
    firestore_client,
    user_id: str,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkerMailboxReadiness:
    uid = str(user_id or "").strip()
    if not uid:
        return WorkerMailboxReadiness(False, "mailbox_readiness_unavailable")

    try:
        current_snapshot = (
            firestore_client.collection("users").document(uid)
            .collection("graphSubscription").document("current")
            .get()
        )
        if not current_snapshot.exists:
            return WorkerMailboxReadiness(False, "mailbox_not_ready")

        current = current_snapshot.to_dict() or {}
        subscription_id = str(current.get("subscriptionId") or "").strip()
        client_state = str(current.get("clientState") or "").strip()
        expiration = _as_utc_datetime(current.get("expirationDateTime"))
        current_time = _as_utc_datetime(now())
        if (
            current.get("status") != "active"
            or not subscription_id
            or not client_state
            or expiration is None
            or current_time is None
            or expiration <= current_time + RENEWAL_RUNWAY
        ):
            return WorkerMailboxReadiness(False, "mailbox_not_ready")

        reverse_snapshot = (
            firestore_client.collection("graphSubscriptions")
            .document(subscription_id)
            .get()
        )
        reverse = reverse_snapshot.to_dict() if reverse_snapshot.exists else None
        if not reverse or reverse.get("uid") != uid or reverse.get("clientState") != client_state:
            return WorkerMailboxReadiness(False, "mailbox_not_ready")

        return WorkerMailboxReadiness(True, "ready")
    except Exception:
        return WorkerMailboxReadiness(False, "mailbox_readiness_unavailable")
