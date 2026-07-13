import json
import os
import re
import requests
import time
import uuid
import logging
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from google.cloud.firestore import SERVER_TIMESTAMP, Increment
from .utils import (
    GRAPH_SEND_MAX_RETRIES,
    exponential_backoff_request,
    safe_preview,
    _body_kind,
    _normalize_email,
    validate_recipient_emails,
    is_valid_email,
    resolve_signature_settings,
)
from .messaging import save_thread_root, save_message, index_message_id, index_conversation_id, lookup_thread_by_message_id
from .clients import _get_sheet_id_or_fail, _sheets_client
from .sheets import _find_row_by_email, _get_first_tab_title, _read_header_row2, _header_index_map, _execute_with_retry, highlight_row
from .notifications import delete_notification_and_decrement_counters
from .utils import normalize_message_id
from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_sent_conversation_continuation_for_retry,
    find_matching_sent_message_for_retry,
    send_result_from_sent_match,
    sent_after_from_retry_data,
)
from .results_feature_gate import (
    RESULTS_FEATURE_PAUSED_REASON,
    is_tour_invite_outbox,
    should_pause_results_outbox_for_user,
)
from .campaign_safety import (
    CAMPAIGN_AUTOMATION_ALLOW,
    CAMPAIGN_AUTOMATION_BLOCKED,
    CampaignAutomationDecision,
    get_client_automation_decision,
    get_client_automation_pause,
)
from .outbound_safety import validate_outbound_body
from .column_config import (
    get_column_config_error,
    response_requests_nonrequestable_fields,
)

logger = logging.getLogger(__name__)
_ORIGINAL_GET_CLIENT_AUTOMATION_PAUSE = get_client_automation_pause

# Maximum retry attempts before moving to dead-letter queue
MAX_OUTBOX_ATTEMPTS = 5
# Maximum retries for indexing operations
MAX_INDEX_RETRIES = 3
# Claim timeout in seconds (if a claim is older than this, it's considered stale)
CLAIM_TIMEOUT_SECONDS = 300  # 5 minutes

# Outbox items from these dashboard flows already contain operator-reviewed body
# copy. They must not be replaced by contact-history campaign fallback text.
EXACT_OUTBOX_SOURCES = {"dashboard_tour_planner"}
EXACT_OUTBOX_ACTION_TYPES = {"tour_invite"}

CAMPAIGN_LAUNCH_SOURCES = {"dashboard_new_campaign"}
CAMPAIGN_LAUNCH_ACTION_TYPES = {"campaign_creation", "campaign_launch"}
NEW_PROPERTY_OUTREACH_SOURCES = {"dashboard_new_property", "new_property_notification"}
NEW_PROPERTY_OUTREACH_ACTION_TYPES = {"new_property_outreach", "accept_new_property"}

# Unique worker ID for this process
WORKER_ID = str(uuid.uuid4())[:8]


# --- Rail 3: global outbound kill switch -----------------------------------
# A single fail-closed lever an operator can flip (env var, no code deploy) to
# halt or downgrade ALL outbound Graph sends the instant a bad-template or
# wrong-recipient blast is discovered. Scoped levers (per-client pause, per-user
# auto-reply allowlists, scheduler_scope dev-scoping) do NOT cover outbox
# outreach or follow-ups; this gate does, at the send call itself.
OUTBOUND_MODE_ENV = "SITESIFT_OUTBOUND_MODE"
OUTBOUND_MODE_LIVE = "live"
OUTBOUND_MODE_DRY_RUN = "dry_run"
OUTBOUND_MODE_PAUSED = "paused"
_VALID_OUTBOUND_MODES = {
    OUTBOUND_MODE_LIVE,
    OUTBOUND_MODE_DRY_RUN,
    OUTBOUND_MODE_PAUSED,
}


def resolve_outbound_mode() -> str:
    """Resolve the global outbound send mode. Fail closed on anything unclear.

    Reads ``SITESIFT_OUTBOUND_MODE`` once per send. Recognized values:

    - ``live`` (or unset / empty): normal sending. Absence preserves existing
      behavior so the default deployment and the test suite are unaffected.
    - ``dry_run``: skip every Graph send; leave the item queued.
    - ``paused``: skip every Graph send; leave the item queued.

    Any unrecognized / malformed value fails **closed** to ``paused`` so a typo
    ("off", "true", "Live!", "stop") can never silently keep blasting outbound.
    """
    raw = os.environ.get(OUTBOUND_MODE_ENV)
    if raw is None or not raw.strip():
        return OUTBOUND_MODE_LIVE
    normalized = raw.strip().lower()
    if normalized in _VALID_OUTBOUND_MODES:
        return normalized
    print(
        f"🛑 Unrecognized {OUTBOUND_MODE_ENV}={raw!r}; failing closed to "
        f"'{OUTBOUND_MODE_PAUSED}' (no outbound will be sent)"
    )
    return OUTBOUND_MODE_PAUSED


def outbound_sending_enabled() -> bool:
    """True only when the global kill switch resolves to ``live``."""
    return resolve_outbound_mode() == OUTBOUND_MODE_LIVE


def _kill_switch_suppressed(mode: str, *, context: str) -> None:
    """Emit the suppression audit line for a blocked outbound send."""
    reason = f"suppressed_by_kill_switch (SITESIFT_OUTBOUND_MODE={mode})"
    print(f"🛑 {reason}: skipping Graph send for {context}")
    logger.warning(
        "outbound.suppressed_by_kill_switch",
        extra={"outbound_mode": mode, "context": context},
    )

# ---------------------------------------------------------------------------
# Rail 2 — aggregate daily send cap (fail-closed, off-by-default-SAFE)
# ---------------------------------------------------------------------------
# Per-item retry caps (MAX_OUTBOX_ATTEMPTS, etc.) bound a single item's storm;
# they do NOT bound total volume. A runaway producer (frontend bug mass-creating
# outbox docs, a bulk re-queue, a reprocessing loop) can drain unbounded real
# broker emails before anyone notices. This rail keeps a per-day counter and
# stops draining once the ceiling is hit, retaining the queue for the next cycle.
#
# Off-by-default-SAFE: an UNSET env var keeps the rail ON at a conservative
# built-in ceiling (absence of config must NOT silently disable it). Only an
# explicit operator opt-out ("0" / "off" / "none" / "disabled" / "false")
# turns it off. Fail-closed: if the shared counter cannot be read or the
# increment cannot be recorded, draining STOPS and the queue is retained.
DEFAULT_DAILY_SEND_CAP = 500
SEND_COUNTERS_COLLECTION = "sendCounters"
SEND_CAP_HEALTH_COLLECTION = "systemHealth"
SEND_CAP_HEALTH_DOC_ID = "emailAutomation"
GLOBAL_SEND_COUNTER_PREFIX = "global"
DAILY_CAP_REACHED_REASON = "daily_cap_reached"
DAILY_CAP_COUNTER_UNAVAILABLE_REASON = "daily_send_cap_counter_unavailable"
_DAILY_CAP_DISABLED_TOKENS = {"", "0", "off", "none", "disabled", "false", "no"}


def _resolve_send_cap(env_var: str, default: Optional[int]) -> Optional[int]:
    """Resolve a daily send ceiling from an env var.

    Returns the integer cap, or None when the rail is disabled for that scope.
    An unset var falls back to ``default`` (for the per-user cap this is a
    non-None conservative ceiling, so absence of config keeps the rail ON).
    An unparseable value is treated as "keep the default" — never as a silent
    disable — so a typo cannot open the floodgates.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _DAILY_CAP_DISABLED_TOKENS:
        return None
    try:
        cap = int(token)
    except (TypeError, ValueError):
        return default
    if cap <= 0:
        return None
    return cap


def _resolve_daily_send_cap() -> Optional[int]:
    """Per-user daily ceiling. Unset env keeps the rail ON at the default."""
    return _resolve_send_cap("SITESIFT_DAILY_SEND_CAP", DEFAULT_DAILY_SEND_CAP)


def _resolve_global_daily_send_cap() -> Optional[int]:
    """Optional fleet-wide ceiling. Unset = disabled (this scope is opt-in)."""
    return _resolve_send_cap("SITESIFT_GLOBAL_DAILY_SEND_CAP", None)


def _send_counter_day_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _snapshot_count(snapshot) -> int:
    if snapshot is None:
        return 0
    if hasattr(snapshot, "exists") and not snapshot.exists:
        return 0
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else None
    try:
        return int((data or {}).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _read_daily_send_count(fs, user_id: str, day_key: str) -> int:
    """Read today's per-user send count. Raises on store failure (fail-closed)."""
    snapshot = (
        fs.collection("users").document(user_id)
        .collection(SEND_COUNTERS_COLLECTION).document(day_key)
        .get()
    )
    return _snapshot_count(snapshot)


def _increment_daily_send_count(fs, user_id: str, day_key: str, amount: int) -> None:
    """Atomically add ``amount`` to today's per-user counter. Raises on failure."""
    if amount <= 0:
        return
    (
        fs.collection("users").document(user_id)
        .collection(SEND_COUNTERS_COLLECTION).document(day_key)
        .set(
            {"count": Increment(amount), "day": day_key, "updatedAt": SERVER_TIMESTAMP},
            merge=True,
        )
    )


def _read_global_send_count(fs, day_key: str) -> int:
    snapshot = (
        fs.collection(SEND_COUNTERS_COLLECTION)
        .document(f"{GLOBAL_SEND_COUNTER_PREFIX}-{day_key}")
        .get()
    )
    return _snapshot_count(snapshot)


def _increment_global_send_count(fs, day_key: str, amount: int) -> None:
    if amount <= 0:
        return
    (
        fs.collection(SEND_COUNTERS_COLLECTION)
        .document(f"{GLOBAL_SEND_COUNTER_PREFIX}-{day_key}")
        .set(
            {"count": Increment(amount), "day": day_key, "updatedAt": SERVER_TIMESTAMP},
            merge=True,
        )
    )


def _record_send_cap_health(
    fs,
    user_id: str,
    *,
    status: str,
    reason: str,
    cap: Optional[int],
    count: Optional[int],
    day_key: str,
    scope: str = "user",
) -> None:
    """Record cap state on systemHealth so a stalled queue is observable.

    Best-effort: a health-write failure must never crash the send path (the
    send decision has already been made fail-closed by the caller).
    """
    try:
        payload = {
            "sendCap": {
                "status": status,
                "reason": reason,
                "cap": cap,
                "count": count,
                "day": day_key,
                "scope": scope,
                "updatedAt": SERVER_TIMESTAMP,
            }
        }
        (
            fs.collection("users").document(user_id)
            .collection(SEND_CAP_HEALTH_COLLECTION).document(SEND_CAP_HEALTH_DOC_ID)
            .set(payload, merge=True)
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break sends
        print(f"   ⚠️ Could not record send-cap health ({reason}): {exc}")


def _fresh_graph_headers(
    headers: Dict[str, str],
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
) -> Dict[str, str]:
    if headers_provider:
        fresh_headers = headers_provider()
        if fresh_headers:
            return fresh_headers
    return headers


def _update_action_audit(user_id: str, action_audit_id: Optional[str], payload: Dict[str, Any]) -> None:
    if not action_audit_id:
        return
    try:
        from .clients import _fs
        (
            _fs.collection("users").document(user_id)
            .collection("actionAudit").document(action_audit_id)
            .set(payload, merge=True)
        )
    except Exception as e:
        print(f"   ⚠️ Could not update action audit {action_audit_id}: {e}")


def _terminalize_outbox_action_audit(
    user_id: Optional[str],
    doc_ref,
    data: Dict[str, Any],
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the final dashboard action-audit state for a no-send/send outcome."""
    if not user_id:
        return

    payload = {
        "status": status,
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": data.get("clientId"),
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "updatedAt": SERVER_TIMESTAMP,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    _update_action_audit(user_id, data.get("actionAuditId"), payload)


def _mark_outbox_action_audit_retrying(
    user_id: Optional[str],
    doc_ref,
    data: Dict[str, Any],
    attempts: int,
    error_msg: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Expose a retryable send failure without terminalizing the action."""
    payload = {
        "attempts": attempts,
        "maxAttempts": MAX_OUTBOX_ATTEMPTS,
        "lastError": error_msg,
        "lastFailedAt": SERVER_TIMESTAMP,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    _terminalize_outbox_action_audit(
        user_id,
        doc_ref,
        data,
        "retrying",
        payload,
    )


def _first_result_value(mapping: Any, preferred_keys: Optional[List[str]] = None) -> Optional[Any]:
    if not mapping:
        return None
    if isinstance(mapping, dict):
        for key in preferred_keys or []:
            if mapping.get(key):
                return mapping[key]
        for value in mapping.values():
            if value:
                return value
        return None
    if isinstance(mapping, list):
        return next((value for value in mapping if value), None)
    return mapping


def _ordered_unique(values: List[Any]) -> List[Any]:
    result = []
    for value in values or []:
        if value and value not in result:
            result.append(value)
    return result


def _send_identity_recipients(send_result: Optional[Dict[str, Any]]) -> List[str]:
    if not send_result:
        return []
    recipients = []
    for key in ("sentMessageIds", "internetMessageIds", "threadIds", "conversationIds"):
        values = send_result.get(key) or {}
        if not isinstance(values, dict):
            continue
        for recipient, identity_value in values.items():
            if isinstance(recipient, str) and recipient and identity_value:
                recipients.append(recipient)
    return _ordered_unique(recipients)


def _send_identity_payload(send_result: Optional[Dict[str, Any]], recipients: Optional[List[str]] = None) -> Dict[str, Any]:
    if not send_result:
        return {}

    recipients = recipients or []
    sent_message_ids = send_result.get("sentMessageIds") or {}
    internet_message_ids = send_result.get("internetMessageIds") or {}
    thread_ids = send_result.get("threadIds") or {}
    conversation_ids = send_result.get("conversationIds") or {}

    payload = {
        "sentMessageId": _first_result_value(sent_message_ids, recipients),
        "internetMessageId": _first_result_value(internet_message_ids, recipients),
        "sentThreadId": _first_result_value(thread_ids, recipients),
        "conversationId": _first_result_value(conversation_ids, recipients),
        "sentRecipients": send_result.get("sent"),
        "sentMessageIds": sent_message_ids or None,
        "internetMessageIds": internet_message_ids or None,
        "sentThreadIds": thread_ids or None,
        "conversationIds": conversation_ids or None,
    }
    return {k: v for k, v in payload.items() if v}


def _is_tour_invite_outbox(data: Optional[Dict[str, Any]] = None) -> bool:
    return is_tour_invite_outbox(data)


def _should_pause_results_outbox_for_user(user_id: Optional[str], data: Optional[Dict[str, Any]] = None) -> bool:
    return should_pause_results_outbox_for_user(user_id, data)


def _pause_results_outbox_item_if_needed(user_id: str, doc_ref, data: dict) -> bool:
    if not _should_pause_results_outbox_for_user(user_id, data):
        return False
    _move_to_dead_letter(user_id, doc_ref, data, RESULTS_FEATURE_PAUSED_REASON)
    return True


def _read_client_automation_decision(user_id: str, client_id: Optional[str]):
    """Use the tri-state reader while preserving legacy test/extension patches."""
    if get_client_automation_pause is _ORIGINAL_GET_CLIENT_AUTOMATION_PAUSE:
        return get_client_automation_decision(user_id, client_id)

    paused, reason, client_data = get_client_automation_pause(user_id, client_id)
    if not paused:
        return CampaignAutomationDecision(
            state=CAMPAIGN_AUTOMATION_ALLOW,
            reason="",
            client_data=client_data or {},
            metadata={"terminal": False, "stopKind": "none", "source": "legacy_adapter"},
        )
    normalized_reason = str(reason or "").strip().lower()
    terminal = any(token in normalized_reason for token in ("stop", "archiv", "delet", "complet"))
    return CampaignAutomationDecision(
        state=CAMPAIGN_AUTOMATION_BLOCKED,
        reason=str(reason or "client_automation_paused"),
        client_data=client_data or {},
        metadata={
            "terminal": terminal,
            "stopKind": "terminal_stop" if terminal else "maintenance_pause",
            "source": "legacy_adapter",
        },
    )


def _pause_client_outbox_item_if_needed(user_id: str, doc_ref, data: dict) -> bool:
    client_id = (data.get("clientId") or "").strip()
    decision = _read_client_automation_decision(user_id, client_id)
    if decision.state == CAMPAIGN_AUTOMATION_ALLOW:
        return False

    if decision.state == CAMPAIGN_AUTOMATION_BLOCKED and decision.metadata.get("terminal"):
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            f"Client campaign is stopped; send canceled: {decision.reason}",
        )
        return True

    _preserve_retryable_outbox_suppression(user_id, doc_ref, data, decision)
    return True


def _preserve_retryable_outbox_suppression(
    user_id: str,
    doc_ref,
    data: Dict[str, Any],
    decision,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Release a claim without consuming an attempt or destroying queued work."""
    suppression_payload = {
        "status": "queued",
        "processingBy": None,
        "processingAt": None,
        "automationSuppressedState": decision.state,
        "automationSuppressedReason": decision.reason,
        "automationSuppressedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
        **(extra or {}),
    }
    doc_ref.set(suppression_payload, merge=True)
    _terminalize_outbox_action_audit(
        user_id,
        doc_ref,
        data,
        "queued",
        {
            "automationSuppressedState": decision.state,
            "automationSuppressedReason": decision.reason,
            "automationSuppressedAt": SERVER_TIMESTAMP,
        },
    )


def _campaign_suppression_result(decision) -> Dict[str, Any]:
    terminal = bool(
        decision.state == CAMPAIGN_AUTOMATION_BLOCKED
        and decision.metadata.get("terminal")
    )
    return {
        "campaignAutomationSuppressed": True,
        "campaignAutomationState": decision.state,
        "campaignAutomationReason": decision.reason,
        "campaignAutomationTerminal": terminal,
    }


def _handle_suppressed_outbox_send_result(
    user_id: str,
    doc_ref,
    data: Dict[str, Any],
    send_result: Optional[Dict[str, Any]],
    *,
    previously_sent_recipients: Optional[List[str]] = None,
) -> bool:
    if not (send_result or {}).get("campaignAutomationSuppressed"):
        return False

    accepted_recipients = _ordered_unique(
        list(data.get("sentRecipients") or [])
        + list(previously_sent_recipients or [])
        + list(send_result.get("sent") or [])
        + _send_identity_recipients(send_result)
    )
    assigned_recipients = [
        recipient
        for recipient in (data.get("assignedEmails") or [])
        if isinstance(recipient, str)
    ]
    accepted_set = {
        recipient.strip().lower()
        for recipient in accepted_recipients
        if isinstance(recipient, str) and recipient.strip()
    }
    remaining_recipients = [
        recipient
        for recipient in assigned_recipients
        if recipient.strip().lower() not in accepted_set
    ]
    partial_state = {}
    if accepted_recipients:
        partial_state = {
            "assignedEmails": remaining_recipients,
            "sentRecipients": accepted_recipients,
            "partialSend": True,
        }

    if send_result.get("campaignAutomationTerminal"):
        terminal_data = {**data, **partial_state}
        _move_to_dead_letter(
            user_id,
            doc_ref,
            terminal_data,
            "Client campaign stopped during send preparation; send canceled: "
            f"{send_result.get('campaignAutomationReason') or 'campaign_stopped'}",
        )
        return True

    class _SuppressionDecision:
        state = send_result.get("campaignAutomationState") or "unknown"
        reason = send_result.get("campaignAutomationReason") or "campaign_state_unavailable"

    _preserve_retryable_outbox_suppression(
        user_id,
        doc_ref,
        data,
        _SuppressionDecision(),
        extra=partial_state,
    )
    return True


def _is_campaign_launch_outbox(data: dict) -> bool:
    source = str(data.get("source") or "").strip().lower()
    action_type = str(data.get("actionType") or "").strip().lower()
    return source in CAMPAIGN_LAUNCH_SOURCES or action_type in CAMPAIGN_LAUNCH_ACTION_TYPES


def _is_initial_outreach_outbox(data: dict) -> bool:
    """Identify campaign/new-property first touches, including legacy writes."""
    if _is_tour_invite_outbox(data):
        return False

    source = str(data.get("source") or "").strip().lower()
    action_type = str(data.get("actionType") or "").strip().lower()
    if _is_campaign_launch_outbox(data):
        return True
    if source in NEW_PROPERTY_OUTREACH_SOURCES or action_type in NEW_PROPERTY_OUTREACH_ACTION_TYPES:
        return True
    if data.get("threadId") or data.get("replyToMessageId"):
        return False

    legacy_new_property = bool(
        data.get("notificationId")
        and data.get("forceScript") is True
        and str(data.get("scriptSelectionMode") or "").strip().lower() == "exact"
        and isinstance(data.get("property"), dict)
    )
    legacy_campaign_launch = bool(
        data.get("clientId")
        and data.get("rowNumber")
        and data.get("subject")
        and data.get("assignedEmails")
    )
    return legacy_new_property or legacy_campaign_launch


def _is_outbox_thread_reply(data: dict) -> bool:
    """Route explicit first-touch actions as outreach despite stale reply ids."""
    return bool(
        data.get("threadId")
        and data.get("replyToMessageId")
        and not _is_initial_outreach_outbox(data)
    )


def _dead_letter_invalid_initial_outreach_column_contract_if_needed(
    user_id: str,
    doc_ref,
    data: dict,
    body: str,
) -> bool:
    """Fail closed when first-touch copy violates the persisted column contract."""
    if not _is_initial_outreach_outbox(data):
        return False

    client_id = str(data.get("clientId") or "").strip()
    if not client_id:
        reason = "Initial outreach has no clientId for persisted columnConfig validation"
    else:
        try:
            decision = _read_client_automation_decision(user_id, client_id)
            client_data = decision.client_data if decision else {}
            column_config = (client_data or {}).get("columnConfig")
            config_error = get_column_config_error(column_config)
            if config_error:
                reason = f"Initial outreach has invalid persisted columnConfig: {config_error}"
            elif response_requests_nonrequestable_fields(body, column_config):
                reason = "Initial outreach requests a non-requestable Note, Skip, or formula field"
            else:
                return False
        except Exception as exc:
            reason = f"Initial outreach columnConfig validation failed: {exc}"

    _move_to_dead_letter(
        user_id,
        doc_ref,
        data,
        f"{reason}; manual review required before sending",
    )
    return True


def _email_values_from_row(header: List[str], row_values: List[str]) -> List[str]:
    idx_map = _header_index_map(header)
    email_indexes = [
        idx_map[key] - 1
        for key in ("email", "email address", "contact email", "e-mail", "e mail")
        if key in idx_map
    ]
    values = []
    for idx in email_indexes:
        if 0 <= idx < len(row_values):
            raw_value = str(row_values[idx] or "")
            candidates = re.findall(
                r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}",
                raw_value,
                flags=re.IGNORECASE,
            ) or [raw_value]
            for candidate in candidates:
                normalized = _normalize_email(candidate)
                if normalized and is_valid_email(normalized):
                    values.append(normalized)
    return _ordered_unique(values)


CAMPAIGN_CONTACT_NAME_HEADER_KEYS = (
    "contact name",
    "contact first name",
    "leasing contact name",
    "leasing contact",
    "leasing agent name",
    "leasing agent",
    "broker name",
    "broker contact",
    "broker first name",
    "recipient name",
    "recipient first name",
    "first name",
    "full name",
)


def _contact_name_resolution_from_campaign_row(
    header: List[str],
    row_values: List[str],
) -> Dict[str, Optional[str]]:
    idx_map = _header_index_map(header)
    candidates = []
    for key in CAMPAIGN_CONTACT_NAME_HEADER_KEYS:
        idx = idx_map.get(key)
        if not idx:
            continue
        row_idx = idx - 1
        if row_idx < 0 or row_idx >= len(row_values):
            continue
        value = str(row_values[row_idx] or "").strip()
        if not value:
            continue
        if _safe_greeting_first_name(value):
            candidates.append(value)

    unique = _ordered_unique(candidates)
    if len(unique) == 1:
        return {"contact_name": unique[0], "failure_reason": None}
    if len(unique) > 1:
        return {
            "contact_name": None,
            "failure_reason": (
                "Ambiguous sheet contact/name source for [NAME]; "
                f"found {len(unique)} different safe values in explicit contact-name columns"
            ),
        }
    return {
        "contact_name": None,
        "failure_reason": "No safe sheet contact/name source found for [NAME]",
    }


def _contact_name_from_campaign_row(header: List[str], row_values: List[str]) -> Optional[str]:
    return _contact_name_resolution_from_campaign_row(header, row_values).get("contact_name")


def _campaign_sheet_header_and_row(
    user_id: str,
    client_id: str,
    row_number: int,
    *,
    operation_name: str,
    sheet_metadata_cache: Optional[Dict[str, Any]] = None,
) -> tuple[List[str], List[str]]:
    sheet_id = _get_sheet_id_or_fail(user_id, client_id)
    sheets = _sheets_client()
    cache = sheet_metadata_cache if sheet_metadata_cache is not None else {}
    metadata = cache.get(sheet_id)
    if metadata:
        tab_title = metadata["tab_title"]
        header = metadata["header"]
    else:
        tab_title = _get_first_tab_title(sheets, sheet_id)
        header = _read_header_row2(sheets, sheet_id, tab_title)
        cache[sheet_id] = {"tab_title": tab_title, "header": header}

    row_cache_key = f"{sheet_id}:row:{row_number}"
    row_values = cache.get(row_cache_key)
    if row_values is None:
        resp = _execute_with_retry(
            sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"{tab_title}!{row_number}:{row_number}",
            ),
            operation_name,
        )
        row_values = (resp.get("values") or [[]])[0]
        cache[row_cache_key] = row_values

    padded_row = row_values + [""] * max(0, len(header) - len(row_values))
    return header, padded_row


def _resolve_campaign_launch_contact_name_result_from_sheet(
    user_id: str,
    data: Dict[str, Any],
    *,
    row_number_override: Optional[object] = None,
    sheet_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[str]]:
    if not _is_campaign_launch_outbox(data):
        return {"contact_name": None, "failure_reason": None}

    raw_row_number = data.get("rowNumber") or row_number_override
    client_id = (data.get("clientId") or "").strip()
    if not client_id or raw_row_number in (None, ""):
        return {"contact_name": None, "failure_reason": "Missing sheet row metadata for [NAME]"}

    try:
        row_number = int(raw_row_number)
    except (TypeError, ValueError):
        return {"contact_name": None, "failure_reason": "Invalid sheet row metadata for [NAME]"}
    if row_number < 1:
        return {"contact_name": None, "failure_reason": "Invalid sheet row metadata for [NAME]"}

    header, row_values = _campaign_sheet_header_and_row(
        user_id,
        client_id,
        row_number,
        operation_name="outbox_contact_name_row_guard",
        sheet_metadata_cache=sheet_metadata_cache,
    )
    return _contact_name_resolution_from_campaign_row(header, row_values)


def _resolve_campaign_launch_contact_name_from_sheet(
    user_id: str,
    data: Dict[str, Any],
    *,
    row_number_override: Optional[object] = None,
    sheet_metadata_cache: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    return _resolve_campaign_launch_contact_name_result_from_sheet(
        user_id,
        data,
        row_number_override=row_number_override,
        sheet_metadata_cache=sheet_metadata_cache,
    ).get("contact_name")


def _dead_letter_campaign_recipient_row_mismatch_if_needed(
    user_id: str,
    doc_ref,
    data: dict,
    recipient_email: str,
    row_number_override: Optional[object] = None,
    sheet_metadata_cache: Optional[Dict[str, Any]] = None,
) -> bool:
    if not _is_campaign_launch_outbox(data):
        return False

    client_id = (data.get("clientId") or "").strip()
    raw_row_number = data.get("rowNumber") or row_number_override
    recipient = _normalize_email(recipient_email)
    missing = []
    if not client_id:
        missing.append("clientId")
    if raw_row_number in (None, ""):
        missing.append("rowNumber")
    if not recipient:
        missing.append("recipient")
    if missing:
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            "Campaign launch outbox is missing required campaign launch metadata "
            f"({', '.join(missing)}); manual review required before sending.",
        )
        return True

    try:
        row_number = int(raw_row_number)
    except (TypeError, ValueError):
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            f"Campaign launch outbox has invalid rowNumber ({raw_row_number!r}); manual review required before sending.",
        )
        return True

    if row_number < 1:
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            f"Campaign launch outbox has invalid rowNumber ({row_number}); manual review required before sending.",
        )
        return True

    try:
        header, row_values = _campaign_sheet_header_and_row(
            user_id,
            client_id,
            row_number,
            operation_name="outbox_recipient_row_guard",
            sheet_metadata_cache=sheet_metadata_cache,
        )
        row_emails = _email_values_from_row(header, row_values)
    except Exception as exc:
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            f"Could not verify queued recipient against sheet row {row_number}; manual review required before sending: {exc}",
        )
        return True

    if recipient not in row_emails:
        expected = ", ".join(row_emails) if row_emails else "no email found on row"
        _move_to_dead_letter(
            user_id,
            doc_ref,
            data,
            (
                f"Queued recipient does not match sheet row {row_number}; "
                f"queued={recipient}, row={expected}. Manual review required before sending."
            ),
        )
        return True

    return False


def _dead_letter_unsafe_outbound_body_if_needed(
    user_id: str,
    doc_ref,
    data: dict,
    body: str,
) -> bool:
    validation = validate_outbound_body(
        body,
        allow_scheduling_language=_is_tour_invite_outbox(data),
    )
    if validation.is_safe:
        return False
    _move_to_dead_letter(
        user_id,
        doc_ref,
        data,
        f"{validation.reason}; manual review required before sending",
    )
    return True


def _dead_letter_unresolved_name_placeholder_if_needed(
    user_id: str,
    doc_ref,
    data: dict,
    body: str,
    failure_reason: Optional[str],
) -> bool:
    if not failure_reason or not NAME_PLACEHOLDER_RE.search(body or ""):
        return False
    _move_to_dead_letter(
        user_id,
        doc_ref,
        data,
        f"{failure_reason}; manual review required before sending",
    )
    return True


# Thread statuses that may still receive dashboard replies. Anything else
# (stopped/completed/closed/unknown) is terminal for the send pipeline.
OPEN_THREAD_STATUSES = {"active", "paused"}


def _validate_outbox_thread_reply_target(user_id: str, data: dict) -> Dict[str, Any]:
    """Re-validate the client-supplied thread binding on a dashboard thread reply.

    The outbox document is written entirely client-side (InlineReplyComposer),
    so threadId, replyToMessageId and clientId arrive unvalidated. Before any
    Graph send the pipeline must confirm that:
      - the thread exists under this user,
      - the thread belongs to the same client as the outbox item,
      - the thread is still open (active/paused, never stopped/completed),
      - replyToMessageId is a message actually recorded under that thread
        (doc id match, or sourceMessage.graphMessageId for messages keyed by
        internetMessageId).

    Fail-closed: lookup failures return ok=False so the item is dead-lettered
    for manual review instead of sending with unverified context.
    """
    from .clients import _fs

    thread_id = str(data.get("threadId") or "").strip()
    reply_to_msg_id = str(data.get("replyToMessageId") or "").strip()
    client_id = str(data.get("clientId") or "").strip()

    if not thread_id or not reply_to_msg_id:
        return {"ok": False, "reason": "thread_reply_missing_identifiers", "thread": None}

    try:
        thread_ref = (
            _fs.collection("users").document(user_id)
            .collection("threads").document(thread_id)
        )
        snapshot = thread_ref.get()
    except Exception as e:
        return {"ok": False, "reason": f"thread_lookup_failed: {e}", "thread": None}

    if not getattr(snapshot, "exists", False):
        return {"ok": False, "reason": "thread_not_found", "thread": None}

    thread = snapshot.to_dict() or {}

    thread_client_id = str(thread.get("clientId") or "").strip()
    if thread_client_id and thread_client_id != client_id:
        return {"ok": False, "reason": "thread_client_mismatch", "thread": None}

    status = str(thread.get("status") or "active").strip().lower()
    if status not in OPEN_THREAD_STATUSES:
        return {"ok": False, "reason": f"thread_no_longer_open (status={status})", "thread": None}

    recorded = False
    try:
        message_doc = thread_ref.collection("messages").document(reply_to_msg_id).get()
        recorded = bool(getattr(message_doc, "exists", False))
    except Exception:
        recorded = False
    if not recorded:
        try:
            matches = list(
                thread_ref.collection("messages")
                .where("sourceMessage.graphMessageId", "==", reply_to_msg_id)
                .limit(1)
                .stream()
            )
            recorded = bool(matches)
        except Exception as e:
            return {"ok": False, "reason": f"reply_target_lookup_failed: {e}", "thread": None}
    if not recorded:
        return {"ok": False, "reason": "reply_target_not_in_thread", "thread": None}

    return {"ok": True, "reason": None, "thread": thread, "status": status}


def _mark_tour_invite_thread_sent(
    user_id: str,
    data: Dict[str, Any],
    outbox_id: Optional[str] = None,
    send_result: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist the post-send lifecycle state for reviewed tour invites."""
    thread_id = (data.get("threadId") or "").strip()
    if not thread_id or not _is_tour_invite_outbox(data):
        return

    recipients = data.get("assignedEmails") or []
    identity = _send_identity_payload(send_result, recipients)
    payload = {
        "tourStatus": "awaiting_confirmation",
        "tourInvite.status": "sent",
        "tourInvite.sentAt": SERVER_TIMESTAMP,
        "tourInvite.outboxId": outbox_id,
        "tourInvite.actionAuditId": data.get("actionAuditId"),
        "tourInvite.sentMessageId": identity.get("sentMessageId"),
        "tourInvite.internetMessageId": identity.get("internetMessageId"),
        "tourInvite.sentThreadId": identity.get("sentThreadId"),
        "tourInvite.conversationId": identity.get("conversationId"),
    }

    try:
        from .clients import _fs
        (
            _fs.collection("users").document(user_id)
            .collection("threads").document(thread_id)
            .set({key: value for key, value in payload.items() if value is not None}, merge=True)
        )
    except Exception as e:
        print(f"   ⚠️ Could not mark tour invite thread {thread_id} sent: {e}")


def _fetch_graph_message_metadata(headers: dict, message_id: str, base: str) -> Dict[str, Any]:
    if not message_id:
        return {}
    try:
        response = exponential_backoff_request(
            lambda: requests.get(
                f"{base}/me/messages/{message_id}",
                headers=headers,
                params={
                    "$select": (
                        "conversationId,subject,from,sender,replyTo,"
                        "toRecipients,ccRecipients"
                    )
                },
                timeout=30,
            )
        )
        return response.json() if response else {}
    except Exception as e:
        print(f"   ⚠️ Could not fetch reply source metadata: {e}")
        return {}


def _find_recent_sent_reply_identity(
    headers: dict,
    base: str,
    conversation_id: Optional[str],
    sent_after: datetime,
    *,
    attempts: int = 4,
) -> Dict[str, Any]:
    """Resolve Graph identity for /reply sends, whose API response has no body."""
    if not conversation_id:
        return {}

    sent_after_iso = sent_after.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    params = {
        "$top": "25",
        "$orderby": "sentDateTime desc",
        "$select": "id,internetMessageId,conversationId,subject,sentDateTime",
        "$filter": f"sentDateTime ge {sent_after_iso}",
    }

    for attempt in range(attempts):
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/mailFolders/SentItems/messages",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
            )
            for msg in (response.json() if response else {}).get("value", []):
                if msg.get("conversationId") != conversation_id:
                    continue
                return {
                    "sentMessageId": msg.get("id"),
                    "internetMessageId": msg.get("internetMessageId"),
                    "conversationId": msg.get("conversationId"),
                }
        except Exception as e:
            print(f"   ⚠️ Could not resolve sent reply identity: {e}")

        if attempt < attempts - 1:
            time.sleep(0.75 * (attempt + 1))

    print("   ⚠️ Sent reply identity not found in SentItems yet")
    return {}


def _merge_send_identity(accumulator: Dict[str, Any], send_result: Dict[str, Any]) -> None:
    for key in ("sentMessageIds", "internetMessageIds", "threadIds", "conversationIds"):
        accumulator.setdefault(key, {})
        values = send_result.get(key) or {}
        if isinstance(values, dict):
            accumulator[key].update(values)


def _all_send_errors_are_opt_out(errors: Dict[str, Any]) -> bool:
    if not errors:
        return False
    return all("opted out" in str(message).lower() for message in errors.values())


def _has_existing_thread_for_property(
    user_id: str,
    recipient_email: str,
    property_address: str,
    *,
    client_id: Optional[str] = None,
) -> bool:
    """
    Check if we've already sent an email to this recipient about this property.

    This is a defense-in-depth check to prevent duplicate outreach emails
    even if duplicate outbox entries are somehow created. When a client id is
    known, only threads from that client can block the send; otherwise old test
    or archived campaigns with the same real property address can suppress a
    valid new campaign outreach.

    Returns True if a matching thread already exists, False otherwise.
    """
    from .clients import _fs

    if not recipient_email or not property_address:
        return False

    recipient_lower = recipient_email.lower().strip()

    # Normalize property address for comparison
    # Remove common prefixes and get just the street address
    property_normalized = property_address.lower().strip()
    if ',' in property_normalized:
        property_normalized = property_normalized.split(',')[0].strip()

    try:
        threads_ref = _fs.collection("users").document(user_id).collection("threads")

        # Query threads where this email was a recipient
        query = threads_ref.where("email", "array_contains", recipient_lower)
        results = list(query.stream())

        for thread in results:
            data = thread.to_dict() or {}
            if client_id and data.get("clientId") != client_id:
                continue
            subject = (data.get("subject") or "").lower()

            # Check if subject contains the property address
            if property_normalized in subject:
                print(f"   🔍 Found existing thread for {recipient_email} + '{property_address}'")
                return True

        return False

    except Exception as e:
        print(f"   ⚠️ Error checking for existing thread: {e}")
        # On error, allow send (don't block on lookup failure)
        return False


def _claim_outbox_item(doc_ref, data: dict, user_id: Optional[str] = None) -> bool:
    """
    Attempt to claim an outbox item for processing using a transaction.
    Prevents duplicate sends when multiple processes run concurrently.

    Returns True if successfully claimed, False if already being processed.
    """
    from .clients import _fs
    from google.cloud.firestore import transactional

    cancelled_seen = {}

    @transactional
    def claim_transaction(transaction, doc_ref):
        # Read current state
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            # Item was already deleted
            return False

        current_data = snapshot.to_dict() or {}
        if _is_cancelled_outbox_item(current_data):
            transaction.delete(doc_ref)
            cancelled_seen["data"] = current_data
            print(f"   🗑️ Deleted canceled outbox item {doc_ref.id}")
            return False

        processing_by = current_data.get("processingBy")
        processing_at = current_data.get("processingAt")

        now = datetime.now(timezone.utc)

        # Check if already being processed
        if processing_by and processing_at:
            # Check if claim is stale (older than CLAIM_TIMEOUT_SECONDS)
            if hasattr(processing_at, 'timestamp'):
                # Firestore timestamp
                claim_age = (now - processing_at.replace(tzinfo=timezone.utc)).total_seconds()
            else:
                # Already a datetime
                claim_age = (now - processing_at).total_seconds()

            if claim_age < CLAIM_TIMEOUT_SECONDS:
                # Claim is still valid, skip this item
                print(f"   ⏭️ Item {doc_ref.id} already being processed by {processing_by} ({int(claim_age)}s ago)")
                return False
            else:
                print(f"   ⚠️ Stale claim on {doc_ref.id} by {processing_by} ({int(claim_age)}s ago), reclaiming")

        # Claim the item
        transaction.update(doc_ref, {
            "processingBy": WORKER_ID,
            "processingAt": now
        })
        return True

    try:
        transaction = _fs.transaction()
        claimed = claim_transaction(transaction, doc_ref)
        if not claimed and cancelled_seen:
            _terminalize_outbox_action_audit(
                user_id,
                doc_ref,
                cancelled_seen.get("data") or data,
                "cancelled",
                {"cancelledAt": SERVER_TIMESTAMP},
            )
        return claimed
    except Exception as e:
        print(f"   ⚠️ Failed to claim {doc_ref.id}: {e}")
        return False


def _release_claim(doc_ref):
    """Release claim on an outbox item (called on failure to allow retry)."""
    try:
        doc_ref.update({
            "processingBy": None,
            "processingAt": None
        })
    except Exception as e:
        print(f"   ⚠️ Failed to release claim on {doc_ref.id}: {e}")


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _get_reply_message_sender(headers: dict, reply_to_msg_id: str) -> Optional[str]:
    """Fetch the sender address of the message a dashboard reply targets."""
    if not reply_to_msg_id:
        return None

    try:
        resp = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{reply_to_msg_id}",
                headers=headers,
                params={"$select": "from"},
                timeout=30,
            )
        )
        if not resp or resp.status_code != 200:
            print(f"   ⚠️ Could not resolve reply recipient source: {resp.status_code if resp else 'no response'}")
            return None
        return _normalize_email(
            ((resp.json() or {}).get("from") or {})
            .get("emailAddress", {})
            .get("address", "")
        )
    except Exception as e:
        print(f"   ⚠️ Could not resolve reply recipient source: {e}")
        return None


def _assigned_emails_match_reply_sender(assigned_emails: List[str], reply_sender: Optional[str]) -> bool:
    """True when Graph /reply would send to the same single recipient shown in the UI."""
    normalized = [_normalize_email(email) for email in (assigned_emails or []) if _normalize_email(email)]
    if len(normalized) != 1 or not reply_sender:
        return False
    return normalized[0] == _normalize_email(reply_sender)


def _recipient_address(recipient: Dict[str, Any]) -> str:
    if isinstance(recipient, str):
        return recipient.strip()
    return (
        ((recipient or {}).get("emailAddress") or {}).get("address")
        or ""
    ).strip()


def _recipient_display_name(recipient: Dict[str, Any]) -> str:
    if not isinstance(recipient, dict):
        return ""
    return (
        ((recipient or {}).get("emailAddress") or {}).get("name")
        or ""
    ).strip()


def _graph_recipient(address: str, name: Optional[str] = None) -> Dict[str, Any]:
    email_address = {"address": address}
    if name:
        email_address["name"] = name
    return {"emailAddress": email_address}


# Last operator (sender) mailbox this process has filtered reply-all audiences
# for. SiteSift runs per-mailbox, so once we've seen the operator address we can
# still strip it from a reply-all audience even if a caller forgets to thread
# user_email through — preventing the mailbox from reply-all'ing itself.
_LAST_KNOWN_OPERATOR_EMAIL: Optional[str] = None


def _filter_reply_all_draft_recipients(
    user_id: str,
    draft: Dict[str, Any],
    *,
    user_email: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Keep Microsoft Graph's reply-all audience, but remove unsafe recipients
    before the draft is sent.

    Direct /replyAll does not let us filter CCs. Using createReplyAll + patch
    lets Graph preserve thread semantics while SiteSift still honors opt-outs,
    blocked contacts, duplicate prevention, and the operator's own address.
    """
    from .processing import is_contact_opted_out, _mailbox_identity_without_plus

    global _LAST_KNOWN_OPERATOR_EMAIL
    operator = _normalize_email(user_email)
    if operator:
        # Remember the operator so a later caller that omits user_email still
        # gets the self-send guard.
        _LAST_KNOWN_OPERATOR_EMAIL = operator
    elif _LAST_KNOWN_OPERATOR_EMAIL:
        operator = _LAST_KNOWN_OPERATOR_EMAIL
    # Compare on the plus-alias-stripped mailbox identity so an operator alias
    # (e.g. agent+campaign1@sitesift.com) is recognized as the operator's own
    # mailbox and removed — otherwise reply-all would deliver back to us.
    operator_identity = _mailbox_identity_without_plus(operator) if operator else None
    seen = set()
    payload = {"toRecipients": [], "ccRecipients": []}
    skipped = {
        "operator": [],
        "duplicate": [],
        "invalid": [],
        "optedOut": [],
    }

    for key in ("toRecipients", "ccRecipients"):
        for recipient in draft.get(key) or []:
            raw_address = _recipient_address(recipient)
            normalized = _normalize_email(raw_address)
            if not normalized:
                skipped["invalid"].append(raw_address)
                continue
            if operator_identity and _mailbox_identity_without_plus(normalized) == operator_identity:
                skipped["operator"].append(normalized)
                continue
            if normalized in seen:
                skipped["duplicate"].append(normalized)
                continue
            if not is_valid_email(normalized):
                skipped["invalid"].append(normalized)
                continue

            # Fail closed: if the opt-out lookup errors we must NOT keep the
            # recipient. Let the error propagate so the whole reply-all filter
            # aborts (callers delete the draft and require manual review)
            # rather than risk emailing an opted-out / blocked contact.
            optout_record = is_contact_opted_out(user_id, normalized)
            if optout_record:
                skipped["optedOut"].append({
                    "email": normalized,
                    "reason": optout_record.get("reason", "unknown"),
                })
                continue

            seen.add(normalized)
            payload[key].append(
                _graph_recipient(normalized, _recipient_display_name(recipient))
            )

    return {
        "payload": payload,
        "skipped": {key: value for key, value in skipped.items() if value},
        "sentRecipients": [
            _recipient_address(recipient)
            for recipient in payload["toRecipients"] + payload["ccRecipients"]
            if _recipient_address(recipient)
        ],
        "ccRecipients": [
            _recipient_address(recipient)
            for recipient in payload["ccRecipients"]
            if _recipient_address(recipient)
        ],
    }


def _hydrate_reply_all_draft_recipients(
    headers: Dict[str, str],
    draft: Dict[str, Any],
    *,
    base: str = "https://graph.microsoft.com/v1.0",
) -> Dict[str, Any]:
    """
    Microsoft Graph may return only the draft id from createReplyAll even though
    the saved draft has the computed To/CC audience. Fetch the draft before
    applying SiteSift's safety filter so a sparse response does not strand the
    outbox item as retrying.
    """
    if not isinstance(draft, dict):
        return {}
    if draft.get("toRecipients") or draft.get("ccRecipients"):
        return draft

    draft_id = draft.get("id")
    if not draft_id:
        return draft

    try:
        fetched_resp = exponential_backoff_request(
            lambda: requests.get(
                f"{base}/me/messages/{draft_id}",
                headers=headers,
                params={"$select": "id,toRecipients,ccRecipients"},
                timeout=30,
            ),
            max_retries=GRAPH_SEND_MAX_RETRIES,
        )
        if not fetched_resp or fetched_resp.status_code != 200:
            print(
                "   ⚠️ Could not fetch reply-all draft recipients: "
                f"{fetched_resp.status_code if fetched_resp else 'None'}"
            )
            return draft
        fetched = fetched_resp.json() or {}
        hydrated = dict(draft)
        hydrated["toRecipients"] = fetched.get("toRecipients") or []
        hydrated["ccRecipients"] = fetched.get("ccRecipients") or []
        return hydrated
    except Exception as exc:
        print(f"   ⚠️ Could not fetch reply-all draft recipients: {exc}")
        return draft


def _draft_has_recipients(draft: Dict[str, Any]) -> bool:
    return bool((draft or {}).get("toRecipients") or (draft or {}).get("ccRecipients"))


def _source_message_reply_all_fallback(
    draft: Dict[str, Any],
    source_message: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Rebuild a reply-all audience from the source message when Graph creates an
    empty draft. Safety filtering still happens afterward, so operator,
    duplicate, invalid, and opted-out recipients are removed before send.
    """
    if not isinstance(draft, dict):
        return {}
    if _draft_has_recipients(draft):
        return draft
    if not isinstance(source_message, dict) or not source_message:
        return draft

    primary_recipients = list(
        source_message.get("replyTo")
        or source_message.get("replyToEmails")
        or []
    )
    if not primary_recipients:
        for key in ("from", "fromEmail", "sender", "senderEmail"):
            recipient = source_message.get(key)
            if recipient:
                primary_recipients.append(recipient)
                break

    copied_recipients = []
    copied_recipients.extend(source_message.get("toRecipients") or source_message.get("to") or [])
    copied_recipients.extend(source_message.get("ccRecipients") or source_message.get("cc") or [])

    if not (primary_recipients or copied_recipients):
        return draft

    rebuilt = dict(draft)
    rebuilt["toRecipients"] = primary_recipients
    rebuilt["ccRecipients"] = copied_recipients
    print("   🧭 Rebuilt reply-all recipients from source message metadata")
    return rebuilt


def _reviewed_recipient_reply_all_fallback(
    draft: Dict[str, Any],
    *,
    to_emails: Optional[List[str]] = None,
    cc_emails: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Last-resort fallback when Microsoft Graph returns an empty reply-all draft
    and source metadata is unavailable. The supplied emails are already the
    dashboard-reviewed/current-processing recipients; normal safety filtering
    still runs after this helper before the draft can send.
    """
    if not isinstance(draft, dict):
        return {}
    if _draft_has_recipients(draft):
        return draft

    to_recipients = [
        _graph_recipient(email)
        for email in (to_emails or [])
        if isinstance(email, str) and email.strip()
    ]
    cc_recipients = [
        _graph_recipient(email)
        for email in (cc_emails or [])
        if isinstance(email, str) and email.strip()
    ]
    if not (to_recipients or cc_recipients):
        return draft

    rebuilt = dict(draft)
    rebuilt["toRecipients"] = to_recipients
    rebuilt["ccRecipients"] = cc_recipients
    print("   🧭 Rebuilt reply-all recipients from reviewed recipient fallback")
    return rebuilt


def _delete_graph_reply_draft(
    headers: Dict[str, str],
    draft_id: Optional[str],
    *,
    base: str = "https://graph.microsoft.com/v1.0",
) -> bool:
    """Best-effort cleanup for createReplyAll drafts that are abandoned pre-send."""
    if not draft_id:
        return False
    try:
        delete_resp = exponential_backoff_request(
            lambda: requests.delete(
                f"{base}/me/messages/{draft_id}",
                headers=headers,
                timeout=30,
            ),
            max_retries=1,
        )
        if delete_resp and delete_resp.status_code in {200, 202, 204}:
            print(f"   🧹 Deleted abandoned reply-all draft {draft_id}")
            return True
        print(
            "   ⚠️ Could not delete abandoned reply-all draft "
            f"{draft_id}: {delete_resp.status_code if delete_resp else 'None'}"
        )
    except Exception as exc:
        print(f"   ⚠️ Could not delete abandoned reply-all draft {draft_id}: {exc}")
    return False


def _get_thread_row_number(user_id: str, thread_id: str) -> Optional[int]:
    """Return the stored Sheet row number for a known thread, if present."""
    if not thread_id:
        return None

    try:
        from .clients import _fs
        thread_doc = (
            _fs.collection("users").document(user_id)
            .collection("threads").document(thread_id).get()
        )
        if not thread_doc.exists:
            return None
        row_number = (thread_doc.to_dict() or {}).get("rowNumber")
        return int(row_number) if row_number else None
    except Exception as e:
        print(f"   ⚠️ Could not resolve row number from thread {thread_id[:20]}...: {e}")
        return None


def _save_outbox_reply_message(
    user_id: str,
    thread_id: str,
    assigned_emails: List[str],
    subject: str,
    body: str,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    cc_emails: Optional[List[str]] = None,
) -> bool:
    """Persist a dashboard-approved Graph reply-all draft send into conversation history."""
    if not thread_id:
        return False

    try:
        from .utils import format_email_body_with_footer

        synthetic_id = f"dashboard-reply-{int(time.time() * 1000)}"
        html_body = format_email_body_with_footer(
            body,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        payload = {
            "direction": "outbound",
            "from": "me",
            "to": assigned_emails or [],
            "cc": cc_emails or [],
            "subject": subject,
            "body": {
                "content": html_body,
                "preview": safe_preview(body, 300),
            },
            "bodyPreview": safe_preview(body, 300),
            "sentDateTime": datetime.now(timezone.utc).isoformat(),
            "headers": {"internetMessageId": synthetic_id},
            "source": "dashboard_outbox_reply",
        }
        return save_message(user_id, thread_id, synthetic_id, payload)
    except Exception as e:
        print(f"   ⚠️ Could not save dashboard reply message for {thread_id[:20]}...: {e}")
        return False


def _send_outbox_as_reply(user_id: str, headers: dict, body: str, reply_to_msg_id: str,
                          thread_id: str, user_signature: str = None,
                          signature_mode: str = None, user_email: str = None,
                          fallback_to_emails: Optional[List[str]] = None,
                          fallback_cc_emails: Optional[List[str]] = None,
                          client_id: Optional[str] = None) -> dict:
    """
    Send an outbox item as a reply to an existing message in a thread.

    Used when user responds via frontend to an action_needed notification.
    The email is sent as a reply to maintain thread continuity.

    Returns: dict with 'sent' (bool) and 'error' (str or None)
    """
    from .utils import get_signature_attachments, needs_signature_attachments, format_email_body_with_footer

    # RAIL 3 (kill switch): gate before touching Graph (even the metadata read).
    outbound_mode = resolve_outbound_mode()
    if outbound_mode != OUTBOUND_MODE_LIVE:
        _kill_switch_suppressed(
            outbound_mode,
            context=f"_send_outbox_as_reply thread {thread_id}",
        )
        return {
            "sent": False,
            "error": f"suppressed_by_kill_switch (SITESIFT_OUTBOUND_MODE={outbound_mode})",
            "suppressedByKillSwitch": True,
            "outboundMode": outbound_mode,
        }

    base = "https://graph.microsoft.com/v1.0"
    source_metadata = _fetch_graph_message_metadata(headers, reply_to_msg_id, base)

    # Format body as HTML with footer
    html_body = format_email_body_with_footer(
        body,
        user_signature,
        signature_mode,
        user_email=user_email,
    )
    logger.debug(
        "outbox.reply_recipient_resolution",
        extra={
            "user_id": user_id,
            "thread_id": thread_id,
            "reply_to_msg_id": reply_to_msg_id,
            "recipient_source": "microsoft_graph_reply_endpoint",
            "assigned_emails_honored": False,
        },
    )

    try:
        create_reply_resp = exponential_backoff_request(
            lambda: requests.post(
                f"{base}/me/messages/{reply_to_msg_id}/createReplyAll",
                headers=headers,
                timeout=30,
            ),
            max_retries=GRAPH_SEND_MAX_RETRIES,
        )

        if not create_reply_resp or create_reply_resp.status_code not in [200, 201]:
            error_msg = f"createReplyAll failed: {create_reply_resp.status_code if create_reply_resp else 'None'}"
            print(f"   ❌ {error_msg}")
            return {"sent": False, "error": error_msg}

        reply_draft = create_reply_resp.json() or {}
        reply_draft_id = reply_draft.get("id")
        if not reply_draft_id:
            error_msg = "createReplyAll did not return a draft id"
            print(f"   ❌ {error_msg}")
            return {"sent": False, "error": error_msg}

        reply_draft = _hydrate_reply_all_draft_recipients(
            headers,
            reply_draft,
            base=base,
        )
        reply_draft = _source_message_reply_all_fallback(
            reply_draft,
            source_metadata,
        )
        reply_draft = _reviewed_recipient_reply_all_fallback(
            reply_draft,
            to_emails=fallback_to_emails,
            cc_emails=fallback_cc_emails,
        )

        recipient_result = _filter_reply_all_draft_recipients(
            user_id,
            reply_draft,
            user_email=user_email,
        )
        recipient_payload = recipient_result["payload"]
        if (
            not (recipient_payload["toRecipients"] or recipient_payload["ccRecipients"])
            and (fallback_to_emails or fallback_cc_emails)
        ):
            reviewed_reply_draft = dict(reply_draft)
            reviewed_reply_draft["toRecipients"] = []
            reviewed_reply_draft["ccRecipients"] = []
            reviewed_reply_draft = _reviewed_recipient_reply_all_fallback(
                reviewed_reply_draft,
                to_emails=fallback_to_emails,
                cc_emails=fallback_cc_emails,
            )
            recipient_result = _filter_reply_all_draft_recipients(
                user_id,
                reviewed_reply_draft,
                user_email=user_email,
            )
            recipient_payload = recipient_result["payload"]

        if not (recipient_payload["toRecipients"] or recipient_payload["ccRecipients"]):
            error_msg = "Reply-all draft has no safe recipients after filtering"
            print(f"   ❌ {error_msg}")
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            return {
                "sent": False,
                "error": error_msg,
                "skippedRecipients": recipient_result.get("skipped"),
            }

        patch_payload = {
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": recipient_payload["toRecipients"],
            "ccRecipients": recipient_payload["ccRecipients"],
        }
        patch_resp = exponential_backoff_request(
            lambda: requests.patch(
                f"{base}/me/messages/{reply_draft_id}",
                headers=headers,
                json=patch_payload,
                timeout=30,
            ),
            max_retries=GRAPH_SEND_MAX_RETRIES,
        )
        if not patch_resp or patch_resp.status_code not in [200, 202, 204]:
            error_msg = f"Patch reply-all draft failed: {patch_resp.status_code if patch_resp else 'None'}"
            print(f"   ❌ {error_msg}")
            return {"sent": False, "error": error_msg}

        if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
            signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)
            for attachment in signature_attachments:
                try:
                    att_resp = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{reply_draft_id}/attachments",
                            headers=headers,
                            json=att,
                            timeout=30,
                        ),
                        max_retries=GRAPH_SEND_MAX_RETRIES,
                    )
                    if att_resp.status_code in [200, 201]:
                        print(f"   📎 Attached {attachment['name']}")
                except Exception as e:
                    print(f"   ⚠️ Error attaching {attachment['name']}: {e}")

        decision = _read_client_automation_decision(user_id, client_id)
        if decision.denies_autonomous_work:
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            reason = f"Campaign send suppressed before Graph send: {decision.reason}"
            print(f"   🛑 {reason}")
            return {
                "sent": False,
                "error": reason,
                **_campaign_suppression_result(decision),
            }

        sent_after = datetime.now(timezone.utc) - timedelta(seconds=10)
        resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{reply_draft_id}/send", headers=headers, timeout=30),
            max_retries=1,
            operation="graph_send",
        )

        if resp and resp.status_code in [200, 202]:
            print(f"   ✅ Sent reply-all draft to thread {thread_id}")
            identity = _find_recent_sent_reply_identity(
                headers,
                base,
                source_metadata.get("conversationId"),
                sent_after,
            )
            return {
                "sent": True,
                "error": None,
                "toRecipients": [
                    _recipient_address(recipient)
                    for recipient in recipient_payload["toRecipients"]
                    if _recipient_address(recipient)
                ],
                "ccRecipients": recipient_result.get("ccRecipients") or [],
                "sentRecipients": recipient_result.get("sentRecipients") or [],
                "skippedRecipients": recipient_result.get("skipped"),
                **identity,
            }

        error_msg = f"Send draft failed: {resp.status_code if resp else 'None'}"
        print(f"   ❌ {error_msg}")
        return {"sent": False, "error": error_msg}

    except Exception as e:
        error_msg = str(e)
        print(f"   ❌ Error sending reply: {error_msg}")
        return {"sent": False, "error": error_msg}


def get_contact_email_count(user_id: str, recipient_email: str) -> int:
    """
    Count how many outbound emails have been sent to this contact.
    Used to determine whether to use primary or secondary script.
    """
    from .clients import _fs

    threads_ref = _fs.collection("users").document(user_id).collection("threads")

    # Query threads where this email was a recipient
    # The 'email' field is an array of recipient emails
    query = threads_ref.where("email", "array_contains", recipient_email.lower().strip())
    results = list(query.stream())

    return len(results)


def _extract_requirements_from_primary(primary_script: str) -> str:
    """
    Extract the requirements section from a primary script for reuse in fallback scenarios.
    Returns just the requirements bullets/section, not the full script.
    """
    if not primary_script:
        return ""

    # Look for common requirement markers
    markers = ["requirements are:", "requirements:", "looking for:", "they need:", "their requirements"]

    script_lower = primary_script.lower()
    for marker in markers:
        if marker in script_lower:
            idx = script_lower.index(marker)
            # Extract from marker to end of bullet list or paragraph
            requirements_section = primary_script[idx:]

            # Find end (next paragraph break or common closing phrases)
            end_markers = ["if you think", "if it is no longer", "please let me know",
                          "alternatively", "if this might", "thanks"]
            for end in end_markers:
                if end in requirements_section.lower():
                    end_idx = requirements_section.lower().index(end)
                    return requirements_section[:end_idx].strip()

            # No end marker found, return the section (up to reasonable length)
            lines = requirements_section.split('\n')
            result_lines = []
            for line in lines:
                if line.strip() == "":
                    # Empty line might signal end of requirements
                    if len(result_lines) > 0:
                        break
                result_lines.append(line)
            return '\n'.join(result_lines).strip()

    # No requirement marker found, return empty
    return ""


NAME_PLACEHOLDER_RE = re.compile(
    r"\[(?:name|first[\s_-]*name|contact[\s_-]*name|broker[\s_-]*name|recipient[\s_-]*name)\]",
    re.IGNORECASE,
)


# Company-name suffix/keyword tokens. If a "name" column value carries any of
# these, it's an organization, not a person — do not fabricate a human
# first-name greeting ("Hi Acme,"); fall back to a neutral greeting instead.
_COMPANY_NAME_TOKENS = frozenset({
    "llc", "inc", "corp", "co", "company", "realty", "group",
    "partners", "llp", "advisors", "associates", "properties",
    "capital", "holdings", "ltd",
})


def _looks_like_company_name(candidate: str) -> bool:
    for token in (candidate or "").split():
        cleaned = re.sub(r"[^a-z]", "", token.lower())
        if cleaned in _COMPANY_NAME_TOKENS:
            return True
    return False


def _safe_greeting_first_name(contact_name: Optional[str]) -> Optional[str]:
    candidate = (contact_name or "").strip()
    if not candidate or "@" in candidate or "[" in candidate or "]" in candidate:
        return None
    if _looks_like_company_name(candidate):
        return None
    first = candidate.split()[0].strip(" ,;:()[]{}")
    if not first or not re.fullmatch(r"[A-Za-z][A-Za-z.'-]{0,63}", first):
        return None
    return first


def _personalize_name_placeholders(script: Optional[str], contact_name: Optional[str]) -> str:
    body = script or ""
    if "[" not in body:
        return body
    first_name = _safe_greeting_first_name(contact_name)
    if not first_name:
        return body
    return NAME_PLACEHOLDER_RE.sub(lambda _match: first_name, body)


def _select_script_for_recipient(user_id: str, recipient_email: str,
                                  scripts: List[str], contact_name: str = None) -> str:
    """
    Select appropriate script based on contact history.

    scripts is an array where:
    - scripts[0] = Primary script (1st contact)
    - scripts[1] = Secondary script (2nd contact)
    - scripts[2] = 3rd contact script
    - etc.

    If no script exists for the contact count, uses the last available script
    with a "staying organized" note for 3rd+ contacts.
    """
    if not scripts or len(scripts) == 0:
        return ""

    email_count = get_contact_email_count(user_id, recipient_email)
    print(f"📊 Contact history for {recipient_email}: {email_count} previous email(s)")

    # Primary script for first contact
    primary_script = scripts[0]

    if email_count == 0:
        print(f"  → Using script[0] - PRIMARY (first contact)")
        return _personalize_name_placeholders(primary_script, contact_name)

    # For subsequent contacts, try to use the matching script index
    script_index = email_count  # 1st contact uses [0], 2nd uses [1], etc.

    if script_index < len(scripts) and scripts[script_index] and scripts[script_index].strip():
        print(f"  → Using script[{script_index}] ({script_index + 1}{'st' if script_index == 0 else 'nd' if script_index == 1 else 'rd' if script_index == 2 else 'th'} contact)")
        script_to_use = scripts[script_index]

        # Add organized note for 3rd+ contacts
        if email_count >= 2:
            organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
            return _personalize_name_placeholders(script_to_use.rstrip() + organized_note, contact_name)

        return _personalize_name_placeholders(script_to_use, contact_name)

    # Fallback: use the last available script
    last_script = None
    for s in reversed(scripts):
        if s and s.strip():
            last_script = s
            break

    if last_script and last_script != primary_script:
        print(f"  → Using last available script (fallback for contact #{email_count + 1})")
        if email_count >= 2:
            organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
            return _personalize_name_placeholders(last_script.rstrip() + organized_note, contact_name)
        return _personalize_name_placeholders(last_script, contact_name)

    # Ultimate fallback: generate from primary
    print(f"  → Using GENERATED fallback (contact #{email_count + 1})")
    requirements = _extract_requirements_from_primary(primary_script)

    # Extract first name from contact_name for greeting.
    first_name = _safe_greeting_first_name(contact_name)
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    if email_count == 1:
        if requirements:
            return f"""{greeting}

I just emailed you about another one of your listings, but I was wondering if you think there might be a fit at the above address as well.

As a reminder, {requirements}

Thanks!"""
        else:
            return f"""{greeting}

I just emailed you about another one of your listings, but I was wondering if you think there might be a fit at the above address as well.

Please let me know if you have any information on this property, or if it's no longer available.

Thanks!"""
    else:
        organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
        if requirements:
            return f"""{greeting}

I've reached out about a couple of your other listings. I'm also interested in the property at the above address.

As a reminder, {requirements}
{organized_note}

Thanks!"""
        else:
            return f"""{greeting}

I've reached out about a couple of your other listings. I'm also interested in the property at the above address.

Please let me know if you have any information on this property, or if it's no longer available.
{organized_note}

Thanks!"""


def _should_use_exact_outbox_script(data: Dict[str, Any]) -> bool:
    """True when the outbox item contains approved copy that must not be re-selected."""
    source = str(data.get("source") or "").strip().lower()
    action_type = str(data.get("actionType") or "").strip().lower()
    return (
        data.get("scriptSelectionMode") == "exact"
        or data.get("forceScript") is True
        or source in EXACT_OUTBOX_SOURCES
        or action_type in EXACT_OUTBOX_ACTION_TYPES
    )


def _thread_context_from_outbox(data: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve dashboard-flow context on the sent thread for later reply handling."""
    if not data:
        return {}

    context = {}
    for key in ("source", "actionType", "tourInvite", "actionAuditId", "property"):
        value = data.get(key)
        if value:
            context[key] = value
    return context


def _property_address_from_thread_context(thread_context: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the real property address from dashboard context when subject is workflow copy."""
    if not isinstance(thread_context, dict):
        return None

    property_data = thread_context.get("property")
    if not isinstance(property_data, dict):
        return None

    address = str(property_data.get("address") or "").strip()
    city = str(property_data.get("city") or "").strip()

    if address and city:
        return f"{address}, {city}"
    if address:
        return address
    return None


_CANCELLED_OUTBOX_STATUSES = {
    "cancel_requested",
    "cancelled",
    "canceled",
    # optimistic in-progress cancel states set by the UI on click
    "cancelling",
    "canceling",
}


def _flag_is_truthy(value: Any) -> bool:
    """Truthy-check a loosely-typed flag WITHOUT an identity match.

    Dashboard/Firestore-REST/form-encoded writes can land a cancel flag as a
    real bool, an int (1/0), or a string ("true"/"false"). `is True` misses all
    but the native bool, so we coerce string/int forms explicitly.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "t"}
    return False


def _is_cancelled_outbox_item(data: Dict[str, Any]) -> bool:
    """True when the dashboard has requested cancellation before the worker sends."""
    # Normalize delimiter variants ("cancel-requested" -> "cancel_requested")
    # so differently-formatted dashboard/REST writes still register as cancels.
    status = re.sub(r"[\s-]+", "_", (data.get("status") or "").strip().lower())
    if _flag_is_truthy(data.get("cancelRequested")):
        return True
    return status in _CANCELLED_OUTBOX_STATUSES


def _delete_cancelled_outbox_item_if_needed(
    doc_ref,
    data: Dict[str, Any],
    user_id: Optional[str] = None,
) -> bool:
    if not _is_cancelled_outbox_item(data):
        return False
    doc_id = getattr(doc_ref, "id", "unknown")
    try:
        doc_ref.delete()
        _terminalize_outbox_action_audit(
            user_id,
            doc_ref,
            data,
            "cancelled",
            {"cancelledAt": SERVER_TIMESTAMP},
        )
        print(f"   🗑️ Deleted canceled outbox item {doc_id}")
    except Exception as e:
        print(f"   ⚠️ Could not delete canceled outbox item {doc_id}: {e}")
    return True


def _must_process_outbox_item_individually(data: Dict[str, Any]) -> bool:
    """Dashboard-approved replies/exact-copy items must not be bundled into campaign outreach."""
    return bool(
        data.get("threadId")
        or data.get("replyToMessageId")
        or data.get("notificationId")
        or _should_use_exact_outbox_script(data)
    )


def _get_current_outbox_data(doc_ref) -> Optional[Dict[str, Any]]:
    if not hasattr(doc_ref, "get"):
        return {}
    try:
        snapshot = doc_ref.get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}
    except Exception as e:
        print(f"   ⚠️ Could not refresh outbox item {getattr(doc_ref, 'id', 'unknown')}: {e}")
        return None


def _finalize_successful_outbox_item(
    user_id: str,
    doc_ref,
    data: Dict[str, Any],
    row_number: Optional[int] = None,
    client_id: Optional[str] = None,
    send_result: Optional[Dict[str, Any]] = None,
):
    """Delete sent outbox and apply post-send dashboard state only after send success."""
    from .clients import _fs

    client_id = client_id or (data.get("clientId") or "").strip()

    doc_ref.delete()

    audit_payload = {
        "status": "sent",
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": client_id or None,
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "sentAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    audit_payload.update(_send_identity_payload(send_result, data.get("assignedEmails") or []))
    if data.get("sentRecipients"):
        audit_payload["sentRecipients"] = _ordered_unique(
            [
                email for email in data.get("sentRecipients", [])
                if isinstance(email, str)
            ] + ((send_result or {}).get("sent") or [])
        )
    if data.get("partialSend") or data.get("remainingRecipients"):
        audit_payload["partialSend"] = False
        audit_payload["remainingRecipients"] = []
    _update_action_audit(user_id, data.get("actionAuditId"), audit_payload)
    _mark_tour_invite_thread_sent(
        user_id,
        data,
        outbox_id=getattr(doc_ref, "id", None),
        send_result=send_result,
    )

    if row_number and client_id:
        try:
            sheet_id = _get_sheet_id_or_fail(user_id, client_id)
            highlight_row(sheet_id, row_number)
        except Exception as e:
            print(f"  ⚠️ Could not highlight row {row_number}: {e}")

    notification_id = data.get("notificationId")
    notification_client_id = data.get("notificationClientId") or client_id
    if data.get("deleteNotificationOnSend") and notification_id and notification_client_id:
        try:
            delete_notification_and_decrement_counters(user_id, notification_client_id, notification_id)
            print(f"   🗑️ Deleted action notification {notification_id} after send")
        except Exception as e:
            print(f"   ⚠️ Could not delete action notification {notification_id}: {e}")

    thread_id = data.get("threadId")
    if data.get("resumeThreadOnSend") and thread_id:
        # resumeThreadOnSend and threadId are client-supplied (InlineReplyComposer),
        # so re-check the thread's current state before flipping it active:
        # never resurrect a terminal (stopped/completed) thread and never touch a
        # thread that belongs to a different client. Fail closed on any doubt.
        try:
            thread_ref = (
                _fs.collection("users").document(user_id)
                .collection("threads").document(thread_id)
            )
            resume_block_reason = None
            snapshot = thread_ref.get()
            if not getattr(snapshot, "exists", False):
                resume_block_reason = "thread_not_found"
            else:
                thread = snapshot.to_dict() or {}
                thread_client_id = str(thread.get("clientId") or "").strip()
                status = str(thread.get("status") or "active").strip().lower()
                if thread_client_id and thread_client_id != (client_id or ""):
                    resume_block_reason = "thread_client_mismatch"
                elif status not in OPEN_THREAD_STATUSES:
                    resume_block_reason = f"thread_no_longer_open (status={status})"
            if resume_block_reason:
                print(
                    f"   ⏭️ Skipped thread resume for {thread_id[:20]}... after send: "
                    f"{resume_block_reason}"
                )
            else:
                thread_ref.set({
                    "status": "active",
                    "followUpStatus": "waiting",
                    "lastOperatorReplySentAt": SERVER_TIMESTAMP,
                    "updatedAt": SERVER_TIMESTAMP,
                }, merge=True)
                print(f"   ▶️ Resumed thread {thread_id[:20]}... after dashboard send")
        except Exception as e:
            print(f"   ⚠️ Could not resume thread {thread_id[:20]}... after send: {e}")


def _subject_for_recipient(uid: str, client_id: str, recipient_email: str) -> Optional[str]:
    """
    Look up the row by email and return 'property address, city' as subject.
    Falls back to None if sheet/row/columns not found.
    """
    try:
        sheet_id = _get_sheet_id_or_fail(uid, client_id)
        sheets   = _sheets_client()
        tab      = _get_first_tab_title(sheets, sheet_id)
        header   = _read_header_row2(sheets, sheet_id, tab)

        rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab, header, recipient_email)
        if rownum is None or not rowvals:
            print(f"⚠️ No row found for {recipient_email} in sheet {sheet_id}")
            return None

        # Build a header index map and support common variants
        idx_map = {(h or "").strip().lower(): i for i, h in enumerate(header, start=1)}  # 1-based

        # Try a few reasonable header name variants
        addr_keys = [
            "property address", "address", "street address", "property", "property_address"
        ]
        city_keys = [
            "city", "town", "municipality"
        ]

        def _get_val(keys: List[str]) -> Optional[str]:
            for k in keys:
                if k in idx_map:
                    i = idx_map[k] - 1  # 0-based for rowvals
                    if 0 <= i < len(rowvals):
                        v = (rowvals[i] or "").strip()
                        if v:
                            return v
            return None

        prop = _get_val(addr_keys)
        city = _get_val(city_keys)

        if prop and city:
            return f"{prop}, {city}"
        if prop:
            return prop
        if city:
            return city

        print(f"ℹ️ Address/city columns not found for {recipient_email}")
        return None

    except Exception as e:
        print(f"⚠️ Subject lookup failed for {recipient_email}: {e}")
        return None

def send_and_index_email(user_id: str, headers: Dict[str, str], script: str, recipients: List[str],
                        client_id_or_none: Optional[str] = None, row_number: int = None, user_signature: str = None,
                        subject_override: str = None, signature_mode: str = None, followup_config: Dict = None,
                        contact_name: str = None, user_email: str = None,
                        thread_context: Optional[Dict[str, Any]] = None,
                        allow_scheduling_language: bool = False):
    """
    Send email and immediately index it in Firestore for reply tracking.

    Automatically appends the email footer (signature) to all emails.
    For outbox items: script content comes from frontend LLM, footer is appended here.
    For inbox replies: script content may come from backend LLM or templates, footer is appended here.

    Args:
        user_id: The Firebase user ID
        headers: Auth headers for Graph API
        script: The email body content
        recipients: List of recipient email addresses
        client_id_or_none: Optional client ID for tracking
        row_number: Optional row number for thread anchoring
        user_signature: Optional custom signature from user settings
        subject_override: Optional pre-computed subject (e.g., from property data)
        signature_mode: Signature mode - "none", "custom", or "professional"
        followup_config: Optional follow-up configuration from outbox
        contact_name: Optional contact name for follow-up personalization
        user_email: Sender profile email used to gate Jill's explicit legacy footer
        thread_context: Optional dashboard context to store on newly indexed threads

    SAFETY: All recipient emails are validated before sending to prevent sending to malformed addresses.
    SAFETY: Opted-out contacts are filtered out before sending.
    """
    # RAIL 3 (kill switch): resolved once per send, checked before any Graph
    # call. Fail closed — anything but "live" skips the send entirely.
    outbound_mode = resolve_outbound_mode()
    if outbound_mode != OUTBOUND_MODE_LIVE:
        _kill_switch_suppressed(
            outbound_mode,
            context=f"send_and_index_email to {len(recipients or [])} recipient(s)",
        )
        return {
            "sent": [],
            "errors": {"_all": f"suppressed_by_kill_switch (SITESIFT_OUTBOUND_MODE={outbound_mode})"},
            "suppressedByKillSwitch": True,
            "outboundMode": outbound_mode,
        }

    if not recipients:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    body_validation = validate_outbound_body(
        script,
        allow_scheduling_language=allow_scheduling_language,
    )
    if not body_validation.is_safe:
        reason = f"{body_validation.reason}; manual review required before sending"
        print(f"🛑 Blocked unsafe send_and_index_email body: {reason}")
        return {"sent": [], "errors": {"_all": reason}}

    # CRITICAL: Check for opted-out contacts before sending
    from .processing import is_contact_opted_out
    opted_out_recipients = []
    active_recipients = []

    for recipient in recipients:
        optout_record = is_contact_opted_out(user_id, recipient)
        if optout_record:
            opted_out_recipients.append({
                "email": recipient,
                "reason": optout_record.get("reason", "unknown"),
                "optedOutAt": str(optout_record.get("optedOutAt", ""))
            })
            print(f"🚫 Skipping opted-out contact: {recipient} (reason: {optout_record.get('reason')})")
        else:
            active_recipients.append(recipient)

    if not active_recipients:
        errors = {"_all": "All recipients have opted out"}
        for optout in opted_out_recipients:
            errors[optout["email"]] = f"Contact opted out ({optout['reason']})"
        return {"sent": [], "errors": errors, "opted_out": opted_out_recipients}

    recipients = active_recipients  # Continue with non-opted-out recipients

    # CRITICAL: Validate all recipient emails before sending
    valid_recipients, invalid_recipients = validate_recipient_emails(recipients)

    if invalid_recipients:
        print(f"⚠️ REJECTED invalid email addresses: {invalid_recipients}")

    if not valid_recipients:
        return {"sent": [], "errors": {"_all": f"No valid recipients. Invalid: {invalid_recipients}"}}

    content_type, content = _body_kind(script)

    # Initialize results with opted_out info
    results = {
        "sent": [],
        "errors": {},
        "opted_out": opted_out_recipients,
        "sentMessageIds": {},
        "internetMessageIds": {},
        "threadIds": {},
        "conversationIds": {},
    }

    # Add opted-out recipients to errors for visibility
    for optout in opted_out_recipients:
        results["errors"][optout["email"]] = f"Contact opted out ({optout['reason']})"

    # Append footer to all emails (signature with logo, contact info, etc.)
    from .utils import get_email_footer, format_email_body_with_footer, get_signature_attachments, needs_signature_attachments

    # Check if we need to attach signature images (for professional mode)
    signature_attachments = []
    if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
        signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)
        print(f"📎 Will attach {len(signature_attachments)} signature image(s)")

    if content_type == "HTML":
        # If already HTML, wrap it properly and append footer
        # Check if content is already wrapped in HTML structure
        footer_html = get_email_footer(user_signature, signature_mode, user_email=user_email)
        if not content.strip().startswith("<!DOCTYPE") and not content.strip().startswith("<html"):
            # Wrap existing HTML content and add footer (only if footer exists)
            if footer_html:
                content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px;">
{content}
<!-- Email signature separator - prevents collapse -->
<div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;">
<div style="margin-top: 20px;">
{footer_html}
</div>
</div>
</div>
</body>
</html>"""
            else:
                # No footer - just wrap in HTML structure
                content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px;">
{content}
</div>
</body>
</html>"""
        else:
            # Content is already wrapped, only append footer if it exists
            if footer_html:
                if "</body>" in content:
                    footer_with_wrapper = f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{footer_html}</div></div>'
                    content = content.replace("</body>", footer_with_wrapper + "</body>")
                else:
                    # No body tag, just append
                    content = content + f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{footer_html}</div></div>'
            # If no footer, leave content as-is
    else:
        # Convert to HTML and add footer (this function now wraps in proper HTML structure)
        content = format_email_body_with_footer(
            content,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        content_type = "HTML"
    base = "https://graph.microsoft.com/v1.0"

    # Add invalid recipients to errors
    for invalid in invalid_recipients:
        results["errors"][invalid] = "Invalid email address format"

    for addr in valid_recipients:
        # Use pre-computed subject if provided, otherwise look up from sheet
        if subject_override:
            subject_to_use = subject_override
        else:
            dynamic_subject = None
            if client_id_or_none:
                dynamic_subject = _subject_for_recipient(user_id, client_id_or_none, (addr or "").lower())
            subject_to_use = dynamic_subject or "Client Outreach"

        msg = {
            "subject": subject_to_use,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": addr}}],
        }
        
        # Add headers
        internet_headers = []
        if client_id_or_none:
            internet_headers.append({"name": "x-client-id", "value": client_id_or_none})
        if row_number:
            internet_headers.append({"name": "x-row-anchor", "value": f"rowNumber={row_number}"})
        
        if internet_headers:
            msg["internetMessageHeaders"] = internet_headers

        try:
            # 1. Create draft
            create_response = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages", headers=headers, json=msg, timeout=30),
                max_retries=GRAPH_SEND_MAX_RETRIES,
            )
            draft_id = create_response.json()["id"]
            print(f"📝 Created draft {draft_id} for {addr}")

            # 1b. Add signature image attachments (for professional mode)
            # CID (Content-ID) attachments are the most reliable way to embed images in emails
            if signature_attachments:
                for attachment in signature_attachments:
                    attach_response = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{draft_id}/attachments",
                            headers=headers,
                            json=att,
                            timeout=30
                        ),
                        max_retries=GRAPH_SEND_MAX_RETRIES,
                    )
                    if attach_response.status_code in [200, 201]:
                        print(f"   📎 Attached {attachment['name']}")
                    else:
                        print(f"   ⚠️ Failed to attach {attachment['name']}: {attach_response.status_code}")

            # 2. Get message identifiers
            get_response = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages/{draft_id}",
                    headers=headers,
                    params={"$select": "internetMessageId,conversationId,subject,toRecipients"},
                    timeout=30
                )
            )
            message_data = get_response.json()

            internet_message_id = message_data.get("internetMessageId")
            conversation_id = message_data.get("conversationId")
            subject = message_data.get("subject", "")

            if not internet_message_id:
                raise Exception("No internetMessageId returned from Graph")

            # 3. Send draft. This is the irreversible boundary, so campaign
            # eligibility must be read again after draft preparation.
            decision = _read_client_automation_decision(user_id, client_id_or_none)
            if decision.denies_autonomous_work:
                _delete_graph_reply_draft(headers, draft_id, base=base)
                reason = f"Campaign send suppressed before Graph send: {decision.reason}"
                results["errors"][addr] = reason
                results.update(_campaign_suppression_result(decision))
                print(f"🛑 {reason}")
                return results

            exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages/{draft_id}/send", headers=headers, timeout=30),
                max_retries=1,
                operation="graph_send",
            )

            # 4. Index in Firestore with retry logic
            # CRITICAL: Email is already sent at this point. We MUST index it successfully
            # or future replies will be orphaned (unable to match to this thread).
            root_id = normalize_message_id(internet_message_id)
            results["sentMessageIds"][addr] = draft_id
            results["internetMessageIds"][addr] = internet_message_id
            results["threadIds"][addr] = root_id
            results["conversationIds"][addr] = conversation_id

            # Thread root
            thread_meta = {
                "subject": subject,
                "clientId": client_id_or_none,
                "email": [addr],
                "conversationId": conversation_id,
                "status": "active",  # New threads start as active
            }

            # Store row number for anchoring if provided
            if row_number:
                thread_meta["rowNumber"] = row_number

            # Store contact name for follow-up personalization
            if contact_name:
                thread_meta["contactName"] = contact_name

            if isinstance(thread_context, dict):
                thread_meta.update({
                    key: value for key, value in thread_context.items()
                    if key in {"source", "actionType", "tourInvite", "actionAuditId"} and value
                })

            # Store property address for PDF/data matching
            # Prefer explicit dashboard property context for workflow subjects
            # like "Tour slot: 0 Gemini Ave at 9:00 AM"; fall back to subject.
            context_property_address = _property_address_from_thread_context(thread_context)
            if context_property_address:
                thread_meta["propertyAddress"] = context_property_address
            elif subject:
                # Remove common prefixes like "Re:", "RE:", "Fwd:", etc.
                clean_subject = subject.strip()
                for prefix in ["Re:", "RE:", "Fwd:", "FWD:", "Fw:"]:
                    if clean_subject.startswith(prefix):
                        clean_subject = clean_subject[len(prefix):].strip()
                thread_meta["propertyAddress"] = clean_subject

            # Combined send mode: one thread covers ALL of a broker's properties.
            # Record every property/row it spans (additive — singular
            # propertyAddress/rowNumber above stay set for backward compatibility)
            # so a future reply parser can fan an answer back across the listings.
            if isinstance(thread_context, dict):
                combined_addresses = thread_context.get("propertyAddresses")
                if isinstance(combined_addresses, list):
                    cleaned_addresses = [str(a).strip() for a in combined_addresses if a]
                    if cleaned_addresses:
                        thread_meta["propertyAddresses"] = cleaned_addresses
                combined_rows = thread_context.get("rows")
                if isinstance(combined_rows, list):
                    cleaned_rows = [r for r in combined_rows if r]
                    if cleaned_rows:
                        thread_meta["rows"] = cleaned_rows

            # Save thread root with retry
            thread_saved = False
            for attempt in range(MAX_INDEX_RETRIES):
                if save_thread_root(user_id, root_id, thread_meta):
                    thread_saved = True
                    break
                print(f"⚠️ Thread save attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))  # Backoff

            if not thread_saved:
                raise Exception(f"Failed to save thread root after {MAX_INDEX_RETRIES} attempts - replies will be orphaned")

            # Message record
            message_record = {
                "direction": "outbound",
                "subject": subject,
                "from": "me",  # Graph doesn't return our own address easily
                "to": [addr],
                "sentDateTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "receivedDateTime": None,
                "headers": {
                    "internetMessageId": internet_message_id,
                    "inReplyTo": None,
                    "references": []
                },
                "body": {
                    "contentType": content_type,
                    "content": content,
                    "preview": safe_preview(content)
                }
            }

            # Save message with retry
            message_saved = False
            for attempt in range(MAX_INDEX_RETRIES):
                if save_message(user_id, root_id, root_id, message_record):
                    message_saved = True
                    break
                print(f"⚠️ Message save attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))

            if not message_saved:
                print(f"⚠️ Failed to save message record after {MAX_INDEX_RETRIES} attempts (thread exists, non-critical)")

            # Index message ID with retry and verification (CRITICAL for reply matching)
            msg_indexed = False
            for attempt in range(MAX_INDEX_RETRIES):
                if index_message_id(user_id, internet_message_id, root_id):
                    # Verify the index was actually written
                    time.sleep(0.2)  # Brief delay for consistency
                    if lookup_thread_by_message_id(user_id, internet_message_id) == root_id:
                        msg_indexed = True
                        break
                    print(f"⚠️ Index verification failed on attempt {attempt + 1}")
                print(f"⚠️ Message index attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))

            if not msg_indexed:
                raise Exception(f"CRITICAL: Failed to index message ID after {MAX_INDEX_RETRIES} attempts - replies will be orphaned")

            # Index conversation ID with retry (fallback lookup, less critical but still important)
            if conversation_id:
                conv_indexed = False
                for attempt in range(MAX_INDEX_RETRIES):
                    if index_conversation_id(user_id, conversation_id, root_id):
                        conv_indexed = True
                        break
                    print(f"⚠️ Conversation index attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                    time.sleep(0.5 * (attempt + 1))

                if not conv_indexed:
                    # Log but don't fail - message ID index is the primary lookup
                    print(f"⚠️ Failed to index conversation ID (fallback) - primary index succeeded")

            results["sent"].append(addr)
            print(f"✅ Sent and indexed email to {addr} (threadId: {root_id})")

            # Schedule follow-up if configured
            if followup_config and followup_config.get("enabled", False):
                from .followup import schedule_followup_for_thread
                from .clients import _fs
                # Store contact name on thread for follow-up personalization
                if contact_name:
                    _fs.collection("users").document(user_id).collection("threads").document(root_id).update({
                        "contactName": contact_name
                    })
                schedule_followup_for_thread(user_id, root_id, followup_config)

        except Exception as e:
            msg = str(e)
            print(f"❌ Failed to send/index to {addr}: {msg}")
            results["errors"][addr] = msg

    return results

def _move_to_dead_letter(user_id: str, doc_ref, data: dict, reason: str):
    """Move a failed outbox item to the dead-letter queue for manual review."""
    from .clients import _fs
    from google.cloud.firestore import SERVER_TIMESTAMP

    dead_letter_ref = _fs.collection("users").document(user_id).collection("deadLetterQueue")
    attempts = max(int(data.get("attempts") or 0), MAX_OUTBOX_ATTEMPTS)

    # Copy data to dead-letter queue with failure info
    dead_letter_data = {
        **data,
        "originalDocId": doc_ref.id,
        "status": "dead_lettered",
        "attempts": attempts,
        "maxAttempts": MAX_OUTBOX_ATTEMPTS,
        "lastError": reason,
        "failureReason": reason,
        "failedAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "source": "outbox"
    }

    dead_letter_ref.add(dead_letter_data)
    _update_action_audit(user_id, data.get("actionAuditId"), {
        "status": "dead_lettered",
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": data.get("clientId"),
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "failureReason": reason,
        "attempts": attempts,
        "maxAttempts": MAX_OUTBOX_ATTEMPTS,
        "lastError": reason,
        "deadLetteredAt": SERVER_TIMESTAMP,
        "failedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })
    doc_ref.delete()
    print(f"☠️ Moved item {doc_ref.id} to dead-letter queue: {reason}")


def _record_outbox_reconciliation(
    user_id: str,
    doc_ref,
    data: Dict[str, Any],
    reason: str,
    send_result: Dict[str, Any],
    recipients: List[str],
    *,
    delete_original: bool = False,
) -> None:
    """Expose a Graph-accepted send that could not be fully indexed.

    Graph has already accepted the message, so retrying the same outbox item could
    duplicate-send. Operators need a visible reconciliation item instead.
    """
    from .clients import _fs

    recipients = _ordered_unique([email for email in recipients if isinstance(email, str)])
    identity_payload = _send_identity_payload({**(send_result or {}), "sent": recipients}, recipients)
    dead_letter_payload = {
        **data,
        "originalDocId": getattr(doc_ref, "id", None),
        "assignedEmails": recipients,
        "sentRecipients": recipients,
        "source": "outbox",
        "status": "needs_reconciliation",
        "alreadySent": True,
        "failureReason": reason,
        "deadLetteredAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
        **identity_payload,
    }
    _fs.collection("users").document(user_id).collection("deadLetterQueue").add(dead_letter_payload)

    if delete_original:
        _update_action_audit(user_id, data.get("actionAuditId"), {
            "status": "needs_reconciliation",
            "outboxId": getattr(doc_ref, "id", None),
            "clientId": data.get("clientId"),
            "notificationId": data.get("notificationId"),
            "threadId": data.get("threadId"),
            "alreadySent": True,
            "failureReason": reason,
            "deadLetteredAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
            **identity_payload,
        })
        doc_ref.delete()


def _should_preflight_sent_items_retry(data: Dict[str, Any]) -> bool:
    data = data or {}
    return (
        int(data.get("attempts") or 0) > 0
        or bool(data.get("lastError"))
        or bool(data.get("requiresSentItemsPreflight"))
    )


def _sent_retry_reconciliation_result(
    headers: Dict[str, str],
    data: Dict[str, Any],
    recipient: str,
    body: str,
    subject: Optional[str],
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not _should_preflight_sent_items_retry(data):
        return {}
    try:
        match = find_matching_sent_message_for_retry(
            headers,
            recipient=recipient,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
            sent_after=sent_after_from_retry_data(data),
        )
    except SentMailGuardLookupError as exc:
        return {"guardLookupError": str(exc)}
    if match:
        return send_result_from_sent_match(match, recipient)
    try:
        manual_continuation = find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=conversation_id,
            sent_after=sent_after_from_retry_data(data),
        )
    except SentMailGuardLookupError as exc:
        return {"guardLookupError": str(exc)}
    if manual_continuation:
        return {"manualContinuation": manual_continuation}
    return {}


def _manual_continuation_retry_reason(prior_send: Dict[str, Any]) -> str:
    sent_at = ((prior_send or {}).get("manualContinuation") or {}).get("sentDateTime")
    suffix = f" at {sent_at}" if sent_at else ""
    return (
        "Queued send stopped because Sent Items shows the user manually continued "
        f"this conversation{suffix}; review before retrying the stale draft."
    )


def _outbox_send_operation_state(
    status: str,
    doc_id: Optional[str] = None,
    recipient: Optional[str] = None,
    error: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a Graph operation-state for an outbox send outcome (GO-condition #3).

    Shape matches what ``main._combine_graph_operation_states`` consumes: a dict
    carrying a ``status`` in {"healthy", "error", "unknown"} plus optional context.
    """
    state: Dict[str, Any] = {"status": status, "operation": "outbox_send"}
    if doc_id:
        state["docId"] = doc_id
    if recipient:
        state["recipient"] = recipient
    if error is not None:
        state["error"] = str(error)[:1500]
    return state


def _record_operation_state(operation_states, state) -> None:
    """Append an op-state to the accumulator when one is being collected."""
    if operation_states is not None:
        operation_states.append(state)


def send_outboxes(
    user_id: str,
    headers: Dict[str, str],
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    """
    Process outbox items: read script content (generated by frontend LLM), append footer, and send.

    Flow:
    1. Frontend LLM generates email content and writes to Firestore outbox with 'script' field
    2. Backend reads script as-is (no LLM processing here)
    3. If multiple properties are queued for the same broker, combine into one natural email
    4. Footer is automatically appended by send_and_index_email()
    5. Email is sent and indexed for reply tracking

    Items are retried up to MAX_OUTBOX_ATTEMPTS times, then moved to dead-letter queue.

    Returns a list of Graph operation-states (GO-condition #3): one per item that
    reached a send outcome, so a swallowed per-item Graph send failure now
    escalates the health rail via ``main._combine_graph_operation_states``.
    """
    from .clients import _fs
    from collections import defaultdict

    operation_states: List[Dict[str, Any]] = []

    # Fetch user's email signature settings
    user_doc = _fs.collection("users").document(user_id).get()
    user_signature = None
    signature_mode = None
    user_email = None
    if user_doc.exists:
        user_data = user_doc.to_dict() or {}
        user_signature, signature_mode, user_email = resolve_signature_settings(user_data)
        if signature_mode:
            print(f"📝 Signature mode: {signature_mode}")
        elif user_signature:
            print(f"📝 Using custom email signature for user")

    outbox_ref = _fs.collection("users").document(user_id).collection("outbox")
    # Order by createdAt to send emails in the order they were queued (oldest first)
    docs = list(outbox_ref.order_by("createdAt").stream())

    if not docs:
        print("📭 Outbox empty")
        return operation_states

    print(f"📬 Found {len(docs)} outbox item(s)")

    # Group outbox items by recipient email to detect multi-property scenarios
    email_groups = defaultdict(list)
    for d in docs:
        data = d.to_dict() or {}
        if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
            continue
        emails = data.get("assignedEmails") or []

        if _must_process_outbox_item_individually(data):
            email_lower = emails[0].lower().strip() if emails else f"__no_recipient__:{d.id}"
            email_groups[f"__single__:{d.id}"].append({
                'doc': d,
                'data': data,
                'email': email_lower
            })
            continue

        for email in emails:
            email_lower = email.lower().strip()
            email_groups[email_lower].append({
                'doc': d,
                'data': data,
                'email': email
            })

    # Rail 2: aggregate daily send cap (fail-closed, off-by-default-SAFE).
    # Resolved once per drain; enforced per accepted send below.
    daily_cap = _resolve_daily_send_cap()
    global_cap = _resolve_global_daily_send_cap()
    cap_day_key = _send_counter_day_key()

    # Process each unique recipient
    recipients_list = list(email_groups.items())
    for idx, (recipient_email, items) in enumerate(recipients_list):
        # Filter out items that have exceeded max attempts
        valid_items = []
        for item in items:
            data = item['data']
            attempts = int(data.get("attempts") or 0)
            if attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(
                    user_id, item['doc'].reference, data,
                    f"Exceeded max attempts ({MAX_OUTBOX_ATTEMPTS}): {data.get('lastError', 'unknown error')}"
                )
            else:
                valid_items.append(item)

        if not valid_items:
            continue

        # --- Rail 2: verify we are UNDER the ceiling before sending ---------
        # Re-read the shared counter each iteration so a fleet-wide blast (many
        # workers / processes) is bounded, not just a single drain. If the
        # counter cannot be read we STOP draining and retain the queue — a
        # transient store blip must never open the floodgates (fail-closed).
        send_count = len(valid_items)
        if daily_cap is not None:
            try:
                current = _read_daily_send_count(_fs, user_id, cap_day_key)
            except Exception as exc:  # noqa: BLE001 - fail closed on read error
                print(
                    f"🛑 Daily send-cap counter unavailable for {user_id} — "
                    f"retaining outbox (fail-closed): {exc}"
                )
                _record_send_cap_health(
                    _fs, user_id, status="error",
                    reason=DAILY_CAP_COUNTER_UNAVAILABLE_REASON,
                    cap=daily_cap, count=None, day_key=cap_day_key,
                )
                return operation_states
            if current >= daily_cap:
                print(
                    f"🛑 Daily send cap reached for {user_id} "
                    f"({current}/{daily_cap}) — retaining outbox for next cycle."
                )
                _record_send_cap_health(
                    _fs, user_id, status="warning",
                    reason=DAILY_CAP_REACHED_REASON,
                    cap=daily_cap, count=current, day_key=cap_day_key,
                )
                return operation_states

        if global_cap is not None:
            try:
                current_global = _read_global_send_count(_fs, cap_day_key)
            except Exception as exc:  # noqa: BLE001 - fail closed on read error
                print(
                    "🛑 Global send-cap counter unavailable — "
                    f"retaining outbox (fail-closed): {exc}"
                )
                _record_send_cap_health(
                    _fs, user_id, status="error",
                    reason=DAILY_CAP_COUNTER_UNAVAILABLE_REASON,
                    cap=global_cap, count=None, day_key=cap_day_key, scope="global",
                )
                return operation_states
            if current_global >= global_cap:
                print(
                    "🛑 Global daily send cap reached "
                    f"({current_global}/{global_cap}) — retaining outbox for next cycle."
                )
                _record_send_cap_health(
                    _fs, user_id, status="warning",
                    reason=DAILY_CAP_REACHED_REASON,
                    cap=global_cap, count=current_global, day_key=cap_day_key,
                    scope="global",
                )
                return operation_states

        # Check if multiple properties for same broker
        if len(valid_items) > 1:
            # Per-campaign send mode: 'separate' (default — one email per property)
            # or 'combined' (one email covering ALL of this broker's properties).
            # Absent / any non-'combined' value → separate, so queued items and
            # older frontends stay byte-identical to today's behavior.
            send_mode = (valid_items[0].get('data') or {}).get('sendMode') or 'separate'
            if send_mode == 'combined':
                print(f"🔗 Detected {len(valid_items)} properties for same broker (COMBINED mode): {recipient_email}")
                _send_combined_property_email(
                    user_id,
                    _fresh_graph_headers(headers, headers_provider),
                    recipient_email,
                    valid_items,
                    user_signature,
                    signature_mode,
                    user_email,
                    headers_provider=headers_provider,
                    operation_states=operation_states,
                )
            else:
                print(f"🔗 Detected {len(valid_items)} properties for same broker: {recipient_email}")
                _send_multi_property_email(
                    user_id,
                    _fresh_graph_headers(headers, headers_provider),
                    recipient_email,
                    valid_items,
                    user_signature,
                    signature_mode,
                    user_email,
                    headers_provider=headers_provider,
                    operation_states=operation_states,
                )
        else:
            # Single property - send normally
            item = valid_items[0]
            _send_single_outbox_item(
                user_id,
                _fresh_graph_headers(headers, headers_provider),
                item,
                user_signature,
                signature_mode,
                user_email,
                headers_provider=headers_provider,
                operation_states=operation_states,
            )

        # --- Rail 2: record the sends we just made -------------------------
        # If we cannot persist the increment we can no longer trust the ceiling
        # for subsequent recipients, so we halt the drain (fail-closed).
        if daily_cap is not None or global_cap is not None:
            try:
                if daily_cap is not None:
                    _increment_daily_send_count(_fs, user_id, cap_day_key, send_count)
                if global_cap is not None:
                    _increment_global_send_count(_fs, cap_day_key, send_count)
            except Exception as exc:  # noqa: BLE001 - fail closed on write error
                print(
                    f"🛑 Could not record daily send count for {user_id} — "
                    f"halting further drains (fail-closed): {exc}"
                )
                _record_send_cap_health(
                    _fs, user_id, status="error",
                    reason=DAILY_CAP_COUNTER_UNAVAILABLE_REASON,
                    cap=daily_cap if daily_cap is not None else global_cap,
                    count=None, day_key=cap_day_key,
                )
                return operation_states

        # 2-minute delay between ALL emails to avoid spam detection
        if idx < len(recipients_list) - 1:
            print("  ⏳ Waiting 2 minutes before next recipient to avoid spam detection...")
            time.sleep(120)

    return operation_states


def _send_multi_property_email(
    user_id: str,
    headers: Dict[str, str],
    recipient_email: str,
    items: list,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
    operation_states: Optional[list] = None,
):
    """
    Send SEPARATE emails for multiple properties to the same broker.
    Each property gets its own thread for clean tracking.
    The first email acknowledges there are multiple and explains the organization strategy.
    """
    # Check for opted-out contacts first
    from .processing import is_contact_opted_out
    optout_record = is_contact_opted_out(user_id, recipient_email)
    if optout_record:
        print(f"🚫 Skipping multi-property emails to opted-out contact: {recipient_email}")
        # Delete all outbox items for this recipient
        for item in items:
            _terminalize_outbox_action_audit(
                user_id,
                item['doc'].reference,
                item['data'],
                "opt_out_skipped",
                {
                    "skippedAt": SERVER_TIMESTAMP,
                    "skipReason": optout_record.get("reason", "contact_opted_out"),
                },
            )
            item['doc'].reference.delete()
        print(f"🗑️ Deleted {len(items)} outbox items (recipient opted out)")
        return

    # Extract property info from each item
    properties = []
    for item in items:
        data = item['data']
        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue
        subject = data.get("subject", "")
        # Try to extract property address from subject or script
        property_name = subject or _extract_property_from_script(data.get("script", ""))
        properties.append({
            'item': item,
            'name': property_name,
            'subject': subject,  # Pre-computed subject from property data
            'clientId': data.get("clientId", ""),
            'script': data.get("script", ""),
            'rowNumber': data.get("rowNumber")
        })

    print(f"📬 Sending {len(properties)} separate property emails to {recipient_email}")

    # Send each property as its own email/thread
    # Each email uses the exact script that was approved in the frontend
    recipient_guard_sheet_cache: Dict[str, Any] = {}
    for idx, prop in enumerate(properties):
        item = prop['item']
        data = item['data']

        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue

        # CRITICAL: Claim the item before processing to prevent duplicate sends
        if not _claim_outbox_item(item['doc'].reference, data, user_id=user_id):
            print(f"   ⏭️ Skipping property {idx + 1} - already being processed by another worker")
            continue

        fresh_data = _get_current_outbox_data(item['doc'].reference)
        if fresh_data is None:
            continue
        if fresh_data:
            data = fresh_data

        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue

        clientId = (data.get("clientId") or "").strip()
        attempts = int(data.get("attempts") or 0)
        row_number = data.get("rowNumber") or prop.get('rowNumber')

        if _pause_client_outbox_item_if_needed(user_id, item['doc'].reference, data):
            print(f"   ⏸️ Moved outbox item for paused/stopped client {clientId or 'n/a'} to dead letter")
            continue

        if _dead_letter_campaign_recipient_row_mismatch_if_needed(
            user_id,
            item['doc'].reference,
            data,
            recipient_email,
            row_number_override=row_number,
            sheet_metadata_cache=recipient_guard_sheet_cache,
        ):
            print(f"   🛑 Blocked row-recipient mismatch for {recipient_email}")
            continue

        # DUPLICATE CHECK: Skip if we've already sent to this recipient about this property
        property_address = prop.get('subject') or prop.get('name') or ''
        if _has_existing_thread_for_property(user_id, recipient_email, property_address, client_id=clientId):
            print(f"   🚫 DUPLICATE DETECTED: Already sent to {recipient_email} about '{property_address}'")
            print(f"   🗑️ Deleting duplicate outbox entry")
            _terminalize_outbox_action_audit(
                user_id,
                item['doc'].reference,
                data,
                "duplicate_skipped",
                {"skippedAt": SERVER_TIMESTAMP, "skipReason": "existing_thread_for_property"},
            )
            item['doc'].reference.delete()
            continue

        print(f"  → Property {idx + 1}/{len(properties)}: {prop['name'] or 'Unknown'} (attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

        try:
            contact_name = data.get("contactName") or data.get("firstName")
            contact_name_failure_reason = None
            raw_script = data.get("script", prop["script"])
            if not contact_name and NAME_PLACEHOLDER_RE.search(raw_script or ""):
                name_resolution = _resolve_campaign_launch_contact_name_result_from_sheet(
                    user_id,
                    data,
                    row_number_override=row_number,
                    sheet_metadata_cache=recipient_guard_sheet_cache,
                )
                contact_name = name_resolution.get("contact_name")
                contact_name_failure_reason = name_resolution.get("failure_reason")
            # Personalize only name-style launch placeholders; unsafe leftovers still hard-stop below.
            script = _personalize_name_placeholders(raw_script, contact_name)
            if _dead_letter_unresolved_name_placeholder_if_needed(
                user_id,
                item['doc'].reference,
                data,
                script,
                contact_name_failure_reason,
            ):
                print(f"   🛑 Blocked unresolved contact name for {recipient_email}; manual review required")
                continue
            if _dead_letter_invalid_initial_outreach_column_contract_if_needed(
                user_id,
                item['doc'].reference,
                data,
                script,
            ):
                print(f"   🛑 Blocked column-contract violation for {recipient_email}; manual review required")
                continue
            if _dead_letter_unsafe_outbound_body_if_needed(user_id, item['doc'].reference, data, script):
                print(f"   🛑 Blocked unsafe outbound body for {recipient_email}; manual review required")
                continue

            # Use pre-computed subject from property data
            subject_override = prop.get('subject') or None
            followup_config = data.get("followUpConfig")

            # Fallback: fetch followUpConfig from client if not on outbox item
            if not followup_config and clientId:
                try:
                    from .clients import _fs
                    client_doc = _fs.collection("users").document(user_id).collection("clients").document(clientId).get()
                    if client_doc.exists:
                        client_data = client_doc.to_dict()
                        followup_config = client_data.get("followUpConfig")
                        if followup_config:
                            print(f"   📋 Fetched followUpConfig from client (enabled={followup_config.get('enabled')})")
                except Exception as e:
                    print(f"   ⚠️ Could not fetch followUpConfig from client: {e}")

            current_headers = _fresh_graph_headers(headers, headers_provider)
            prior_send = _sent_retry_reconciliation_result(
                current_headers,
                data,
                recipient_email,
                script,
                subject_override,
            )
            if prior_send.get("sent"):
                _record_outbox_reconciliation(
                    user_id,
                    item['doc'].reference,
                    data,
                    "Prior failed attempt appears already sent in Sent Items; stopped before retry",
                    prior_send,
                    prior_send.get("sent", []),
                    delete_original=True,
                )
                print(f"  ⚠️ Prior send detected for {recipient_email}; moved grouped item to reconciliation without retrying")
                continue
            if prior_send.get("manualContinuation"):
                _move_to_dead_letter(
                    user_id,
                    item['doc'].reference,
                    data,
                    _manual_continuation_retry_reason(prior_send),
                )
                continue
            if prior_send.get("guardLookupError"):
                _move_to_dead_letter(
                    user_id,
                    item['doc'].reference,
                    data,
                    f"Sent Items retry guard could not verify prior send; manual review required before retry: {prior_send['guardLookupError']}",
                )
                continue
            send_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            res = send_and_index_email(user_id, current_headers, script, [recipient_email],
                                       client_id_or_none=clientId, row_number=row_number,
                                       user_signature=user_signature, subject_override=subject_override,
                                       signature_mode=signature_mode, followup_config=followup_config,
                                       contact_name=contact_name, user_email=user_email,
                                       allow_scheduling_language=_is_tour_invite_outbox(data))
            if _handle_suppressed_outbox_send_result(
                user_id, item['doc'].reference, data, res
            ):
                continue
            any_errors = bool([e for e in res.get("errors", {}) if "opted out" not in str(res["errors"].get(e, ""))])

            if not any_errors and res["sent"]:
                _finalize_successful_outbox_item(
                    user_id, item['doc'].reference, data,
                    row_number=row_number, client_id=clientId,
                    send_result=res,
                )
                print(f"  ✅ Sent and deleted outbox item for {prop['name']}")
                _record_operation_state(
                    operation_states,
                    _outbox_send_operation_state(
                        "healthy", doc_id=item['doc'].id, recipient=recipient_email
                    ),
                )
            elif not res.get("sent") and res.get("opted_out") and _all_send_errors_are_opt_out(res.get("errors", {})):
                _terminalize_outbox_action_audit(
                    user_id,
                    item['doc'].reference,
                    data,
                    "opt_out_skipped",
                    {"skippedAt": SERVER_TIMESTAMP, "skipReason": "contact_opted_out"},
                )
                item['doc'].reference.delete()
                print(f"  🚫 Deleted outbox item for opted-out recipient {recipient_email}")
            else:
                new_attempts = attempts + 1
                error_msg = json.dumps(res["errors"])[:1500]
                identity_recipients = _send_identity_recipients(res)

                if identity_recipients:
                    _record_outbox_reconciliation(
                        user_id,
                        item['doc'].reference,
                        data,
                        error_msg,
                        {**res, "sent": identity_recipients},
                        identity_recipients,
                        delete_original=True,
                    )
                    print(f"  ⚠️ Moved grouped item to reconciliation; Graph accepted send but indexing failed")
                    # Graph accepted the send (indexing pending) -> not a send failure.
                    _record_operation_state(
                        operation_states,
                        _outbox_send_operation_state(
                            "healthy", doc_id=item['doc'].id, recipient=recipient_email
                        ),
                    )
                    continue

                if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                    _move_to_dead_letter(user_id, item['doc'].reference, data,
                        f"Send errors after {new_attempts} attempts: {error_msg}")
                else:
                    # Release claim and update attempts so it can be retried
                    item['doc'].reference.set(
                        {
                            "attempts": new_attempts,
                            "lastError": error_msg,
                            "lastSendAttemptAt": send_started_at,
                            "status": "retrying",
                            "processingBy": None,
                            "processingAt": None,
                        },
                        merge=True,
                    )
                    _mark_outbox_action_audit_retrying(
                        user_id,
                        item['doc'].reference,
                        data,
                        new_attempts,
                        error_msg,
                    )
                print(f"  ⚠️ Kept item with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")
                # Swallowed per-item Graph send failure -> surface to the health rail.
                _record_operation_state(
                    operation_states,
                    _outbox_send_operation_state(
                        "error", doc_id=item['doc'].id, recipient=recipient_email, error=error_msg
                    ),
                )

        except Exception as e:
            new_attempts = attempts + 1
            error_msg = str(e)[:1500]

            if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(user_id, item['doc'].reference, data,
                    f"Exception after {new_attempts} attempts: {error_msg}")
            else:
                # Release claim and update attempts so it can be retried
                item['doc'].reference.set(
                    {
                        "attempts": new_attempts,
                        "lastError": error_msg,
                        "lastSendAttemptAt": datetime.now(timezone.utc) - timedelta(seconds=5),
                        "status": "retrying",
                        "processingBy": None,
                        "processingAt": None,
                    },
                    merge=True,
                )
                _mark_outbox_action_audit_retrying(
                    user_id,
                    item['doc'].reference,
                    data,
                    new_attempts,
                    error_msg,
                )
            print(f"  💥 Error: {e}; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")
            _record_operation_state(
                operation_states,
                _outbox_send_operation_state(
                    "error", doc_id=item['doc'].id, recipient=recipient_email, error=error_msg
                ),
            )

        # 2-minute delay between emails to same recipient to avoid spam flags
        if idx < len(properties) - 1:
            print(f"  ⏳ Waiting 2 minutes before sending next email to avoid spam detection...")
            time.sleep(120)


def _send_combined_property_email(
    user_id: str,
    headers: Dict[str, str],
    recipient_email: str,
    items: list,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
    operation_states: Optional[list] = None,
):
    """
    Send ONE combined email covering ALL of a broker's properties.

    Opt-in counterpart to _send_multi_property_email (which sends one email per
    property). Selected when the outbox items carry sendMode == 'combined'.

    The N per-property outbox items collapse into a single Graph send / thread.
    It is ONE atomic send unit: every surviving row is finalized together on
    success, or bumped/dead-lettered together on failure. We never retry a
    subset — one Graph send already covered every property, so a per-item retry
    would double-send.

    The combined body is generated once by the frontend and stamped identically
    on every item, so items[0]['script'] is the whole message; we do NOT
    concatenate here.
    """
    # Opt-out short-circuit (mirror separate mode): drop every queued item.
    from .processing import is_contact_opted_out
    optout_record = is_contact_opted_out(user_id, recipient_email)
    if optout_record:
        print(f"🚫 Skipping combined email to opted-out contact: {recipient_email}")
        for item in items:
            _terminalize_outbox_action_audit(
                user_id,
                item['doc'].reference,
                item['data'],
                "opt_out_skipped",
                {
                    "skippedAt": SERVER_TIMESTAMP,
                    "skipReason": optout_record.get("reason", "contact_opted_out"),
                },
            )
            item['doc'].reference.delete()
        print(f"🗑️ Deleted {len(items)} outbox items (recipient opted out)")
        return

    # Build the claimed working set: skip cancelled items, claim each (so no other
    # worker double-processes a row), drop duplicates. Everything that survives
    # here is part of the single send.
    recipient_guard_sheet_cache: Dict[str, Any] = {}
    claimed = []
    for item in items:
        ref = item['doc'].reference
        data = item['data']

        if _delete_cancelled_outbox_item_if_needed(ref, data, user_id=user_id):
            continue

        if not _claim_outbox_item(ref, data, user_id=user_id):
            print("   ⏭️ Skipping a property - already being processed by another worker")
            continue

        fresh_data = _get_current_outbox_data(ref)
        if fresh_data is None:
            continue
        if fresh_data:
            data = fresh_data

        if _delete_cancelled_outbox_item_if_needed(ref, data, user_id=user_id):
            continue

        clientId = (data.get("clientId") or "").strip()
        row_number = data.get("rowNumber")

        if _pause_client_outbox_item_if_needed(user_id, ref, data):
            print(f"   ⏸️ Moved combined outbox item for paused/stopped client {clientId or 'n/a'} to dead letter")
            continue

        if _dead_letter_campaign_recipient_row_mismatch_if_needed(
            user_id,
            ref,
            data,
            recipient_email,
            row_number_override=row_number,
            sheet_metadata_cache=recipient_guard_sheet_cache,
        ):
            print(f"   🛑 Blocked row-recipient mismatch for {recipient_email}")
            continue

        subject = data.get("subject", "")
        property_address = subject or _extract_property_from_script(data.get("script", ""))

        # Defense in depth: don't re-send about a property that already has a thread.
        if _has_existing_thread_for_property(user_id, recipient_email, property_address, client_id=clientId):
            print(f"   🚫 DUPLICATE DETECTED: Already sent to {recipient_email} about '{property_address}' — dropping from combined set")
            _terminalize_outbox_action_audit(
                user_id,
                ref,
                data,
                "duplicate_skipped",
                {"skippedAt": SERVER_TIMESTAMP, "skipReason": "existing_thread_for_property"},
            )
            ref.delete()
            continue

        claimed.append({
            "item": item,
            "ref": ref,
            "data": data,
            "clientId": clientId,
            "rowNumber": row_number,
            "subject": subject,
            "property_address": property_address,
            "attempts": int(data.get("attempts") or 0),
        })

    if not claimed:
        print(f"   ⚠️ No combinable properties left for {recipient_email} (all cancelled/duplicate/claimed elsewhere)")
        return

    print(f"📬 Sending ONE combined email to {recipient_email} covering {len(claimed)} propert{'y' if len(claimed) == 1 else 'ies'}")

    primary = claimed[0]
    data0 = primary["data"]
    clientId = primary["clientId"]
    primary_row = primary["rowNumber"]
    primary_doc_id = primary["item"]['doc'].id

    def _fail_all(reason_prefix: str, error_msg: str):
        """Atomic failure: bump attempts / dead-letter EVERY claimed item together."""
        for c in claimed:
            new_attempts = c["attempts"] + 1
            if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(
                    user_id, c["ref"], c["data"],
                    f"{reason_prefix} after {new_attempts} attempts: {error_msg}",
                )
            else:
                c["ref"].set(
                    {
                        "attempts": new_attempts,
                        "lastError": error_msg,
                        "lastSendAttemptAt": datetime.now(timezone.utc) - timedelta(seconds=5),
                        "status": "retrying",
                        "processingBy": None,
                        "processingAt": None,
                    },
                    merge=True,
                )
                _mark_outbox_action_audit_retrying(user_id, c["ref"], c["data"], new_attempts, error_msg)
        _record_operation_state(
            operation_states,
            _outbox_send_operation_state("error", doc_id=primary_doc_id, recipient=recipient_email, error=error_msg),
        )

    def _dead_letter_all(reason: str):
        """Terminal (manual-review) failure: dead-letter every claimed item."""
        for c in claimed:
            _move_to_dead_letter(user_id, c["ref"], c["data"], reason)
        _record_operation_state(
            operation_states,
            _outbox_send_operation_state("error", doc_id=primary_doc_id, recipient=recipient_email, error=reason),
        )

    try:
        # Contact name + name-placeholder resolution (once, from the primary row).
        contact_name = data0.get("contactName") or data0.get("firstName")
        contact_name_failure_reason = None
        raw_script = data0.get("script", "")
        if not contact_name and NAME_PLACEHOLDER_RE.search(raw_script or ""):
            name_resolution = _resolve_campaign_launch_contact_name_result_from_sheet(
                user_id,
                data0,
                row_number_override=primary_row,
                sheet_metadata_cache=recipient_guard_sheet_cache,
            )
            contact_name = name_resolution.get("contact_name")
            contact_name_failure_reason = name_resolution.get("failure_reason")

        script = _personalize_name_placeholders(raw_script, contact_name)

        # Shared-body guards → the whole group is terminal (manual review).
        # The guard dead-letters the primary; dead-letter the rest to match.
        if _dead_letter_unresolved_name_placeholder_if_needed(
            user_id, primary["ref"], data0, script, contact_name_failure_reason
        ):
            for c in claimed[1:]:
                _move_to_dead_letter(user_id, c["ref"], c["data"], "Unresolved contact name in combined send; manual review required")
            _record_operation_state(
                operation_states,
                _outbox_send_operation_state("error", doc_id=primary_doc_id, recipient=recipient_email, error="unresolved_contact_name"),
            )
            print(f"   🛑 Blocked unresolved contact name for combined {recipient_email}; manual review required")
            return
        contract_blocked = []
        for c in claimed:
            if _dead_letter_invalid_initial_outreach_column_contract_if_needed(
                user_id,
                c["ref"],
                c["data"],
                script,
            ):
                contract_blocked.append(c)
        if contract_blocked:
            blocked_ids = {id(c) for c in contract_blocked}
            for c in claimed:
                if id(c) not in blocked_ids:
                    _move_to_dead_letter(
                        user_id,
                        c["ref"],
                        c["data"],
                        "Combined initial outreach failed another row's column contract; manual review required before sending",
                    )
            _record_operation_state(
                operation_states,
                _outbox_send_operation_state(
                    "error",
                    doc_id=primary_doc_id,
                    recipient=recipient_email,
                    error="invalid_initial_outreach_column_contract",
                ),
            )
            print(f"   🛑 Blocked combined column-contract violation for {recipient_email}; manual review required")
            return
        if _dead_letter_unsafe_outbound_body_if_needed(user_id, primary["ref"], data0, script):
            for c in claimed[1:]:
                _move_to_dead_letter(user_id, c["ref"], c["data"], "Unsafe outbound body in combined send; manual review required")
            _record_operation_state(
                operation_states,
                _outbox_send_operation_state("error", doc_id=primary_doc_id, recipient=recipient_email, error="unsafe_outbound_body"),
            )
            print(f"   🛑 Blocked unsafe combined body for {recipient_email}; manual review required")
            return

        combined_subject = data0.get("combinedSubject") or primary["subject"] or None

        # Sent-Items retry guard: if a prior attempt of this combined send already
        # went out, reconcile ALL items instead of re-sending (avoids double-send).
        current_headers = _fresh_graph_headers(headers, headers_provider)
        prior_send = _sent_retry_reconciliation_result(
            current_headers,
            data0,
            recipient_email,
            script,
            combined_subject,
        )
        if prior_send.get("sent"):
            for c in claimed:
                _record_outbox_reconciliation(
                    user_id,
                    c["ref"],
                    c["data"],
                    "Prior failed attempt appears already sent in Sent Items; stopped before retry",
                    prior_send,
                    prior_send.get("sent", []),
                    delete_original=True,
                )
            print(f"  ⚠️ Prior combined send detected for {recipient_email}; reconciled {len(claimed)} items without retrying")
            return
        if prior_send.get("manualContinuation"):
            _dead_letter_all(_manual_continuation_retry_reason(prior_send))
            return
        if prior_send.get("guardLookupError"):
            _dead_letter_all(
                f"Sent Items retry guard could not verify prior send; manual review required before retry: {prior_send['guardLookupError']}"
            )
            return

        # followUpConfig (from primary item, client-doc fallback).
        followup_config = data0.get("followUpConfig")
        if not followup_config and clientId:
            try:
                from .clients import _fs
                client_doc = _fs.collection("users").document(user_id).collection("clients").document(clientId).get()
                if client_doc.exists:
                    followup_config = (client_doc.to_dict() or {}).get("followUpConfig")
            except Exception as e:
                print(f"   ⚠️ Could not fetch followUpConfig from client: {e}")

        property_addresses = [c["property_address"] for c in claimed if c["property_address"]]
        rows = [c["rowNumber"] for c in claimed if c["rowNumber"]]
        thread_context = {
            "propertyAddresses": property_addresses,
            "rows": rows,
        }

        send_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        res = send_and_index_email(
            user_id, current_headers, script, [recipient_email],
            client_id_or_none=clientId, row_number=primary_row,
            user_signature=user_signature, subject_override=combined_subject,
            signature_mode=signature_mode, followup_config=followup_config,
            contact_name=contact_name, user_email=user_email,
            thread_context=thread_context,
        )
        if res.get("campaignAutomationSuppressed"):
            for c in claimed:
                _handle_suppressed_outbox_send_result(
                    user_id, c["ref"], c["data"], res
                )
            return
        any_errors = bool([e for e in res.get("errors", {}) if "opted out" not in str(res["errors"].get(e, ""))])

        if not any_errors and res.get("sent"):
            for c in claimed:
                _finalize_successful_outbox_item(
                    user_id, c["ref"], c["data"],
                    row_number=c["rowNumber"], client_id=c["clientId"],
                    send_result=res,
                )
            print(f"  ✅ Sent ONE combined email + finalized {len(claimed)} rows for {recipient_email}")
            _record_operation_state(
                operation_states,
                _outbox_send_operation_state("healthy", doc_id=primary_doc_id, recipient=recipient_email),
            )
        elif not res.get("sent") and res.get("opted_out") and _all_send_errors_are_opt_out(res.get("errors", {})):
            for c in claimed:
                _terminalize_outbox_action_audit(
                    user_id, c["ref"], c["data"], "opt_out_skipped",
                    {"skippedAt": SERVER_TIMESTAMP, "skipReason": "contact_opted_out"},
                )
                c["ref"].delete()
            print(f"  🚫 Deleted {len(claimed)} combined outbox items for opted-out recipient {recipient_email}")
        else:
            error_msg = json.dumps(res.get("errors", {}))[:1500]
            identity_recipients = _send_identity_recipients(res)
            if identity_recipients:
                # Graph accepted the send but indexing failed -> reconcile all,
                # not a send failure (retrying would double-send).
                for c in claimed:
                    _record_outbox_reconciliation(
                        user_id, c["ref"], c["data"], error_msg,
                        {**res, "sent": identity_recipients}, identity_recipients,
                        delete_original=True,
                    )
                print(f"  ⚠️ Combined send accepted by Graph but indexing failed; reconciled {len(claimed)} items")
                _record_operation_state(
                    operation_states,
                    _outbox_send_operation_state("healthy", doc_id=primary_doc_id, recipient=recipient_email),
                )
            else:
                _fail_all("Combined send errors", error_msg)
                print(f"  ⚠️ Combined send failed for {recipient_email}; bumped/retried {len(claimed)} items")

    except Exception as e:
        error_msg = str(e)[:1500]
        _fail_all("Combined send exception", error_msg)
        print(f"  💥 Combined send error for {recipient_email}: {e}")


def _send_single_outbox_item(
    user_id: str,
    headers: Dict[str, str],
    item: dict,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
    operation_states: Optional[list] = None,
):
    """
    Send a single outbox item with smart script selection based on contact history.

    The script selection logic:
    - scripts[0] = 1st contact (primary)
    - scripts[1] = 2nd contact (follow-up)
    - scripts[2] = 3rd contact, etc.

    Uses claim mechanism to prevent duplicate sends when multiple processes run concurrently.
    """
    d = item['doc']
    data = item['data']

    # RAIL 3 (kill switch): gate the outbox driver before claiming or sending.
    # Fail closed — anything but "live" leaves the item queued and untouched
    # (no claim, no delete, no Graph call) so it resumes cleanly once re-enabled.
    outbound_mode = resolve_outbound_mode()
    if outbound_mode != OUTBOUND_MODE_LIVE:
        _kill_switch_suppressed(
            outbound_mode,
            context=f"outbox item {getattr(d, 'id', 'unknown')} (left queued)",
        )
        return

    if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
        return

    # CRITICAL: Claim the item before processing to prevent duplicate sends
    if not _claim_outbox_item(d.reference, data, user_id=user_id):
        print(f"   ⏭️ Skipping {d.id} - already being processed by another worker")
        return

    fresh_data = _get_current_outbox_data(d.reference)
    if fresh_data is None:
        return
    if fresh_data:
        data = fresh_data

    if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
        return

    if _pause_results_outbox_item_if_needed(user_id, d.reference, data):
        print(f"   ⏸️ Paused Results/Tour outbox item {d.id} for non-admin user {user_id}")
        return

    emails = data.get("assignedEmails") or []
    clientId = (data.get("clientId") or "").strip()
    if _pause_client_outbox_item_if_needed(user_id, d.reference, data):
        print(f"   ⏸️ Moved outbox item {d.id} for paused/stopped client {clientId or 'n/a'} to dead letter")
        return

    attempts = int(data.get("attempts") or 0)
    row_number = data.get("rowNumber")
    thread_id = data.get("threadId")
    reply_to_msg_id = data.get("replyToMessageId")

    # If row_number is missing (e.g., user reply from UI), prefer the known thread anchor.
    # Broker email can appear on several rows in a campaign; email fallback is only safe for new outreach.
    if not row_number and thread_id:
        row_number = _get_thread_row_number(user_id, thread_id)
        if row_number:
            print(f"   📍 Resolved row number from thread {thread_id[:20]}...: {row_number}")

    if not row_number and emails and clientId:
        try:
            sheet_id = _get_sheet_id_or_fail(user_id, clientId)
            sheets = _sheets_client()
            tab_title = _get_first_tab_title(sheets, sheet_id)
            sheet_headers = _read_header_row2(sheets, sheet_id, tab_title)
            row_number, _row_values = _find_row_by_email(sheets, sheet_id, tab_title, sheet_headers, emails[0])
            if row_number:
                print(f"   📍 Looked up row number: {row_number} for {emails[0]}")
        except Exception as e:
            print(f"   ⚠️ Could not look up row number: {e}")

    # Get pre-computed subject from outbox data (property-specific)
    subject_override = data.get("subject")

    # Threading support: check if this is a reply to an existing thread
    is_thread_reply = _is_outbox_thread_reply(data)

    # SECURITY: threadId/replyToMessageId/clientId on the outbox doc are
    # client-supplied. Before ANY send (including Graph metadata preflights),
    # re-validate the binding against the server-side thread state. Fail closed
    # to dead-letter so a stale or crafted outbox item can neither reply into
    # a stopped/foreign thread nor silently convert into a new indexed send.
    if is_thread_reply:
        thread_reply_target = _validate_outbox_thread_reply_target(user_id, data)
        if not thread_reply_target.get("ok"):
            reason = thread_reply_target.get("reason") or "thread_reply_validation_failed"
            _move_to_dead_letter(
                user_id,
                d.reference,
                data,
                f"Thread reply failed pre-send validation: {reason}; "
                "manual review required before sending",
            )
            print(f"   🛑 Blocked thread reply outbox item {d.id}: {reason}")
            return
        # Re-resolve the sheet anchor from the confirmed thread, not the
        # unvalidated client payload.
        validated_thread = thread_reply_target.get("thread") or {}
        try:
            validated_row_number = int(validated_thread.get("rowNumber") or 0)
        except (TypeError, ValueError):
            validated_row_number = 0
        if validated_row_number:
            row_number = validated_row_number

    # Get scripts array (new format) or build from legacy fields
    email_scripts = data.get("emailScripts")
    if not email_scripts or len(email_scripts) == 0:
        # Fallback to legacy script/secondaryScript fields
        primary_script = data.get("script") or ""
        secondary_script = data.get("secondaryScript")
        email_scripts = [primary_script]
        if secondary_script:
            email_scripts.append(secondary_script)

    if is_thread_reply:
        print(f"→ Sending outbox item {d.id} as REPLY to thread {thread_id} (attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")
    else:
        print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s) (clientId={clientId or 'n/a'}, {len(email_scripts)} script(s), attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

        # DUPLICATE CHECK: For new outreach (not replies), check if thread already exists
        # Only check if we have a subject (property address)
        if subject_override and len(emails) == 1:
            if _has_existing_thread_for_property(user_id, emails[0], subject_override, client_id=clientId):
                print(f"   🚫 DUPLICATE DETECTED: Already sent to {emails[0]} about '{subject_override}'")
                print(f"   🗑️ Deleting duplicate outbox entry")
                _terminalize_outbox_action_audit(
                    user_id,
                    d.reference,
                    data,
                    "duplicate_skipped",
                    {"skippedAt": SERVER_TIMESTAMP, "skipReason": "existing_thread_for_property"},
                )
                d.reference.delete()
                return

    # Track results for all recipients
    all_sent = []
    all_errors = {}
    send_identity = {
        "sentMessageIds": {},
        "internetMessageIds": {},
        "threadIds": {},
        "conversationIds": {},
    }

    # Get follow-up config if present
    followup_config = data.get("followUpConfig")

    # Fallback: fetch followUpConfig from client if not on outbox item
    if not followup_config and clientId:
        try:
            from .clients import _fs
            client_doc = _fs.collection("users").document(user_id).collection("clients").document(clientId).get()
            if client_doc.exists:
                client_data = client_doc.to_dict()
                followup_config = client_data.get("followUpConfig")
                if followup_config:
                    print(f"   📋 Fetched followUpConfig from client (enabled={followup_config.get('enabled')})")
        except Exception as e:
            print(f"   ⚠️ Could not fetch followUpConfig from client: {e}")

    contact_name = data.get("contactName") or data.get("firstName")
    reply_retry_metadata: Dict[str, Any] = {}
    if is_thread_reply and _should_preflight_sent_items_retry(data) and reply_to_msg_id:
        reply_retry_metadata = _fetch_graph_message_metadata(
            _fresh_graph_headers(headers, headers_provider),
            reply_to_msg_id,
            "https://graph.microsoft.com/v1.0",
        )
    retry_subject = subject_override or reply_retry_metadata.get("subject")
    retry_conversation_id = (
        data.get("conversationId")
        or data.get("sourceConversationId")
        or reply_retry_metadata.get("conversationId")
    )

    # If this is a reply to an existing thread, use _send_outbox_as_reply
    if is_thread_reply:
        # For replies, use the script directly (already personalized by frontend)
        script_content = email_scripts[0] if email_scripts else ""
        if _dead_letter_unsafe_outbound_body_if_needed(user_id, d.reference, data, script_content):
            print(f"   🛑 Blocked unsafe dashboard reply body in outbox item {d.id}; manual review required")
            return
        current_headers = _fresh_graph_headers(headers, headers_provider)
        reply_sender = _get_reply_message_sender(current_headers, reply_to_msg_id)
        use_graph_reply = _assigned_emails_match_reply_sender(emails, reply_sender)

        if use_graph_reply:
            recipient = emails[0] if emails else "unknown"
            current_headers = _fresh_graph_headers(headers, headers_provider)
            prior_send = _sent_retry_reconciliation_result(
                current_headers,
                data,
                recipient,
                script_content,
                retry_subject,
                conversation_id=retry_conversation_id,
            )
            if prior_send.get("sent"):
                _merge_send_identity(send_identity, prior_send)
                all_errors[recipient] = (
                    "Prior failed attempt appears already sent in Sent Items; "
                    "operator reconciliation required"
                )
            elif prior_send.get("manualContinuation"):
                _move_to_dead_letter(
                    user_id,
                    d.reference,
                    data,
                    _manual_continuation_retry_reason(prior_send),
                )
                return
            elif prior_send.get("guardLookupError"):
                _move_to_dead_letter(
                    user_id,
                    d.reference,
                    data,
                    f"Sent Items retry guard could not verify prior send; manual review required before retry: {prior_send['guardLookupError']}",
                )
                return
            else:
                try:
                    res = _send_outbox_as_reply(
                        user_id, current_headers, script_content, reply_to_msg_id,
                        thread_id, user_signature=user_signature,
                        signature_mode=signature_mode, user_email=user_email,
                        fallback_to_emails=emails,
                        fallback_cc_emails=data.get("ccEmails") or data.get("ccRecipients") or [],
                        client_id=clientId,
                    )

                    if _handle_suppressed_outbox_send_result(
                        user_id,
                        d.reference,
                        data,
                        res,
                        previously_sent_recipients=_ordered_unique(
                            all_sent + _send_identity_recipients(send_identity)
                        ),
                    ):
                        return

                    if res.get("sent"):
                        recipient = emails[0] if emails else "unknown"
                        sent_recipients = res.get("sentRecipients") or [recipient]
                        all_sent.extend(sent_recipients)
                        if res.get("sentMessageId"):
                            send_identity["sentMessageIds"][recipient] = res.get("sentMessageId")
                        if res.get("internetMessageId"):
                            send_identity["internetMessageIds"][recipient] = res.get("internetMessageId")
                        if res.get("conversationId"):
                            send_identity["conversationIds"][recipient] = res.get("conversationId")
                        _save_outbox_reply_message(
                            user_id, thread_id, res.get("toRecipients") or emails, subject_override,
                            script_content, user_signature, signature_mode, user_email,
                            cc_emails=res.get("ccRecipients") or [],
                        )
                        if not (res.get("sentMessageId") or res.get("internetMessageId")):
                            all_errors[recipient] = (
                                "Graph accepted reply but Sent Items identity lookup failed; "
                                "operator reconciliation required"
                            )
                    else:
                        all_errors[emails[0] if emails else "unknown"] = res.get("error", "Unknown error")

                except Exception as e:
                    all_errors[emails[0] if emails else "unknown"] = str(e)
                    print(f"💥 Error sending reply: {e}")
        else:
            if emails and reply_sender:
                print(f"   ↪️ Dashboard recipient differs from reply sender ({reply_sender}); sending new indexed message")
            elif emails:
                print("   ↪️ Could not verify Graph reply recipient; sending new indexed message to dashboard recipient")
            else:
                all_errors["_all"] = "Thread reply has no assigned recipient"

            for recipient_email in emails:
                try:
                    current_headers = _fresh_graph_headers(headers, headers_provider)
                    prior_send = _sent_retry_reconciliation_result(
                        current_headers,
                        data,
                        recipient_email,
                        script_content,
                        retry_subject,
                        conversation_id=retry_conversation_id,
                    )
                    if prior_send.get("sent"):
                        _merge_send_identity(send_identity, prior_send)
                        all_errors[recipient_email] = (
                            "Prior failed attempt appears already sent in Sent Items; "
                            "operator reconciliation required"
                        )
                        continue
                    if prior_send.get("manualContinuation"):
                        _move_to_dead_letter(
                            user_id,
                            d.reference,
                            data,
                            _manual_continuation_retry_reason(prior_send),
                        )
                        return
                    if prior_send.get("guardLookupError"):
                        _move_to_dead_letter(
                            user_id,
                            d.reference,
                            data,
                            f"Sent Items retry guard could not verify prior send; manual review required before retry: {prior_send['guardLookupError']}",
                        )
                        return
                    res = send_and_index_email(
                        user_id, current_headers, script_content, [recipient_email],
                        client_id_or_none=clientId, row_number=row_number,
                        user_signature=user_signature, subject_override=subject_override,
                        signature_mode=signature_mode, followup_config=followup_config,
                        contact_name=contact_name, user_email=user_email,
                        thread_context=_thread_context_from_outbox(data),
                        allow_scheduling_language=_is_tour_invite_outbox(data),
                    )
                    if _handle_suppressed_outbox_send_result(
                        user_id,
                        d.reference,
                        data,
                        res,
                        previously_sent_recipients=_ordered_unique(
                            all_sent + _send_identity_recipients(send_identity)
                        ),
                    ):
                        return
                    all_sent.extend(res.get("sent", []))
                    all_errors.update(res.get("errors", {}))
                    _merge_send_identity(send_identity, res)
                except Exception as e:
                    all_errors[recipient_email] = str(e)
                    print(f"💥 Error sending redirected thread reply to {recipient_email}: {e}")
    else:
        # For each recipient, select the appropriate script based on contact history
        use_exact_script = _should_use_exact_outbox_script(data)
        recipient_guard_sheet_cache: Dict[str, Any] = {}
        for recipient_email in emails:
            recipient_contact_name = contact_name
            recipient_contact_name_failure_reason = None
            if _dead_letter_campaign_recipient_row_mismatch_if_needed(
                user_id,
                d.reference,
                data,
                recipient_email,
                row_number_override=row_number,
                sheet_metadata_cache=recipient_guard_sheet_cache,
            ):
                print(f"   🛑 Blocked row-recipient mismatch for outbox item {d.id}")
                return

            if not recipient_contact_name and any(NAME_PLACEHOLDER_RE.search(script or "") for script in email_scripts):
                try:
                    name_resolution = _resolve_campaign_launch_contact_name_result_from_sheet(
                        user_id,
                        data,
                        row_number_override=row_number,
                        sheet_metadata_cache=recipient_guard_sheet_cache,
                    )
                    recipient_contact_name = name_resolution.get("contact_name")
                    recipient_contact_name_failure_reason = name_resolution.get("failure_reason")
                except Exception as e:
                    all_errors[recipient_email] = f"Could not resolve contact name from sheet row: {e}"
                    continue

            if use_exact_script:
                selected_script = _personalize_name_placeholders(
                    email_scripts[0] if email_scripts else "",
                    recipient_contact_name,
                )
                print(f"  → Using exact outbox script for {recipient_email}")
            else:
                selected_script = _select_script_for_recipient(
                    user_id, recipient_email, email_scripts, contact_name=recipient_contact_name
                )

            if _dead_letter_unresolved_name_placeholder_if_needed(
                user_id,
                d.reference,
                data,
                selected_script,
                recipient_contact_name_failure_reason,
            ):
                print(f"   🛑 Blocked unresolved contact name for {recipient_email}; manual review required")
                return

            if _dead_letter_invalid_initial_outreach_column_contract_if_needed(
                user_id,
                d.reference,
                data,
                selected_script,
            ):
                print(f"   🛑 Blocked column-contract violation for {recipient_email}; manual review required")
                return

            if _dead_letter_unsafe_outbound_body_if_needed(user_id, d.reference, data, selected_script):
                print(f"   🛑 Blocked unsafe outbound body for {recipient_email}; manual review required")
                return

            try:
                current_headers = _fresh_graph_headers(headers, headers_provider)
                prior_send = _sent_retry_reconciliation_result(
                    current_headers,
                    data,
                    recipient_email,
                    selected_script,
                    subject_override,
                )
                if prior_send.get("sent"):
                    _merge_send_identity(send_identity, prior_send)
                    all_errors[recipient_email] = (
                        "Prior failed attempt appears already sent in Sent Items; "
                        "operator reconciliation required"
                    )
                    continue
                if prior_send.get("manualContinuation"):
                    _move_to_dead_letter(
                        user_id,
                        d.reference,
                        data,
                        _manual_continuation_retry_reason(prior_send),
                    )
                    return
                if prior_send.get("guardLookupError"):
                    _move_to_dead_letter(
                        user_id,
                        d.reference,
                        data,
                        f"Sent Items retry guard could not verify prior send; manual review required before retry: {prior_send['guardLookupError']}",
                    )
                    return
                res = send_and_index_email(user_id, current_headers, selected_script, [recipient_email],
                                           client_id_or_none=clientId, row_number=row_number,
                                           user_signature=user_signature, subject_override=subject_override,
                                           signature_mode=signature_mode, followup_config=followup_config,
                                           contact_name=recipient_contact_name, user_email=user_email,
                                           thread_context=_thread_context_from_outbox(data),
                                           allow_scheduling_language=_is_tour_invite_outbox(data))

                if _handle_suppressed_outbox_send_result(
                    user_id,
                    d.reference,
                    data,
                    res,
                    previously_sent_recipients=_ordered_unique(
                        all_sent + _send_identity_recipients(send_identity)
                    ),
                ):
                    return

                all_sent.extend(res.get("sent", []))
                all_errors.update(res.get("errors", {}))
                _merge_send_identity(send_identity, res)

            except Exception as e:
                all_errors[recipient_email] = str(e)
                print(f"💥 Error sending to {recipient_email}: {e}")

    # Determine success/failure for the outbox item
    any_errors = bool(all_errors)

    if not any_errors and all_sent:
        final_sent = _ordered_unique([
            email for email in (data.get("sentRecipients") or [])
            if isinstance(email, str)
        ] + all_sent)
        _finalize_successful_outbox_item(
            user_id, d.reference, data,
            row_number=row_number, client_id=clientId,
            send_result={**send_identity, "sent": final_sent},
        )
        print(f"🗑️ Deleted outbox item {d.id}")
        _record_operation_state(
            operation_states, _outbox_send_operation_state("healthy", doc_id=d.id)
        )
    elif not all_sent and _all_send_errors_are_opt_out(all_errors):
        _terminalize_outbox_action_audit(
            user_id,
            d.reference,
            data,
            "opt_out_skipped",
            {"skippedAt": SERVER_TIMESTAMP, "skipReason": "contact_opted_out"},
        )
        d.reference.delete()
        print(f"🚫 Deleted outbox item {d.id}; all recipients opted out")
    else:
        new_attempts = attempts + 1
        error_msg = json.dumps(all_errors)[:1500]
        sent_set = {email for email in all_sent if isinstance(email, str)}
        identity_set = set(_send_identity_recipients(send_identity))
        previous_sent = [
            email for email in data.get("sentRecipients", [])
            if isinstance(email, str)
        ]
        accepted_set = sent_set | identity_set
        sent_recipients = _ordered_unique(
            previous_sent + [email for email in emails if email in accepted_set]
        )
        sent_but_unindexed_recipients = [
            email for email in emails
            if email in identity_set and email not in sent_set
        ]
        remaining_recipients = [email for email in emails if email not in accepted_set]

        if sent_but_unindexed_recipients and remaining_recipients:
            _record_outbox_reconciliation(
                user_id,
                d.reference,
                data,
                error_msg,
                {**send_identity, "sent": sent_but_unindexed_recipients},
                sent_but_unindexed_recipients,
            )

        if accepted_set and not remaining_recipients:
            _record_outbox_reconciliation(
                user_id,
                d.reference,
                data,
                error_msg,
                {**send_identity, "sent": sent_recipients},
                sent_recipients,
                delete_original=True,
            )
            print(f"⚠️ Moved outbox item {d.id} to reconciliation; Graph accepted send but indexing failed")
            # Graph accepted the send (indexing pending) -> not a send failure.
            _record_operation_state(
                operation_states, _outbox_send_operation_state("healthy", doc_id=d.id)
            )
            return

        partial_send_retry = bool(accepted_set and remaining_recipients and remaining_recipients != emails)
        retry_extra = {}
        if partial_send_retry:
            retry_extra = {
                "assignedEmails": remaining_recipients,
                "sentRecipients": sent_recipients,
                "partialSend": True,
            }
        audit_retry_extra = {
            "sentRecipients": sent_recipients or None,
            "remainingRecipients": remaining_recipients if partial_send_retry else None,
            "reconciliationRecipients": sent_but_unindexed_recipients or None,
            "partialSend": True if partial_send_retry else None,
        }

        if new_attempts >= MAX_OUTBOX_ATTEMPTS:
            dead_letter_data = {**data, **retry_extra} if retry_extra else data
            _move_to_dead_letter(user_id, d.reference, dead_letter_data, f"Send errors after {new_attempts} attempts: {error_msg}")
        else:
            # Release claim and update attempts so it can be retried
            d.reference.set(
                {
                    "attempts": new_attempts,
                    "lastError": error_msg,
                    "lastSendAttemptAt": datetime.now(timezone.utc) - timedelta(seconds=5),
                    "status": "retrying",
                    "processingBy": None,
                    "processingAt": None,
                    **retry_extra,
                },
                merge=True,
            )
            _mark_outbox_action_audit_retrying(
                user_id,
                d.reference,
                data,
                new_attempts,
                error_msg,
                extra=audit_retry_extra,
            )
            print(f"⚠️ Kept item {d.id} with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")

        # Swallowed per-item Graph send failure -> surface to the health rail.
        _record_operation_state(
            operation_states, _outbox_send_operation_state("error", doc_id=d.id, error=error_msg)
        )


def _extract_property_from_script(script: str) -> str:
    """Try to extract property address from email script."""
    import re
    # Look for common patterns like "123 Main St" or "at 456 Oak Ave"
    patterns = [
        r'(?:about|for|at|regarding)\s+(\d+[^,.\n]+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Way|Lane|Ln|Ct|Court|Pl|Place)[^\n,]*)',
        r'(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Way|Lane|Ln|Ct|Court|Pl|Place))',
    ]
    for pattern in patterns:
        match = re.search(pattern, script, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""

# Legacy Functions (kept for compatibility)
def send_email(headers, script: str, emails: List[str], client_id: Optional[str] = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")
