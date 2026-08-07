from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from google.cloud.firestore import SERVER_TIMESTAMP

from .clients import _fs


HEALTH_COLLECTION = "systemHealth"
HEALTH_DOC_ID = "emailAutomation"
QUEUE_COLLECTIONS = (
    "outbox",
    "deadLetterQueue",
    "pendingResponses",
    "processingFailures",
)

RESOLVED_DEAD_LETTER_STATUSES = {
    "acknowledged",
    "discarded",
    "reconciled",
    "requeued",
}

TERMINAL_DEAD_LETTER_STATUSES = RESOLVED_DEAD_LETTER_STATUSES | {
    "cancelled",
    "canceled",
    "campaign_stopped",
}

NON_ACTIONABLE_PROCESSING_FAILURE_RECOVERY_STATUSES = {
    "blocked_existing_outbound_artifact",
    "blocked_manual_continuation",
    "blocked_manual_conversation_continued",
    "blocked_manual_retry_guard_unreadable",
    "blocked_retry_guard_unreadable",
    "campaign_stopped",
    "operator_replay_blocked_artifact",
    "operator_replay_blocked_postcondition",
    "replayed",
    "stale_manual_review",
}


# A queue count of this sentinel means the Firestore read failed — the count is
# UNKNOWN, not zero. Health must never treat an unknown count as an empty queue.
COUNT_ERROR = -1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _count_error_severity() -> str:
    """Severity applied when a queue count could not be read.

    Fail-closed by default: absence of config -> "error" (health cannot go green
    while a queue read is failing). Operators may downgrade to "warning" via
    HEALTH_COUNT_ERROR_SEVERITY, but there is deliberately no value that lets an
    unreadable count report "healthy" — that would restore the silent-lie bug.
    """
    raw = str(os.environ.get("HEALTH_COUNT_ERROR_SEVERITY") or "").strip().lower()
    return "warning" if raw == "warning" else "error"


def _count_error_queues(queues: Dict[str, int]) -> List[str]:
    return [name for name, value in queues.items() if isinstance(value, int) and value < 0]


def _count_collection(user_ref, collection_name: str, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection(collection_name)
        query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
        return len(list(query.stream()))
    except Exception as exc:
        print(f"⚠️ Could not count {collection_name}: {exc}")
        return COUNT_ERROR


def _snapshot_data(snapshot) -> Dict:
    if hasattr(snapshot, "to_dict"):
        return snapshot.to_dict() or {}
    return {}


def _is_terminal_dead_letter(data: Dict) -> bool:
    status = str(data.get("status") or "").strip().lower()
    recovery_status = str(data.get("recoveryStatus") or "").strip().lower()
    return (
        data.get("retryable") is False
        or status in TERMINAL_DEAD_LETTER_STATUSES
        or recovery_status in TERMINAL_DEAD_LETTER_STATUSES
    )


def _is_resolved_dead_letter(data: Dict) -> bool:
    """Backward-compatible alias for the shared terminal predicate."""
    return _is_terminal_dead_letter(data)


def _is_actionable_processing_failure(data: Dict) -> bool:
    if data.get("retryable") is False:
        return False
    recovery_status = str(data.get("recoveryStatus") or "").strip().lower()
    return recovery_status not in NON_ACTIONABLE_PROCESSING_FAILURE_RECOVERY_STATUSES


def _count_active_dead_letters(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("deadLetterQueue")
        query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
        return sum(
            1
            for snapshot in query.stream()
            if not _is_terminal_dead_letter(_snapshot_data(snapshot))
        )
    except Exception as exc:
        print(f"⚠️ Could not count active deadLetterQueue: {exc}")
        return COUNT_ERROR


def _count_actionable_processing_failures(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("processingFailures")
        query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
        return sum(
            1
            for snapshot in query.stream()
            if _is_actionable_processing_failure(_snapshot_data(snapshot))
        )
    except Exception as exc:
        print(f"⚠️ Could not count actionable processingFailures: {exc}")
        return COUNT_ERROR


def _overall_status(token_state: Dict, graph_state: Dict, queues: Dict[str, int]) -> str:
    if token_state.get("status") == "error" or graph_state.get("status") == "error":
        return "error"
    # Fail closed: a queue we could not read (COUNT_ERROR sentinel) is an UNKNOWN
    # backlog, not an empty one. It must never be treated as healthy — a Firestore
    # read outage could be hiding a growing dead-letter / pending backlog of stuck
    # or misdirected sends. Default severity is "error"; operators may downgrade to
    # "warning" via HEALTH_COUNT_ERROR_SEVERITY but never to "healthy".
    if _count_error_queues(queues):
        return _count_error_severity()
    if any(value > 0 for value in queues.values()):
        return "warning"
    if token_state.get("status") == "unknown" or graph_state.get("status") == "unknown":
        return "warning"
    return "healthy"


def collect_user_health(
    user_id: str,
    *,
    fs_client=None,
    token_state: Optional[Dict] = None,
    graph_state: Optional[Dict] = None,
    now: Optional[datetime] = None,
) -> Dict:
    fs_client = fs_client or _fs
    token_state = token_state or {"status": "unknown"}
    graph_state = graph_state or {"status": "unknown"}
    now = now or _utc_now()
    user_ref = fs_client.collection("users").document(user_id)
    queues = {
        name: (
            _count_active_dead_letters(user_ref)
            if name == "deadLetterQueue"
            else (
                _count_actionable_processing_failures(user_ref)
                if name == "processingFailures"
                else _count_collection(user_ref, name)
            )
        )
        for name in QUEUE_COLLECTIONS
    }

    return {
        "status": _overall_status(token_state, graph_state, queues),
        "token": token_state,
        "graph": graph_state,
        "queues": queues,
        "countErrors": _count_error_queues(queues),
        "lastCheckedAt": now,
        "updatedAt": SERVER_TIMESTAMP,
    }


def write_user_health(user_id: str, payload: Dict, *, fs_client=None) -> None:
    fs_client = fs_client or _fs
    (
        fs_client.collection("users").document(user_id)
        .collection(HEALTH_COLLECTION).document(HEALTH_DOC_ID)
        .set(payload)
    )


def record_user_health(
    user_id: str,
    *,
    fs_client=None,
    token_state: Optional[Dict] = None,
    graph_state: Optional[Dict] = None,
) -> Dict:
    payload = collect_user_health(
        user_id,
        fs_client=fs_client,
        token_state=token_state,
        graph_state=graph_state,
    )
    write_user_health(user_id, payload, fs_client=fs_client)
    return payload
