import re
import requests
import hashlib
import json
import time
import logging
from contextvars import ContextVar
from dataclasses import dataclass, replace
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from urllib.parse import quote
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter

from .clients import _fs, _get_sheet_id_or_fail, _get_client_config, _sheets_client
from .sheets import format_sheet_columns_autosize_with_exceptions, _get_first_tab_title, _read_header_row2, append_links_to_flyer_link_column, append_links_to_floorplan_column, write_property_image_columns, is_floorplan_filename, _header_index_map, _find_row_by_email, clear_row_highlight, highlight_row, ROW_HIGHLIGHT_BLUE
from .sheet_operations import _find_row_by_anchor, ensure_nonviable_divider, move_row_below_divider, insert_property_row_above_divider, _is_row_below_nonviable, sync_thread_row_numbers_after_move, stop_threads_for_row, complete_threads_for_row
from .messaging import (save_message, save_thread_root, index_message_id, index_conversation_id,
                       dump_thread_from_firestore, has_processed, mark_processed, set_last_scan_iso,
                       lookup_thread_by_message_id, lookup_thread_by_conversation_id,
                       is_event_handled, mark_event_handled, build_event_key,
                       update_thread_status, get_thread_status, THREAD_STATUS)
from .logging import write_message_order_test
from .ai_processing import propose_sheet_updates, apply_proposal_to_sheet, get_row_anchor, check_missing_required_fields, _append_ai_meta
from .file_handling import fetch_and_process_linked_assets, fetch_and_process_pdfs, upload_pdf_to_drive
from .notifications import (
    write_notification,
    add_client_notifications,
    delete_notification_and_decrement_counters,
)
from .notification_payloads import (
    build_new_property_suggested_email,
    build_wrong_contact_suggested_email,
    should_skip_original_reply_for_new_property_referral,
)
from .tour_scheduling import (
    build_tour_unavailable_reply,
    build_schedule_aware_tour_reply,
    evaluate_alternate_tour_time,
    format_tour_date_label,
    looks_like_tour_only_unavailable,
    parse_tour_time_minutes,
    tour_date_from_thread_data,
)
from .outbound_safety import validate_outbound_body
from .utils import (exponential_backoff_request, strip_html_tags, safe_preview,
                   parse_references_header, normalize_message_id, fetch_url_as_text, _sanitize_url,
                   format_email_body_with_footer, strip_email_quotes, strip_outbound_body_signoff,
                   b64url_id)
from .pending_responses import queue_pending_response, record_sent_unindexed_response
from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_sent_conversation_continuation_for_retry,
)
from .app_config import INBOX_SCAN_WINDOW_HOURS
from .column_config import (
    contains_column_field_term,
    find_client_comment_column_index,
    find_notes_comment_column_index,
    get_column_config_error,
    get_required_fields_for_close,
    is_asset_column_name,
    response_requests_nonrequestable_fields,
)
from .property_images import (
    PROPERTY_IMAGE_SOURCE_REASON,
    build_property_image_sheet_updates,
    select_property_image_candidate,
)
from .campaign_safety import (
    campaign_suppression_kind as classify_campaign_suppression,
    get_client_automation_decision,
    stopped_followup_patch,
)

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ReplySendOutcome:
    error: Optional[str] = None
    sent_but_unindexed: bool = False
    outcome: Optional[str] = None
    subject: Optional[str] = None
    conversation_id: Optional[str] = None
    send_attempt_at: Optional[datetime] = None
    campaign_decision: Optional[Any] = None
    campaign_suppression_kind: Optional[str] = None


_REPLY_SEND_OUTCOME = ContextVar("reply_send_outcome", default=ReplySendOutcome())


DEFAULT_AUTOMATIC_INBOX_REPLY_ALLOWLIST = {
    # Emergency launch safety: Baylor test lane only by default.
    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
}

DEFAULT_TOUR_ACTION_ALLOWLIST = {
    # Tour scheduling is still in the Baylor proof lane, not general production.
    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
}


class RetryableProcessingError(Exception):
    """Raised when a message should remain unprocessed so the next scan can retry it."""


def _set_reply_campaign_suppression(decision) -> None:
    kind = classify_campaign_suppression(decision)
    _set_reply_send_outcome(
        error=f"Campaign automation suppressed before Graph send: {decision.reason}",
        outcome=(
            "blocked_campaign_terminal"
            if kind == "terminal"
            else f"suppressed_campaign_{kind}"
        ),
        campaign_decision=decision,
        campaign_suppression_kind=kind,
    )


def _mirror_reply_send_outcome(outcome: ReplySendOutcome) -> None:
    send_reply_in_thread.last_error = outcome.error
    send_reply_in_thread.sent_but_unindexed = outcome.sent_but_unindexed
    send_reply_in_thread.last_outcome = outcome.outcome
    send_reply_in_thread.last_subject = outcome.subject
    send_reply_in_thread.last_conversation_id = outcome.conversation_id
    send_reply_in_thread.last_send_attempt_at = outcome.send_attempt_at
    send_reply_in_thread.last_campaign_decision = outcome.campaign_decision


def _set_reply_send_outcome(**changes) -> ReplySendOutcome:
    outcome = replace(_REPLY_SEND_OUTCOME.get(), **changes)
    _REPLY_SEND_OUTCOME.set(outcome)
    _mirror_reply_send_outcome(outcome)
    return outcome


def _reset_reply_send_outcome() -> ReplySendOutcome:
    outcome = ReplySendOutcome()
    _REPLY_SEND_OUTCOME.set(outcome)
    _mirror_reply_send_outcome(outcome)
    return outcome


def _get_reply_send_outcome() -> ReplySendOutcome:
    return _REPLY_SEND_OUTCOME.get()


def _get_reply_campaign_suppression():
    outcome = _get_reply_send_outcome()
    return outcome.campaign_suppression_kind, outcome.campaign_decision


def _clear_reply_campaign_suppression() -> None:
    _set_reply_send_outcome(
        campaign_suppression_kind=None,
        campaign_decision=None,
    )


def _should_mark_processed_after_error(error: Optional[Exception]) -> bool:
    return error is None


# Manifest entries surfaced by file_handling as extraction failures rather than
# usable results (see fetch_and_process_pdfs / fetch_and_process_linked_assets):
#   - "failed_extraction" + extraction_failed: PDF text extraction AND the
#     OpenAI upload fallback both failed for an attachment.
#   - "failed" + download_failed: a broker-supplied link could not be
#     downloaded (dead link, 403 protected Drive file, ...).
#   - "manual_review_required" + requires_manual_review: a broker file-share
#     link (SharePoint/OneDrive/Box/WeTransfer/Drive folder) that cannot be
#     auto-downloaded and needs an operator.
_EXTRACTION_FAILURE_METHODS = ("failed", "failed_extraction", "manual_review_required")


def _extraction_failure_entries(manifest: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Return the manifest entries that represent surfaced extraction failures."""
    failures: List[Dict[str, Any]] = []
    for entry in manifest or []:
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("extraction_failed")
            or entry.get("download_failed")
            or entry.get("requires_manual_review")
            or (entry.get("method") or "") in _EXTRACTION_FAILURE_METHODS
        ):
            failures.append(entry)
    return failures


def _raise_on_extraction_failures(manifest: Optional[List[Dict[str, Any]]]) -> None:
    """Convert surfaced extraction failures into a retryable processing error.

    SAFETY: an extraction failure that surfaces as *nothing* leaves error=None,
    so the caller's _should_mark_processed_after_error(None) gate marks the
    message processed and the broker's attachment/link payload is silently lost
    with no retry and no operator visibility. Raising RetryableProcessingError
    keeps the message unprocessed (retried by the next scan, then visible in
    processingFailures for manual review after max attempts).
    """
    failures = _extraction_failure_entries(manifest)
    if not failures:
        return
    details = "; ".join(
        f"{entry.get('name') or entry.get('source_url') or 'unknown asset'} "
        f"[{entry.get('method') or 'failed'}]: {entry.get('error') or 'extraction failed'}"
        for entry in failures
    )
    raise RetryableProcessingError(
        f"Broker asset extraction failed for {len(failures)} asset(s); "
        f"leaving message unprocessed for retry/manual review: {details}"
    )


def _sheet_updates_committed_non_asset_evidence(
    apply_result: Optional[Dict[str, Any]],
    column_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether validated broker text was durably applied to the sheet."""
    if not isinstance(apply_result, dict) or not isinstance(apply_result.get("applied"), list):
        return False
    applied_evidence = any(
        isinstance(update, dict)
        and bool((update.get("column") or "").strip())
        and not is_asset_column_name(update.get("column"), column_config)
        for update in apply_result["applied"]
    )
    if applied_evidence:
        return True

    skipped = apply_result.get("skipped")
    if not isinstance(skipped, list):
        return False
    return any(
        isinstance(update, dict)
        and update.get("reason") == "no-change"
        and bool((update.get("column") or "").strip())
        and not is_asset_column_name(update.get("column"), column_config)
        and str(update.get("oldValue") or "").strip() != ""
        and str(update.get("oldValue")) == str(update.get("newValue"))
        for update in skipped
    )


def _without_extraction_failures(
    manifest: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep manifest entries that are not the exact surfaced failure objects."""
    failure_ids = {id(entry) for entry in failures}
    return [entry for entry in manifest if id(entry) not in failure_ids]


def _record_asset_extraction_warning(
    user_id: str,
    client_id: str,
    thread_id: str,
    message_id: str,
    failures: List[Dict[str, Any]],
) -> bool:
    """Persist failed asset provenance when usable message text still commits."""
    if not failures:
        return True
    assets = [
        {
            "name": entry.get("name"),
            "sourceUrl": entry.get("source_url"),
            "sourceType": entry.get("source_type"),
            "method": entry.get("method"),
            "error": entry.get("error"),
        }
        for entry in failures
    ]
    warning_key = hashlib.sha256(
        json.dumps(
            {
                "threadId": thread_id,
                "messageId": message_id,
                "assets": assets,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    try:
        _fs.collection("users").document(user_id).collection("assetWarnings").document(warning_key).set({
            "clientId": client_id,
            "threadId": thread_id,
            "messageId": message_id,
            "status": "degraded_text_processed",
            "retryable": False,
            "assets": assets,
            "createdAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
        return True
    except Exception as exc:
        print(f"⚠️ Could not persist non-blocking asset extraction warning: {exc}")
        fallback_recorded = _record_ai_processing_failure(
            user_id,
            client_id,
            thread_id,
            message_id,
            f"Asset warning persistence failed: {exc}",
            retryable=False,
            recovery_status="asset_warning_persistence_failed",
            record_key_suffix="asset_warning_persistence",
            metadata={"assetWarnings": assets},
        )
        if not fallback_recorded:
            raise RetryableProcessingError(
                "Asset warning and fallback persistence both failed; leaving message "
                "unprocessed for operator visibility"
            )
        return False


def _queue_response_retry_or_reconciliation(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    *,
    source_context: str = "autoResponse",
) -> str:
    """Queue a retry only when Graph did not already accept the reply."""
    send_outcome = _get_reply_send_outcome()
    failure_reason = send_outcome.error or "send_reply_in_thread returned False"
    sent_but_unindexed = (
        send_outcome.sent_but_unindexed
        or send_outcome.outcome == "sent_but_unindexed"
    )
    if (
        send_outcome.campaign_suppression_kind == "terminal"
        or send_outcome.outcome == "blocked_campaign_terminal"
    ):
        print("⏹️ Campaign stopped during auto-reply preparation; no retry was queued")
        return "campaign_stopped"
    if sent_but_unindexed:
        record_sent_unindexed_response(
            user_id,
            thread_id,
            msg_id,
            recipient,
            response_body,
            client_id,
            failure_reason,
            source_context=source_context,
        )
        print("⚠️ Reply may have sent but was not indexed; recorded reconciliation item instead of retrying send")
        return "sent_unindexed"

    queue_pending_response(
        user_id,
        thread_id,
        msg_id,
        recipient,
        response_body,
        client_id,
        error=failure_reason,
        subject=send_outcome.subject,
        conversation_id=send_outcome.conversation_id,
        last_send_attempt_at=send_outcome.send_attempt_at,
    )
    return "queued_retry"


def _handle_auto_response_send_failure(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    *,
    failure_label: str = "automatic response",
) -> bool:
    print(f"❌ Failed to send {failure_label}")
    outcome = _queue_response_retry_or_reconciliation(
        user_id,
        thread_id,
        msg_id,
        recipient,
        response_body,
        client_id,
    )
    return outcome == "sent_unindexed"


def _parse_graph_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


GRAPH_RECOVERY_HINTS = {
    "MailboxNotEnabledForRESTAPI": (
        "Microsoft Graph can authenticate this user, but the mailbox is not available to Graph. "
        "Ask the Microsoft 365/Exchange admin to verify the user has an active Exchange Online "
        "mailbox/license and is not on-premises, inactive, or soft-deleted. Admin consent alone "
        "is not enough until the mailbox is Graph-accessible."
    ),
}


def _graph_operation_error_state(operation: str, error: Exception) -> Dict[str, Any]:
    """Return a dashboard-safe health payload for a failed Graph operation."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    graph_error = {}

    if response is not None:
        try:
            payload = response.json() or {}
            graph_error = payload.get("error") or {}
        except Exception:
            try:
                payload = json.loads(getattr(response, "text", "") or "{}")
                graph_error = payload.get("error") or {}
            except Exception:
                graph_error = {}

    error_code = graph_error.get("code")
    error_message = graph_error.get("message")

    state: Dict[str, Any] = {
        "status": "error",
        "operation": operation,
    }

    if status_code is not None:
        state["httpStatus"] = status_code

    if error_code:
        state["errorCode"] = error_code
        if error_message:
            state["errorMessage"] = error_message
            state["error"] = f"{error_code}: {error_message}"
        else:
            state["error"] = error_code
    else:
        state["error"] = str(error)

    recovery_hint = GRAPH_RECOVERY_HINTS.get(error_code)
    if recovery_hint:
        state["recoveryHint"] = recovery_hint

    return state


def _find_recent_sent_message_for_conversation(
    headers: Dict[str, str],
    base: str,
    conversation_id: str,
    sent_after: datetime,
    *,
    attempts: int = 4,
) -> Optional[Dict[str, Any]]:
    """Find the Graph sent item created by the current reply send."""
    if not conversation_id or not sent_after:
        return None

    sent_after_utc = sent_after.astimezone(timezone.utc)
    sent_after_iso = sent_after_utc.isoformat().replace("+00:00", "Z")
    params = {
        "$orderby": "sentDateTime desc",
        "$top": "25",
        "$select": "id,internetMessageId,conversationId,subject,toRecipients,sentDateTime,body,bodyPreview",
        "$filter": f"sentDateTime ge {sent_after_iso}",
    }

    for attempt in range(attempts):
        try:
            sent_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/mailFolders/SentItems/messages",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
            )
            if sent_resp.status_code != 200:
                print(f"   ⚠️ Failed to fetch sent message: {sent_resp.status_code}")
                return None

            candidates = []
            for msg in sent_resp.json().get("value", []):
                if msg.get("conversationId") != conversation_id:
                    continue
                sent_time = _parse_graph_datetime(msg.get("sentDateTime"))
                if sent_time and sent_time < sent_after_utc:
                    continue
                candidates.append(msg)

            if candidates:
                candidates.sort(
                    key=lambda item: _parse_graph_datetime(item.get("sentDateTime")) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                return candidates[0]
        except Exception as e:
            print(f"   ⚠️ Could not find sent reply for indexing: {e}")

        if attempt < attempts - 1:
            time.sleep(0.75 * (attempt + 1))

    print("   ⚠️ Could not find new sent reply in SentItems to index")
    return None


def _record_ai_processing_failure(
    user_id: str,
    client_id: str,
    thread_id: str,
    message_id: str,
    reason: str,
    *,
    retryable: bool = True,
    recovery_status: Optional[str] = None,
    record_key_suffix: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        doc_id = f"{thread_id}__{message_id or int(time.time())}"
        if record_key_suffix:
            safe_suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", record_key_suffix).strip("_")
            if safe_suffix:
                doc_id = f"{doc_id}__{safe_suffix}"
        payload = {
            "clientId": client_id,
            "threadId": thread_id,
            "messageId": message_id,
            "reason": reason,
            "retryable": retryable,
            "createdAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        }
        if recovery_status:
            payload["recoveryStatus"] = recovery_status
        if isinstance(metadata, dict) and metadata:
            payload["metadata"] = metadata
        _fs.collection("users").document(user_id).collection("processingFailures").document(doc_id).set(
            payload,
            merge=True,
        )
        return True
    except Exception as e:
        print(f"⚠️ Could not record AI processing failure: {e}")
        return False


def _has_processing_failure_record(user_id: str, thread_id: str, message_id: str) -> bool:
    if not thread_id or not message_id:
        return False
    try:
        doc_id = f"{thread_id}__{message_id}"
        doc = _fs.collection("users").document(user_id).collection("processingFailures").document(doc_id).get()
        return bool(getattr(doc, "exists", False))
    except Exception as e:
        print(f"⚠️ Could not check processing failure retry state: {e}")
        return False


def _record_processing_failure_blocked_by_manual_continuation(
    user_id: str,
    client_id: str,
    thread_id: str,
    message_id: str,
    sent_artifact: Dict[str, Any],
):
    try:
        doc_id = f"{thread_id}__{message_id or int(time.time())}"
        guard_unreadable = bool(sent_artifact.get("guardUnreadable"))
        recovery_status = (
            "blocked_manual_retry_guard_unreadable"
            if guard_unreadable
            else "blocked_manual_conversation_continued"
        )
        last_retry_error = (
            "Could not verify whether the user manually continued this conversation "
            f"after the processing failure ({sent_artifact.get('guardError') or 'Sent Items unreadable'}); "
            "leaving visible for manual review before retry."
            if guard_unreadable
            else (
                "Inbox retry skipped because Sent Items shows this conversation was "
                "continued after the failure; leaving visible for manual review to "
                "avoid stale or duplicate handling."
            )
        )
        _fs.collection("users").document(user_id).collection("processingFailures").document(doc_id).set({
            "clientId": client_id,
            "threadId": thread_id,
            "messageId": message_id,
            "retryable": False,
            "recoveryStatus": recovery_status,
            "recoveryArtifactCollection": sent_artifact.get("collection") or "SentItems/manualContinuation",
            "recoverySentMessageId": sent_artifact.get("id") or sent_artifact.get("sentMessageId"),
            "recoverySentInternetMessageId": sent_artifact.get("internetMessageId"),
            "recoveryConversationId": sent_artifact.get("conversationId"),
            "recoverySentDateTime": sent_artifact.get("sentDateTime"),
            "recoveryGuardError": sent_artifact.get("guardError"),
            "lastRetryAt": SERVER_TIMESTAMP,
            "lastRetryError": last_retry_error,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Could not record manual-continuation processing failure block: {e}")


def _client_id_for_processing_failure(user_id: str, thread_id: str) -> str:
    try:
        if not thread_id:
            return "unknown"
        doc = _fs.collection("users").document(user_id).collection("threads").document(thread_id).get()
        if not doc.exists:
            return "unknown"
        return (doc.to_dict() or {}).get("clientId") or "unknown"
    except Exception:
        return "unknown"


def _clear_ai_processing_failure(user_id: str, thread_id: str, message_id: str):
    if not message_id:
        return
    try:
        doc_id = f"{thread_id}__{message_id}"
        _fs.collection("users").document(user_id).collection("processingFailures").document(doc_id).delete()
    except Exception as e:
        print(f"⚠️ Could not clear AI processing failure: {e}")


def _timestamp_to_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_datetime"):
            value = value.to_datetime()
        elif isinstance(value, (int, float)):
            value = datetime.fromtimestamp(float(value), tz=timezone.utc)
        elif isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    except Exception:
        return None
    return None


def _mark_processing_failure_stale_for_manual_review(doc, max_failure_age_hours: float):
    try:
        label = f"{max_failure_age_hours:g}"
        doc.reference.set({
            "retryable": False,
            "recoveryStatus": "stale_manual_review",
            "lastRetryAt": SERVER_TIMESTAMP,
            "lastRetryError": (
                f"Processing failure is older than {label} hours; "
                "leaving visible for manual review before any retry."
            ),
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Could not mark stale processing failure for manual review: {e}")


def _message_identity_candidates(*values: Any) -> set:
    candidates = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        candidates.add(text)
        try:
            normalized = normalize_message_id(text)
            if normalized:
                candidates.add(normalized)
        except Exception:
            pass
    return candidates


def _value_matches_message_candidates(value: Any, candidates: set) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_value_matches_message_candidates(item, candidates) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_value_matches_message_candidates(item, candidates) for item in value)
    return bool(_message_identity_candidates(value) & candidates)


def _source_message_match(data: Dict[str, Any], candidates: set) -> bool:
    if not candidates:
        return False

    direct_keys = (
        "msgId",
        "replyToMessageId",
        "sourceMessageId",
        "sourceGraphMessageId",
        "sourceInternetMessageId",
        "originalMessageId",
        "currentMsgId",
        "detectedInMessageId",
    )
    for key in direct_keys:
        if _value_matches_message_candidates((data or {}).get(key), candidates):
            return True

    for nested_key in ("meta", "tourInvite", "sourceMessage", "source"):
        nested = (data or {}).get(nested_key)
        if isinstance(nested, dict) and _source_message_match(nested, candidates):
            return True

    return False


def _recipient_email_address(recipient: Any) -> str:
    if isinstance(recipient, str):
        return recipient.strip()
    if isinstance(recipient, dict):
        return (
            ((recipient or {}).get("emailAddress") or {}).get("address")
            or ""
        ).strip()
    return ""


def _recipient_email_addresses(recipients: Any) -> List[str]:
    addresses = []
    seen = set()
    for recipient in recipients or []:
        address = _recipient_email_address(recipient)
        normalized = address.lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        addresses.append(address)
    return addresses


def _source_message_envelope(msg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(msg, dict) or not msg:
        return {}

    envelope: Dict[str, Any] = {}
    id_mappings = (
        ("id", "graphMessageId"),
        ("internetMessageId", "internetMessageId"),
        ("conversationId", "conversationId"),
        ("subject", "subject"),
        ("receivedDateTime", "receivedDateTime"),
        ("sentDateTime", "sentDateTime"),
    )
    for source_key, target_key in id_mappings:
        value = msg.get(source_key)
        if value:
            envelope[target_key] = value

    for key in ("from", "sender"):
        recipient = msg.get(key)
        address = _recipient_email_address(recipient)
        if recipient:
            envelope[key] = recipient
        if address:
            envelope[f"{key}Email"] = address

    recipient_list_keys = (
        ("replyTo", "replyToEmails"),
        ("toRecipients", "to"),
        ("ccRecipients", "cc"),
    )
    for source_key, address_key in recipient_list_keys:
        recipients = msg.get(source_key) or []
        addresses = _recipient_email_addresses(recipients)
        if recipients:
            envelope[source_key] = recipients
        if addresses:
            envelope[address_key] = addresses

    return envelope


def _source_message_identity_meta(
    msg_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    msg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {}
    if msg_id:
        payload["sourceMessageId"] = msg_id
        payload["sourceGraphMessageId"] = msg_id
    if internet_message_id:
        payload["sourceInternetMessageId"] = internet_message_id
    envelope = _source_message_envelope(msg)
    if envelope:
        payload["sourceMessage"] = envelope
        if envelope.get("cc"):
            payload["ccEmails"] = envelope["cc"]
    return payload


def _stream_limited(collection_ref, limit: int = 200):
    query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
    return list(query.stream())


def _guard_unreadable_artifact(collection_name: str, error: Exception) -> Dict[str, Any]:
    return {
        "collection": collection_name,
        "id": "unreadable",
        "status": "guard_scan_failed",
        "guardUnreadable": True,
        "guardError": str(error),
    }


PROCESSING_RETRY_SOURCE_MESSAGE_FIELDS = (
    "msgId",
    "replyToMessageId",
    "sourceMessageId",
    "sourceGraphMessageId",
    "sourceInternetMessageId",
    "originalMessageId",
    "currentMsgId",
    "detectedInMessageId",
    "meta.msgId",
    "meta.replyToMessageId",
    "meta.sourceMessageId",
    "meta.sourceGraphMessageId",
    "meta.sourceInternetMessageId",
    "meta.originalMessageId",
    "meta.currentMsgId",
    "meta.detectedInMessageId",
    "tourInvite.msgId",
    "tourInvite.replyToMessageId",
    "tourInvite.sourceMessageId",
    "tourInvite.sourceGraphMessageId",
    "tourInvite.sourceInternetMessageId",
    "source.msgId",
    "source.replyToMessageId",
    "source.sourceMessageId",
    "source.sourceGraphMessageId",
    "source.sourceInternetMessageId",
)


def _query_source_message_artifacts(
    collection_ref,
    candidates: set,
    fields: tuple,
    limit_per_query: int = 10,
    *,
    fail_closed_on_limit: bool = False,
) -> List[Any]:
    docs = []
    seen = set()
    where = getattr(collection_ref, "where", None)
    if not callable(where):
        if fail_closed_on_limit:
            raise RuntimeError(
                "Exact source-message artifact query is unavailable"
            )
        return docs

    for field in fields:
        for candidate in candidates:
            query_limit = limit_per_query + 1 if fail_closed_on_limit else limit_per_query
            query = collection_ref.where(filter=FieldFilter(field, "==", candidate)).limit(query_limit)
            query_docs = list(query.stream())
            if fail_closed_on_limit and len(query_docs) > limit_per_query:
                raise RuntimeError(
                    "Exact source-message artifact query exceeded the safe result limit"
                )
            for doc in query_docs:
                doc_id = getattr(doc, "id", None)
                key = doc_id or id(doc)
                if key in seen:
                    continue
                seen.add(key)
                docs.append(doc)
                if fail_closed_on_limit and len(docs) > limit_per_query:
                    raise RuntimeError(
                        "Exact source-message artifact query exceeded the safe result limit"
                    )
    return docs


def _query_thread_artifacts(collection_ref, thread_id: Optional[str], limit: int = 100) -> List[Any]:
    if not thread_id:
        return []
    where = getattr(collection_ref, "where", None)
    if not callable(where):
        return []
    query = collection_ref.where(filter=FieldFilter("threadId", "==", thread_id)).limit(limit)
    return list(query.stream())


def _candidate_artifact_docs(
    collection_ref,
    candidates: set,
    fields: tuple,
    thread_id: Optional[str],
    *,
    allow_broad_scan: bool = True,
) -> List[Any]:
    if not allow_broad_scan:
        return _query_source_message_artifacts(
            collection_ref,
            candidates,
            fields,
            fail_closed_on_limit=True,
        )

    docs = _query_thread_artifacts(collection_ref, thread_id)
    if not docs and not thread_id:
        docs = _query_source_message_artifacts(collection_ref, candidates, fields)
    seen = {getattr(doc, "id", None) or id(doc) for doc in docs}
    for doc in _stream_limited(collection_ref):
        doc_id = getattr(doc, "id", None)
        key = doc_id or id(doc)
        if key in seen:
            continue
        seen.add(key)
        docs.append(doc)
    return docs


def _find_handled_event_for_message(user_ref, thread_id: str, candidates: set) -> Optional[Dict[str, Any]]:
    if not thread_id:
        return None
    try:
        thread_snapshot = user_ref.collection("threads").document(thread_id).get()
    except Exception as e:
        return _guard_unreadable_artifact(f"threads/{thread_id}", e)

    if getattr(thread_snapshot, "exists", False) is not True:
        return None

    thread_data = thread_snapshot.to_dict() or {}
    handled_events = thread_data.get("handledEvents") or {}
    if not isinstance(handled_events, dict):
        return None

    for event_key, event_data in handled_events.items():
        if isinstance(event_data, dict) and _source_message_match(event_data, candidates):
            return {
                "collection": f"threads/{thread_id}/handledEvents",
                "id": event_key,
                "status": "handled",
            }
    return None


def _artifact_matches_retry_source(
    artifact,
    collection_name: str,
    candidates: set,
    thread_id: Optional[str],
    include_terminal_outbox: bool = False,
) -> Optional[Dict[str, Any]]:
    data = artifact.to_dict() or {}
    if collection_name == "outbox" and not include_terminal_outbox:
        status = str(data.get("status") or "").strip().lower()
        if status in NON_PENDING_OUTBOX_STATUSES:
            return None
    if thread_id and data.get("threadId") and data.get("threadId") != thread_id:
        return None
    if _source_message_match(data, candidates):
        return {
            "collection": collection_name,
            "id": getattr(artifact, "id", None),
            "status": data.get("kind") or data.get("status"),
        }
    return None


def _scan_retry_artifact_collection(
    collection_ref,
    collection_name: str,
    candidates: set,
    thread_id: Optional[str],
    include_terminal_outbox: bool = False,
    *,
    allow_broad_scan: bool = True,
) -> Optional[Dict[str, Any]]:
    try:
        docs = _candidate_artifact_docs(
            collection_ref,
            candidates,
            PROCESSING_RETRY_SOURCE_MESSAGE_FIELDS,
            thread_id,
            allow_broad_scan=allow_broad_scan,
        )
    except Exception as e:
        print(f"⚠️ Could not scan processing retry guard collection {collection_name}: {e}")
        return _guard_unreadable_artifact(collection_name, e)

    for artifact in docs:
        match = _artifact_matches_retry_source(
            artifact,
            collection_name,
            candidates,
            thread_id,
            include_terminal_outbox=include_terminal_outbox,
        )
        if match:
            return match
    return None


def _find_existing_retry_artifact_for_message(
    user_id: str,
    thread_id: str,
    message_id: str,
    client_id: Optional[str] = None,
    additional_message_ids: Optional[List[str]] = None,
    *,
    allow_broad_scan: bool = True,
) -> Optional[Dict[str, Any]]:
    """Find visible work already created for the broker message being replayed.

    If replaying a failed message would duplicate a pending dashboard action,
    pending response, or already-sent reconciliation item, leave the failure
    visible for manual review instead of silently running the side effects again.
    """
    candidates = _message_identity_candidates(message_id, *(additional_message_ids or []))
    if not candidates:
        return None

    try:
        user_ref = _fs.collection("users").document(user_id)
    except Exception as e:
        return _guard_unreadable_artifact("users", e)

    handled_event_artifact = _find_handled_event_for_message(user_ref, thread_id, candidates)
    if handled_event_artifact:
        return handled_event_artifact

    collection_checks = (
        ("outbox", False),
        ("pendingResponses", True),
        ("deadLetterQueue", True),
        ("actionAudit", True),
    )
    for collection_name, include_terminal_outbox in collection_checks:
        artifact = _scan_retry_artifact_collection(
            user_ref.collection(collection_name),
            collection_name,
            candidates,
            thread_id,
            include_terminal_outbox=include_terminal_outbox,
            allow_broad_scan=allow_broad_scan,
        )
        if artifact:
            return artifact

    if client_id:
        try:
            notifications_ref = user_ref.collection("clients").document(client_id).collection("notifications")
        except Exception as e:
            print(f"⚠️ Could not scan client notifications before processing retry: {e}")
            return _guard_unreadable_artifact(f"clients/{client_id}/notifications", e)
        artifact = _scan_retry_artifact_collection(
            notifications_ref,
            f"clients/{client_id}/notifications",
            candidates,
            thread_id,
            include_terminal_outbox=True,
            allow_broad_scan=allow_broad_scan,
        )
        if artifact:
            return artifact

    return None


def _mark_processing_failure_blocked_by_existing_artifact(doc, artifact: Dict[str, Any]):
    try:
        collection = artifact.get("collection") or "unknown"
        artifact_id = artifact.get("id") or "unknown"
        guard_unreadable = bool(artifact.get("guardUnreadable"))
        recovery_status = (
            "blocked_retry_guard_unreadable"
            if guard_unreadable
            else "blocked_existing_outbound_artifact"
        )
        last_retry_error = (
            "Could not verify duplicate-send guard before processing retry "
            f"({collection}: {artifact.get('guardError') or 'unreadable'}); "
            "leaving the failure visible for manual review."
            if guard_unreadable
            else (
                "Processing retry skipped because an existing visible outbound/action "
                f"artifact already references this source message ({collection}/{artifact_id})."
            )
        )
        doc.reference.set({
            "retryable": False,
            "recoveryStatus": recovery_status,
            "recoveryArtifactCollection": collection,
            "recoveryArtifactId": artifact_id,
            "recoveryArtifactStatus": artifact.get("status"),
            "recoveryGuardError": artifact.get("guardError"),
            "lastRetryAt": SERVER_TIMESTAMP,
            "lastRetryError": last_retry_error,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Could not mark processing failure blocked by existing artifact: {e}")


def _find_sent_item_continuing_conversation(
    headers: Dict[str, str],
    conversation_id: Optional[str],
    sent_after: Any,
    *,
    base: str = "https://graph.microsoft.com/v1.0",
) -> Optional[Dict[str, Any]]:
    try:
        return find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=conversation_id,
            sent_after=_timestamp_to_utc(sent_after),
            base=base,
        )
    except SentMailGuardLookupError as e:
        return _guard_unreadable_artifact("SentItems/manualContinuation", e)


def _mark_processing_failure_blocked_by_manual_continuation(doc, sent_artifact: Dict[str, Any]):
    try:
        guard_unreadable = bool(sent_artifact.get("guardUnreadable"))
        recovery_status = (
            "blocked_manual_retry_guard_unreadable"
            if guard_unreadable
            else "blocked_manual_conversation_continued"
        )
        last_retry_error = (
            "Could not verify whether the user manually continued this conversation "
            f"after the processing failure ({sent_artifact.get('guardError') or 'Sent Items unreadable'}); "
            "leaving the failure visible for manual review before retry."
            if guard_unreadable
            else (
                "Processing retry skipped because Sent Items shows this conversation "
                "was continued after the failure; leaving visible for manual review "
                "to avoid stale or duplicate handling."
            )
        )
        doc.reference.set({
            "retryable": False,
            "recoveryStatus": recovery_status,
            "recoveryArtifactCollection": sent_artifact.get("collection") or "SentItems/manualContinuation",
            "recoverySentMessageId": sent_artifact.get("id"),
            "recoverySentInternetMessageId": sent_artifact.get("internetMessageId"),
            "recoveryConversationId": sent_artifact.get("conversationId"),
            "recoverySentDateTime": sent_artifact.get("sentDateTime"),
            "recoveryGuardError": sent_artifact.get("guardError"),
            "lastRetryAt": SERVER_TIMESTAMP,
            "lastRetryError": last_retry_error,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
    except Exception as e:
        print(f"⚠️ Could not mark processing failure blocked by manual continuation: {e}")


def reconcile_stale_processing_failures(user_id: str, limit: int = 100) -> Dict[str, int]:
    """Clear failure markers for messages that are already marked processed.

    This is intentionally conservative: it never retries, sends, or changes
    campaign state. Unprocessed failures stay visible for operator review.
    """
    result = {"checked": 0, "cleared": 0, "retained": 0}
    try:
        failures_ref = _fs.collection("users").document(user_id).collection("processingFailures")
        query = failures_ref.limit(limit) if hasattr(failures_ref, "limit") else failures_ref
        docs = list(query.stream())
    except Exception as e:
        print(f"⚠️ Could not read processing failures for reconciliation: {e}")
        return result

    for doc in docs:
        result["checked"] += 1
        try:
            data = doc.to_dict() or {}
            message_id = data.get("messageId")
            preserve_operator_warning = (
                data.get("recoveryStatus") == "asset_warning_persistence_failed"
            )
            if message_id and has_processed(user_id, message_id) and not preserve_operator_warning:
                doc.reference.delete()
                result["cleared"] += 1
            else:
                result["retained"] += 1
        except Exception as e:
            result["retained"] += 1
            print(f"⚠️ Could not reconcile processing failure {getattr(doc, 'id', 'unknown')}: {e}")

    if result["checked"]:
        print(
            "🧹 Processing failure reconciliation: "
            f"checked={result['checked']}, cleared={result['cleared']}, retained={result['retained']}"
        )
    return result


def _fetch_graph_message_by_id(headers: Dict[str, str], message_id: str) -> Dict[str, Any]:
    encoded_id = quote(str(message_id or ""), safe="")
    response = exponential_backoff_request(
        lambda: requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages/{encoded_id}",
            headers=headers,
            params={
                "$select": (
                    "id,subject,from,sender,replyTo,toRecipients,ccRecipients,"
                    "receivedDateTime,sentDateTime,conversationId,internetMessageId,"
                    "internetMessageHeaders,bodyPreview,hasAttachments"
                )
            },
            timeout=30,
        )
    )
    return response.json() or {}


def retry_processing_failures(
    user_id: str,
    headers: Dict[str, str],
    *,
    limit: int = 10,
    max_attempts: int = 3,
    max_failure_age_hours: Optional[float] = None,
) -> Dict[str, int]:
    """Retry exact stored processing failures outside the inbox scan time window."""
    result = {"checked": 0, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    try:
        failures_ref = _fs.collection("users").document(user_id).collection("processingFailures")
        query = failures_ref.limit(limit) if hasattr(failures_ref, "limit") else failures_ref
        docs = list(query.stream())
    except Exception as e:
        print(f"⚠️ Could not read processing failures for retry: {e}")
        return result

    for doc in docs:
        result["checked"] += 1
        data = doc.to_dict() or {}
        message_id = data.get("messageId")
        thread_id = data.get("threadId")
        client_id = data.get("clientId")
        attempts = int(data.get("processingAttempts") or 0)

        if data.get("recoveryStatus") == "asset_warning_persistence_failed":
            result["skipped"] += 1
            continue

        decision = get_client_automation_decision(user_id, client_id)
        suppression_kind = classify_campaign_suppression(decision)
        if suppression_kind:
            terminal = suppression_kind == "terminal"
            try:
                doc.reference.set({
                    "processingAttempts": attempts,
                    "retryable": False if terminal else bool(data.get("retryable", True)),
                    "recoveryStatus": (
                        "campaign_stopped"
                        if terminal
                        else "campaign_automation_suppressed"
                    ),
                    "automationSuppressedState": decision.state,
                    "automationSuppressedReason": decision.reason,
                    "automationSuppressedAt": SERVER_TIMESTAMP,
                    "updatedAt": SERVER_TIMESTAMP,
                }, merge=True)
            except Exception as update_error:
                print(f"⚠️ Could not preserve processing failure campaign gate: {update_error}")
            result["skipped"] += 1
            continue

        if not data.get("retryable", True) or not message_id or attempts >= max_attempts:
            result["skipped"] += 1
            continue

        if has_processed(user_id, message_id):
            doc.reference.delete()
            result["skipped"] += 1
            continue

        existing_artifact = _find_existing_retry_artifact_for_message(
            user_id,
            thread_id,
            message_id,
            client_id,
        )
        if existing_artifact:
            result["skipped"] += 1
            _mark_processing_failure_blocked_by_existing_artifact(doc, existing_artifact)
            continue

        if max_failure_age_hours and max_failure_age_hours > 0:
            failure_time = _timestamp_to_utc(data.get("createdAt") or data.get("updatedAt"))
            if failure_time and datetime.now(timezone.utc) - failure_time > timedelta(hours=max_failure_age_hours):
                result["skipped"] += 1
                _mark_processing_failure_stale_for_manual_review(doc, max_failure_age_hours)
                continue

        processing_error = None
        msg = None
        try:
            msg = _fetch_graph_message_by_id(headers, message_id)
            if not msg.get("id"):
                raise RetryableProcessingError("Graph message fetch returned no message id")
            expanded_existing_artifact = _find_existing_retry_artifact_for_message(
                user_id,
                thread_id,
                message_id,
                client_id,
                additional_message_ids=[
                    msg.get("id"),
                    msg.get("internetMessageId"),
                    msg.get("conversationId"),
                ],
            )
            if expanded_existing_artifact:
                result["skipped"] += 1
                _mark_processing_failure_blocked_by_existing_artifact(doc, expanded_existing_artifact)
                continue
            manual_continuation = _find_sent_item_continuing_conversation(
                headers,
                msg.get("conversationId"),
                data.get("createdAt") or data.get("updatedAt"),
            )
            if manual_continuation:
                result["skipped"] += 1
                _mark_processing_failure_blocked_by_manual_continuation(doc, manual_continuation)
                continue
            result["retried"] += 1
            process_inbox_message(user_id, headers, msg)
            processed_keys = [
                key
                for key in [message_id, msg.get("id"), msg.get("internetMessageId")]
                if key
            ]
            for processed_key in dict.fromkeys(processed_keys):
                mark_processed(user_id, processed_key)
            doc.reference.delete()
            result["succeeded"] += 1
        except Exception as e:
            processing_error = e
            result["failed"] += 1
            next_attempts = attempts + 1
            still_retryable = not _should_mark_processed_after_error(e) and next_attempts < max_attempts
            try:
                doc.reference.set({
                    "processingAttempts": next_attempts,
                    "retryable": still_retryable,
                    "lastRetryAt": SERVER_TIMESTAMP,
                    "lastRetryError": str(e),
                    "updatedAt": SERVER_TIMESTAMP,
                }, merge=True)
            except Exception as update_error:
                print(f"⚠️ Could not update processing failure retry state: {update_error}")
            if _should_mark_processed_after_error(processing_error):
                mark_processed(user_id, message_id)

    if result["checked"]:
        print(
            "🔁 Processing failure retry: "
            f"checked={result['checked']}, retried={result['retried']}, "
            f"succeeded={result['succeeded']}, failed={result['failed']}, skipped={result['skipped']}"
        )
    return result


def _find_manual_continuation_for_inbox_retry(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    msg: Dict[str, Any],
    processed_key: str,
) -> Optional[Dict[str, Any]]:
    if not _has_processing_failure_record(user_id, thread_id, processed_key):
        return None
    try:
        return find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=msg.get("conversationId"),
            sent_after=_timestamp_to_utc(msg.get("receivedDateTime") or msg.get("sentDateTime")),
        )
    except SentMailGuardLookupError as e:
        return _guard_unreadable_artifact("SentItems/manualContinuation", e)


def _skip_inbox_retry_after_manual_continuation(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    msg: Dict[str, Any],
    processed_key: str,
) -> bool:
    manual_continuation = _find_manual_continuation_for_inbox_retry(
        user_id,
        headers,
        thread_id,
        msg,
        processed_key,
    )
    if not manual_continuation:
        return False

    _record_processing_failure_blocked_by_manual_continuation(
        user_id,
        _client_id_for_processing_failure(user_id, thread_id),
        thread_id,
        processed_key,
        manual_continuation,
    )
    mark_processed(user_id, processed_key)
    return True


PDF_LINK_CHANGE_REASON = "Broker PDF attachment uploaded to Drive."
PDF_LINK_COLUMN_ALIASES = {
    "Flyer / Link": ("flyer / link", "flyer/link", "flyer"),
    "Floorplan": ("floorplan", "floor plan"),
}


def _find_header_column_name(header: List[str], canonical_column: str) -> Optional[str]:
    idx_map = _header_index_map(header or [])
    aliases = PDF_LINK_COLUMN_ALIASES.get(canonical_column, (canonical_column.strip().lower(),))
    for alias in aliases:
        col_idx = idx_map.get(alias)
        if col_idx and (col_idx - 1) < len(header or []):
            return (header[col_idx - 1] or canonical_column).strip() or canonical_column
    return None


def _read_row_cell_by_header(header: List[str], rowvals: List[str], column_name: str) -> str:
    idx_map = _header_index_map(header or [])
    col_idx = idx_map.get((column_name or "").strip().lower())
    if not col_idx:
        return ""
    value_index = col_idx - 1
    if value_index >= len(rowvals or []):
        return ""
    return str((rowvals or [])[value_index] or "").strip()


def _merge_link_lines(existing_value: str, added_links: List[str]) -> str:
    existing_lines = [
        line.strip()
        for line in str(existing_value or "").splitlines()
        if line.strip()
    ]
    seen = set(existing_lines)
    merged = list(existing_lines)
    for raw_link in added_links or []:
        link = str(raw_link or "").strip()
        if not link or link in seen:
            continue
        merged.append(link)
        seen.add(link)
    return "\n".join(merged)


def _build_pdf_link_sheet_change_applied_record(
    header: List[str],
    rowvals: List[str],
    link_updates_by_column: Dict[str, List[str]],
    *,
    row_number: Optional[int] = None,
) -> Dict[str, Any]:
    applied = []
    for canonical_column, added_links in (link_updates_by_column or {}).items():
        column_name = _find_header_column_name(header, canonical_column)
        if not column_name:
            continue
        old_value = _read_row_cell_by_header(header, rowvals or [], column_name)
        new_value = _merge_link_lines(old_value, added_links)
        if not new_value or new_value == old_value:
            continue
        applied.append({
            "column": column_name,
            "oldValue": old_value,
            "newValue": new_value,
            "confidence": 1.0,
            "reason": PDF_LINK_CHANGE_REASON,
        })

    return {
        "applied": applied,
        "skipped": [],
        "rowNumber": row_number,
        "source": "pdf_link_write",
    }


def _build_property_image_sheet_change_applied_record(
    header: List[str],
    rowvals: List[str],
    image_updates_by_column: Dict[str, List[str]],
    *,
    row_number: Optional[int] = None,
) -> Dict[str, Any]:
    applied = []
    for canonical_column, values in (image_updates_by_column or {}).items():
        column_name = _find_header_column_name(header, canonical_column) or canonical_column
        old_value = _read_row_cell_by_header(header, rowvals or [], column_name)
        if old_value:
            continue
        new_value = ""
        for raw_value in values or []:
            value = str(raw_value or "").strip()
            if value:
                new_value = value
                break
        if not new_value:
            continue
        applied.append({
            "column": column_name,
            "oldValue": old_value,
            "newValue": new_value,
            "confidence": 1.0,
            "reason": PROPERTY_IMAGE_SOURCE_REASON,
        })

    return {
        "applied": applied,
        "skipped": [],
        "rowNumber": row_number,
        "source": "property_image_write",
    }


def _store_pdf_link_sheet_change(
    user_id: str,
    client_id: str,
    sheet_id: str,
    header: List[str],
    rownum: int,
    rowvals: List[str],
    thread_id: str,
    email: str,
    pdf_manifest: List[Dict[str, Any]],
    link_updates_by_column: Dict[str, List[str]],
) -> Optional[str]:
    apply_result = _build_pdf_link_sheet_change_applied_record(
        header,
        rowvals,
        link_updates_by_column,
        row_number=rownum,
    )
    if not apply_result.get("applied"):
        return None

    try:
        applied_hash = hashlib.sha256(
            json.dumps(apply_result, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        now_id = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-").replace("+00:00", "Z")
        file_ids = [
            p.get("file_id") or p.get("id")
            for p in (pdf_manifest or [])
            if p.get("file_id") or p.get("id")
        ]
        doc_id = f"{thread_id}__pdf_links__{now_id}"
        _fs.collection("users").document(user_id).collection("sheetChangeLog").document(doc_id).set({
            "clientId": client_id,
            "email": email,
            "sheetId": sheet_id,
            "rowNumber": rownum,
            "targetAnchor": get_row_anchor(rowvals, header),
            "applied": apply_result,
            "status": "applied",
            "source": "pdf_link_write",
            "threadId": thread_id,
            "createdAt": SERVER_TIMESTAMP,
            "fileIds": file_ids,
            "proposalHash": applied_hash,
        })
        print(f"💾 Stored PDF link sheetChangeLog/{doc_id}")
        return doc_id
    except Exception as e:
        print(f"⚠️ Failed to store PDF link sheetChangeLog record: {e}")
        return None


def _store_property_image_sheet_change(
    user_id: str,
    client_id: str,
    sheet_id: str,
    header: List[str],
    rownum: int,
    rowvals: List[str],
    thread_id: str,
    email: str,
    image_candidate: Optional[Dict[str, Any]],
    image_updates_by_column: Dict[str, List[str]],
) -> Optional[str]:
    apply_result = _build_property_image_sheet_change_applied_record(
        header,
        rowvals,
        image_updates_by_column,
        row_number=rownum,
    )
    if not apply_result.get("applied"):
        return None

    try:
        safe_candidate = {
            key: (image_candidate or {}).get(key)
            for key in ("url", "sourceLabel", "sourceType", "sourceFilename", "sourceDriveLink", "meta")
            if (image_candidate or {}).get(key) is not None
        }
        applied_hash = hashlib.sha256(
            json.dumps({"applyResult": apply_result, "candidate": safe_candidate}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        now_id = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-").replace("+00:00", "Z")
        doc_id = f"{thread_id}__property_image__{now_id}"
        _fs.collection("users").document(user_id).collection("sheetChangeLog").document(doc_id).set({
            "clientId": client_id,
            "email": email,
            "sheetId": sheet_id,
            "rowNumber": rownum,
            "targetAnchor": get_row_anchor(rowvals, header),
            "applied": apply_result,
            "status": "applied",
            "source": "property_image_write",
            "threadId": thread_id,
            "createdAt": SERVER_TIMESTAMP,
            "propertyImage": safe_candidate,
            "proposalHash": applied_hash,
        })
        print(f"💾 Stored property image sheetChangeLog/{doc_id}")
        return doc_id
    except Exception as e:
        print(f"⚠️ Failed to store property image sheetChangeLog record: {e}")
        return None


def _clear_thread_action_notifications(
    user_id: str,
    client_id: str,
    thread_id: str,
    *,
    notifications_ref=None,
) -> int:
    if not client_id or not thread_id:
        return 0

    try:
        if notifications_ref is None:
            notifications_ref = (
                _fs.collection("users").document(user_id)
                .collection("clients").document(client_id)
                .collection("notifications")
            )

        query = (
            notifications_ref
            .where(filter=FieldFilter("threadId", "==", thread_id))
            .where(filter=FieldFilter("kind", "==", "action_needed"))
        )
        deleted = 0
        for doc in query.stream():
            notification_id = getattr(doc, "id", None)
            if notification_id:
                delete_notification_and_decrement_counters(user_id, client_id, notification_id)
            else:
                doc.reference.delete()
            deleted += 1
        if deleted:
            print(f"🧹 Cleared {deleted} stale action notification(s) for completed thread")
        return deleted
    except Exception as e:
        print(f"⚠️ Could not clear stale action notifications for completed thread: {e}")
        return 0


def _resume_paused_thread_after_manual_continuation(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    thread_data: Dict[str, Any],
    msg: Dict[str, Any],
) -> bool:
    """Handle an operator's out-of-band manual reply on a paused/escalated thread.

    When the operator replies to an escalated thread directly from Outlook (a
    Sent-Items continuation) instead of using the dashboard, the escalation's
    open ``action_needed`` notification and the ``paused`` thread status become
    stale — the thread would otherwise stay paused forever. On the next scan we
    detect the operator's manual continuation (a Sent-Items message in the same
    conversation sent after the thread was paused) and, when found:

    (a) clear the stale open ``action_needed`` notification for the thread, and
    (b) resume (unpause) the thread so processing continues normally.

    Returns True when the thread was resumed. Conservative on failure: if the
    Sent Items guard is unreadable we leave the escalation visible.
    """
    if (thread_data or {}).get("status") != THREAD_STATUS["paused"]:
        return False

    conversation_id = msg.get("conversationId")
    if not conversation_id:
        return False

    # Anchor on when the thread was paused/escalated — the operator's manual
    # continuation would have been sent after that point.
    paused_after = _timestamp_to_utc(
        (thread_data or {}).get("statusUpdatedAt")
        or (thread_data or {}).get("updatedAt")
    )
    if not paused_after:
        return False

    try:
        manual_continuation = find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=conversation_id,
            sent_after=paused_after,
        )
    except SentMailGuardLookupError as e:
        # Sent Items unreadable: stay conservative and leave the escalation visible.
        print(
            f"⚠️ Could not verify operator manual continuation for paused thread "
            f"{thread_id[:20]}...: {e}"
        )
        return False

    if not manual_continuation:
        return False

    client_id = (thread_data or {}).get("clientId")
    _clear_thread_action_notifications(user_id, client_id, thread_id)
    update_thread_status(
        user_id,
        thread_id,
        THREAD_STATUS["active"],
        "manual_continuation_resumed",
    )
    print(
        f"▶️ Resumed paused thread {thread_id[:20]}... after operator manually "
        "continued the conversation out-of-band; cleared stale action notification"
    )
    return True


TERMINAL_THREAD_STATUSES = {THREAD_STATUS["completed"], THREAD_STATUS["stopped"]}
NON_PENDING_OUTBOX_STATUSES = {
    "cancel_requested",
    "cancelled",
    "canceled",
    "sent",
    "duplicate_skipped",
    "opt_out_skipped",
    "dead_lettered",
}


def _maybe_mark_client_completed(
    user_id: str,
    client_id: str,
    *,
    client_ref=None,
    threads_ref=None,
    notifications_ref=None,
    outbox_ref=None,
) -> bool:
    """Mark a campaign completed once every thread is terminal and no current work remains."""
    if not client_id:
        return False

    try:
        user_ref = _fs.collection("users").document(user_id)
        if client_ref is None:
            client_ref = user_ref.collection("clients").document(client_id)
        if threads_ref is None:
            threads_ref = user_ref.collection("threads")
        if notifications_ref is None:
            notifications_ref = client_ref.collection("notifications")
        if outbox_ref is None:
            outbox_ref = user_ref.collection("outbox")

        client_snapshot = client_ref.get()
        client_data = client_snapshot.to_dict() if getattr(client_snapshot, "exists", False) else {}
        status = str((client_data or {}).get("status") or "").strip().lower()
        if status in {"archived", "deleted"}:
            return False

        thread_docs = list(
            threads_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        )
        if not thread_docs:
            return False

        active_threads = []
        terminal_threads = []
        for doc in thread_docs:
            data = doc.to_dict() or {}
            thread_status = str(data.get("status") or THREAD_STATUS["active"]).strip().lower()
            if thread_status in TERMINAL_THREAD_STATUSES:
                terminal_threads.append(doc)
            else:
                active_threads.append(doc)

        action_docs = list(
            notifications_ref
            .where(filter=FieldFilter("kind", "==", "action_needed"))
            .stream()
        )
        outbox_docs = []
        for doc in (
            outbox_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        ):
            data = doc.to_dict() or {}
            outbox_status = str(data.get("status") or "").strip().lower()
            if outbox_status not in NON_PENDING_OUTBOX_STATUSES:
                outbox_docs.append(doc)

        if active_threads or action_docs or outbox_docs:
            return False

        client_ref.set({
            "status": "completed",
            "completedAt": SERVER_TIMESTAMP,
            "statusUpdatedAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
            "completionSummary": {
                "terminalThreads": len(terminal_threads),
                "activeThreads": len(active_threads),
                "pendingOutbox": len(outbox_docs),
                "currentActions": len(action_docs),
            },
        }, merge=True)
        print(f"✅ Marked client {client_id} completed after {len(terminal_threads)} terminal threads")
        return True
    except Exception as e:
        print(f"⚠️ Could not evaluate client completion for {client_id}: {e}")
        return False


TERMINAL_CLOSE_REASONS_WITHOUT_COMPLETE_FIELDS = {
    "exclusive_with_another",
    "deal_pending",
    "not_a_fit",
    "natural_end",
}

PROPERTY_UNAVAILABLE_KEYWORDS = [
    "no longer available", "not available", "off the market",
    "has been leased", "space is leased", "property is unavailable",
    "building unavailable", "no longer considering", "isnt available",
    "isn't available", "unavailable", "off market",
    "under contract", "went under contract", "already leased",
    "just leased", "pending lease", "contract pending",
    "accepted an offer", "lease signed", "taken off market",
    "fully leased",
    "not a good fit", "wouldn't be a good fit", "wouldn’t be a good fit",
    "not the right fit", "does not meet the client's requirements",
    "doesn't meet the client's requirements", "requirements mismatch",
    "more office heavy", "mostly office", "office-heavy",
    "not a true warehouse", "lacks warehouse space", "lacks industrial warehouse",
    "no drive in space", "no drive-in space", "does not have drive-in access",
]


def _normalize_replacement_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _property_unavailable_event_applies_to_row(
    event: Dict[str, Any],
    *,
    row_anchor: str = "",
    message_text: str = "",
    unavailable_keywords: Optional[List[str]] = None,
) -> bool:
    """
    Guard row-moving against stale unavailable context in replacement-property threads.

    Brokers often say "A is leased, try B instead" and later send specs for B in
    the same thread. If the model repeats the old unavailable event while the row
    is anchored to B, do not move B below the NON-VIABLE divider.
    """
    if (event or {}).get("type") != "property_unavailable":
        return True

    if looks_like_tour_only_unavailable(message_text):
        return False

    row_norm = _normalize_replacement_match_text(row_anchor)
    message_norm = _normalize_replacement_match_text(message_text)
    keywords = [
        _normalize_replacement_match_text(keyword)
        for keyword in (unavailable_keywords or PROPERTY_UNAVAILABLE_KEYWORDS)
        if keyword
    ]

    event_property = _format_event_property(event)
    if event_property:
        event_norm = _normalize_replacement_match_text(event_property)
        row_primary = row_norm.split(",", 1)[0].strip()
        event_primary = event_norm.split(",", 1)[0].strip()
        if not row_norm or not event_norm:
            return True
        return bool(
            event_primary
            and (
                event_primary in row_norm
                or row_primary in event_norm
            )
        )

    if not row_norm or not message_norm:
        return True

    row_candidates = [
        candidate
        for candidate in {
            row_norm,
            row_norm.split(",", 1)[0].strip(),
        }
        if len(candidate) >= 6
    ]
    row_positions = [
        idx
        for candidate in row_candidates
        for idx in [message_norm.find(candidate)]
        if idx >= 0
    ]
    if not row_positions:
        return True

    keyword_positions = [
        idx
        for keyword in keywords
        for idx in [message_norm.find(keyword)]
        if idx >= 0
    ]
    if not keyword_positions:
        return True

    row_number_match = re.search(r"\b\d{2,6}\b", row_norm)
    row_number = row_number_match.group(0) if row_number_match else None

    for row_pos in row_positions:
        for keyword_pos in keyword_positions:
            if 0 <= keyword_pos - row_pos <= 180:
                return True
            if 0 <= row_pos - keyword_pos <= 80:
                previous_window = message_norm[max(0, keyword_pos - 120):keyword_pos]
                previous_numbers = set(re.findall(r"\b\d{2,6}\b", previous_window))
                if previous_numbers and (not row_number or previous_numbers != {row_number}):
                    continue
                return True

    return False


def _active_replacement_context(thread_data: Optional[Dict[str, Any]], message_text: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(thread_data, dict):
        return None

    replacement = (
        thread_data.get("activeReplacementProperty")
        or thread_data.get("replacementProperty")
        or thread_data.get("activeReplacement")
    )
    if not isinstance(replacement, dict):
        return None

    raw_address = (
        replacement.get("address")
        or replacement.get("propertyAddress")
        or replacement.get("rowAnchor")
        or ""
    )
    address = str(raw_address or "").strip()
    if not address:
        return None

    row_number = replacement.get("rowNumber")
    try:
        row_number = int(row_number)
    except (TypeError, ValueError):
        return None

    normalized_message = _normalize_replacement_match_text(message_text)
    normalized_address = _normalize_replacement_match_text(address)
    if normalized_message and normalized_address not in normalized_message:
        return None

    return {
        **replacement,
        "address": address,
        "city": str(replacement.get("city") or "").strip(),
        "rowNumber": row_number,
    }


def _should_skip_processing_for_terminal_thread(
    thread_status: Optional[str],
    thread_data: Optional[Dict[str, Any]] = None,
    message_text: str = "",
) -> bool:
    if thread_status == THREAD_STATUS["completed"]:
        return True
    if thread_status == THREAD_STATUS["stopped"]:
        return _active_replacement_context(thread_data, message_text) is None
    return False


def _extract_tour_time_options(question: str) -> List[str]:
    text = str(question or "").strip()
    if not text or text.lower() == "tour requested":
        return []

    parenthetical_options = [
        match.group(1).strip()
        for match in re.finditer(r"\(([^)]*)\)", text)
        if re.search(r"\b(?:offered|available|any time|am|pm|\d{1,2}:\d{2})\b", match.group(1), flags=re.IGNORECASE)
    ]
    if parenthetical_options:
        text = parenthetical_options[-1]

    text = re.sub(r"^tour availability offered\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[A-Z][A-Za-z' -]+\s+offered\s+(?:tour\s+times?\s*:\s*)?", "", text, flags=re.IGNORECASE)
    text = text.strip(" .")
    if not text:
        return []

    has_time_signal = re.search(
        r"\b(mon|tue|wed|thu|fri|sat|sun|morning|afternoon|noon|am|pm|\d{1,2}:\d{2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not has_time_signal:
        return []

    parts = []
    for part in re.split(r"\s+(?:or|/)\s+|;\s*", text):
        if not part.strip(" ."):
            continue
        cleaned = re.sub(r"\s+instead\b", "", part.strip(" .,)"), flags=re.IGNORECASE).strip(" .")
        cleaned = _strip_tour_duration_note(cleaned)
        if cleaned:
            parts.append(cleaned)
    return [part for part in parts[:3] if part] if parts else [text]


def _strip_tour_duration_note(text: str = "") -> str:
    cleaned = re.sub(
        r"\s*\(?\b(?:about|approximately|approx\.?)?\s*\d+\s*(?:minutes?|mins?|hours?|hrs?)"
        r"\s+(?:on[-\s]?site|onsite|for\s+(?:the\s+)?tour)\b\.?\)?\s*",
        " ",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" .,)(")


def _extract_tour_duration_sentence(text: str = "") -> str:
    match = re.search(
        r"\(?\b((?:about|approximately|approx\.?)?\s*\d+\s*(?:minutes?|mins?|hours?|hrs?)"
        r"\s+(?:on[-\s]?site|onsite|for\s+(?:the\s+)?tour))\b\.?\)?",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return ""

    phrase = re.sub(r"\s+", " ", match.group(1)).strip(" .")
    phrase = re.sub(r"\bmins?\b", "minutes", phrase, flags=re.IGNORECASE)
    phrase = re.sub(r"\bhrs?\b", "hours", phrase, flags=re.IGNORECASE)
    phrase = re.sub(r"\bon[-\s]?site\b", "on site", phrase, flags=re.IGNORECASE)
    phrase = re.sub(r"\bonsite\b", "on site", phrase, flags=re.IGNORECASE)
    return f"Please plan for {phrase}."


def _safe_tour_greeting_name(contact_name: str = "", recipient_email: str = "") -> str:
    candidate = str(contact_name or "").strip()
    recipient_local = str(recipient_email or "").split("@", 1)[0].strip().lower()
    compact_candidate = re.sub(r"[^a-z0-9]", "", candidate.lower())
    compact_local = re.sub(r"[^a-z0-9]", "", recipient_local)
    if not candidate or "@" in candidate or (compact_local and compact_candidate == compact_local):
        return "there"
    return candidate


def _build_tour_fallback_suggested_email(contact_name: str = "", recipient_email: str = "", question: str = "") -> str:
    return _build_default_tour_suggested_email(
        _safe_tour_greeting_name(contact_name, recipient_email),
        question,
    )


def _build_default_tour_suggested_email(broker_name: str, question: str = "") -> str:
    greeting_name = (broker_name or "there").strip()
    time_options = _extract_tour_time_options(question)
    duration_sentence = _extract_tour_duration_sentence(question)

    if time_options:
        primary = time_options[0]
        alternate = time_options[1] if len(time_options) > 1 else None
        timing_sentence = f"{primary} would work on my end."
        if alternate:
            timing_sentence += f" If that time is no longer available, {alternate} could also work."
        if duration_sentence:
            timing_sentence += f"\n\n{duration_sentence}"
        follow_up = "Could you please confirm what works best?"
    else:
        timing_sentence = "Could you let me know what tour windows are available?"
        follow_up = "Once I have a few options, I can confirm the best fit."

    return f"""Hi {greeting_name},

Thank you for offering to show me the property. I'd like to schedule a tour.

{timing_sentence}

{follow_up}

Thanks!"""


def _is_tour_invite_thread(thread_data: Optional[Dict[str, Any]] = None) -> bool:
    if not isinstance(thread_data, dict):
        return False
    source = str(thread_data.get("source") or "").strip().lower()
    action_type = str(thread_data.get("actionType") or "").strip().lower()
    return bool(
        source == "dashboard_tour_planner"
        or action_type == "tour_invite"
        or isinstance(thread_data.get("tourInvite"), dict)
    )


def _extract_tour_reply_time_mentions(text: str) -> List[str]:
    seen = set()
    times = []
    for match in re.finditer(
        r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|morning|afternoon)\b",
        str(text or ""),
        flags=re.IGNORECASE,
    ):
        value = re.sub(r"\s+", " ", match.group(0).strip()).upper()
        normalized = value.replace("AM", "AM").replace("PM", "PM")
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        times.append(normalized)
    return times[:4]


def _tour_time_minutes(value: str = "") -> Optional[int]:
    return parse_tour_time_minutes(value)


def _filter_requested_tour_times(
    times: List[str],
    thread_data: Optional[Dict[str, Any]] = None,
) -> List[str]:
    invite = (thread_data or {}).get("tourInvite") or {}
    requested = {
        minutes
        for minutes in (
            _tour_time_minutes(invite.get("arrivalTime")),
            _tour_time_minutes(invite.get("departureTime")),
        )
        if minutes is not None
    }
    if not requested:
        return times
    return [
        time_value
        for time_value in times
        if _tour_time_minutes(time_value) not in requested
    ]


# A single clock token (e.g. "10 AM", "10:00 AM", "2pm", "noon"). Used to pull the
# specific time out of a reject / propose construction so we can tell the REJECTED
# slot apart from the PROPOSED alternate.
_TOUR_CLOCK_TOKEN = r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon"

# Constructions where the captured time is the one the broker is REJECTING.
# Broadened / typo-tolerant on purpose (fail closed: better to treat a slot as
# rejected than to auto-confirm a time the broker just refused).
_REJECTED_TOUR_TIME_PATTERNS = [
    # "10 AM does not/doesn't/won't/will not/no longer work(s)" (time BEFORE the negation)
    re.compile(
        rf"({_TOUR_CLOCK_TOKEN})(?:\s+\w+){{0,4}}?\s+"
        r"(?:does\s+not|does\s*n[’']?t|do\s*n[’']?t|will\s+not|wo\s*n[’']?t|no\s+longer)"
        r"\s+works?\b",
        re.IGNORECASE,
    ),
    # "can't/cannot do 10 AM" (time AFTER the negation)
    re.compile(rf"\b(?:can[’']?t|cannot|can\s+not)\s+do\s+({_TOUR_CLOCK_TOKEN})", re.IGNORECASE),
    # "not available at 10 AM" / "unavailable at 10 AM"
    re.compile(rf"\b(?:not\s+available|unavailable)\s+(?:at\s+)?({_TOUR_CLOCK_TOKEN})", re.IGNORECASE),
    # "2 PM instead of 10 AM" -> 10 AM is the rejected one
    re.compile(rf"\binstead\s+of\s+({_TOUR_CLOCK_TOKEN})", re.IGNORECASE),
    # "2 PM works better than the 10 AM" -> 10 AM is the rejected one
    re.compile(rf"\bthan\s+(?:the\s+)?({_TOUR_CLOCK_TOKEN})", re.IGNORECASE),
]

# Constructions where the captured time is the PROPOSED alternate (the offer).
_PROPOSED_TOUR_TIME_PATTERNS = [
    # "2 PM instead" (but NOT "instead of 10 AM", which rejects a slot)
    re.compile(rf"({_TOUR_CLOCK_TOKEN})\s+instead\b(?!\s+of)", re.IGNORECASE),
    # "can you do 2 PM" / "how about 2 PM" / "let's do 11 AM"
    re.compile(rf"\b(?:do|about)\s+({_TOUR_CLOCK_TOKEN})", re.IGNORECASE),
]


def _tour_time_minutes_from_patterns(patterns: List[Any], text: str) -> set:
    found = set()
    for pattern in patterns:
        for match in pattern.finditer(str(text or "")):
            minutes = _tour_time_minutes(match.group(1))
            if minutes is not None:
                found.add(minutes)
    return found


def _reorder_alternate_tour_times(
    times: List[str],
    text: str = "",
    thread_data: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return extracted tour times with the PROPOSED alternate first and any
    explicitly-REJECTED time dropped.

    The raw extractor returns times in appearance order, so ``times[0]`` can be the
    slot the broker just rejected ("10 AM does not work, do 2 PM instead"). The
    schedule pipeline evaluates/confirms ``alternateTimes[0]``, so we must never let
    a rejected time land there. We drop the stored invite time (the broker is
    replacing it) plus any time tied to a rejection construction, and float the
    proposed offer to the front. An explicitly-proposed time is never treated as
    rejected (fail closed toward the broker's actual offer)."""
    invite = (thread_data or {}).get("tourInvite") or {}
    stored = {
        minutes
        for minutes in (
            _tour_time_minutes(invite.get("arrivalTime")),
            _tour_time_minutes(invite.get("departureTime")),
        )
        if minutes is not None
    }
    text_rejected = _tour_time_minutes_from_patterns(_REJECTED_TOUR_TIME_PATTERNS, text)
    proposed = _tour_time_minutes_from_patterns(_PROPOSED_TOUR_TIME_PATTERNS, text)
    # An explicit rejection construction ("can't do 10 AM") is authoritative even
    # when the same span also trips the "do <time>" offer pattern — reject wins.
    # The stored invite time is only a soft reject: a broker who re-proposes it
    # should still have it honored, so the offer overrides the stored slot there.
    rejected = text_rejected | (stored - proposed)

    kept = [t for t in times if _tour_time_minutes(t) not in rejected]
    if not kept:
        # Everything read as rejected: keep only explicitly-proposed offers. When
        # none was proposed we return [] (below) rather than restoring the original
        # REJECTED order — the schedule pipeline skips evaluation on empty
        # alternateTimes, so a refused slot never reaches alternateTimes[0]
        # (CodeRabbit PR#15).
        kept = [t for t in times if _tour_time_minutes(t) in proposed]

    proposed_first = [t for t in kept if _tour_time_minutes(t) in proposed]
    rest = [t for t in kept if _tour_time_minutes(t) not in proposed]
    return proposed_first + rest


def _build_tour_reply_hold_suggested_email(
    contact_name: str = "",
    recipient_email: str = "",
    alternate_times: Optional[List[str]] = None,
    tour_date: str = "",
) -> str:
    greeting_name = _safe_tour_greeting_name(contact_name, recipient_email)
    alternate_text = ""
    date_label = format_tour_date_label(tour_date)
    if alternate_times:
        alternate_label = ", ".join(alternate_times)
        if date_label and date_label.lower() not in alternate_label.lower():
            alternate_label = f"{date_label} at {alternate_label}"
        alternate_text = f" I saw the alternate time you suggested ({alternate_label})."
    elif date_label:
        alternate_text = f" I saw the update for the {date_label} tour."

    return f"""Hi {greeting_name},

Thanks for letting me know.{alternate_text}

I'm checking the route and schedule on my end and will circle back once I can confirm a workable time."""


def _load_sibling_tour_schedule(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    current_thread_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    current_thread_data = dict(current_thread_data or {})
    schedule = []
    schedule_complete = True
    try:
        threads_ref = _fs.collection("users").document(user_id).collection("threads")
        campaign_id = current_thread_data.get("campaignId") or current_thread_data.get("campaign_id")
        query = threads_ref.where(filter=FieldFilter("clientId", "==", client_id))
        for doc in query.stream():
            data = doc.to_dict() or {}
            if campaign_id and campaign_id not in {data.get("campaignId"), data.get("campaign_id")}:
                continue
            if isinstance(data.get("tourInvite"), dict):
                schedule.append({**data, "id": getattr(doc, "id", None)})
    except Exception as e:
        schedule_complete = False
        print(f"⚠️ Could not load sibling tour schedule for schedule-aware reply: {e}")

    if current_thread_id and not any(str(item.get("id") or "") == str(current_thread_id) for item in schedule):
        schedule.append({**current_thread_data, "id": current_thread_id})
    if not schedule_complete:
        schedule = [{**item, "scheduleComplete": False} for item in schedule]
    return schedule


def _clean_tour_signal_text(*parts: str) -> str:
    """Use only the newest broker-authored text when judging tour actions."""
    joined = "\n".join(str(part or "") for part in parts if str(part or "").strip())
    return strip_email_quotes(joined).strip()


def _is_no_new_reply_text(text: str = "") -> bool:
    """True when an inbound message has no broker-authored text above quoted history."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    return normalized.startswith("[No new text content in reply")


def _looks_like_explicit_tour_offer_or_request(text: str = "") -> bool:
    latest = (text or "").lower()
    if not latest:
        return False

    tour_noun = (
        r"(?:tour|showing|walk[-\s]?through|walkthrough|"
        r"show\s+(?:you|your\s+client)|see\s+(?:it|the\s+space|the\s+property)|"
        r"come\s+by|stop\s+by|take\s+a\s+look)"
    )
    patterns = [
        rf"\b(?:schedule|arrange|set\s+up|book|coordinate)\s+(?:a\s+)?{tour_noun}\b",
        rf"\b(?:would\s+you\s+like|do\s+you\s+want|want)\s+to\s+(?:schedule\s+)?{tour_noun}\b",
        r"\b(?:offered|sent|provided|gave)\s+(?:available\s+)?(?:tour\s+)?(?:times|windows|slots|availability)\b",
        rf"\b(?:happy|glad|able|available)\s+to\s+(?:show|tour|walk)\b",
        rf"\b(?:can|could)\s+(?:show|tour|walk|meet)\b",
        rf"\b(?:can|could)\s+(?:you|your\s+client|we)\s+(?:tour|come\s+by|stop\s+by|see)\b",
        rf"\b(?:tour|showing|walk[-\s]?through|walkthrough)\s+(?:is\s+)?(?:available|offered)\b",
        r"\b(?:tours?|showings?|walk[-\s]?throughs?|walkthroughs?)\s+(?:are|is)\s+(?:available|offered)\b",
    ]
    return any(re.search(pattern, latest) for pattern in patterns)


def _classify_tour_invite_reply(
    message_text: str = "",
    *,
    event: Optional[Dict[str, Any]] = None,
    thread_data: Optional[Dict[str, Any]] = None,
    contact_name: str = "",
    recipient_email: str = "",
    schedule_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event = event or {}
    thread_data = thread_data or {}
    raw_text = " ".join([
        str(message_text or ""),
        str(event.get("question") or ""),
        str(event.get("notes") or ""),
    ]).strip()
    clean_text = _clean_tour_signal_text(raw_text)
    text = clean_text.lower()
    tour_invite_context = _is_tour_invite_thread(thread_data) or event.get("reason") == "tour_slot_reply"
    tour_date = tour_date_from_thread_data(thread_data)

    if not tour_invite_context and not _looks_like_explicit_tour_offer_or_request(clean_text):
        return {
            "outcome": "not_tour",
            "needsOperatorAction": False,
            "canCloseThread": False,
            "alternateTimes": [],
            "details": "Broker did not explicitly offer or request a tour.",
            "suggestedEmail": "",
        }

    negative_time_signal = bool(re.search(
        r"\b(?:does\s+not\s+work|does\s*n[’']?t\s+work|do\s*n[’']?t\s+work|will\s+not\s+work|"
        r"wo\s*n[’']?t\s+work|no\s+longer\s+works?|can[’']?t\s+do|cannot\s+do|"
        r"not\s+available|unavailable|need\s+to\s+reschedule|inste[a]?d|works\s+better)\b",
        text,
    ))
    declined_signal = bool(re.search(
        r"\b(?:no\s+longer\s+available|cannot\s+show|can't\s+show|not\s+able\s+to\s+show|"
        r"no\s+tour|not\s+touring|cancel(?:led)?\s+the\s+tour)\b",
        text,
    ))
    tour_unavailable_signal = looks_like_tour_only_unavailable(clean_text)
    confirmation_signal = bool(re.search(
        r"\b(?:that\s+(?:time|slot)\s+works?|works\s+for\s+(?:us|me|my\s+team|our\s+team|the\s+team|[\w#&'./-]+)|"
        r"confirmed\b(?!\s+(?:stop|stops|tour|tours|slot|slots|showing|showings|appointment|appointments|"
        r"meeting|meetings|property|properties|visit|visits))|confirming|"
        r"see\s+you\s+(?:then|there)|we\s+are\s+confirmed|we're\s+confirmed|sounds\s+good)\b",
        text,
    ))
    slot_scoped_decline_signal = bool(re.search(
        r"\b(?:that|requested|scheduled)\s+(?:time|slot)\b|\bat\s+that\s+time\b",
        text,
    ))
    alternate_times = _extract_tour_reply_time_mentions(clean_text)

    if tour_invite_context and tour_unavailable_signal and not alternate_times and not slot_scoped_decline_signal:
        suggested_email = build_tour_unavailable_reply(
            contact_name,
            recipient_email,
            thread_data,
            tour_date,
        )
        return {
            "outcome": "tour_unavailable",
            "needsOperatorAction": True,
            "canCloseThread": False,
            "alternateTimes": [],
            "tourDate": tour_date,
            "details": "Tours are unavailable for this property, but the property should remain in the campaign results.",
            "suggestedEmail": suggested_email,
        }

    if tour_invite_context and declined_signal and not alternate_times:
        return {
            "outcome": "declined",
            "needsOperatorAction": True,
            "canCloseThread": False,
            "alternateTimes": [],
            "tourDate": tour_date,
            "details": "Broker declined or cancelled the requested tour slot.",
            "suggestedEmail": _build_tour_reply_hold_suggested_email(contact_name, recipient_email, tour_date=tour_date),
        }

    if tour_invite_context and (negative_time_signal or "inste" in text) and alternate_times:
        alternate_times = _reorder_alternate_tour_times(alternate_times, clean_text, thread_data)
        suggested_email = _build_tour_reply_hold_suggested_email(contact_name, recipient_email, alternate_times, tour_date=tour_date)
        if schedule_decision:
            suggested_email = build_schedule_aware_tour_reply(
                contact_name,
                recipient_email,
                thread_data,
                schedule_decision,
            )
        details = (
            f"Broker said the requested tour slot does not work and offered {', '.join(alternate_times)}."
            if alternate_times
            else "Broker said the requested tour slot does not work but did not offer a usable alternate."
        )
        return {
            "outcome": "alternate_requested",
            "needsOperatorAction": True,
            "canCloseThread": False,
            "alternateTimes": alternate_times,
            "details": details,
            "tourDate": tour_date,
            "scheduleDecision": schedule_decision,
            "suggestedEmail": suggested_email,
        }

    if tour_invite_context and confirmation_signal and not negative_time_signal and not declined_signal:
        return {
            "outcome": "confirmed",
            "needsOperatorAction": False,
            "canCloseThread": True,
            "alternateTimes": alternate_times,
            "tourDate": tour_date,
            "details": "Broker confirmed the requested tour slot.",
            "suggestedEmail": "",
        }

    return {
        "outcome": "tour_offer_or_request",
        "needsOperatorAction": True,
        "canCloseThread": False,
        "alternateTimes": alternate_times,
        "tourDate": tour_date,
        "details": "Broker tour/showing message needs operator review.",
        "suggestedEmail": "",
    }


def _build_tour_invite_reply_state_update(
    classification: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build durable thread fields for a broker reply to a reviewed tour invite."""
    classification = classification or {}
    outcome = str(classification.get("outcome") or "").strip().lower()
    alternate_times = list(classification.get("alternateTimes") or [])
    tour_date = str(classification.get("tourDate") or "").strip()

    payload = {
        "tourInvite.lastReplyOutcome": outcome or None,
        "tourInvite.lastReplyAt": SERVER_TIMESTAMP,
        "tourInvite.lastReplyDetails": classification.get("details") or "",
    }

    if outcome == "confirmed":
        payload.update({
            "tourStatus": "confirmed",
            "tourConfirmedAt": SERVER_TIMESTAMP,
            "tourInvite.status": "confirmed",
            "tourInvite.confirmedAt": SERVER_TIMESTAMP,
            "tourInvite.alternateTimes": [],
        })
    elif outcome == "alternate_requested":
        schedule_decision = classification.get("scheduleDecision")
        payload.update({
            "tourStatus": "alternate_requested",
            "tourInvite.status": "alternate_requested",
            "tourInvite.alternateTimes": alternate_times,
            "tourInvite.rescheduleRequestedAt": SERVER_TIMESTAMP,
        })
        if schedule_decision:
            payload["tourInvite.requestedAlternate"] = schedule_decision
    elif outcome == "declined":
        payload.update({
            "tourStatus": "declined",
            "tourInvite.status": "declined",
            "tourInvite.alternateTimes": alternate_times,
            "tourInvite.declinedAt": SERVER_TIMESTAMP,
        })
    elif outcome == "tour_unavailable":
        payload.update({
            "tourStatus": "tour_unavailable",
            "tourInvite.status": "tour_unavailable",
            "tourInvite.alternateTimes": alternate_times,
            "tourInvite.tourUnavailableAt": SERVER_TIMESTAMP,
        })

    if tour_date:
        payload["tourInvite.tourDate"] = tour_date

    return {key: value for key, value in payload.items() if value is not None}


def _tour_event_needs_operator_action(
    event: Dict[str, Any],
    message_text: str = "",
    thread_data: Optional[Dict[str, Any]] = None,
) -> bool:
    classification = _classify_tour_invite_reply(
        message_text,
        event=event,
        thread_data=thread_data,
    )
    if classification.get("outcome") == "not_tour":
        return False
    if classification.get("outcome") == "confirmed":
        return False

    suggested = event.get("suggestedEmail")
    if isinstance(suggested, dict):
        suggested_body = suggested.get("body") or ""
    else:
        suggested_body = suggested or ""
    if str(suggested_body).strip():
        return True

    question = str(event.get("question") or "").strip().lower()
    if not question:
        return True

    confirmation_pattern = (
        r"\b(?:is|are|for)\s+confirmed\b|"
        r"\bconfirmed\s+(?:for|at|on)\b|"
        r"\b(?:tour|showing|appointment)\s+(?:is|has been)\s+confirmed\b"
    )
    if re.search(confirmation_pattern, question):
        return False

    return True


def _sanitize_dashboard_suggested_email_body(body: Any) -> str:
    """Strip draft-body closings before the user's configured signature is appended."""
    return strip_outbound_body_signoff(str(body or "")).strip()


def _sanitize_dashboard_suggested_email_payload(payload: Any) -> Any:
    """Clean suggested-email payload bodies without altering suggested contact addresses."""
    if not isinstance(payload, dict):
        return payload
    clean_payload = dict(payload)
    if "body" in clean_payload:
        clean_payload["body"] = _sanitize_dashboard_suggested_email_body(clean_payload.get("body"))
    return clean_payload


def _close_reason_from_event(event: Dict[str, Any]) -> str:
    return (
        event.get("notes")
        or event.get("reason")
        or event.get("closeReason")
        or "all_info_gathered"
    )


def _close_event_can_bypass_missing_fields(event: Dict[str, Any]) -> bool:
    return _close_reason_from_event(event) in TERMINAL_CLOSE_REASONS_WITHOUT_COMPLETE_FIELDS


def _event_text(event: Dict[str, Any], key: str) -> str:
    return str((event or {}).get(key) or "").strip()


def _proposal_events(proposal: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_events = (proposal or {}).get("events") or []
    if not isinstance(raw_events, list):
        return []

    normalized_events = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        event_type = _event_text(event, "type")
        if not event_type:
            continue
        normalized = dict(event)
        normalized["type"] = event_type
        normalized_events.append(normalized)
    return normalized_events


def _contains_field_term(text: str, term: str) -> bool:
    return contains_column_field_term(text, term)


def _response_requests_nonrequestable_fields(
    response_body: str,
    column_config: Optional[dict],
) -> bool:
    return response_requests_nonrequestable_fields(response_body, column_config)


def _response_mentions_missing_fields(
    response_body: str,
    missing_fields: List[str],
    column_config: Optional[dict] = None,
) -> bool:
    """Accept only replies that request missing Ask fields and no Note/Skip fields."""
    body = (response_body or "").lower()
    if not body or not missing_fields:
        return False
    if _response_requests_nonrequestable_fields(body, column_config):
        return False

    aliases = {
        "rail access": ["rail"],
        "docks": ["dock"],
        "drive ins": ["drive", "grade"],
        "drive-ins": ["drive", "grade"],
        "ceiling ht": ["ceiling", "clear height"],
        "power": ["power", "electrical", "amps", "voltage"],
        "ops ex /sf": ["ops", "nnn", "cam", "operating"],
        "flyer / link": ["flyer", "brochure", "marketing"],
        "total sf": ["sf", "square footage", "size"],
    }

    for field in missing_fields:
        key = (field or "").strip().lower()
        candidates = aliases.get(key, [part for part in re.split(r"[^a-z0-9]+", key) if len(part) > 2])
        if any(_contains_field_term(body, candidate) for candidate in candidates):
            return True
    return False


def _select_automatic_response_body(
    scenario: str,
    llm_response_email: Optional[str],
    column_config: Optional[dict],
    contact_name: Optional[str],
) -> str:
    """Use LLM copy only when it does not request configured Note/Skip fields."""
    if llm_response_email and not _response_requests_nonrequestable_fields(
        llm_response_email,
        column_config,
    ):
        return llm_response_email

    greeting = _build_greeting(contact_name)
    fallbacks = {
        "nonviable_with_alternative": f"""{greeting}

Thank you for letting me know that property is no longer available, and thanks for suggesting the alternative property.

I'll review the new property details and get back to you if I have any questions.""",
        "nonviable": f"""{greeting}

Thank you for letting me know that property is no longer available.

Do you have any other properties that might be a good fit for our requirements?""",
        "complete": f"""{greeting}

Thank you for providing all the requested information! We now have everything we need for your property details.

We'll be in touch if we need any additional information.""",
    }
    if scenario not in fallbacks:
        raise ValueError(f"Unknown automatic response scenario: {scenario}")
    return fallbacks[scenario]


def _format_event_property(event: Dict[str, Any]) -> str:
    address = _event_text(event, "address")
    city = _event_text(event, "city")
    if address and city:
        return f"{address}, {city}"
    return address or city


def _build_property_unavailable_comment(current_date: str, found_keyword: str, events: List[Dict[str, Any]]) -> str:
    base = f"[{current_date}] Property marked unavailable - contact said: '{found_keyword}'"
    new_property_events = [event for event in (events or []) if event.get("type") == "new_property"]

    alternates = []
    for event in new_property_events:
        alternate = _format_event_property(event)
        notes = _event_text(event, "notes")

        if alternate:
            alternates.append(f"Suggested alternate: {alternate}")
        if notes:
            alternates.append(f"Alternate context: {notes}")

    if not alternates:
        return base

    return f"{base} ({'; '.join(alternates)})"


def _has_new_property_path(
    events: List[Dict[str, Any]],
    new_row_created: bool = False,
    new_property_pending_created: bool = False,
) -> bool:
    if new_row_created or new_property_pending_created:
        return True
    return any((event or {}).get("type") == "new_property" for event in (events or []))


EVENTS_ALLOWED_AFTER_ORIGINAL_ROW_NONVIABLE = {
    "new_property",
    "contact_optout",
}


def _should_skip_event_after_original_row_terminalized(
    event_type: str,
    *,
    old_row_became_nonviable: bool,
) -> bool:
    if not old_row_became_nonviable:
        return False
    return event_type not in EVENTS_ALLOWED_AFTER_ORIGINAL_ROW_NONVIABLE


# Events whose handlers move the THREAD to a terminal state (stopped/completed).
# They must process AFTER informational events: a crash mid-loop after one of
# these has terminalized the thread strands every remaining event forever —
# the retry re-scans the message, hits the terminal-thread guard, and saves it
# "for history only" (LIVE break 900 Alt Suggest St: the run died between
# property_unavailable and new_property; the suggested replacement property was
# permanently lost with no operator notification).
_TERMINALIZING_EVENT_TYPES = ("contact_optout", "property_unavailable", "close_conversation")


def _order_events_for_processing(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable-order proposal events so terminalizing events run LAST.

    Also makes the final thread state deterministic when the LLM emits a
    multi-intent list in arbitrary order (e.g. [contact_optout, wrong_contact]
    previously ended 'paused'; terminal-last always ends 'stopped').
    """
    if not events:
        return events
    informational = [e for e in events if (e or {}).get("type") not in _TERMINALIZING_EVENT_TYPES]
    terminalizing = [e for e in events if (e or {}).get("type") in _TERMINALIZING_EVENT_TYPES]
    return informational + terminalizing


def _property_exists_in_sheet(
    sheets,
    sheet_id: str,
    tab_title: str,
    header: List[str],
    address: str,
    city: str,
) -> bool:
    """
    Best-effort duplicate check for replacement-property approvals.

    If Sheets is temporarily rate limited, fail open so the dashboard still
    surfaces the pending replacement. A duplicate action is recoverable; a
    dropped action can hide unresolved user work.
    """
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab_title}!3:1000",
        ).execute()
    except Exception as e:
        print(f"⚠️ Could not check for existing replacement property, creating approval action anyway: {e}")
        return False

    existing_rows = resp.get("values", [])
    idx_map = _header_index_map(header)
    addr_col = idx_map.get("property address") or idx_map.get("address")
    city_col = idx_map.get("city")

    if addr_col is None:
        return False

    address_normalized = str(address or "").strip().lower()
    city_normalized = str(city or "").strip().lower()

    for row_idx, row in enumerate(existing_rows, start=3):
        if len(row) <= (addr_col - 1):
            continue
        existing_addr = str(row[addr_col - 1] or "").strip().lower()
        existing_city = ""

        if city_col is not None and len(row) > (city_col - 1):
            existing_city = str(row[city_col - 1] or "").strip().lower()

        if existing_addr == address_normalized and existing_city == city_normalized:
            print(f"ℹ️ Property '{address}, {city}' already exists in row {row_idx}, skipping")
            return True

    return False


def _store_contact_optout(user_id: str, email: str, reason: str, thread_id: str) -> bool:
    """
    Store a contact's opt-out status in Firestore.
    This prevents future emails from being sent to this contact.
    """
    try:
        import hashlib
        from google.cloud.firestore import SERVER_TIMESTAMP

        # Use email hash as document ID for consistent lookups
        email_lower = email.lower().strip()
        email_hash = hashlib.sha256(email_lower.encode('utf-8')).hexdigest()[:16]

        optout_ref = _fs.collection("users").document(user_id).collection("optedOutContacts").document(email_hash)

        optout_ref.set({
            "email": email_lower,
            "reason": reason,
            "optedOutAt": SERVER_TIMESTAMP,
            "threadId": thread_id
        })

        print(f"📝 Stored opt-out for {email_lower} (reason: {reason})")
        return True

    except Exception as e:
        print(f"⚠️ Failed to store opt-out for {email}: {e}")
        return False


def is_contact_opted_out(user_id: str, email: str) -> Optional[Dict]:
    """
    Check if a contact has opted out of communications.
    Returns the opt-out record if found, None otherwise.

    Safety posture is FAIL CLOSED: every send path reads a None return as
    "safe to send". If the backing store cannot be read we therefore return a
    non-None sentinel record (never None) so a transient Firestore error can
    never re-open a send to a contact who may have opted out. This matches the
    fail-closed handling the follow-up sender already wraps around this call.

    An opt-out is stored under the exact address hash, but a broker reached via
    a plus alias (broker+leasing@x.com) is the SAME mailbox as the bare address
    (broker@x.com), so we also probe the plus-stripped mailbox identity.
    """
    try:
        import hashlib

        email_lower = str(email or "").lower().strip()

        # Probe the exact address first, then the plus-alias-stripped mailbox
        # identity so an opted-out mailbox reached via a plus alias is caught.
        candidates: List[str] = []
        for candidate in (email_lower, _mailbox_identity_without_plus(email_lower)):
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        optout_collection = (
            _fs.collection("users").document(user_id).collection("optedOutContacts")
        )
        for candidate in candidates:
            email_hash = hashlib.sha256(candidate.encode('utf-8')).hexdigest()[:16]
            doc = optout_collection.document(email_hash).get()
            if doc.exists:
                return doc.to_dict()
        return None

    except Exception as e:
        print(f"⚠️ Failed to check opt-out status for {email}: {e}")
        # FAIL CLOSED: a lookup error must never read as "not opted out".
        return {
            "reason": "lookup_error",
            "failClosed": True,
            "email": str(email or "").lower().strip(),
        }


def _build_greeting(contact_name: Optional[str]) -> str:
    """Build a personalized greeting using the contact's first name, or generic 'Hi,' if no name."""
    if contact_name:
        first_name = contact_name.split()[0]
        return f"Hi {first_name},"
    return "Hi,"


def _normalize_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value if "@" in value else None


def _mailbox_identity_without_plus(email: Optional[str]) -> Optional[str]:
    normalized = _normalize_email(email)
    if not normalized:
        return None
    local, domain = normalized.rsplit("@", 1)
    return f"{local.split('+', 1)[0]}@{domain}"


def _same_mailbox_alias(first_email: Optional[str], second_email: Optional[str]) -> bool:
    first_identity = _mailbox_identity_without_plus(first_email)
    second_identity = _mailbox_identity_without_plus(second_email)
    return bool(first_identity and second_identity and first_identity == second_identity)


def _row_value_by_header(rowvals: Optional[List[str]], header: Optional[List[str]], names: List[str]) -> Optional[str]:
    if not rowvals or not header:
        return None
    idx_map = _header_index_map(header)
    for name in names:
        idx = idx_map.get(name)
        if idx and (idx - 1) < len(rowvals):
            value = (rowvals[idx - 1] or "").strip()
            if value:
                return value
    return None


def _resolve_reply_identity(
    *,
    thread_data: Dict[str, Any],
    rowvals: Optional[List[str]],
    header: Optional[List[str]],
    from_addr: Optional[str],
    from_name: Optional[str],
) -> Dict[str, Optional[str]]:
    """
    Resolve the identity used for automatic replies.

    Graph reply endpoints reply to the current inbound message, so forwarded or
    delegated threads must use the current sender's identity instead of stale
    campaign-start contact metadata.
    """
    sender_email = _normalize_email(from_addr)
    sender_name = (from_name or "").strip() or None

    thread_emails = [
        email for email in (
            _normalize_email(email)
            for email in (thread_data.get("email") or [])
        )
        if email
    ]
    sheet_email = _normalize_email(_row_value_by_header(
        rowvals,
        header,
        ["email", "email address", "contact email", "leasing email"],
    ))
    original_email = sheet_email or (thread_emails[0] if thread_emails else None)

    stored_contact = (thread_data.get("contactName") or "").strip() or None
    sheet_contact = _row_value_by_header(
        rowvals,
        header,
        ["leasing contact", "contact name", "name", "contact", "broker name", "broker"],
    )

    if sender_email and (not original_email or sender_email != original_email):
        if original_email and _same_mailbox_alias(sender_email, original_email):
            contact_name = stored_contact or sheet_contact or sender_name
            return {
                "recipient_email": sender_email,
                "contact_name": contact_name,
                "source": "same_mailbox_contact" if (stored_contact or sheet_contact) else "current_sender",
                "original_email": original_email,
            }

        return {
            "recipient_email": sender_email,
            "contact_name": sender_name,
            "source": "current_sender",
            "original_email": original_email,
        }

    contact_name = stored_contact or sheet_contact or sender_name
    source = (
        "stored_contact" if stored_contact
        else "sheet_contact" if sheet_contact
        else "current_sender" if sender_name
        else "unknown"
    )
    return {
        "recipient_email": original_email or sender_email,
        "contact_name": contact_name,
        "source": source,
        "original_email": original_email,
    }


def _align_response_greeting(response_body: Optional[str], contact_name: Optional[str]) -> Optional[str]:
    """Replace a stale named greeting with the resolved reply identity greeting."""
    if not response_body:
        return response_body

    expected = _build_greeting(contact_name)
    greeting_re = re.compile(
        r"^(\s*)(?:hi|hello|hey|thanks|thank you)\s+"
        r"[a-z][a-z'’.-]*(?:\s+[a-z][a-z'’.-]*)?\s*(?:,|[-–—])",
        re.IGNORECASE,
    )
    return greeting_re.sub(lambda match: f"{match.group(1)}{expected}", response_body, count=1)


def _mark_reply_sent_but_unindexed(reason: str) -> bool:
    _set_reply_send_outcome(
        error=reason,
        sent_but_unindexed=True,
        outcome="sent_but_unindexed",
    )
    print(f"   ⚠️ SENT-BUT-UNINDEXED: {reason}")
    return False


def _automatic_inbox_replies_allowed(user_id: str) -> bool:
    raw_allowlist = os.environ.get("SITESIFT_AUTO_REPLY_ALLOWLIST")
    if raw_allowlist is None:
        allowed = DEFAULT_AUTOMATIC_INBOX_REPLY_ALLOWLIST
    else:
        raw_allowlist = raw_allowlist.strip()
        if raw_allowlist == "*":
            return True
        allowed = {
            value.strip()
            for value in re.split(r"[,\s]+", raw_allowlist)
            if value.strip()
        }
    return str(user_id or "").strip() in allowed


def _tour_actions_allowed(user_id: str) -> bool:
    raw_allowlist = os.environ.get("SITESIFT_TOUR_ACTION_ALLOWLIST")
    if raw_allowlist is None:
        allowed = DEFAULT_TOUR_ACTION_ALLOWLIST
    else:
        raw_allowlist = raw_allowlist.strip()
        if raw_allowlist == "*":
            return True
        allowed = {
            value.strip()
            for value in re.split(r"[,\s]+", raw_allowlist)
            if value.strip()
        }
    return str(user_id or "").strip() in allowed


def send_reply_in_thread(user_id: str, headers: dict, body: str, current_msg_id: str, recipient: str, thread_id: str) -> bool:
    """Send a reply to the current message being processed and index it for future replies"""
    _reset_reply_send_outcome()
    body_validation = validate_outbound_body(body)
    if not body_validation.is_safe:
        _set_reply_send_outcome(
            error=f"{body_validation.reason}; manual review required before auto-reply",
            outcome="blocked_unsafe_body",
        )
        print(f"   🛑 Blocked unsafe auto-reply body: {body_validation.reason}")
        return False
    if not _automatic_inbox_replies_allowed(user_id):
        _set_reply_send_outcome(
            error=(
                "Automatic inbox replies are disabled for this user; "
                "manual review required before auto-reply"
            ),
            outcome="blocked_auto_reply_policy",
        )
        print(f"   🛑 Blocked automatic inbox reply for non-allowlisted user {user_id}")
        return False
    try:
        from .utils import (
            GRAPH_SEND_MAX_RETRIES,
            exponential_backoff_request,
            safe_preview,
            get_signature_attachments,
            needs_signature_attachments,
            resolve_signature_settings,
        )
        from .messaging import save_message, index_message_id, index_conversation_id, lookup_thread_by_message_id
        from .clients import _fs
        from .email import (
            _delete_graph_reply_draft,
            _filter_reply_all_draft_recipients,
            _hydrate_reply_all_draft_recipients,
            _reviewed_recipient_reply_all_fallback,
            _source_message_reply_all_fallback,
        )
        from datetime import datetime, timezone
        import requests
        import time

        thread_doc = (
            _fs.collection("users")
            .document(user_id)
            .collection("threads")
            .document(thread_id)
            .get()
        )
        thread_data = thread_doc.to_dict() if thread_doc.exists else {}
        client_id = (thread_data or {}).get("clientId")
        decision = get_client_automation_decision(user_id, client_id)
        if decision.denies_autonomous_work:
            _set_reply_campaign_suppression(decision)
            print(f"   🛑 {_get_reply_send_outcome().error}")
            return False

        base = "https://graph.microsoft.com/v1.0"
        current_meta = {}

        try:
            current_meta_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages/{current_msg_id}",
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
            if current_meta_resp.status_code == 200:
                current_meta = current_meta_resp.json() or {}
                _set_reply_send_outcome(
                    subject=current_meta.get("subject"),
                    conversation_id=current_meta.get("conversationId"),
                )
        except Exception as exc:
            print(f"   ⚠️ Could not fetch reply thread identity before send: {exc}")

        # Fetch user's signature settings to use the same signature as outbox emails
        user_signature = None
        signature_mode = None
        user_email = None
        try:
            user_doc = _fs.collection("users").document(user_id).get()
            if user_doc.exists:
                user_data = user_doc.to_dict() or {}
                user_signature, signature_mode, user_email = resolve_signature_settings(user_data)
        except Exception as e:
            print(f"   ⚠️ Failed to fetch user signature settings: {e}")

        # Format body as HTML with footer (uses user's signature settings)
        html_body = format_email_body_with_footer(
            body,
            user_signature,
            signature_mode,
            user_email=user_email,
        )

        # Track if reply was sent successfully
        reply_sent_successfully = False
        reply_sent_after = None

        create_reply_resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{current_msg_id}/createReplyAll", headers=headers, timeout=30),
            max_retries=GRAPH_SEND_MAX_RETRIES,
        )
        if not create_reply_resp or create_reply_resp.status_code not in [200, 201]:
            failure_reason = f"createReplyAll failed: {create_reply_resp.status_code if create_reply_resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            print(f"   ❌ {failure_reason}")
            return False

        reply_draft = create_reply_resp.json() or {}
        reply_draft_id = reply_draft.get("id")
        if not reply_draft_id:
            _set_reply_send_outcome(
                error="createReplyAll returned no draft id",
                outcome="send_failed",
            )
            print("   ❌ createReplyAll returned no draft id")
            return False

        reply_draft = _hydrate_reply_all_draft_recipients(
            headers,
            reply_draft,
            base=base,
        )
        reply_draft = _source_message_reply_all_fallback(
            reply_draft,
            current_meta,
        )
        reply_draft = _reviewed_recipient_reply_all_fallback(
            reply_draft,
            to_emails=[recipient],
        )

        recipient_result = _filter_reply_all_draft_recipients(
            user_id,
            reply_draft,
            user_email=user_email,
        )
        recipient_payload = recipient_result["payload"]
        if not (recipient_payload["toRecipients"] or recipient_payload["ccRecipients"]):
            _set_reply_send_outcome(
                error="No safe reply-all recipients remained after filtering",
                outcome="send_failed",
            )
            print("   ❌ No safe reply-all recipients remained after filtering")
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            return False

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
                timeout=30
            ),
            max_retries=GRAPH_SEND_MAX_RETRIES,
        )
        if not patch_resp or patch_resp.status_code not in [200, 202, 204]:
            failure_reason = f"Reply-all draft patch failed: {patch_resp.status_code if patch_resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            print(f"   ❌ {failure_reason}")
            return False

        signature_attachments = []
        if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
            signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)

        for attachment in signature_attachments:
            try:
                att_resp = exponential_backoff_request(
                    lambda att=attachment: requests.post(
                        f"{base}/me/messages/{reply_draft_id}/attachments",
                        headers=headers,
                        json=att,
                        timeout=30
                    ),
                    max_retries=GRAPH_SEND_MAX_RETRIES,
                )
                if att_resp.status_code in [200, 201]:
                    print(f"   📎 Attached {attachment['name']}")
            except Exception as e:
                print(f"   ⚠️ Error attaching {attachment['name']}: {e}")

        decision = get_client_automation_decision(user_id, client_id)
        if decision.denies_autonomous_work:
            _set_reply_campaign_suppression(decision)
            _delete_graph_reply_draft(headers, reply_draft_id, base=base)
            print(f"   🛑 {_get_reply_send_outcome().error}")
            return False

        reply_sent_after = datetime.now(timezone.utc) - timedelta(seconds=3)
        _set_reply_send_outcome(send_attempt_at=reply_sent_after)
        resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{reply_draft_id}/send", headers=headers, timeout=30),
            max_retries=1,
            operation="graph_send",
        )
        reply_sent_successfully = resp and resp.status_code in [200, 202]
        if reply_sent_successfully:
            print(f"   ✅ Sent reply via createReplyAll draft")

        if not reply_sent_successfully:
            failure_reason = f"Reply-all draft send failed: {resp.status_code if resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            print(f"   ❌ {failure_reason}")
            return False

        # Reply was sent successfully - now index it
        # CRITICAL: Index the sent message so future replies can find the thread
        # Graph draft sends do not return the sent message ID, so fetch it from SentItems.
        try:
            # Wait a moment for the message to appear in SentItems
            time.sleep(1)

            # Fetch the most recent message from SentItems for this conversation
            # Get conversationId from the current message
            current_msg_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages/{current_msg_id}",
                    headers=headers,
                    params={"$select": "conversationId"},
                    timeout=30
                )
            )
            conversation_id = current_msg_resp.json().get("conversationId") if current_msg_resp.status_code == 200 else None
            if conversation_id:
                _set_reply_send_outcome(conversation_id=conversation_id)

            if conversation_id:
                sent_msg = _find_recent_sent_message_for_conversation(
                    headers,
                    base,
                    conversation_id,
                    reply_sent_after or (datetime.now(timezone.utc) - timedelta(minutes=5)),
                )

                if sent_msg:
                    sent_internet_msg_id = sent_msg.get("internetMessageId")

                    if sent_internet_msg_id:
                        # Index this sent message with retry logic
                        normalized_id = normalize_message_id(sent_internet_msg_id)

                        # Retry indexing up to 3 times
                        MAX_RETRIES = 3
                        msg_indexed = False
                        for attempt in range(MAX_RETRIES):
                            if index_message_id(user_id, sent_internet_msg_id, thread_id):
                                # Verify the index was written
                                time.sleep(0.2)
                                if lookup_thread_by_message_id(user_id, sent_internet_msg_id) == thread_id:
                                    msg_indexed = True
                                    break
                            print(f"   ⚠️ Reply index attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
                            time.sleep(0.5 * (attempt + 1))

                        if not msg_indexed:
                            error_msg = f"Failed to index reply after {MAX_RETRIES} attempts"
                            print(f"   ⚠️ CRITICAL: {error_msg} - future replies may be orphaned")
                            return _mark_reply_sent_but_unindexed(error_msg)

                        # Also save the message record
                        to_recipients = [r.get("emailAddress", {}).get("address", "") for r in sent_msg.get("toRecipients", [])]
                        cc_recipients = [r.get("emailAddress", {}).get("address", "") for r in sent_msg.get("ccRecipients", [])]
                        body_obj = sent_msg.get("body", {}) or {}
                        body_content = body_obj.get("content", "")

                        message_record = {
                            "direction": "outbound",
                            "subject": sent_msg.get("subject", ""),
                            "from": "me",
                            "to": to_recipients,
                            "cc": cc_recipients,
                            "sentDateTime": sent_msg.get("sentDateTime"),
                            "receivedDateTime": None,
                            "headers": {
                                "internetMessageId": sent_internet_msg_id,
                                "inReplyTo": None,  # Would need to extract from headers
                                "references": []
                            },
                            "body": {
                                "contentType": body_obj.get("contentType", "HTML"),
                                "content": body_content,
                                "preview": sent_msg.get("bodyPreview", "")[:200] or safe_preview(body_content)
                            }
                        }
                        save_message(user_id, thread_id, normalized_id, message_record)

                        # Index conversation ID with retry
                        if conversation_id:
                            for attempt in range(MAX_RETRIES):
                                if index_conversation_id(user_id, conversation_id, thread_id):
                                    break
                                time.sleep(0.5 * (attempt + 1))

                        print(f"   📝 Indexed sent reply message: {sent_internet_msg_id[:50]}...")
                    else:
                        return _mark_reply_sent_but_unindexed("Sent message has no internetMessageId, cannot index")
                else:
                    return _mark_reply_sent_but_unindexed("Could not find new sent message in SentItems to index")
            else:
                return _mark_reply_sent_but_unindexed("Could not get conversationId to index sent message")
        except Exception as e:
            return _mark_reply_sent_but_unindexed(f"Failed to index sent reply: {e}")

        _set_reply_send_outcome(outcome="sent_indexed")
        return True

    except Exception as e:
        _set_reply_send_outcome(
            error=str(e),
            sent_but_unindexed=False,
            outcome="send_failed",
        )
        print(f"   ❌ Failed to send reply: {e}")
        return False

def _find_client_id_by_email(uid: str, email: str) -> Optional[str]:
    """
    Search through all clients (active and archived) to find which one has a sheet
    with a row matching the given email address.
    Returns clientId if found, None otherwise.
    """
    if not email:
        return None
    
    email_lower = email.lower().strip()
    
    try:
        # Search active clients
        clients_ref = _fs.collection("users").document(uid).collection("clients")
        clients = list(clients_ref.stream())
        
        for client_doc in clients:
            client_id = client_doc.id
            client_data = client_doc.to_dict() or {}
            sheet_id = client_data.get("sheetId")
            
            if not sheet_id:
                continue
            
            try:
                # Try to find email in this client's sheet
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)
                header = _read_header_row2(sheets, sheet_id, tab_title)
                rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, email_lower)
                
                if rownum is not None:
                    print(f"   ✅ Found email {email_lower} in client {client_id}, sheet {sheet_id}, row {rownum}")
                    return client_id
            except Exception as e:
                # Skip this client if sheet access fails
                continue
        
        # Search archived clients
        archived_clients_ref = _fs.collection("users").document(uid).collection("archivedClients")
        archived_clients = list(archived_clients_ref.stream())
        
        for client_doc in archived_clients:
            client_id = client_doc.id
            client_data = client_doc.to_dict() or {}
            sheet_id = client_data.get("sheetId")
            
            if not sheet_id:
                continue
            
            try:
                # Try to find email in this archived client's sheet
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)
                header = _read_header_row2(sheets, sheet_id, tab_title)
                rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, email_lower)
                
                if rownum is not None:
                    print(f"   ✅ Found email {email_lower} in archived client {client_id}, sheet {sheet_id}, row {rownum}")
                    return client_id
            except Exception as e:
                # Skip this client if sheet access fails
                continue
        
        return None
    except Exception as e:
        print(f"   ⚠️ Failed to search clients for email {email_lower}: {e}")
        return None

def fetch_and_log_sheet_for_thread(uid: str, thread_id: str, counterparty_email: Optional[str]):
    # Read thread (to get clientId)
    tdoc = (_fs.collection("users").document(uid)
            .collection("threads").document(thread_id).get())
    if not tdoc.exists:
        print("⚠️ Thread doc not found; cannot fetch sheet")
        return None, None, None, None, None, None, None  # Return tuple for unpacking

    tdata = tdoc.to_dict() or {}
    client_id = tdata.get("clientId")
    if not client_id:
        print("⚠️ Thread has no clientId; cannot fetch sheet")
        return None, None, None, None, None, None, None

    # Required: sheetId on client doc, also get columnConfig and extractionFields
    try:
        sheet_id, column_config, extraction_fields = _get_client_config(uid, client_id)
    except RuntimeError as e:
        print(str(e))
        return None, None, None, None, None, None, None

    # Counterparty email fallback: use thread's stored recipients if missing
    if not counterparty_email:
        recips = tdata.get("email") or []
        if recips:
            counterparty_email = recips[0]

    # Connect to Sheets; header = row 2
    sheets = _sheets_client()
    tab_title = _get_first_tab_title(sheets, sheet_id)
    header = _read_header_row2(sheets, sheet_id, tab_title)

    # Ensure sizing/behavior is correct on every run (idempotent)
    format_sheet_columns_autosize_with_exceptions(sheet_id, header)

    print(f"📄 Sheet fetched: title='{tab_title}', sheetId={sheet_id}")
    print(f"   Header (row 2): {header}")
    print(f"   Counterparty email (row match): {counterparty_email or 'unknown'}")

    # NEW: Use row anchoring for enhanced row matching
    rownum, rowvals = _find_row_by_anchor(uid, thread_id, sheets, sheet_id, tab_title, header, counterparty_email or "")

    if rownum is not None:
        print(f"📌 Matched row {rownum}: {rowvals}")
        return client_id, sheet_id, header, rownum, rowvals, column_config, extraction_fields
    else:
        # Be loud – row must exist for our workflow
        print(f"❌ No sheet row found with email = {counterparty_email}")
        return client_id, sheet_id, header, None, None, column_config, extraction_fields

# Unambiguous auto-reply subject markers across locales. Defense-in-depth
# backstop (FIX-18) for RFC-3834 header detection: localized out-of-office
# replies that lack the standard headers must still be skipped so
# temporary-absence messages never reach the classifier as real broker data.
#
# Every phrase here is an auto-responder *system* string — it does not occur in
# a human broker's freeform subject line — so a subject-only substring match is
# safe. Ambiguous words that a human broker legitimately writes (e.g. "on
# vacation", "fuori sede") live in AUTO_REPLY_SUBJECT_AMBIGUOUS_MARKERS below
# and are only honored when an independent auto-reply signal corroborates them.
AUTO_REPLY_SUBJECT_MARKERS = [
    # English
    "out of office", "automatic reply", "auto-reply", "auto reply",
    "autoreply", "away from office", "ooo:",
    # German
    "automatische antwort", "abwesenheitsnotiz",
    # French
    "réponse automatique", "reponse automatique", "absence du bureau",
    # Spanish
    "respuesta automática", "respuesta automatica",
    "ausencia temporal", "fuera de la oficina",
    # Italian
    "risposta automatica", "assente dall'ufficio",
    # Portuguese
    "resposta automática", "resposta automatica", "ausência temporária",
    # Dutch
    "automatisch antwoord", "afwezigheidsassistent",
]

# Ambiguous phrases that COLLIDE with legitimate human broker replies.
# In CRE broker context these frequently appear in real, actionable messages:
#   - "fuori sede"   → Italian "off-site", often "off-site but AVAILABLE"
#   - "on vacation"  → "our tenant is on vacation until August, but the space
#                       is available" is a real reply, not an auto-responder.
# A bare subject-substring match on these dropped genuine broker replies and
# stalled the follow-up loop (CodeRabbit false-positive class). They are only
# treated as auto-reply markers when an INDEPENDENT auto-reply signal
# (RFC-3834 header, auto-responder sender, etc.) is also present.
AUTO_REPLY_SUBJECT_AMBIGUOUS_MARKERS = [
    "on vacation",
    "fuori sede",
]

# Local-part / address fragments that identify a machine auto-responder or
# bounce sender. A human broker never replies from one of these.
AUTO_REPLY_SENDER_MARKERS = [
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "do_not_reply", "mailer-daemon", "mailer_daemon", "postmaster",
    "auto-reply", "autoreply", "autorespond", "bounce",
]


def _is_auto_reply_sender(sender: Optional[str]) -> bool:
    """Return True if the sender address looks like a machine auto-responder.

    Corroborating signal for the ambiguous subject markers: a genuine broker
    reply never arrives from a no-reply / mailer-daemon / postmaster address.
    Pure function for deterministic testing (no live Graph call).
    """
    sender_lower = (sender or "").lower()
    if "@" not in sender_lower:
        return False
    return any(marker in sender_lower for marker in AUTO_REPLY_SENDER_MARKERS)


def _is_auto_reply_subject(
    subject: Optional[str], *, has_auto_reply_signal: bool = False
) -> bool:
    """Return True if the subject line indicates an auto-reply/OOO message.

    Context-aware (FIX-18 / M08 variant, CodeRabbit over-match fix):

    * Unambiguous auto-responder subject strings (AUTO_REPLY_SUBJECT_MARKERS)
      match on the subject alone — they never occur in a human broker subject.
    * Ambiguous phrases (AUTO_REPLY_SUBJECT_AMBIGUOUS_MARKERS) — "on vacation",
      "fuori sede" — only count when ``has_auto_reply_signal`` is True, i.e.
      an independent auto-reply signal (RFC-3834 header or auto-responder
      sender) already corroborates the classification. This prevents a
      legitimate broker reply whose subject merely *contains* one of these
      words from being dropped and stalling the follow-up loop.

    Pure function so the guard is deterministically testable without a live
    Graph/model call.
    """
    subject_lower = (subject or "").lower()
    if any(marker in subject_lower for marker in AUTO_REPLY_SUBJECT_MARKERS):
        return True
    if has_auto_reply_signal and any(
        marker in subject_lower for marker in AUTO_REPLY_SUBJECT_AMBIGUOUS_MARKERS
    ):
        return True
    return False


def _validate_operator_replay_claims(
    user_id: str,
    graph_message_id: str,
    internet_message_id: str,
    attempt_id: str,
) -> None:
    """Require the durable two-message preclaim before operator replay effects."""
    if not attempt_id:
        raise RetryableProcessingError("Operator replay claim is missing")
    user_ref = _fs.collection("users").document(user_id)
    for message_id in (graph_message_id, internet_message_id):
        if not message_id:
            raise RetryableProcessingError("Operator replay claim message ID is missing")
        snapshot = (
            user_ref.collection("processedMessages")
            .document(b64url_id(message_id))
            .get()
        )
        claim = snapshot.to_dict() if getattr(snapshot, "exists", False) else {}
        if (
            not isinstance(claim, dict)
            or claim.get("status") != "operator_replay_in_progress"
            or claim.get("replayAttemptId") != attempt_id
        ):
            raise RetryableProcessingError(
                "Operator replay claim does not match both exact message IDs"
            )


def process_inbox_message(
    user_id: str,
    headers: Dict[str, str],
    msg: Dict[str, Any],
    *,
    allow_outbound_reply: bool = True,
    operator_replay_attempt_id: Optional[str] = None,
):
    """ENHANCED: Process a single inbox message with full pipeline including events."""
    if not allow_outbound_reply:
        _reset_reply_send_outcome()
    msg_id = msg.get("id")
    subject = msg.get("subject", "")
    from_info = msg.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    from_name = from_info.get("name", "")  # Extract sender name from email
    internet_message_id = msg.get("internetMessageId")
    conversation_id = msg.get("conversationId")
    received_dt = msg.get("receivedDateTime")
    sent_dt = msg.get("sentDateTime")
    body_preview = msg.get("bodyPreview", "")
    has_attachments = bool(msg.get("hasAttachments"))

    if operator_replay_attempt_id:
        if allow_outbound_reply:
            raise RetryableProcessingError(
                "Operator replay attempt cannot enable outbound replies"
            )
        _validate_operator_replay_claims(
            user_id,
            msg_id,
            internet_message_id,
            operator_replay_attempt_id,
        )
    
    full_msg = {}
    # NEW: fetch full message body and normalize to plain text
    try:
        full_msg = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                headers=headers,
                params={"$select": "body,hasAttachments,sender,replyTo,ccRecipients"},
                timeout=30
            )
        ).json() or {}
        full_body_resp = full_msg.get("body", {}) or {}
        has_attachments = bool(has_attachments or full_msg.get("hasAttachments"))
        _raw_content = full_body_resp.get("content", "") or ""
        _ctype = (full_body_resp.get("contentType") or "Text").upper()
        _full_text = strip_html_tags(_raw_content) if _ctype == "HTML" else _raw_content
    except Exception as e:
        print(f"⚠️ Could not fetch full body for {msg_id}: {e}")
        _full_text = body_preview or ""

    # Strip quoted content for AI processing (keep full text for storage)
    # This prevents the AI from misinterpreting quoted content as the broker's message
    _text_for_ai = strip_email_quotes(_full_text)

    merged_msg = {**msg, **{k: v for k, v in full_msg.items() if k not in msg or not msg.get(k)}}
    to_recipients = _recipient_email_addresses(merged_msg.get("toRecipients"))
    cc_recipients = _recipient_email_addresses(merged_msg.get("ccRecipients"))
    reply_to_recipients = _recipient_email_addresses(merged_msg.get("replyTo"))
    sender_addr = _recipient_email_address(merged_msg.get("sender"))
    source_envelope = _source_message_envelope(merged_msg)
    
    # Get headers if not present
    internet_message_headers = msg.get("internetMessageHeaders")
    if not internet_message_headers:
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            internet_message_headers = response.json().get("internetMessageHeaders", [])
        except Exception as e:
            print(f"⚠️ Could not fetch headers for {msg_id}: {e}")
            internet_message_headers = []
    
    # Extract reply headers and check for auto-replies
    in_reply_to = None
    references = []
    is_auto_reply = False

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)
        # Detect auto-reply headers (RFC 3834)
        elif name == "auto-submitted" and value.lower() != "no":
            is_auto_reply = True
        elif name == "x-auto-response-suppress":
            is_auto_reply = True
        elif name == "x-autoreply" or name == "x-autorespond":
            is_auto_reply = True
        elif name == "precedence" and value.lower() in ["bulk", "junk", "auto_reply"]:
            is_auto_reply = True

    # Also check subject line for common auto-reply patterns.
    # Ambiguous subject phrases ("on vacation", "fuori sede") only count when
    # an independent auto-reply signal corroborates them: the RFC-3834 header
    # match above, or a machine auto-responder sender address. This keeps the
    # subject guard from dropping legitimate broker replies that merely contain
    # those words while still catching real localized auto-responders.
    auto_reply_signal = is_auto_reply or _is_auto_reply_sender(
        sender_addr or from_addr
    )
    if _is_auto_reply_subject(subject, has_auto_reply_signal=auto_reply_signal):
        is_auto_reply = True

    # SAFETY: Skip auto-replies to prevent processing OOO messages as real data
    if is_auto_reply:
        print(f"⏭️ Skipping auto-reply from {from_addr}: {subject}")
        print(f"   Auto-reply emails are not processed to prevent data corruption")
        return

    # SAFETY: Skip emails from ourselves (e.g., forwarded back via auto-forward rules)
    # This prevents our own outbound emails from being processed as broker replies
    try:
        my_email = None

        # Try /me endpoint first
        my_email_resp = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers=headers,
            params={"$select": "mail,userPrincipalName"},
            timeout=10
        )
        if my_email_resp.status_code == 200:
            my_data = my_email_resp.json()
            my_email = (my_data.get("mail") or my_data.get("userPrincipalName") or "").lower()

        # Fallback: get our email from a sent message (works for personal accounts)
        if not my_email:
            sent_resp = requests.get(
                "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages",
                headers=headers,
                params={"$top": "1", "$select": "from"},
                timeout=10
            )
            if sent_resp.status_code == 200:
                sent_data = sent_resp.json()
                if sent_data.get("value"):
                    my_email = (sent_data["value"][0].get("from", {}).get("emailAddress", {}).get("address") or "").lower()

        if my_email and from_addr.lower() == my_email:
            print(f"⏭️ Skipping self-email (forwarded back): {subject}")
            print(f"   Sender {from_addr} matches our own address - likely auto-forwarded")
            return
    except Exception as e:
        # Don't fail the whole process if this check fails
        print(f"⚠️ Could not check for self-email: {e}")

    print(f"📧 Processing: {subject} from {from_addr}")
    print(f"   In-Reply-To: {in_reply_to}")
    print(f"   References: {references}")
    
    # Match against our index
    thread_id = None
    matched_header = None
    
    # Try In-Reply-To first
    if in_reply_to:
        thread_id = lookup_thread_by_message_id(user_id, in_reply_to)
        if thread_id:
            matched_header = f"In-Reply-To: {in_reply_to}"
    
    # Try References (newest to oldest)
    if not thread_id and references:
        for ref in reversed(references):  # References are oldest to newest, we want newest first
            ref = normalize_message_id(ref)
            thread_id = lookup_thread_by_message_id(user_id, ref)
            if thread_id:
                matched_header = f"References: {ref}"
                break
    
    # Fallback to conversation ID
    if not thread_id and conversation_id:
        thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
        if thread_id:
            matched_header = f"ConversationId: {conversation_id}"
    
    # If no thread match found, this is a NEW conversation we didn't start - ignore it
    # Only process emails that are actual replies to messages we sent
    # (matched via In-Reply-To, References, or indexed conversationId)
    if not thread_id:
        print(f"⏭️ Ignoring email from {from_addr} - not a reply to any tracked thread")
        print(f"   Subject: {subject}")
        print(f"   ConversationId: {conversation_id} (not in our index)")
        return
    
    print(f"🎯 Matched via {matched_header} -> thread {thread_id}")

    thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
    thread_data = {}
    try:
        thread_doc = thread_ref.get()
        if thread_doc.exists:
            thread_data = thread_doc.to_dict() or {}
    except Exception as e:
        print(f"⚠️ Could not fetch thread status data: {e}")

    thread_status = thread_data.get("status") or get_thread_status(user_id, thread_id)
    client_id_for_gate = thread_data.get("clientId")
    if not client_id_for_gate and from_addr:
        client_id_for_gate = _find_client_id_by_email(user_id, from_addr)
        if client_id_for_gate:
            thread_data["clientId"] = client_id_for_gate
            try:
                thread_ref.set({"clientId": client_id_for_gate}, merge=True)
                print(
                    f"   ✅ Recovered clientId {client_id_for_gate} before campaign safety gate"
                )
            except Exception as e:
                print(
                    "   ⚠️ Recovered clientId could not be persisted before the campaign "
                    f"safety gate: {e}"
                )
    campaign_decision = get_client_automation_decision(
        user_id,
        client_id_for_gate,
    )
    campaign_suppression_kind = classify_campaign_suppression(campaign_decision)
    client_terminal = campaign_suppression_kind == "terminal"
    client_denied = campaign_suppression_kind is not None
    if client_terminal:
        try:
            thread_ref.update(stopped_followup_patch(campaign_decision.reason))
        except Exception as e:
            print(f"⚠️ Could not mark stopped client thread stopped: {e}")
        thread_data.update({
            "status": THREAD_STATUS["stopped"],
            "followUpStatus": "stopped",
            "statusReason": campaign_decision.reason,
        })
        thread_status = THREAD_STATUS["stopped"]
        print(
            f"⏹️ Client campaign is stopped for thread {thread_id[:20]}...; "
            "saving inbound message for history only"
        )
    elif client_denied:
        print(
            f"⏸️ Client automation is unavailable for thread {thread_id[:20]}...; "
            "saving inbound message for history only without changing terminal state"
        )

    # If the operator manually replied to a paused/escalated thread directly from
    # Outlook (out-of-band Sent-Items continuation) instead of using the dashboard,
    # clear the stale open action_needed notification and resume the thread so
    # processing continues normally rather than staying paused forever.
    if thread_status == THREAD_STATUS["paused"] and not client_denied:
        if _resume_paused_thread_after_manual_continuation(
            user_id, headers, thread_id, thread_data, msg
        ):
            thread_data["status"] = THREAD_STATUS["active"]
            thread_data["statusReason"] = "manual_continuation_resumed"
            thread_status = THREAD_STATUS["active"]

    # Terminal threads keep late replies for history but must not generate new AI work or auto-replies,
    # except when the user approved a same-contact replacement property in this email thread.
    replacement_context = _active_replacement_context(thread_data, _full_text)
    if replacement_context and thread_status == THREAD_STATUS["stopped"] and not client_denied:
        replacement_subject = replacement_context["address"]
        if replacement_context.get("city"):
            replacement_subject = f"{replacement_subject}, {replacement_context['city']}"
        thread_patch = {
            "rowNumber": replacement_context["rowNumber"],
            "subject": replacement_subject,
            "status": THREAD_STATUS["active"],
            "followUpStatus": "waiting",
            "statusReason": "same_contact_replacement_reply",
            "updatedAt": SERVER_TIMESTAMP,
        }
        thread_ref.set(thread_patch, merge=True)
        thread_data.update(thread_patch)
        thread_status = THREAD_STATUS["active"]
        print(
            f"🔁 Reactivated stopped thread for replacement property "
            f"{replacement_subject} row {replacement_context['rowNumber']}"
        )

    if client_denied or _should_skip_processing_for_terminal_thread(thread_status, thread_data, _full_text):
        reason_label = (
            f"campaign automation is {campaign_suppression_kind}"
            if client_denied
            else f"thread is {thread_status}"
        )
        print(
            f"⏹️ {reason_label} for {thread_id[:20]}... - "
            "saving message but skipping processing"
        )
        # Still save the message for conversation history, but don't process or auto-reply
        # Fall through to message saving, but set a flag to skip processing
        skip_processing_for_terminal = True
    else:
        skip_processing_for_terminal = False

    # Create message record
    message_record = {
        "direction": "inbound",
        "subject": subject,
        "from": from_addr,
        "sender": sender_addr,
        "to": to_recipients,
        "cc": cc_recipients,
        "replyTo": reply_to_recipients,
        "sentDateTime": sent_dt,
        "receivedDateTime": received_dt,
        "headers": {
            "internetMessageId": internet_message_id,
            "inReplyTo": in_reply_to,
            "references": references
        },
        "body": {
            "contentType": "Text",
            "content": _full_text,
            "preview": safe_preview(_full_text)
        },
        "hasAttachments": has_attachments,
        "sourceMessage": source_envelope,
    }
    
    # Save to Firestore with retry logic for reliability
    import time
    MAX_RETRIES = 3

    if internet_message_id:
        # Save message with retry
        for attempt in range(MAX_RETRIES):
            if save_message(user_id, thread_id, internet_message_id, message_record):
                break
            print(f"⚠️ Inbound message save attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
            time.sleep(0.5 * (attempt + 1))

        # Index with retry and verification
        msg_indexed = False
        for attempt in range(MAX_RETRIES):
            if index_message_id(user_id, internet_message_id, thread_id):
                time.sleep(0.2)
                if lookup_thread_by_message_id(user_id, internet_message_id) == thread_id:
                    msg_indexed = True
                    break
            print(f"⚠️ Inbound message index attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
            time.sleep(0.5 * (attempt + 1))

        if not msg_indexed:
            print(f"⚠️ Failed to index inbound message after {MAX_RETRIES} attempts")
    else:
        # Use Graph message ID as fallback
        save_message(user_id, thread_id, msg_id, message_record)
    
    # Update thread timestamp
    try:
        update_payload = {"updatedAt": SERVER_TIMESTAMP}
        if source_envelope:
            update_payload["lastInboundEnvelope"] = source_envelope
        thread_ref.set(update_payload, merge=True)
    except Exception as e:
        print(f"⚠️ Failed to update thread timestamp: {e}")

    if _is_no_new_reply_text(_text_for_ai) and not has_attachments:
        print(
            "⏭️ Inbound reply has no new broker-authored text and no attachments; "
            "saved for history without AI/sheet/follow-up side effects"
        )
        return

    # Cancel/pause any pending follow-ups since broker responded
    try:
        from .followup import cancel_followup_on_response
        cancel_followup_on_response(user_id, thread_id)
    except Exception as e:
        print(f"⚠️ Failed to cancel follow-up: {e}")

    # Dump the conversation
    dump_thread_from_firestore(user_id, thread_id)

    # If thread is terminal, skip further processing (AI, sheet updates, auto-replies)
    if skip_processing_for_terminal:
        print("⏹️ Skipping suppressed processing - message saved for history only")
        if client_denied and not client_terminal:
            raise RetryableProcessingError(
                "Campaign automation is temporarily unavailable; inbound evidence was saved "
                f"but downstream processing remains retryable ({campaign_decision.reason})"
            )
        return

    # Step 1: fetch Google Sheet (required) and log header + counterparty email
    # Also retrieve columnConfig and extractionFields for per-client AI configuration
    client_id, sheet_id, header, rownum, rowvals, column_config, extraction_fields = fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)

    # If no clientId found, try to find it by email and update the thread
    if not client_id and from_addr:
        print(f"   🔍 Retrying clientId lookup for email: {from_addr}")
        client_id = _find_client_id_by_email(user_id, from_addr)
        if client_id:
            print(f"   ✅ Found clientId: {client_id}, updating thread...")
            # Update thread with clientId
            thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
            thread_ref.set({"clientId": client_id}, merge=True)
            # Retry fetching sheet
            client_id, sheet_id, header, rownum, rowvals, column_config, extraction_fields = fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)
    
    # Resolve reply identity from the current inbound message. For normal threads,
    # this preserves the campaign contact; for forwarded/delegated threads, it
    # switches automated replies to the current sender so Graph reply behavior
    # and email copy stay aligned.
    try:
        thread_doc = _fs.collection("users").document(user_id).collection("threads").document(thread_id).get()
        latest_thread_data = thread_doc.to_dict() or {}
        thread_data = {**thread_data, **latest_thread_data}
    except Exception as e:
        print(f"⚠️ Could not fetch thread identity data: {e}")
    
    # Only proceed if we successfully matched a sheet row
    if sheet_id and rownum is not None:
        sender_addr_lower = (from_addr or "").strip().lower()
        identity = _resolve_reply_identity(
            thread_data=thread_data,
            rowvals=rowvals,
            header=header,
            from_addr=from_addr,
            from_name=from_name,
        )
        recipient_email = identity.get("recipient_email") or sender_addr_lower
        contact_name = identity.get("contact_name")
        thread_emails = thread_data.get("email", [])
        external_email = identity.get("original_email")

        print(f"📧 Reply recipient determined: {recipient_email}")
        print(f"   Thread participants: {thread_emails}")
        print(f"   Original sheet/thread email: {external_email or 'None'}")
        print(f"   Current sender: {sender_addr_lower or 'None'}")
        print(f"   Contact identity source: {identity.get('source')}")
        print(f"   Greeting contact: {contact_name or 'generic'}")
        
        # This is the outbound recipient for automated replies, not necessarily the inbound sender.
        to_addr_lower = recipient_email
        logger.debug(
            "identity.recipient_resolved",
            extra={
                "user_id": user_id,
                "client_id": client_id,
                "thread_id": thread_id,
                "message_id": msg_id,
                "sender_addr_lower": sender_addr_lower,
                "to_addr_lower": to_addr_lower,
                "thread_emails": thread_emails,
                "external_email_found": bool(external_email),
            },
        )

        # --- flags for gating later ---
        old_row_became_nonviable = False   # set true when we move the row below divider
        new_row_created = False            # set true when we insert a new property row
        new_property_pending_created = False
        new_row_number = None              # track the newly created row number

        # NEW: Handle PDF attachments with enhanced extraction for current message only
        pdf_manifest = fetch_and_process_pdfs(headers, msg_id)
        flyer_links = []
        floorplan_links = []

        if pdf_manifest:
            # Categorize PDFs into flyers vs floorplans based on filename
            # Categorize PDF links (but don't write yet - wait until after event detection)
            for pdf in pdf_manifest:
                link = pdf.get('drive_link')
                if not link:
                    continue

                filename = pdf.get('name', '')
                if is_floorplan_filename(filename):
                    floorplan_links.append(link)
                    print(f"   📐 Categorized as floorplan: {filename}")
                else:
                    flyer_links.append(link)
                    print(f"   📄 Categorized as flyer: {filename}")

            # NOTE: PDF links will be written AFTER event detection
            # If new_property event is detected, links go to the new row, not this one
            # See deferred PDF link writing after event processing
        
        # URL exploration - find URLs in message and fetch content for AI processing only
        url_texts = []
        clean_urls = []
        url_pattern = r'https?://[^\s<>"\']+'
        urls_found = re.findall(url_pattern, _full_text)
        
        for url in urls_found[:3]:  # Limit to 3 URLs to avoid overwhelming
            clean = _sanitize_url(url)
            clean_urls.append(clean)
            fetched_text = fetch_url_as_text(clean)
            if fetched_text:
                url_texts.append({"url": clean, "text": fetched_text})

        linked_asset_manifest = fetch_and_process_linked_assets(clean_urls)
        if linked_asset_manifest:
            pdf_manifest.extend(linked_asset_manifest)
            for asset in linked_asset_manifest:
                link = asset.get("drive_link")
                if not link:
                    continue
                filename = asset.get("name", "")
                if is_floorplan_filename(filename):
                    floorplan_links.append(link)
                    print(f"   📐 Categorized linked asset as floorplan: {filename}")
                else:
                    flyer_links.append(link)
                    print(f"   📄 Categorized linked asset as flyer: {filename}")

        asset_failures = _extraction_failure_entries(pdf_manifest)
        usable_pdf_manifest = _without_extraction_failures(pdf_manifest, asset_failures)
        pdf_manifest = usable_pdf_manifest

        # Step 2: test write
        write_message_order_test(user_id, thread_id, sheet_id)

        # Step 3: get proposal using Responses API with URL content and PDF data
        # Pass column_config and extraction_fields for per-client AI configuration
        proposal = propose_sheet_updates(
            user_id, client_id, to_addr_lower, sheet_id, header, rownum, rowvals,
            thread_id, pdf_manifest=usable_pdf_manifest, url_texts=url_texts, contact_name=contact_name,
            headers=headers, column_config=column_config, extraction_fields=extraction_fields
        )

        if proposal:
            # Process updates
            if proposal.get("updates"):
                apply_result = apply_proposal_to_sheet(
                    user_id,
                    client_id,
                    sheet_id,
                    header,
                    rownum,
                    rowvals,
                    proposal,
                    column_config=column_config,
                )

                # Store applied record in sheetChangeLog
                try:
                    applied_hash = hashlib.sha256(
                        json.dumps(apply_result, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:16]

                    from datetime import datetime as dt, timezone as tz
                    now_id = dt.now(tz.utc).isoformat().replace(":", "-").replace(".", "-").replace("+00:00", "Z")
                    # Extract file IDs from PDF manifest if available
                    file_ids = [
                        p.get('file_id') or p.get('id')
                        for p in (pdf_manifest or [])
                        if p.get('file_id') or p.get('id')
                    ]

                    _fs.collection("users").document(user_id).collection("sheetChangeLog").document(f"{thread_id}__applied__{now_id}").set({
                        "clientId": client_id,
                        "email": to_addr_lower,
                        "sheetId": sheet_id,
                        "rowNumber": rownum,
                        "applied": apply_result,
                        "status": "applied",
                        "threadId": thread_id,
                        "sourceGraphMessageId": msg_id,
                        "sourceInternetMessageId": internet_message_id,
                        "replayAttemptId": operator_replay_attempt_id,
                        "createdAt": SERVER_TIMESTAMP,
                        "fileIds": file_ids,
                        "proposalHash": applied_hash,
                    })
                except Exception as e:
                    print(f"⚠️ Failed to store applied record: {e}")

                # Get property address for notifications
                property_address = get_row_anchor(rowvals, header)

                # Write client notifications (one per field)
                add_client_notifications(
                    user_id, client_id, to_addr_lower, thread_id,
                    applied_updates=apply_result.get("applied", []),
                    notes=proposal.get("notes"),
                    address=property_address
                )

                if asset_failures:
                    if not _sheet_updates_committed_non_asset_evidence(
                        apply_result,
                        column_config,
                    ):
                        _raise_on_extraction_failures(asset_failures)
                    _record_asset_extraction_warning(
                        user_id,
                        client_id,
                        thread_id,
                        internet_message_id or msg_id,
                        asset_failures,
                    )
                    print(
                        f"⚠️ Continued with broker text after {len(asset_failures)} asset "
                        "extraction warning(s); provenance was saved for review"
                    )
                    asset_failures = []

            if asset_failures:
                _raise_on_extraction_failures(asset_failures)

            # Process events from the proposal
            sheets = _sheets_client()
            row_anchor = get_row_anchor(rowvals, header)

            events = _order_events_for_processing(_proposal_events(proposal))
            # Deterministic stale-event skip: with terminalizing events ordered
            # last, an informational event (tour/call/question) for a row this
            # SAME proposal is about to kill must still be skipped — precompute
            # the outcome instead of depending on the LLM's event order.
            row_will_go_nonviable = any(
                (e or {}).get("type") == "property_unavailable"
                and _property_unavailable_event_applies_to_row(
                    e,
                    row_anchor=row_anchor,
                    message_text=_full_text,
                    unavailable_keywords=PROPERTY_UNAVAILABLE_KEYWORDS,
                )
                for e in events
            )
            print(f"\n{'='*60}")
            print(f"📋 EVENT PROCESSING: {len(events)} event(s) detected by AI")
            print(f"{'='*60}")

            if not events:
                print(f"   ℹ️ No events to process")

            for i, event in enumerate(events):
                event_type = event.get("type")
                print(f"\n🔄 Event {i+1}/{len(events)}: {event_type}")
                print(f"   Event data: {event}")

                # Build event key for deduplication
                event_key = build_event_key(event_type, event, thread_id)
                print(f"   Event key: {event_key}")

                # Check if this event was already handled - prevents duplicate notifications
                # when AI re-detects the same event from conversation history
                if is_event_handled(user_id, thread_id, event_key):
                    print(f"   ✅ Already handled, skipping")
                    continue

                # The precomputed flag only gates INFORMATIONAL events — the
                # terminalizing events themselves (ordered last) must always
                # process, else property_unavailable would self-skip.
                _stale_skip_flag = old_row_became_nonviable or (
                    row_will_go_nonviable
                    and event_type not in _TERMINALIZING_EVENT_TYPES
                )
                if _should_skip_event_after_original_row_terminalized(
                    event_type,
                    old_row_became_nonviable=_stale_skip_flag,
                ):
                    print(
                        "   ℹ️ Skipping stale original-row event after non-viable move; "
                        "replacement/opt-out events will continue."
                    )
                    mark_event_handled(user_id, thread_id, event_key, msg_id, None)
                    continue

                print(f"   ➡️ Processing event...")

                if event_type == "call_requested":
                    # Check if phone number is mentioned in the message
                    phone_pattern = r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})'
                    phone_match = re.search(phone_pattern, _full_text)
                    phone_number = phone_match.group(0) if phone_match else None
                    
                    # Create action_needed notification
                    try:
                        meta = {
                            "reason": "call_requested",
                            "details": "Call requested in conversation",
                            "replyToMessageId": msg_id,  # Graph API message ID for sending reply
                            **_source_message_identity_meta(msg_id, internet_message_id, msg),
                        }
                        if phone_number:
                            meta["phoneNumber"] = phone_number
                            meta["details"] = f"Call requested - phone number provided: {phone_number}"
                        
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=to_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta=meta,
                            dedupe_key=f"call_requested:{thread_id}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"📞 Created call_requested notification" + (f" with phone: {phone_number}" if phone_number else ""))

                        # Update thread status to paused - waiting for user to handle call
                        update_thread_status(user_id, thread_id, THREAD_STATUS["paused"], "call_requested")

                        # A call request ALWAYS escalates to the operator — never auto-reply,
                        # whether or not a phone number was included. LIVE break: a broker who
                        # asked to "talk over the phone" (no number) fell through to the
                        # phone-number-ask AND the missing-fields auto-reply paths below,
                        # talking over the human handoff (only an incidental reply-all filter
                        # stopped delivery). Suppress the response unconditionally so the
                        # operator handles the call; this matches the deterministic guard that
                        # already nulls response_email for call_requested.
                        proposal["skip_response"] = True
                        if phone_number:
                            print(f"📞 Phone number found - skipping email response, notification only")
                        else:
                            print(f"📞 No phone number - escalating to operator, skipping email response")
                        # Highlight blue - row needs user attention (paused)
                        try:
                            highlight_row(sheet_id, rownum, ROW_HIGHLIGHT_BLUE)
                        except Exception as e:
                            print(f"⚠️ Could not highlight row: {e}")
                    except Exception as e:
                        print(f"❌ Failed to write call_requested notification: {e}")

                elif event_type == "tour_requested":
                    # Broker offered a tour - create notification with suggested response
                    try:
                        if not _tour_actions_allowed(user_id):
                            mark_event_handled(user_id, thread_id, event_key, msg_id, None)
                            proposal["skip_response"] = True
                            print(
                                "🏠 Tour actions disabled for this user; "
                                "marked event handled without notification or reply draft"
                            )
                            continue

                        tour_message_text = _clean_tour_signal_text(_text_for_ai or _full_text)
                        clean_event = dict(event)
                        clean_event["question"] = _clean_tour_signal_text(
                            event.get("question") or tour_message_text
                        ) or tour_message_text
                        tour_reply_classification = _classify_tour_invite_reply(
                            tour_message_text,
                            event=clean_event,
                            thread_data=thread_data,
                            contact_name=contact_name,
                            recipient_email=to_addr_lower,
                        )

                        if not _tour_event_needs_operator_action(clean_event, tour_message_text, thread_data):
                            mark_event_handled(user_id, thread_id, event_key, msg_id, None)
                            if tour_reply_classification.get("canCloseThread"):
                                update_thread_status(user_id, thread_id, THREAD_STATUS["completed"], "tour_confirmed")
                                if thread_ref:
                                    thread_ref.update(
                                        _build_tour_invite_reply_state_update(tour_reply_classification)
                                    )
                                complete_threads_for_row(
                                    user_id,
                                    rownum,
                                    client_id=client_id,
                                    reason="tour_confirmed",
                                )
                                _clear_thread_action_notifications(user_id, client_id, thread_id)
                                _maybe_mark_client_completed(user_id, client_id)
                                proposal["skip_response"] = True
                            print(f"🏠 Skipped non-actionable tour event: {tour_reply_classification.get('outcome')}")
                            continue

                        question = clean_event.get("question") or "Tour requested"
                        suggested_email = clean_event.get("suggestedEmail", "")
                        reason = "tour_requested"
                        details = "Tour/showing offered - review and approve response"

                        if tour_reply_classification.get("outcome") in {"alternate_requested", "declined", "tour_unavailable"}:
                            if (
                                tour_reply_classification.get("outcome") == "alternate_requested"
                                and tour_reply_classification.get("alternateTimes")
                            ):
                                tour_schedule = _load_sibling_tour_schedule(
                                    user_id,
                                    client_id,
                                    thread_id,
                                    thread_data,
                                )
                                schedule_decision = evaluate_alternate_tour_time(
                                    tour_schedule,
                                    thread_id,
                                    tour_reply_classification["alternateTimes"][0],
                                )
                                tour_reply_classification = {
                                    **tour_reply_classification,
                                    "scheduleDecision": schedule_decision,
                                    "suggestedEmail": build_schedule_aware_tour_reply(
                                        contact_name,
                                        to_addr_lower,
                                        thread_data,
                                        schedule_decision,
                                    ),
                                }

                            if tour_reply_classification.get("outcome") == "alternate_requested":
                                reason = "tour_reschedule_requested"
                            elif tour_reply_classification.get("outcome") == "tour_unavailable":
                                reason = "tour_unavailable"
                            else:
                                reason = "tour_slot_declined"
                            details = tour_reply_classification.get("details") or details
                            question = details
                            suggested_email = tour_reply_classification.get("suggestedEmail") or suggested_email
                            if thread_ref:
                                thread_ref.update(
                                    _build_tour_invite_reply_state_update(tour_reply_classification)
                                )

                        # If AI didn't generate a suggested email, create a default one
                        if not suggested_email:
                            suggested_email = _build_tour_fallback_suggested_email(
                                contact_name=contact_name,
                                recipient_email=to_addr_lower,
                                question=question,
                            )
                        suggested_email = _sanitize_dashboard_suggested_email_body(suggested_email)

                        meta = {
                            "reason": reason,
                            "details": details,
                            "question": question,
                            "originalMessage": tour_message_text[:500],
                            "status": "pending_response",  # Not pending_approval - no row creation needed
                            "replyToMessageId": msg_id,  # Graph API message ID for sending reply
                            **_source_message_identity_meta(msg_id, internet_message_id, msg),
                            "contactName": contact_name,  # For [NAME] replacement in frontend
                            "tourReplyClassification": tour_reply_classification,
                            "suggestedEmail": {
                                "to": [to_addr_lower],
                                "subject": f"RE: {row_anchor}" if row_anchor else "RE: Property Tour",
                                "body": suggested_email
                            }
                        }

                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=to_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta=meta,
                            dedupe_key=(
                                f"tour_reply:{thread_id}:{msg_id}"
                                if reason in {"tour_reschedule_requested", "tour_slot_declined", "tour_unavailable"}
                                else f"tour_requested:{thread_id}"
                            )
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"🏠 Created {reason} notification with suggested email")

                        # Update thread status to paused - waiting for user to handle tour
                        update_thread_status(user_id, thread_id, THREAD_STATUS["paused"], reason)

                        # Don't auto-respond - user will send the approved email
                        proposal["skip_response"] = True
                        # Highlight blue - row needs user attention (paused)
                        try:
                            highlight_row(sheet_id, rownum, ROW_HIGHLIGHT_BLUE)
                        except Exception as e:
                            print(f"⚠️ Could not highlight row: {e}")

                    except Exception as e:
                        print(f"❌ Failed to write tour_requested notification: {e}")

                elif event_type == "needs_user_input":
                    # Client asked a question or made a request the AI cannot handle
                    # Create notification and skip auto-response
                    try:
                        reason = event.get("reason", "unclear")
                        question = event.get("question", "User input required")

                        reason_labels = {
                            "client_question": "Client asked about your requirements",
                            "scheduling": "Tour/meeting scheduling request",
                            "negotiation": "Price or term negotiation",
                            "confidential": "Asked about client identity",
                            "legal_contract": "Contract or legal question",
                            "unclear": "Message needs your review"
                        }

                        meta = {
                            "reason": f"needs_user_input:{reason}",
                            "details": reason_labels.get(reason, reason_labels["unclear"]),
                            "question": question,
                            "originalMessage": _full_text[:500],  # Include message context
                            "replyToMessageId": msg_id,  # Graph API message ID for sending reply
                            **_source_message_identity_meta(msg_id, internet_message_id, msg),
                            "contactName": contact_name  # For [NAME] replacement in frontend
                        }

                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=to_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta=meta,
                            dedupe_key=f"needs_user_input:{thread_id}:{reason}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"⚠️ Created needs_user_input notification (reason: {reason})")

                        # Update thread status to paused - waiting for user action
                        update_thread_status(user_id, thread_id, THREAD_STATUS["paused"], f"needs_user_input:{reason}")

                        # Only skip response if AI didn't generate one
                        # If AI generated a response (e.g., acknowledging info while deferring the question), send it
                        if not proposal.get("response_email"):
                            proposal["skip_response"] = True
                            print(f"   ℹ️ No AI response generated, will skip email")
                            # Highlight blue - row needs user attention (paused)
                            try:
                                highlight_row(sheet_id, rownum, ROW_HIGHLIGHT_BLUE)
                            except Exception as e:
                                print(f"⚠️ Could not highlight row: {e}")
                        else:
                            print(f"   ℹ️ AI generated response, will send acknowledgment email")
                            # Still highlight blue since thread is paused
                            try:
                                highlight_row(sheet_id, rownum, ROW_HIGHLIGHT_BLUE)
                            except Exception as e:
                                print(f"⚠️ Could not highlight row: {e}")

                    except Exception as e:
                        print(f"❌ Failed to write needs_user_input notification: {e}")

                elif event_type == "property_unavailable":
                    if not _property_unavailable_event_applies_to_row(
                        event,
                        row_anchor=row_anchor,
                        message_text=_full_text,
                        unavailable_keywords=PROPERTY_UNAVAILABLE_KEYWORDS,
                    ):
                        print(
                            "ℹ️ Skipping property_unavailable event because it does not match "
                            f"current row anchor: {row_anchor or 'unknown row'}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, None)
                        continue

                    # Check if row is already below NON-VIABLE divider - if so, skip processing
                    try:
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        if _is_row_below_nonviable(sheets, sheet_id, tab_title, rownum):
                            print(
                                f"ℹ️ Row {rownum} already below NON-VIABLE divider; "
                                "terminalizing thread state without moving the sheet row"
                            )
                            stopped_thread_count = stop_threads_for_row(
                                user_id,
                                rownum,
                                client_id=client_id,
                                reason="property_unavailable",
                            )
                            update_thread_status(user_id, thread_id, THREAD_STATUS["stopped"], "property_unavailable")
                            unavailable_thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
                            unavailable_thread_ref.set({
                                "nonViableAt": SERVER_TIMESTAMP,
                                "nonViableReason": event.get("reason") or "already_below_nonviable",
                                "followUpStatus": "stopped",
                                "updatedAt": SERVER_TIMESTAMP,
                            }, merge=True)
                            mark_event_handled(user_id, thread_id, event_key, msg_id, None)
                            old_row_became_nonviable = True
                            proposal["skip_response"] = True
                            print(
                                "🛑 Stopped already non-viable row thread(s): "
                                f"{stopped_thread_count} row root(s) plus current thread"
                            )
                            if not any((evt or {}).get("type") == "new_property" for evt in events):
                                _maybe_mark_client_completed(user_id, client_id)
                            continue
                    except Exception as e:
                        print(f"⚠️ Failed to check if row is below divider: {e}")
                        # Continue processing if we can't determine position
                    
                    # Move row below divider and create notification
                    # Trust AI detection - GPT-5.2 already analyzed the message context
                    message_content = _full_text.lower()

                    # Find keyword for logging purposes (optional - AI already detected unavailability)
                    found_keyword = next((kw for kw in PROPERTY_UNAVAILABLE_KEYWORDS if kw in message_content), "AI-detected unavailability")
                    print(f"🔍 Processing property_unavailable event (trigger: '{found_keyword}')")

                    try:
                            
                            divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                            new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)

                            # Sync thread rowNumbers after row movement to prevent stale anchors
                            sync_thread_row_numbers_after_move(user_id, rownum, divider_row, new_rownum, client_id=client_id)

                            stopped_thread_count = stop_threads_for_row(
                                user_id,
                                new_rownum,
                                client_id=client_id,
                                reason="property_unavailable",
                            )
                            if stopped_thread_count == 0:
                                update_thread_status(user_id, thread_id, THREAD_STATUS["stopped"], "property_unavailable")
                            unavailable_thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
                            unavailable_thread_ref.set({
                                "rowNumber": new_rownum,
                                "nonViableAt": SERVER_TIMESTAMP,
                                "nonViableReason": found_keyword,
                                "followUpStatus": "stopped",
                                "updatedAt": SERVER_TIMESTAMP,
                            }, merge=True)

                            # Add comment to the best available notes column explaining why it was marked unviable.
                            try:
                                comments_col_idx = find_notes_comment_column_index(header)

                                if comments_col_idx:
                                    # Get current date for the comment
                                    from datetime import datetime
                                    current_date = datetime.now().strftime("%m/%d/%Y")
                                    
                                    # Create comment explaining why property was marked unviable.
                                    unavailable_comment = _build_property_unavailable_comment(
                                        current_date,
                                        found_keyword,
                                        events,
                                    )
                                    
                                    # Get existing comments to append to them
                                    existing_resp = sheets.spreadsheets().values().get(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}"
                                    ).execute()
                                    existing_comment = ""
                                    if existing_resp.get("values"):
                                        existing_comment = existing_resp["values"][0][0] if existing_resp["values"][0] else ""
                                    
                                    # Combine existing and new comments
                                    if existing_comment.strip():
                                        final_comment = f"{existing_comment.strip()} | {unavailable_comment}"
                                    else:
                                        final_comment = unavailable_comment
                                    
                                    # Update the comments cell
                                    sheets.spreadsheets().values().update(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}",
                                        valueInputOption="RAW",
                                        body={"values": [[final_comment]]}
                                    ).execute()
                                    
                                    print(f"💬 Added unavailability comment: {unavailable_comment}")
                                else:
                                    print(f"⚠️ Could not find notes column to add unavailability reason")
                            except Exception as comment_error:
                                print(f"⚠️ Failed to add unavailability comment: {comment_error}")
                            
                            # Reformat after move
                            format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                            
                            # mark the row as non-viable for this run
                            old_row_became_nonviable = True
                            rownum = new_rownum  # keep our pointer accurate if used later

                            # Clear highlight - row is NON-VIABLE, no longer under system control
                            try:
                                clear_row_highlight(sheet_id, new_rownum)
                            except Exception as e:
                                print(f"⚠️ Could not clear row highlight: {e}")

                            # Create notification only after successful move
                            notif_id = write_notification(
                                user_id, client_id,
                                kind="property_unavailable",
                                priority="important",
                                email=to_addr_lower,
                                thread_id=thread_id,
                                row_number=new_rownum,
                                row_anchor=row_anchor,
                                meta={"address": event.get("address", ""), "city": event.get("city", "")},
                                dedupe_key=f"property_unavailable:{thread_id}:{new_rownum}:moved"
                            )
                            mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                            print(f"🚫 Moved property to non-viable and created notification")
                            if not any((evt or {}).get("type") == "new_property" for evt in events):
                                _maybe_mark_client_completed(user_id, client_id)
                    except Exception as e:
                        print(f"❌ Failed to handle property_unavailable: {e}")
                        import traceback
                        traceback.print_exc()
                        _record_ai_processing_failure(
                            user_id,
                            client_id,
                            thread_id,
                            msg_id,
                            f"property_unavailable_event_failed:{e}",
                        )
                        raise RetryableProcessingError(f"property_unavailable event failed: {e}")

                elif event_type == "new_property":
                    try:
                        address = _event_text(event, "address")
                        city = _event_text(event, "city")
                        # AI can provide specific email for new property contact (different from current sender)
                        new_property_email = _event_text(event, "email").lower() or to_addr_lower
                        # Extract contact name if AI provided one (e.g., "Joe" from "email Joe at joe@email.com")
                        new_contact_name = _event_text(event, "contactName")

                        # Determine if this is a different contact than the original sender
                        is_different_contact = new_property_email != to_addr_lower

                        # Get the referrer name (the person who suggested this new contact)
                        # Use the leasing contact from the current row, or extract from sender email
                        referrer_name = ""
                        if is_different_contact:
                            # Try to get leasing contact name from current row first
                            idx_map_temp = _header_index_map(header)
                            leasing_contact_idx_temp = idx_map_temp.get("leasing contact")
                            if leasing_contact_idx_temp and (leasing_contact_idx_temp - 1) < len(rowvals):
                                referrer_name = (rowvals[leasing_contact_idx_temp - 1] or "").strip()
                            # Fallback: extract first name from sender email (before @ and first part)
                            if not referrer_name:
                                email_name = sender_addr_lower.split('@')[0]
                                # Handle formats like "john.doe" or "jdoe"
                                referrer_name = email_name.split('.')[0].title()

                        if is_different_contact:
                            print(f"📧 New property has different contact: {new_property_email} (referred by: {referrer_name or sender_addr_lower})")
                            if new_contact_name:
                                print(f"   👤 Contact name extracted: {new_contact_name}")

                        # Skip if no address provided
                        if not address or not address.strip():
                            print("⚠️ No address provided for new_property event, skipping")
                            continue

                        # Check if property already exists in sheet
                        tab_title = _get_first_tab_title(sheets, sheet_id)

                        # Build header index map to find address/city columns
                        idx_map = _header_index_map(header)
                        property_exists = _property_exists_in_sheet(
                            sheets,
                            sheet_id,
                            tab_title,
                            header,
                            address,
                            city,
                        )

                        if property_exists:
                            continue  # Skip this event - property already exists

                        # Property doesn't exist - store for approval (DON'T create row yet)
                        link = _event_text(event, "link")
                        notes = _event_text(event, "notes")

                        # Fetch client criteria from Firestore for AI email generation
                        client_criteria = ""
                        try:
                            client_doc = _fs.collection("users").document(user_id).collection("clients").document(client_id).get()
                            if client_doc.exists:
                                client_data = client_doc.to_dict() or {}
                                # Get primary criteria (the email script template)
                                client_criteria = client_data.get("criteria", "")
                                print(f"📋 Fetched client criteria for AI generation ({len(client_criteria)} chars)")
                        except Exception as ce:
                            print(f"⚠️ Could not fetch client criteria: {ce}")

                        # Extract leasing company and contact from current row for later use
                        leasing_company = ""
                        leasing_contact = ""
                        leasing_company_idx = idx_map.get("leasing company") or idx_map.get("leasing company ")
                        leasing_contact_idx = idx_map.get("leasing contact")

                        if leasing_company_idx and (leasing_company_idx - 1) < len(rowvals):
                            leasing_company = rowvals[leasing_company_idx - 1] or ""

                        if leasing_contact_idx and (leasing_contact_idx - 1) < len(rowvals):
                            leasing_contact = rowvals[leasing_contact_idx - 1] or ""

                        # Build suggested (not sent) email payload
                        # Use the specific contact email if AI provided one, otherwise use the current sender

                        email_payload = build_new_property_suggested_email(
                            address=address,
                            city=city,
                            to_email=new_property_email,
                            contact_name=new_contact_name,
                            referrer_name=referrer_name if is_different_contact else "",
                            client_id=client_id,
                        )
                        email_payload = _sanitize_dashboard_suggested_email_payload(email_payload)

                        if should_skip_original_reply_for_new_property_referral(
                            original_contact_email=to_addr_lower,
                            new_property_email=new_property_email,
                        ):
                            proposal["skip_response"] = True

                        # Create ACTION_NEEDED notification for approval (no row created yet)
                        property_image_candidate = select_property_image_candidate(
                            pdf_manifest,
                            address=address,
                            city=city,
                            source_url=link,
                        )
                        property_images_meta = [property_image_candidate] if property_image_candidate else []
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=new_property_email,  # Use the specific contact for the new property
                            thread_id=thread_id,   # keep context with original thread
                            row_number=None,  # No row yet
                            row_anchor=f"{address}, {city}" if city else address,
                            meta={
                                "reason": "new_property_pending_approval",
                                "status": "pending_approval",
                                **_source_message_identity_meta(msg_id, internet_message_id, msg),
                                "address": address,
                                "city": city,
                                "link": link,
                                "notes": notes,
                                "leasingCompany": leasing_company,
                                "leasingContact": leasing_contact,
                                "brokerEmail": new_property_email,  # Email for the new property contact
                                "contactName": new_contact_name,  # Extracted full name (e.g., "Joe Smith" from "email Joe Smith at...")
                                "referrerName": referrer_name if is_different_contact else "",  # Who suggested this contact
                                "isDifferentContact": is_different_contact,  # Flag for frontend to know context
                                "sheetId": sheet_id,
                                "tabTitle": tab_title,
                                "suggestedEmail": email_payload,
                                "conversationContext": {
                                    "threadId": thread_id,
                                    "originalMessage": _full_text[:500] if _full_text else ""  # First 500 chars of original message
                                },
                                # Client criteria for AI email generation on frontend
                                "clientCriteria": client_criteria,
                                # PDF links to be applied to new row when created
                                "pdfLinks": [p.get('drive_link') for p in (pdf_manifest or []) if p.get('drive_link')],
                                # Full PDF manifest for AI extraction when new property row is created
                                # Includes extracted text so we can pre-fill columns
                                "pdfManifest": [
                                    {
                                        "name": p.get("name"),
                                        "text": p.get("text", "")[:5000],  # Limit text to 5KB per PDF
                                        "drive_link": p.get("drive_link"),
                                        "id": p.get("file_id") or p.get("id"),  # OpenAI file ID for re-processing if needed
                                        "property_image_url": p.get("property_image_url"),
                                        "property_image_source": p.get("property_image_source"),
                                        "property_image_source_type": p.get("property_image_source_type"),
                                        "property_image_meta": p.get("property_image_meta"),
                                    }
                                    for p in (pdf_manifest or [])
                                ],
                                # Hosted property image previews for the eventual new row.
                                # This intentionally excludes raw extracted images/base64.
                                "propertyImages": property_images_meta,
                            },
                            dedupe_key=f"new_property_pending:{thread_id}:{address}:{city}:{new_property_email}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        new_property_pending_created = True
                        print(f"🏢 Created new property pending approval notification (no row created yet)")

                        # Let the AI-generated response_email flow through normally
                        # The AI prompt instructs it to generate a contextual thank-you + "I'll follow up separately" message
                        # when both property_unavailable and new_property events are detected
                        if proposal.get("response_email"):
                            print(f"   📧 AI generated contextual response for new property scenario")
                        else:
                            print(f"   ℹ️ No AI response generated (user will handle via notification)")

                    except Exception as e:
                        print(f"❌ Failed to handle new_property: {e}")
                        _record_ai_processing_failure(
                            user_id,
                            client_id,
                            thread_id,
                            msg_id,
                            f"new_property_event_failed:{e}",
                        )
                        raise RetryableProcessingError(f"new_property event failed: {e}")
                
                elif event_type == "close_conversation":
                    # Mark thread as closed and notify user
                    try:
                        from datetime import datetime

                        close_reason = _close_reason_from_event(event)
                        if not _close_event_can_bypass_missing_fields(event):
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            current_resp = sheets.spreadsheets().values().get(
                                spreadsheetId=sheet_id,
                                range=f"{tab_title}!{rownum}:{rownum}"
                            ).execute()
                            current_row = current_resp.get("values", [[]])[0] if current_resp.get("values") else []
                            if len(current_row) < len(header):
                                current_row.extend([""] * (len(header) - len(current_row)))
                            missing_for_close = check_missing_required_fields(current_row, header, column_config)
                            if missing_for_close:
                                print(
                                    f"⚠️ Ignoring close_conversation ({close_reason}) because required fields are still missing: {missing_for_close}"
                                )
                                continue

                        # Update thread status to completed using the status system
                        update_thread_status(user_id, thread_id, THREAD_STATUS["completed"], close_reason)
                        complete_threads_for_row(
                            user_id,
                            rownum,
                            client_id=client_id,
                            reason=close_reason,
                        )
                        # Also update legacy fields for backwards compatibility
                        if thread_ref:
                            thread_ref.update({
                                "closedAt": datetime.now().isoformat(),
                                "closeReason": close_reason,
                                "followUpStatus": "stopped",
                                "followUpConfig.processingBy": None,
                                "followUpConfig.processingAt": None,
                            })
                            print(f"💬 Thread marked as completed")

                        # Create notification for user awareness
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="conversation_closed",
                            priority="normal",
                            email=to_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": close_reason,
                                "details": "Broker indicated conversation is complete",
                                "lastMessage": _full_text[:300] if _full_text else ""
                            },
                            dedupe_key=f"conversation_closed:{thread_id}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)

                        # Only skip response if AI didn't generate a closing email
                        # If AI generated a response (e.g., thanking broker), send it before closing
                        if not proposal.get("response_email"):
                            proposal["skip_response"] = True
                            print(f"   ℹ️ No closing email generated, skipping response")
                        else:
                            print(f"   📧 Will send AI-generated closing email")
                        # Clear highlight - row is complete
                        try:
                            clear_row_highlight(sheet_id, rownum)
                        except Exception as e:
                            print(f"⚠️ Could not clear row highlight: {e}")
                        _maybe_mark_client_completed(user_id, client_id)

                    except Exception as e:
                        print(f"❌ Failed to handle close_conversation: {e}")

                elif event_type == "contact_optout":
                    # Contact explicitly doesn't want further communication
                    try:
                        reason = event.get("reason", "not_interested")

                        reason_labels = {
                            "not_interested": "Contact is not interested",
                            "unsubscribe": "Contact requested to be removed from mailing list",
                            "do_not_contact": "Contact requested no further contact",
                            "no_tenant_reps": "Contact doesn't work with tenant rep brokers",
                            "direct_only": "Contact only deals directly with tenants",
                            "hostile": "Contact responded negatively - requires review"
                        }

                        # Store opt-out in Firestore for future reference
                        _store_contact_optout(user_id, sender_addr_lower, reason, thread_id)

                        # Move row to NON-VIABLE with reason
                        try:
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            if not _is_row_below_nonviable(sheets, sheet_id, tab_title, rownum):
                                divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                                new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)

                                # Sync thread rowNumbers after row movement
                                sync_thread_row_numbers_after_move(user_id, rownum, divider_row, new_rownum, client_id=client_id)

                                # Add comment explaining why
                                from datetime import datetime
                                current_date = datetime.now().strftime("%m/%d/%Y")
                                optout_comment = f"[{current_date}] Contact opted out: {reason_labels.get(reason, reason)}"

                                comments_col_idx = find_client_comment_column_index(header)

                                if comments_col_idx:
                                    existing_resp = sheets.spreadsheets().values().get(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}"
                                    ).execute()
                                    existing_comment = ""
                                    if existing_resp.get("values"):
                                        existing_comment = existing_resp["values"][0][0] if existing_resp["values"][0] else ""

                                    final_comment = f"{existing_comment.strip()} | {optout_comment}" if existing_comment.strip() else optout_comment

                                    sheets.spreadsheets().values().update(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}",
                                        valueInputOption="RAW",
                                        body={"values": [[final_comment]]}
                                    ).execute()

                                format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                                old_row_became_nonviable = True
                                rownum = new_rownum
                                print(f"🚫 Moved opted-out contact row to NON-VIABLE")

                                # Clear highlight - row is NON-VIABLE
                                try:
                                    clear_row_highlight(sheet_id, new_rownum)
                                except Exception as e:
                                    print(f"⚠️ Could not clear row highlight: {e}")
                        except Exception as move_err:
                            print(f"⚠️ Could not move row to NON-VIABLE: {move_err}")

                        update_thread_status(user_id, thread_id, THREAD_STATUS["stopped"], f"contact_optout:{reason}")
                        optout_thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
                        optout_thread_ref.set({
                            "rowNumber": rownum,
                            "optedOutAt": SERVER_TIMESTAMP,
                            "optOutReason": reason,
                            "followUpStatus": "stopped",
                            "followUpConfig.processingBy": None,
                            "followUpConfig.processingAt": None,
                            "updatedAt": SERVER_TIMESTAMP,
                        }, merge=True)

                        # Create notification for user awareness
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=sender_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": f"contact_optout:{reason}",
                                "details": reason_labels.get(reason, reason),
                                "contact": sender_addr_lower,
                                "contactName": contact_name,  # For [NAME] replacement in frontend
                                "originalMessage": _full_text[:500]
                            },
                            dedupe_key=f"contact_optout:{thread_id}:{sender_addr_lower}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"🚫 Contact opted out ({reason}): {sender_addr_lower}")

                        # Skip auto-response - don't email someone who asked not to be contacted
                        proposal["skip_response"] = True

                    except Exception as e:
                        print(f"❌ Failed to handle contact_optout: {e}")

                elif event_type == "wrong_contact":
                    # This isn't the right person to contact
                    try:
                        reason = event.get("reason", "wrong_person")
                        suggested_contact = event.get("suggestedContact", "")
                        suggested_email = event.get("suggestedEmail", "")
                        suggested_phone = event.get("suggestedPhone", "")

                        reason_labels = {
                            "no_longer_handles": "Contact no longer handles this property",
                            "wrong_person": "Wrong contact for this property",
                            "forwarded": "Message being forwarded to correct person",
                            "left_company": "Contact no longer with company"
                        }

                        # Build details string
                        details = reason_labels.get(reason, reason)
                        if suggested_contact:
                            details += f". Suggested contact: {suggested_contact}"
                        if suggested_email:
                            details += f" ({suggested_email})"
                        if suggested_phone:
                            details += f" - {suggested_phone}"

                        suggested_email_payload = build_wrong_contact_suggested_email(
                            original_contact=sender_addr_lower,
                            suggested_contact=suggested_contact,
                            suggested_email=suggested_email,
                            row_anchor=row_anchor,
                            referrer_name=contact_name,
                        )
                        suggested_email_payload = _sanitize_dashboard_suggested_email_payload(
                            suggested_email_payload
                        )
                        logger.debug(
                            "notification.wrong_contact",
                            extra={
                                "user_id": user_id,
                                "client_id": client_id,
                                "thread_id": thread_id,
                                "message_id": msg_id,
                                "reason": reason,
                                "original_contact": sender_addr_lower,
                                "suggested_contact": suggested_contact,
                                "suggested_email": suggested_email,
                                "payload_to": suggested_email_payload.get("to", []),
                            },
                        )

                        # Create actionable notification
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=sender_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": f"wrong_contact:{reason}",
                                "details": details,
                                **_source_message_identity_meta(msg_id, internet_message_id, msg),
                                "originalContact": sender_addr_lower,
                                "contactName": contact_name,  # For [NAME] replacement in frontend
                                "suggestedContact": suggested_contact,
                                "suggestedEmail": suggested_email_payload,
                                "suggestedPhone": suggested_phone,
                                "originalMessage": _full_text[:500]
                            },
                            dedupe_key=f"wrong_contact:{thread_id}:{suggested_email or suggested_contact or sender_addr_lower}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"👤 Wrong contact detected ({reason}) - redirect to: {suggested_contact or 'unknown'} ({suggested_email or 'no email'})")

                        # For "forwarded" (someone covering), don't block - just notify as FYI
                        # For other cases (wrong_person, left_company, no_longer_handles), block and pause
                        if reason == "forwarded":
                            # Just an FYI - person is covering temporarily, conversation continues normally
                            print(f"   ℹ️ Forwarded case - continuing conversation (someone covering)")
                        else:
                            # Skip auto-response - don't reply to wrong person
                            proposal["skip_response"] = True
                            # Update thread status to paused
                            update_thread_status(user_id, thread_id, THREAD_STATUS["paused"], f"wrong_contact:{reason}")
                            # Highlight blue - row needs user attention (paused)
                            try:
                                highlight_row(sheet_id, rownum, ROW_HIGHLIGHT_BLUE)
                            except Exception as e:
                                print(f"⚠️ Could not highlight row: {e}")

                    except Exception as e:
                        print(f"❌ Failed to handle wrong_contact: {e}")

                elif event_type == "property_issue":
                    # Property has a notable issue/concern that the user should be aware of
                    try:
                        issue = event.get("issue", "Unknown issue")
                        severity = event.get("severity", "major")  # critical, major, minor

                        severity_labels = {
                            "critical": "Critical Issue (health/safety concern)",
                            "major": "Major Issue (significant concern)",
                            "minor": "Minor Issue (cosmetic/inconvenience)"
                        }

                        priority = "urgent" if severity == "critical" else "important"

                        # Add issue to comments column
                        try:
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            comments_col_idx = find_client_comment_column_index(header)

                            if comments_col_idx:
                                from datetime import datetime
                                current_date = datetime.now().strftime("%m/%d/%Y")
                                issue_comment = f"[{current_date}] ⚠️ PROPERTY ISSUE ({severity.upper()}): {issue}"

                                existing_resp = sheets.spreadsheets().values().get(
                                    spreadsheetId=sheet_id,
                                    range=f"{tab_title}!{chr(64 + comments_col_idx)}{rownum}"
                                ).execute()
                                existing_comment = ""
                                if existing_resp.get("values"):
                                    existing_comment = existing_resp["values"][0][0] if existing_resp["values"][0] else ""

                                final_comment = f"{existing_comment.strip()} | {issue_comment}" if existing_comment.strip() else issue_comment

                                sheets.spreadsheets().values().update(
                                    spreadsheetId=sheet_id,
                                    range=f"{tab_title}!{chr(64 + comments_col_idx)}{rownum}",
                                    valueInputOption="RAW",
                                    body={"values": [[final_comment]]}
                                ).execute()
                                print(f"💬 Added property issue comment: {issue}")
                        except Exception as comment_err:
                            print(f"⚠️ Could not add issue comment: {comment_err}")

                        # Create notification to alert user
                        notif_id = write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority=priority,
                            email=sender_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": f"property_issue:{severity}",
                                "issue": issue,
                                "severity": severity,
                                "severityLabel": severity_labels.get(severity, severity),
                                **_source_message_identity_meta(msg_id, internet_message_id, msg),
                                "contact": sender_addr_lower,
                                "contactName": contact_name,  # For [NAME] replacement in frontend
                                "originalMessage": _full_text[:500],
                                "question": f"Property has an issue: {issue}",  # For AI chat context
                                "replyToMessageId": msg_id  # For sending reply
                            },
                            dedupe_key=f"property_issue:{thread_id}:{issue[:50]}"
                        )
                        mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)
                        print(f"⚠️ Property issue detected ({severity}): {issue}")

                    except Exception as e:
                        print(f"❌ Failed to handle property_issue: {e}")

            # DEFERRED PDF LINK WRITING: Only write to current row if NOT a new_property scenario
            # If new_property was detected, the PDFs belong to the new property, not this row
            has_new_property_path = _has_new_property_path(
                events,
                new_row_created=new_row_created,
                new_property_pending_created=new_property_pending_created,
            )
            if pdf_manifest and not has_new_property_path:
                try:
                    sheets = _sheets_client()
                    pdf_link_updates_for_results: Dict[str, List[str]] = {}
                    property_image_candidate = select_property_image_candidate(
                        pdf_manifest,
                        address=row_anchor,
                    )
                    property_image_updates_for_results: Dict[str, List[str]] = {}

                    if flyer_links:
                        added_flyer_links = append_links_to_flyer_link_column(sheets, sheet_id, header, rownum, flyer_links)
                        if added_flyer_links:
                            pdf_link_updates_for_results["Flyer / Link"] = added_flyer_links
                            logger.debug(
                                "sheet.ai_meta_append",
                                extra={
                                    "spreadsheet_id": sheet_id,
                                    "rownum": rownum,
                                    "column": "Flyer / Link",
                                    "value": "\n".join(added_flyer_links),
                                    "override": False,
                                    "source": "pdf_link_write",
                                },
                            )
                            _append_ai_meta(sheets, sheet_id, rownum, "Flyer / Link", "\n".join(added_flyer_links), override=False)
                        print(f"   🔗 Applied {len(flyer_links)} flyer link(s) to current row")

                    # Delay between writes to avoid Google Sheets API rate limits
                    if flyer_links and floorplan_links:
                        print("   ⏳ Waiting 30s before next sheet write to avoid rate limits...")
                        time.sleep(30)

                    if floorplan_links:
                        added_floorplan_links = append_links_to_floorplan_column(sheets, sheet_id, header, rownum, floorplan_links)
                        if added_floorplan_links:
                            pdf_link_updates_for_results["Floorplan"] = added_floorplan_links
                            logger.debug(
                                "sheet.ai_meta_append",
                                extra={
                                    "spreadsheet_id": sheet_id,
                                    "rownum": rownum,
                                    "column": "Floorplan",
                                    "value": "\n".join(added_floorplan_links),
                                    "override": False,
                                    "source": "pdf_link_write",
                                },
                            )
                            _append_ai_meta(sheets, sheet_id, rownum, "Floorplan", "\n".join(added_floorplan_links), override=False)
                        print(f"   📐 Applied {len(floorplan_links)} floorplan link(s) to current row")

                    property_image_updates = build_property_image_sheet_updates(
                        header,
                        rowvals,
                        property_image_candidate,
                    )
                    if property_image_updates:
                        property_image_updates_for_results = write_property_image_columns(
                            sheets,
                            sheet_id,
                            header,
                            rownum,
                            property_image_updates,
                        )
                        for column, values in property_image_updates_for_results.items():
                            value = "\n".join(values or [])
                            if not value:
                                continue
                            logger.debug(
                                "sheet.ai_meta_append",
                                extra={
                                    "spreadsheet_id": sheet_id,
                                    "rownum": rownum,
                                    "column": column,
                                    "value": value,
                                    "override": False,
                                    "source": "property_image_write",
                                },
                            )
                            _append_ai_meta(sheets, sheet_id, rownum, column, value, override=False)
                        if property_image_updates_for_results:
                            print("   🖼️ Applied hosted property image preview to current row")

                    # Re-read header in case we just created columns
                    if flyer_links or floorplan_links or property_image_updates_for_results:
                        try:
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            header = _read_header_row2(sheets, sheet_id, tab_title)
                            format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                        except Exception as _e:
                            print(f"ℹ️ Skipped re-format after link append: {_e}")

                    if pdf_link_updates_for_results:
                        _store_pdf_link_sheet_change(
                            user_id,
                            client_id,
                            sheet_id,
                            header,
                            rownum,
                            rowvals,
                            thread_id,
                            to_addr_lower,
                            pdf_manifest,
                            pdf_link_updates_for_results,
                        )
                    if property_image_updates_for_results:
                        _store_property_image_sheet_change(
                            user_id,
                            client_id,
                            sheet_id,
                            header,
                            rownum,
                            rowvals,
                            thread_id,
                            to_addr_lower,
                            property_image_candidate,
                            property_image_updates_for_results,
                        )
                except Exception as e:
                    print(f"⚠️ Failed to write PDF link/property image metadata to sheet: {e}")
            elif pdf_manifest and has_new_property_path:
                print(f"   ℹ️ Skipping PDF link write to old row - PDFs belong to new property path")

            # Update the message record with attachment info so frontend can display links
            if pdf_manifest and internet_message_id:
                try:
                    attachments = []
                    for pdf in pdf_manifest:
                        if pdf.get('drive_link'):
                            attachments.append({
                                "name": pdf.get('name', 'attachment.pdf'),
                                "driveLink": pdf.get('drive_link'),
                                "type": "pdf"
                            })
                    if attachments:
                        msg_ref = (_fs.collection("users").document(user_id)
                                   .collection("threads").document(thread_id)
                                   .collection("messages").document(internet_message_id))
                        msg_ref.update({"attachments": attachments})
                        print(f"   📎 Added {len(attachments)} attachment link(s) to message record")
                except Exception as e:
                    print(f"⚠️ Failed to update message with attachments: {e}")

            # Required fields check and remaining questions flow
            # Automatic response logic based on property state
            print(f"\n{'='*60}")
            print(f"📧 RESPONSE SCENARIO SELECTION")
            print(f"{'='*60}")
            print(f"   old_row_became_nonviable: {old_row_became_nonviable}")
            print(f"   new_row_created: {new_row_created}")
            print(f"   new_property_pending_created: {new_property_pending_created}")
            print(f"   LLM response available: {bool(proposal.get('response_email'))}")

            if not allow_outbound_reply:
                _set_reply_send_outcome(outcome="suppressed_operator_replay_no_send")
                print("⏭️ Operator replay extraction-only mode: outbound reply suppressed")
                return

            try:
                response_sent = False

                # Check if we should skip response (e.g., phone number provided in call request)
                skip_response = proposal.get("skip_response", False)
                if skip_response:
                    print(f"⏭️ Skipping email response (notification only)")
                    return  # Exit early, notification already created

                # Check if call was requested but no phone number provided
                call_requested_no_phone = False
                for event in events:
                    if event.get("type") == "call_requested":
                        phone_pattern = r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})'
                        phone_match = re.search(phone_pattern, _full_text)
                        if not phone_match:
                            call_requested_no_phone = True
                            break

                # Check if LLM generated a response email
                llm_response_email = _align_response_greeting(
                    proposal.get("response_email"),
                    contact_name,
                )

                # Scenario 1: Property became non-viable AND new property was suggested
                if old_row_became_nonviable and has_new_property_path:
                    print(f"   📍 SCENARIO 1: Non-viable + new property suggested")
                    response_body = _select_automatic_response_body(
                        "nonviable_with_alternative",
                        llm_response_email,
                        column_config,
                        contact_name,
                    )
                    if response_body == llm_response_email:
                        print(f"🤖 Using LLM-generated response for non-viable + new property scenario")
                    elif llm_response_email:
                        print("⚠️ Ignoring LLM response because it requested a Note/Skip field")
                    
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, to_addr_lower, thread_id)
                    if sent:
                        print(f"📧 Sent thank you + closing (new property suggested) to: {to_addr_lower}")
                        response_sent = True
                    else:
                        response_sent = _handle_auto_response_send_failure(
                            user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                            failure_label="thank you email"
                        )
                
                # Scenario 2: Property became non-viable but NO new property suggested
                elif old_row_became_nonviable and not has_new_property_path:
                    print(f"   📍 SCENARIO 2: Non-viable, no new property")
                    response_body = _select_automatic_response_body(
                        "nonviable",
                        llm_response_email,
                        column_config,
                        contact_name,
                    )
                    if response_body == llm_response_email:
                        print(f"🤖 Using LLM-generated response for non-viable scenario")
                    elif llm_response_email:
                        print("⚠️ Ignoring LLM response because it requested a Note/Skip field")
                    
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, to_addr_lower, thread_id)
                    if sent:
                        print(f"📧 Sent thank you + ask for alternatives to: {to_addr_lower}")
                        response_sent = True
                    else:
                        response_sent = _handle_auto_response_send_failure(
                            user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                            failure_label="alternatives request"
                        )
                
                # Handle call request without phone number - send brief response asking for number
                if call_requested_no_phone and not response_sent:
                    greeting = _build_greeting(contact_name)
                    response_body = f"""{greeting}

Could you please provide your phone number so I can give you a call?"""
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, to_addr_lower, thread_id)
                    if sent:
                        print(f"📞 Sent request for phone number to: {to_addr_lower}")
                        response_sent = True
                    else:
                        response_sent = _handle_auto_response_send_failure(
                            user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                            failure_label="phone number request"
                        )
                
                # Scenario 3 & 4: Property is still viable - check missing fields
                if not response_sent and not old_row_became_nonviable:
                    print(f"   📍 SCENARIO 3/4: Property viable, checking missing fields")
                    sheets = _sheets_client()
                    tab_title = _get_first_tab_title(sheets, sheet_id)
                    
                    # Check if row is below NON-VIABLE divider
                    try:
                        div_resp = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id, range=f"{tab_title}!A:A"
                        ).execute()
                        a_col = div_resp.get("values", [])
                        divider_row = None
                        for i, r in enumerate(a_col, start=1):
                            if r and str(r[0]).strip().upper() == "NON-VIABLE":
                                divider_row = i
                                break
                    except Exception as _e:
                        divider_row = None
                    
                    # Skip if row is below divider or if new row was created
                    if new_row_created or (divider_row and rownum > divider_row):
                        print("ℹ️ Skipping response for non-viable or pending new property row")
                    else:
                        # Re-read row data to check missing fields
                        resp = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id,
                            range=f"{tab_title}!{rownum}:{rownum}"
                        ).execute()
                        current_row = resp.get("values", [[]])[0] if resp.get("values") else []
                        if len(current_row) < len(header):
                            current_row.extend([""] * (len(header) - len(current_row)))
                        
                        missing_fields = check_missing_required_fields(current_row, header, column_config)
                        
                        if missing_fields:
                            # Scenario 3: Thank you + request missing fields
                            # Use LLM-generated response if available, otherwise use template
                            if llm_response_email and _response_mentions_missing_fields(
                                llm_response_email,
                                missing_fields,
                                column_config,
                            ):
                                response_body = llm_response_email
                                # Safety check: Remove "Looking forward to your response" phrases
                                if "Looking forward to your response" in response_body or "Looking forward to hearing from you" in response_body:
                                    print(f"   ⚠️ LLM response contained 'Looking forward' phrase, removing it...")
                                    response_body = response_body.replace("Looking forward to your response", "").replace("Looking forward to hearing from you", "")
                                    # Clean up any double newlines
                                    response_body = "\n".join(line for line in response_body.split("\n") if line.strip())
                                    # Ensure it ends with a simple closing if needed
                                    if response_body.strip() and not response_body.strip().endswith("Thanks") and not response_body.strip().endswith("Thanks."):
                                        response_body = response_body.strip() + "\n\nThanks."
                                print(f"🤖 Using LLM-generated response for missing fields scenario")
                            else:
                                if llm_response_email:
                                    print("⚠️ Ignoring LLM response because it did not ask for the missing fields")
                                greeting = _build_greeting(contact_name)
                                field_list = "\n".join(f"- {field}" for field in missing_fields)
                                response_body = f"""{greeting}

Thank you for the information!

To complete the property details, could you please provide:

{field_list}"""
                            
                            sent = send_reply_in_thread(user_id, headers, response_body, msg_id, to_addr_lower, thread_id)
                            if sent:
                                print(f"📧 Sent thank you + missing fields request to: {to_addr_lower}")
                                try:
                                    from .followup import schedule_followup_after_auto_response
                                    schedule_followup_after_auto_response(user_id, thread_id)
                                except Exception as e:
                                    print(f"⚠️ Failed to reschedule follow-up after missing-fields response: {e}")
                            else:
                                response_sent = _handle_auto_response_send_failure(
                                    user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                                    failure_label="missing fields request"
                                )
                        else:
                            # Scenario 4: All fields complete - send closing
                            response_body = _select_automatic_response_body(
                                "complete",
                                llm_response_email,
                                column_config,
                                contact_name,
                            )
                            if response_body == llm_response_email:
                                print(f"🤖 Using LLM-generated response for all fields complete scenario")
                            elif llm_response_email:
                                print("⚠️ Ignoring LLM response because it requested a Note/Skip field")

                            sent = send_reply_in_thread(user_id, headers, response_body, msg_id, to_addr_lower, thread_id)
                            if sent:
                                print(f"📧 Sent closing email - all fields complete to: {to_addr_lower}")
                                # Create row_completed notification for dashboard stats
                                try:
                                    write_notification(
                                        user_id, client_id,
                                        kind="row_completed",
                                        priority="important",
                                        email=to_addr_lower,
                                        thread_id=thread_id,
                                        row_number=rownum,
                                        row_anchor=row_anchor,
                                        meta={
                                            "completedFields": get_required_fields_for_close(column_config),
                                            "missingFields": []
                                        },
                                        dedupe_key=f"row_completed:{thread_id}:{rownum}"
                                    )
                                    print(f"✅ Created row_completed notification")
                                except Exception as e:
                                    print(f"⚠️ Could not create row_completed notification: {e}")
                                _clear_thread_action_notifications(user_id, client_id, thread_id)
                                # Update thread status to completed
                                update_thread_status(user_id, thread_id, THREAD_STATUS["completed"], "all_fields_gathered")
                                complete_threads_for_row(
                                    user_id,
                                    rownum,
                                    client_id=client_id,
                                    reason="all_fields_gathered",
                                )
                                if thread_ref:
                                    thread_ref.update({
                                        "followUpStatus": "stopped",
                                        "followUpConfig.processingBy": None,
                                        "followUpConfig.processingAt": None,
                                    })
                                # Clear highlight - row is complete, no longer under system control
                                try:
                                    clear_row_highlight(sheet_id, rownum)
                                except Exception as e:
                                    print(f"⚠️ Could not clear row highlight: {e}")
                                _maybe_mark_client_completed(user_id, client_id)
                            else:
                                response_sent = _handle_auto_response_send_failure(
                                    user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                                    failure_label="closing email"
                                )
                        
            except Exception as e:
                print(f"❌ Failed to send automatic response: {e}")
        
        else:
            print("ℹ️ No proposal generated; nothing to apply.")
            _record_ai_processing_failure(
                user_id, client_id, thread_id, msg_id,
                "OpenAI proposal was unavailable or invalid JSON"
            )
            raise RetryableProcessingError("OpenAI proposal was unavailable or invalid JSON")

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """
    Idempotent scan of inbox for replies with early exit on processed messages.

    BATCHING: Groups multiple unprocessed messages in the same thread together
    to prevent conflicting auto-responses when contact sends multiple emails quickly.
    """
    base = "https://graph.microsoft.com/v1.0"

    # Calculate 5-hour cutoff
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()  # ends with +00:00

    cutoff_time = now_utc - timedelta(hours=INBOX_SCAN_WINDOW_HOURS)
    cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")

    # Build filter with time window
    filters = [f"receivedDateTime ge {cutoff_iso}"]
    if only_unread:
        filters.append("isRead eq false")

    filter_str = " and ".join(filters)

    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime asc",  # CHANGED: oldest first for proper batching
        "$select": (
            "id,subject,from,sender,replyTo,toRecipients,ccRecipients,"
            "receivedDateTime,sentDateTime,conversationId,internetMessageId,"
            "internetMessageHeaders,bodyPreview,hasAttachments"
        ),
        "$filter": filter_str
    }

    # PHASE 1: Collect all unprocessed messages and group by thread
    from collections import defaultdict
    thread_messages = defaultdict(list)  # thread_id -> [messages in order]
    orphan_messages = []  # Messages we couldn't match to a thread

    scanned_count = 0
    skipped_count = 0

    try:
        url = f"{base}/me/mailFolders/Inbox/messages"

        while url:
            response = exponential_backoff_request(
                lambda: requests.get(url, headers=headers, params=params, timeout=30)
            )
            data = response.json()
            messages = data.get("value", [])

            if not messages:
                break

            if scanned_count == 0:  # First batch
                print(f"📥 Found {len(messages)} inbox messages to scan")

            for msg in messages:
                scanned_count += 1

                # Check if message is older than scan window
                received_dt = msg.get("receivedDateTime")
                if received_dt:
                    try:
                        msg_time = datetime.fromisoformat(received_dt.replace('Z', '+00:00'))
                        if msg_time < cutoff_time:
                            continue  # Skip but don't stop - we're going oldest first
                    except Exception as e:
                        print(f"⚠️ Failed to parse message time {received_dt}: {e}")

                # Determine processed key (internetMessageId or id)
                processed_key = msg.get("internetMessageId") or msg.get("id")
                if not processed_key:
                    print(f"⚠️ Message has no internetMessageId or id, skipping")
                    continue

                # Check if already processed
                if has_processed(user_id, processed_key):
                    skipped_count += 1
                    continue

                # Try to match to a thread
                thread_id = _match_message_to_thread(user_id, msg, headers)

                if thread_id:
                    thread_messages[thread_id].append(msg)
                else:
                    orphan_messages.append(msg)

            # Handle pagination
            url = data.get("@odata.nextLink")
            if url:
                params = {}  # nextLink includes all parameters

    except Exception as e:
        state = _graph_operation_error_state("inbox_scan", e)
        print(f"❌ Failed to scan inbox: {state.get('error')}")
        return state

    # PHASE 2: Process messages - batched by thread
    processed_count = 0
    batched_count = 0

    # Process thread batches (multiple messages in same thread)
    # Add delay between processing to avoid Google Sheets rate limits (60 reads/min)
    RATE_LIMIT_DELAY = 3  # seconds between processing each thread

    thread_list = list(thread_messages.items())
    for idx, (thread_id, messages) in enumerate(thread_list):
        if len(messages) > 1:
            # BATCH PROCESSING: Multiple messages in same thread
            print(f"📦 Batching {len(messages)} messages for thread {thread_id[:20]}...")
            batched_count += len(messages) - 1  # Count the extras

            # Process only the LAST message (most recent), but include all message content
            # in the conversation history (which is already handled by build_conversation_payload)
            # First, save all the messages to Firestore so they appear in conversation
            for msg in messages[:-1]:  # All but the last
                try:
                    _save_message_to_thread(user_id, thread_id, msg, headers)
                    processed_key = msg.get("internetMessageId") or msg.get("id")
                    mark_processed(user_id, processed_key)
                except Exception as e:
                    print(f"⚠️ Failed to save batched message: {e}")

            # Process the last message (which will see all previous in conversation)
            last_msg = messages[-1]
            processing_error = None
            processed_key = last_msg.get("internetMessageId") or last_msg.get("id")
            if _skip_inbox_retry_after_manual_continuation(user_id, headers, thread_id, last_msg, processed_key):
                skipped_count += 1
                continue
            try:
                process_inbox_message(user_id, headers, last_msg)
                processed_count += 1
                _clear_ai_processing_failure(user_id, thread_id, last_msg.get("internetMessageId") or last_msg.get("id"))
            except Exception as e:
                processing_error = e
                print(f"❌ Failed to process batched message: {e}")
                _record_ai_processing_failure(
                    user_id,
                    _client_id_for_processing_failure(user_id, thread_id),
                    thread_id,
                    processed_key,
                    str(e),
                )
            finally:
                if _should_mark_processed_after_error(processing_error):
                    mark_processed(user_id, processed_key)
                else:
                    print(f"🔁 Leaving batched message retryable: {processed_key}")
        else:
            # Single message - process normally
            msg = messages[0]
            processing_error = None
            processed_key = msg.get("internetMessageId") or msg.get("id")
            if _skip_inbox_retry_after_manual_continuation(user_id, headers, thread_id, msg, processed_key):
                skipped_count += 1
                continue
            try:
                process_inbox_message(user_id, headers, msg)
                processed_count += 1
                _clear_ai_processing_failure(user_id, thread_id, msg.get("internetMessageId") or msg.get("id"))
            except Exception as e:
                processing_error = e
                print(f"❌ Failed to process message {msg.get('id', 'unknown')}: {e}")
                _record_ai_processing_failure(
                    user_id,
                    _client_id_for_processing_failure(user_id, thread_id),
                    thread_id,
                    processed_key,
                    str(e),
                )
            finally:
                if _should_mark_processed_after_error(processing_error):
                    mark_processed(user_id, processed_key)
                else:
                    print(f"🔁 Leaving message retryable: {processed_key}")

        # Rate limit delay between threads (skip delay after last one)
        if idx < len(thread_list) - 1:
            time.sleep(RATE_LIMIT_DELAY)

    # Process orphan messages (couldn't match to thread - will be ignored by process_inbox_message)
    for idx, msg in enumerate(orphan_messages):
        processing_error = None
        processed_key = msg.get("internetMessageId") or msg.get("id")
        try:
            process_inbox_message(user_id, headers, msg)
        except Exception as e:
            processing_error = e
            print(f"❌ Failed to process orphan message: {e}")
            _record_ai_processing_failure(
                user_id,
                "unknown",
                "orphan",
                processed_key,
                str(e),
            )
        finally:
            if _should_mark_processed_after_error(processing_error):
                mark_processed(user_id, processed_key)
            else:
                print(f"🔁 Leaving orphan message retryable: {processed_key}")

        # Rate limit delay between orphan messages (skip delay after last one)
        if idx < len(orphan_messages) - 1:
            time.sleep(RATE_LIMIT_DELAY)

    # Set last scan timestamp
    set_last_scan_iso(user_id, now_utc.isoformat().replace("+00:00", "Z"))

    # Summary log
    if batched_count > 0:
        print(f"📥 Scanned {scanned_count}; processed {processed_count}; batched {batched_count} extra messages; skipped {skipped_count}")
    else:
        print(f"📥 Scanned {scanned_count}; processed {processed_count}; skipped {skipped_count}")

    return {
        "status": "healthy",
        "operation": "inbox_scan",
        "scanned": scanned_count,
        "processed": processed_count,
        "batched": batched_count,
        "skipped": skipped_count,
        "orphaned": len(orphan_messages),
    }


def _match_message_to_thread(user_id: str, msg: dict, headers: dict) -> Optional[str]:
    """
    Try to match an inbox message to an existing thread.
    Returns thread_id if found, None otherwise.
    """
    # Get headers if not present
    internet_message_headers = msg.get("internetMessageHeaders")
    if not internet_message_headers:
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"https://graph.microsoft.com/v1.0/me/messages/{msg.get('id')}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            internet_message_headers = response.json().get("internetMessageHeaders", [])
        except Exception:
            internet_message_headers = []

    # Extract reply headers
    in_reply_to = None
    references = []

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)

    conversation_id = msg.get("conversationId")

    # Try In-Reply-To first
    if in_reply_to:
        thread_id = lookup_thread_by_message_id(user_id, in_reply_to)
        if thread_id:
            return thread_id

    # Try References (newest to oldest)
    if references:
        for ref in reversed(references):
            ref = normalize_message_id(ref)
            thread_id = lookup_thread_by_message_id(user_id, ref)
            if thread_id:
                return thread_id

    # Fallback to conversation ID
    if conversation_id:
        thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
        if thread_id:
            return thread_id

    return None


def _save_message_to_thread(user_id: str, thread_id: str, msg: dict, headers: dict):
    """
    Save a message to a thread without full processing.
    Used for batching - saves earlier messages so they appear in conversation history.
    """
    from_info = msg.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    internet_message_id = msg.get("internetMessageId")
    received_dt = msg.get("receivedDateTime")
    sent_dt = msg.get("sentDateTime")
    subject = msg.get("subject", "")
    to_recipients = _recipient_email_addresses(msg.get("toRecipients"))
    cc_recipients = _recipient_email_addresses(msg.get("ccRecipients"))
    reply_to_recipients = _recipient_email_addresses(msg.get("replyTo"))
    sender_addr = _recipient_email_address(msg.get("sender"))
    source_envelope = _source_message_envelope(msg)
    has_attachments = bool(msg.get("hasAttachments"))

    full_msg = {}
    # Fetch full body
    try:
        full_msg = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{msg.get('id')}",
                headers=headers,
                params={"$select": "body,hasAttachments,sender,replyTo,ccRecipients"},
                timeout=30
            )
        ).json() or {}
        merged_msg = {**msg, **{k: v for k, v in full_msg.items() if k not in msg or not msg.get(k)}}
        cc_recipients = _recipient_email_addresses(merged_msg.get("ccRecipients"))
        reply_to_recipients = _recipient_email_addresses(merged_msg.get("replyTo"))
        sender_addr = _recipient_email_address(merged_msg.get("sender"))
        source_envelope = _source_message_envelope(merged_msg)
        full_body_resp = full_msg.get("body", {}) or {}
        has_attachments = bool(has_attachments or full_msg.get("hasAttachments"))
        _raw_content = full_body_resp.get("content", "") or ""
        _ctype = (full_body_resp.get("contentType") or "Text").upper()
        _full_text = strip_html_tags(_raw_content) if _ctype == "HTML" else _raw_content
    except Exception:
        _full_text = msg.get("bodyPreview", "")

    # Get headers for in_reply_to and references
    internet_message_headers = msg.get("internetMessageHeaders", [])
    in_reply_to = None
    references = []

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)

    # Create message record
    message_record = {
        "direction": "inbound",
        "subject": subject,
        "from": from_addr,
        "sender": sender_addr,
        "to": to_recipients,
        "cc": cc_recipients,
        "replyTo": reply_to_recipients,
        "sentDateTime": sent_dt,
        "receivedDateTime": received_dt,
        "headers": {
            "internetMessageId": internet_message_id,
            "inReplyTo": in_reply_to,
            "references": references
        },
        "body": {
            "contentType": "Text",
            "content": _full_text,
            "preview": safe_preview(_full_text)
        },
        "hasAttachments": has_attachments,
        "sourceMessage": source_envelope,
    }

    # Save to Firestore
    if internet_message_id:
        save_message(user_id, thread_id, internet_message_id, message_record)
        index_message_id(user_id, internet_message_id, thread_id)

    # Update thread timestamp
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        update_payload = {"updatedAt": SERVER_TIMESTAMP}
        if source_envelope:
            update_payload["lastInboundEnvelope"] = source_envelope
        thread_ref.set(update_payload, merge=True)
    except Exception:
        pass

    print(f"  📝 Saved batched message from {from_addr} to thread {thread_id[:20]}...")

def scan_sent_items_for_manual_replies(user_id: str, headers: Dict[str, str], top: int = 50):
    """
    Scan SentItems for Jill's manual replies to conversations we're tracking.
    Indexes them so they appear in conversation history.
    """
    try:
        from .utils import exponential_backoff_request, safe_preview, strip_html_tags
        from .messaging import save_message, index_message_id, index_conversation_id, lookup_thread_by_conversation_id, save_thread_root
        from datetime import datetime, timezone, timedelta
        import requests
        
        base = "https://graph.microsoft.com/v1.0"
        
        # Calculate 5-hour cutoff
        now_utc = datetime.now(timezone.utc)
        cutoff_time = now_utc - timedelta(hours=INBOX_SCAN_WINDOW_HOURS)
        cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")
        
        # Get all tracked conversation IDs from Firestore
        threads_ref = _fs.collection("users").document(user_id).collection("threads")
        threads = list(threads_ref.stream())
        tracked_conversation_ids = set()
        
        for thread_doc in threads:
            thread_data = thread_doc.to_dict() or {}
            conv_id = thread_data.get("conversationId")
            if conv_id:
                tracked_conversation_ids.add(conv_id)
        
        if not tracked_conversation_ids:
            print("📭 No tracked conversations found, skipping SentItems scan")
            return {
                "status": "healthy",
                "operation": "sent_items_scan",
                "scanned": 0,
                "processed": 0,
                "skipped": 0,
                "noTrackedConversations": True,
            }
        
        print(f"📤 Scanning SentItems for manual replies in {len(tracked_conversation_ids)} tracked conversations...")
        
        # Scan SentItems for messages in tracked conversations
        params = {
            "$top": str(top),
            "$orderby": "sentDateTime desc",
            "$select": "id,subject,from,toRecipients,sentDateTime,conversationId,internetMessageId,body,bodyPreview",
            "$filter": f"sentDateTime ge {cutoff_iso}"
        }
        
        processed_count = 0
        scanned_count = 0
        
        try:
            url = f"{base}/me/mailFolders/SentItems/messages"
            
            while url:
                response = exponential_backoff_request(
                    lambda: requests.get(url, headers=headers, params=params, timeout=30)
                )
                data = response.json()
                messages = data.get("value", [])
                
                if not messages:
                    break
                
                for msg in messages:
                    scanned_count += 1
                    
                    # Check if message is older than 5 hours
                    sent_dt = msg.get("sentDateTime")
                    if sent_dt:
                        try:
                            msg_time = datetime.fromisoformat(sent_dt.replace('Z', '+00:00'))
                            if msg_time < cutoff_time:
                                url = None  # Stop pagination
                                break
                        except Exception as e:
                            print(f"⚠️ Failed to parse message time {sent_dt}: {e}")
                    
                    conversation_id = msg.get("conversationId")
                    if not conversation_id or conversation_id not in tracked_conversation_ids:
                        continue  # Not in a tracked conversation
                    
                    internet_message_id = msg.get("internetMessageId")
                    if not internet_message_id:
                        continue  # Need message ID to index
                    
                    # Check if already indexed
                    normalized_id = normalize_message_id(internet_message_id)
                    from .messaging import lookup_thread_by_message_id
                    existing_thread = lookup_thread_by_message_id(user_id, internet_message_id)
                    
                    if existing_thread:
                        continue  # Already indexed
                    
                    # Find or create thread for this conversation
                    # Use exhaustive search to prevent duplicate thread creation
                    from .messaging import lookup_thread_by_conversation_id_exhaustive
                    thread_id = lookup_thread_by_conversation_id_exhaustive(user_id, conversation_id)

                    if not thread_id:
                        # Create new thread from conversation
                        thread_id = normalize_message_id(conversation_id) or conversation_id
                        thread_meta = {
                            "subject": msg.get("subject", "Property information"),
                            "email": [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])],
                            "conversationId": conversation_id,
                            "createdFromSentItem": True
                        }
                        # Save thread with retry
                        for attempt in range(3):
                            if save_thread_root(user_id, thread_id, thread_meta):
                                break
                            time.sleep(0.5 * (attempt + 1))
                        # Index conversation with retry
                        for attempt in range(3):
                            if index_conversation_id(user_id, conversation_id, thread_id):
                                break
                            time.sleep(0.5 * (attempt + 1))
                        print(f"   📝 Created new thread from SentItem: {thread_id}")
                    
                    # Index this sent message
                    to_recipients = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
                    body_obj = msg.get("body", {}) or {}
                    body_content = body_obj.get("content", "")
                    body_type = body_obj.get("contentType", "Text")
                    if body_type == "HTML":
                        body_content = strip_html_tags(body_content)
                    
                    message_record = {
                        "direction": "outbound",
                        "subject": msg.get("subject", ""),
                        "from": "me",
                        "to": to_recipients,
                        "sentDateTime": sent_dt,
                        "receivedDateTime": None,
                        "headers": {
                            "internetMessageId": internet_message_id,
                            "inReplyTo": None,
                            "references": []
                        },
                        "body": {
                            "contentType": body_type,
                            "content": body_content,
                            "preview": msg.get("bodyPreview", "")[:200] or safe_preview(body_content)
                        }
                    }
                    
                    # Save message with retry
                    for attempt in range(3):
                        if save_message(user_id, thread_id, normalized_id, message_record):
                            break
                        time.sleep(0.5 * (attempt + 1))

                    # Index message with retry and verification
                    msg_indexed = False
                    for attempt in range(3):
                        if index_message_id(user_id, internet_message_id, thread_id):
                            time.sleep(0.2)
                            if lookup_thread_by_message_id(user_id, internet_message_id) == thread_id:
                                msg_indexed = True
                                break
                        time.sleep(0.5 * (attempt + 1))

                    if not msg_indexed:
                        print(f"   ⚠️ Failed to index manual reply after retries")

                    processed_count += 1
                    print(f"   📝 Indexed manual reply: {internet_message_id[:50]}... -> thread {thread_id}")
                
                # Check for next page
                url = data.get("@odata.nextLink")
                if url:
                    params = None  # NextLink includes all params
                else:
                    url = None
            
            if processed_count > 0:
                print(f"📤 Indexed {processed_count} manual reply(s) from SentItems")
            else:
                print(f"📤 No new manual replies found in SentItems")

            return {
                "status": "healthy",
                "operation": "sent_items_scan",
                "scanned": scanned_count,
                "processed": processed_count,
            }
                
        except Exception as e:
            state = _graph_operation_error_state("sent_items_scan", e)
            print(f"❌ Failed to scan SentItems: {state.get('error')}")
            return state
            
    except Exception as e:
        state = _graph_operation_error_state("sent_items_scan", e)
        print(f"❌ Failed to scan SentItems for manual replies: {state.get('error')}")
        return state
