from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from google.cloud.firestore import SERVER_TIMESTAMP


CAMPAIGN_AUTOMATION_ALLOW = "allow"
CAMPAIGN_AUTOMATION_BLOCKED = "blocked"
CAMPAIGN_AUTOMATION_UNKNOWN = "unknown"

CLIENT_AUTOMATION_PAUSED_REASON = "client_automation_paused"
CLIENT_AUTOMATION_STATE_NOT_FOUND_REASON = "client_automation_state_not_found"
CLIENT_AUTOMATION_STATE_READ_ERROR_REASON = "client_automation_state_read_error"
CLIENT_AUTOMATION_STATE_MALFORMED_REASON = "client_automation_state_malformed"
MISSING_CLIENT_ID_REASON = "missing_client_id"
GLOBAL_AUTOMATION_DISABLED_REASON = "global_automation_disabled"
GLOBAL_AUTOMATION_STATE_READ_ERROR_REASON = "global_automation_state_read_error"
GLOBAL_AUTOMATION_STATE_MALFORMED_REASON = "global_automation_state_malformed"
GLOBAL_CAMPAIGN_ACCESS_SOURCE = "systemConfig/campaignAccess"
BAYLOR_GLOBAL_AUTOMATION_FALLBACK_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"

CLIENT_TERMINAL_STATUSES = {"stopping", "stopped", "archived", "deleted", "completed"}
CLIENT_ACTIVE_STATUSES = {"active", "live"}
PAUSE_REASON_FIELDS = (
    "automationPauseReason",
    "statusReason",
    "pauseReason",
    "pausedReason",
)


@dataclass(frozen=True)
class CampaignAutomationDecision:
    """A fail-closed decision for work that would autonomously affect a campaign."""

    state: str
    reason: str
    client_data: Dict[str, Any]
    metadata: Dict[str, Any]

    @property
    def allows_autonomous_work(self) -> bool:
        return self.state == CAMPAIGN_AUTOMATION_ALLOW

    @property
    def denies_autonomous_work(self) -> bool:
        return not self.allows_autonomous_work


def normalize_client_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _pause_reason(client_data: Dict[str, Any], default: str) -> Tuple[str, Optional[str]]:
    for field in PAUSE_REASON_FIELDS:
        value = client_data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip(), field
    return default, None


def _decision(
    state: str,
    reason: str,
    client_data: Optional[Dict[str, Any]] = None,
    *,
    source: str = "",
    stop_kind: str = "none",
    terminal: bool = False,
    reason_field: Optional[str] = None,
) -> CampaignAutomationDecision:
    return CampaignAutomationDecision(
        state=state,
        reason=reason,
        client_data=dict(client_data or {}),
        metadata={
            "source": source,
            "stopKind": stop_kind,
            "terminal": terminal,
            "reasonField": reason_field,
        },
    )


def classify_client_automation_state(
    client_data: Optional[Dict[str, Any]],
    *,
    source: str = "",
) -> CampaignAutomationDecision:
    """Classify a loaded client document without permitting ambiguous state.

    The active client status is intentionally allow-listed. A status we do not
    understand is not evidence that autonomous sends, replies, or follow-ups
    are safe to perform.
    """
    if not isinstance(client_data, dict):
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            source=source,
        )

    automation_paused = client_data.get("automationPaused")
    legacy_automation_paused = client_data.get("automation_paused")
    if automation_paused is not None and not isinstance(automation_paused, bool):
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            client_data,
            source=source,
        )
    if legacy_automation_paused is not None and not isinstance(legacy_automation_paused, bool):
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            client_data,
            source=source,
        )
    if (
        automation_paused is not None
        and legacy_automation_paused is not None
        and automation_paused != legacy_automation_paused
    ):
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            client_data,
            source=source,
        )

    status_value = client_data.get("status")
    if status_value is not None and (not isinstance(status_value, str) or not status_value.strip()):
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            client_data,
            source=source,
        )
    status = normalize_client_status(status_value) if status_value is not None else ""

    if source == "archivedClients":
        reason, reason_field = _pause_reason(client_data, CLIENT_AUTOMATION_PAUSED_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_BLOCKED,
            reason,
            client_data,
            source=source,
            stop_kind="terminal_stop",
            terminal=True,
            reason_field=reason_field,
        )

    if status in CLIENT_TERMINAL_STATUSES:
        reason, reason_field = _pause_reason(client_data, CLIENT_AUTOMATION_PAUSED_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_BLOCKED,
            reason,
            client_data,
            source=source,
            stop_kind="terminal_stop",
            terminal=True,
            reason_field=reason_field,
        )

    if automation_paused is True or legacy_automation_paused is True:
        reason, reason_field = _pause_reason(client_data, CLIENT_AUTOMATION_PAUSED_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_BLOCKED,
            reason,
            client_data,
            source=source,
            stop_kind="maintenance_pause",
            terminal=False,
            reason_field=reason_field,
        )

    if status_value is None:
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
            client_data,
            source=source,
        )

    if status in CLIENT_ACTIVE_STATUSES:
        return _decision(
            CAMPAIGN_AUTOMATION_ALLOW,
            "",
            client_data,
            source=source,
        )

    return _decision(
        CAMPAIGN_AUTOMATION_UNKNOWN,
        CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
        client_data,
        source=source,
    )


def is_client_automation_paused(client_data: Optional[Dict[str, Any]]) -> bool:
    """Legacy boolean adapter: deny automation for blocked and unknown state."""
    return classify_client_automation_state(client_data).denies_autonomous_work


def client_automation_pause_reason(client_data: Optional[Dict[str, Any]]) -> str:
    if not isinstance(client_data, dict):
        return CLIENT_AUTOMATION_PAUSED_REASON
    return _pause_reason(client_data, CLIENT_AUTOMATION_PAUSED_REASON)[0]


def _read_client_document(fs, user_id: str, collection_name: str, client_id: str):
    return (
        fs.collection("users")
        .document(user_id)
        .collection(collection_name)
        .document(client_id)
        .get()
    )


def _log_campaign_state_warning(reason: str) -> None:
    print(f"   ⚠️ Campaign automation state unavailable: {reason}")


def _global_campaign_access_decision(
    fs,
    user_id: str,
    client_decision: CampaignAutomationDecision,
) -> CampaignAutomationDecision:
    try:
        policy_doc = fs.collection("systemConfig").document("campaignAccess").get()
    except Exception:
        _log_campaign_state_warning(GLOBAL_AUTOMATION_STATE_READ_ERROR_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            GLOBAL_AUTOMATION_STATE_READ_ERROR_REASON,
            client_decision.client_data,
            source=GLOBAL_CAMPAIGN_ACCESS_SOURCE,
        )

    if not getattr(policy_doc, "exists", False):
        if user_id == BAYLOR_GLOBAL_AUTOMATION_FALLBACK_UID:
            return client_decision
        return _decision(
            CAMPAIGN_AUTOMATION_BLOCKED,
            GLOBAL_AUTOMATION_DISABLED_REASON,
            client_decision.client_data,
            source=GLOBAL_CAMPAIGN_ACCESS_SOURCE,
            stop_kind="global_maintenance",
            terminal=False,
        )

    try:
        policy = policy_doc.to_dict()
    except Exception:
        policy = None
    if not isinstance(policy, dict):
        _log_campaign_state_warning(GLOBAL_AUTOMATION_STATE_MALFORMED_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            GLOBAL_AUTOMATION_STATE_MALFORMED_REASON,
            client_decision.client_data,
            source=GLOBAL_CAMPAIGN_ACCESS_SOURCE,
        )

    automation_enabled = policy.get("automationEnabled")
    allowed_uids = policy.get("allowedUids")
    if (
        not isinstance(automation_enabled, bool)
        or not isinstance(allowed_uids, list)
        or any(not isinstance(uid, str) or not uid.strip() for uid in allowed_uids)
    ):
        _log_campaign_state_warning(GLOBAL_AUTOMATION_STATE_MALFORMED_REASON)
        return _decision(
            CAMPAIGN_AUTOMATION_UNKNOWN,
            GLOBAL_AUTOMATION_STATE_MALFORMED_REASON,
            client_decision.client_data,
            source=GLOBAL_CAMPAIGN_ACCESS_SOURCE,
        )

    if automation_enabled or user_id in {uid.strip() for uid in allowed_uids}:
        return client_decision

    return _decision(
        CAMPAIGN_AUTOMATION_BLOCKED,
        GLOBAL_AUTOMATION_DISABLED_REASON,
        client_decision.client_data,
        source=GLOBAL_CAMPAIGN_ACCESS_SOURCE,
        stop_kind="global_maintenance",
        terminal=False,
    )


def get_client_automation_decision(
    user_id: str,
    client_id: Optional[str],
    *,
    firestore_client=None,
) -> CampaignAutomationDecision:
    """Read active then archived client state and fail closed when it is uncertain."""
    if not isinstance(user_id, str) or not user_id.strip() or not isinstance(client_id, str) or not client_id.strip():
        return _decision(CAMPAIGN_AUTOMATION_UNKNOWN, MISSING_CLIENT_ID_REASON)

    client_id = client_id.strip()
    try:
        fs = firestore_client
        if fs is None:
            from .clients import _fs

            fs = _fs
    except Exception:
        _log_campaign_state_warning(CLIENT_AUTOMATION_STATE_READ_ERROR_REASON)
        return _decision(CAMPAIGN_AUTOMATION_UNKNOWN, CLIENT_AUTOMATION_STATE_READ_ERROR_REASON)

    active_doc = None
    archived_doc = None
    active_read_failed = False
    archived_read_failed = False
    try:
        active_doc = _read_client_document(fs, user_id, "clients", client_id)
    except Exception:
        active_read_failed = True

    try:
        archived_doc = _read_client_document(fs, user_id, "archivedClients", client_id)
    except Exception:
        archived_read_failed = True

    if getattr(archived_doc, "exists", False):
        try:
            return classify_client_automation_state(archived_doc.to_dict(), source="archivedClients")
        except Exception:
            _log_campaign_state_warning(CLIENT_AUTOMATION_STATE_MALFORMED_REASON)
            return _decision(
                CAMPAIGN_AUTOMATION_UNKNOWN,
                CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
                source="archivedClients",
            )

    if active_read_failed or archived_read_failed:
        _log_campaign_state_warning(CLIENT_AUTOMATION_STATE_READ_ERROR_REASON)
        return _decision(CAMPAIGN_AUTOMATION_UNKNOWN, CLIENT_AUTOMATION_STATE_READ_ERROR_REASON)

    if getattr(active_doc, "exists", False):
        try:
            client_decision = classify_client_automation_state(
                active_doc.to_dict(), source="clients"
            )
        except Exception:
            _log_campaign_state_warning(CLIENT_AUTOMATION_STATE_MALFORMED_REASON)
            return _decision(
                CAMPAIGN_AUTOMATION_UNKNOWN,
                CLIENT_AUTOMATION_STATE_MALFORMED_REASON,
                source="clients",
            )
        if client_decision.denies_autonomous_work:
            return client_decision
        return _global_campaign_access_decision(fs, user_id, client_decision)

    return _decision(CAMPAIGN_AUTOMATION_UNKNOWN, CLIENT_AUTOMATION_STATE_NOT_FOUND_REASON)


def get_client_automation_pause(
    user_id: str,
    client_id: Optional[str],
    *,
    firestore_client=None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Legacy tuple adapter. Its boolean now denies autonomous work fail-closed."""
    decision = get_client_automation_decision(
        user_id,
        client_id,
        firestore_client=firestore_client,
    )
    return decision.denies_autonomous_work, decision.reason, decision.client_data


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
