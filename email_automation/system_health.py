from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _count_collection(user_ref, collection_name: str, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection(collection_name)
        query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
        return len(list(query.stream()))
    except Exception as exc:
        print(f"⚠️ Could not count {collection_name}: {exc}")
        return -1


def _overall_status(token_state: Dict, graph_state: Dict, queues: Dict[str, int]) -> str:
    if token_state.get("status") == "error" or graph_state.get("status") == "error":
        return "error"
    if any(value and value > 0 for value in queues.values()):
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
        name: _count_collection(user_ref, name)
        for name in QUEUE_COLLECTIONS
    }

    return {
        "status": _overall_status(token_state, graph_state, queues),
        "token": token_state,
        "graph": graph_state,
        "queues": queues,
        "lastCheckedAt": now,
        "updatedAt": SERVER_TIMESTAMP,
    }


def write_user_health(user_id: str, payload: Dict, *, fs_client=None) -> None:
    fs_client = fs_client or _fs
    (
        fs_client.collection("users").document(user_id)
        .collection(HEALTH_COLLECTION).document(HEALTH_DOC_ID)
        .set(payload, merge=True)
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
