from typing import Any, Dict, Optional, Tuple

from google.cloud.firestore import SERVER_TIMESTAMP


CLIENT_AUTOMATION_PAUSED_REASON = "client_automation_paused"
ORPHAN_DELETED_CAMPAIGN_REASON = "orphan_deleted_campaign"
# "completed" is terminal: `_maybe_mark_client_completed` (processing.py) auto-marks a
# campaign completed once every thread is terminal and no work remains. Honoring it here
# is what stops a finished campaign from being monitored — so campaigns no longer have to
# be frozen by hand. A freshly-started campaign is never "completed", so this never gates
# new work.
CLIENT_TERMINAL_STATUSES = {"stopped", "archived", "deleted", "completed"}


def normalize_client_status(value: Any) -> str:
    return str(value or "").strip().lower()


def is_client_automation_paused(client_data: Optional[Dict[str, Any]]) -> bool:
    """True when a campaign/client should not send, auto-reply, or schedule follow-ups."""
    if not isinstance(client_data, dict):
        return False
    if client_data.get("automationPaused") is True or client_data.get("automation_paused") is True:
        return True
    return normalize_client_status(client_data.get("status")) in CLIENT_TERMINAL_STATUSES


def client_automation_pause_reason(client_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(client_data, dict):
        return CLIENT_AUTOMATION_PAUSED_REASON
    return (
        client_data.get("automationPauseReason")
        or client_data.get("statusReason")
        or client_data.get("pauseReason")
        or CLIENT_AUTOMATION_PAUSED_REASON
    )


def get_client_automation_pause(
    user_id: str,
    client_id: Optional[str],
    *,
    firestore_client=None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Fetch client state and return whether automation should be stopped for it."""
    if not user_id or not client_id:
        return False, "", {}

    fs = firestore_client
    if fs is None:
        from .clients import _fs

        fs = _fs

    try:
        client_doc = (
            fs.collection("users")
            .document(user_id)
            .collection("clients")
            .document(str(client_id))
            .get()
        )
    except Exception as e:
        print(f"   ⚠️ Could not fetch client automation state for {client_id}: {e}")
        return False, "", {}

    if not getattr(client_doc, "exists", False):
        # DELIBERATE fail-open. A missing client doc is NOT proof of a deleted campaign:
        # a live thread can legitimately reach here with a clientId whose doc read returns
        # not-exists (fixture-shaped state, eventual consistency, mid-provision). Gating on
        # exists==False over-gates the mainline inbound path (proven by the tour/nonviable
        # processing suite). True orphan-gating needs an explicit deletion tombstone
        # (e.g. clients/{id}.deletedAt or a deleted-campaigns registry) so a *confirmed*
        # deletion can be told apart from an absent read — do it there, not here. See
        # ORPHAN_DELETED_CAMPAIGN_REASON.
        return False, "", {}

    client_data = client_doc.to_dict() or {}
    if not is_client_automation_paused(client_data):
        return False, "", client_data
    return True, client_automation_pause_reason(client_data), client_data


def stopped_followup_patch(reason: str = CLIENT_AUTOMATION_PAUSED_REASON) -> Dict[str, Any]:
    return {
        "status": "stopped",
        "followUpStatus": "stopped",
        "statusReason": reason or CLIENT_AUTOMATION_PAUSED_REASON,
        "automationPaused": True,
        "automationPauseReason": reason or CLIENT_AUTOMATION_PAUSED_REASON,
        "followUpConfig.enabled": False,
        "followUpConfig.nextFollowUpAt": None,
        "followUpConfig.processingBy": None,
        "followUpConfig.processingAt": None,
        "updatedAt": SERVER_TIMESTAMP,
    }
