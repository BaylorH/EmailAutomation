from email_automation.source_coordinator import SourceCoordinator

import re
import requests
import hashlib
import json
import time
import logging
import copy
from contextvars import ContextVar
from dataclasses import dataclass, replace
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Mapping, Optional
from urllib.parse import urlsplit
from uuid import uuid4
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from googleapiclient.errors import HttpError

from .clients import _fs, _get_sheet_id_or_fail, _get_client_config, _sheets_client
from .firestore_transactions import run_firestore_transaction
from .sheets import AssetLinkWriteError, format_sheet_columns_autosize_with_exceptions, _get_first_tab_title, _read_header_row2, append_links_to_flyer_link_column, append_links_to_floorplan_column, write_property_image_columns, is_floorplan_filename, _header_index_map, _find_row_by_email, clear_row_highlight, highlight_row, ROW_HIGHLIGHT_BLUE, _execute_with_retry, _col_letter
from .sheets import terminal_sheets_provider_window
from .sheet_operations import _find_row_by_anchor, ensure_nonviable_divider, move_row_below_divider, move_row_below_new_divider_atomic, insert_property_row_above_divider, _is_row_below_nonviable, sync_thread_row_numbers_after_move, stop_threads_for_row, complete_threads_for_row
from .messaging import (save_message, save_thread_root, index_message_id, index_conversation_id,
                       dump_thread_from_firestore, has_processed, mark_processed, set_last_scan_iso,
                       lookup_thread_by_message_id, lookup_thread_by_conversation_id,
                       is_event_handled, mark_event_handled, build_event_key,
                       update_thread_status, get_thread_status, THREAD_STATUS)
from .logging import write_message_order_test
from .ai_processing import (
    _ANCILLARY_SUBJECT_RE,
    _UNAVAILABLE_PATTERNS,
    _VIABILITY_NEGATOR_LINK_WORDS,
    _VIABILITY_QUALIFIER_WORDS,
    _VIABILITY_RE,
    _append_ai_meta,
    _detect_target_terminal_reason,
    _looks_like_requirements_mismatch_nonviable,
    _source_mentions_target_property,
    _street_claim_spans,
    _target_street_identity,
    _viability_lexical_negator_count,
    _viability_prefix_negation_count,
    _viability_prefix_is_lexically_negated,
    apply_proposal_to_sheet,
    check_missing_required_fields,
    get_row_anchor,
    propose_sheet_updates,
)
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
from .email import (
    OUTBOUND_MODE_LIVE,
    _graph_message_path_segment,
    _kill_switch_suppressed,
    resolve_outbound_mode,
)
from .utils import (exponential_backoff_request, strip_html_tags, safe_preview,
                   parse_references_header, normalize_message_id, fetch_url_as_text, _sanitize_url,
                   format_email_body_with_footer, strip_email_quotes, strip_outbound_body_signoff,
                   b64url_id)
from .pending_responses import queue_pending_response, record_sent_unindexed_response
from .send_permits import (
    GRAPH_SEND_RESOLVED_STATUSES,
    RESOLVED_PENDING_DRAFT_REVIEW_STATUSES,
    RESOLVED_TERMINAL_GRAPH_REVIEW_STATUSES,
    GraphSendPermitBlocked,
    GraphSendPermitLocalRetryable,
    assert_terminal_reply_permit_settled,
    assert_terminal_staging_allowed,
    begin_graph_draft_attachment,
    begin_graph_draft_creation,
    begin_graph_draft_patch,
    cas_terminal_reply_transition as _cas_graph_terminal_reply_transition,
    complete_graph_draft_attachment,
    complete_graph_draft_creation,
    complete_graph_draft_patch,
    consume_graph_send_capability,
    finalize_graph_draft_preparation,
    graph_send_permit_blocks_new_send,
    expired_graph_send_pre_send_recovery_kind,
    issue_terminal_graph_send_permit,
    read_active_graph_send_permit,
    read_active_terminal_reply_permit,
    read_permit,
    resolve_graph_send_permit,
    validate_unissued_terminal_reply_attempt,
    validate_graph_draft_attachment_plan,
)
from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_exact_sent_message_by_immutable_id,
    find_matching_sent_message_for_retry,
    find_sent_conversation_continuation_for_retry,
    graph_headers_with_immutable_id,
    sent_after_from_retry_data,
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
from .source_coordinator import (
    CoordinatorMode,
    MAX_SOURCE_ALIASES,
    MAX_UNSETTLED_SOURCE_ADMISSIONS,
    SourceCoordinatorAmbiguous,
    SourceCoordinatorConfigError,
    SourceCoordinatorConflict,
    SourceCoordinatorRetryable,
    SourceSettlementNotReady,
    SourceSettlementResult,
    advance_scan_cursor_if_source_authority_clear,
    canonical_json_hash,
    consume_durable_source_resume_context,
    durable_source_resume_contexts,
    normalize_source_alias,
    resolve_source_coordinator_mode,
    release_settled_source_generations,
    source_alias_key,
    verify_settled_source_dispatch_binding,
)
from .system_health import RESOLVED_DEAD_LETTER_STATUSES

logger = logging.getLogger(__name__)

MAX_ENFORCED_INBOX_SCAN_PAGES = 100
MAX_ENFORCED_INBOX_SCAN_MESSAGES = 5000
_GRAPH_INBOX_MESSAGES_PATH = "/v1.0/me/mailFolders/Inbox/messages"


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
    exact_sent_evidence: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class SourceProcessingAuthority:
    canonical_source_id: str
    snapshot_hash: str
    selection_hash: str
    owner_kind: str
    owner_key: str | None
    ledger_hash: str


@dataclass(frozen=True)
class SourceProcessingDisposition:
    """Structured result used by enforced inbox admission and its scanner."""

    mode: CoordinatorMode
    state: str
    authority: SourceProcessingAuthority | None = None
    settlement: SourceSettlementResult | None = None
    blocker_canonical_source_id: str | None = None
    thread_id: str | None = None
    source_alias_keys: tuple[str, ...] = ()

    def __bool__(self):
        return self.state == "settled" and self.settlement is not None


def _is_full_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _source_alias_keys_for_message(
    user_id: str,
    message: Mapping[str, Any],
) -> tuple[str, ...]:
    aliases = [normalize_source_alias("graph", message.get("id"))]
    internet_message_id = message.get("internetMessageId")
    if internet_message_id is not None:
        aliases.append(
            normalize_source_alias(
                "internet_message_id",
                internet_message_id,
            )
        )
    return tuple(sorted(source_alias_key(user_id, alias) for alias in aliases))


def _is_bound_exact_source_settlement(
    result: Any,
    *,
    user_id: str,
    thread_id: str,
    message: Mapping[str, Any],
) -> bool:
    if not (
        isinstance(result, SourceProcessingDisposition)
        and result.mode is CoordinatorMode.ENFORCED
        and result.state == "settled"
        and result.thread_id == thread_id
        and isinstance(result.settlement, SourceSettlementResult)
        and isinstance(result.authority, SourceProcessingAuthority)
    ):
        return False
    expected_alias_keys = _source_alias_keys_for_message(user_id, message)
    authority = result.authority
    settlement = result.settlement
    return (
        result.source_alias_keys == expected_alias_keys
        and all(_is_full_sha256(key) for key in result.source_alias_keys)
        and settlement.canonical_source_id == authority.canonical_source_id
        and _is_full_sha256(settlement.settlement_hash)
        and type(settlement.settlement_revision) is int
        and settlement.settlement_revision == 1
        and type(settlement.alias_projection_count) is int
        and len(expected_alias_keys)
        <= settlement.alias_projection_count
        <= MAX_SOURCE_ALIASES
        and type(settlement.repaired_projection_count) is int
        and 0 <= settlement.repaired_projection_count
        <= settlement.alias_projection_count
        and _is_full_sha256(authority.snapshot_hash)
        and _is_full_sha256(authority.selection_hash)
        and _is_full_sha256(authority.ledger_hash)
        and authority.owner_kind
        in {"none", "contact_optout", "terminal", "human_decision"}
        and (
            (authority.owner_kind == "none" and authority.owner_key is None)
            or (
                authority.owner_kind != "none"
                and _is_full_sha256(authority.owner_key)
            )
        )
    )


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
    send_reply_in_thread.last_exact_sent_evidence = outcome.exact_sent_evidence


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


def _pending_response_source_binding(
    *,
    canonical_source_id: Optional[str],
    work_key: Optional[str],
    proposal_hash: Optional[str],
    selection_hash: Optional[str],
) -> Dict[str, str]:
    """Require exact source/work authority for non-legacy pending writes."""
    if resolve_source_coordinator_mode(os.environ) is CoordinatorMode.DISABLED:
        return {}
    if (
        type(canonical_source_id) is not str
        or not canonical_source_id
        or canonical_source_id != canonical_source_id.strip()
        or not _is_full_sha256(work_key)
        or not _is_full_sha256(proposal_hash)
        or not _is_full_sha256(selection_hash)
    ):
        raise SourceCoordinatorConfigError(
            "pending response source binding is missing or malformed"
        )
    return {
        "canonical_source_id": canonical_source_id,
        "work_key": work_key,
        "proposal_hash": proposal_hash,
        "selection_hash": selection_hash,
    }


def _queue_response_retry_or_reconciliation(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    *,
    source_context: str = "autoResponse",
    canonical_source_id: Optional[str] = None,
    work_key: Optional[str] = None,
    proposal_hash: Optional[str] = None,
    selection_hash: Optional[str] = None,
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
    if send_outcome.outcome == "suppressed_recipient_optout":
        print("⏭️ Reply recipient opted out; no retry was queued")
        return "recipient_suppressed"
    pending_binding = _pending_response_source_binding(
        canonical_source_id=canonical_source_id,
        work_key=work_key,
        proposal_hash=proposal_hash,
        selection_hash=selection_hash,
    )
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
            **pending_binding,
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
        **pending_binding,
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
    canonical_source_id: Optional[str] = None,
    work_key: Optional[str] = None,
    proposal_hash: Optional[str] = None,
    selection_hash: Optional[str] = None,
) -> bool:
    print(f"❌ Failed to send {failure_label}")
    outcome = _queue_response_retry_or_reconciliation(
        user_id,
        thread_id,
        msg_id,
        recipient,
        response_body,
        client_id,
        canonical_source_id=canonical_source_id,
        work_key=work_key,
        proposal_hash=proposal_hash,
        selection_hash=selection_hash,
    )
    return outcome in {"sent_unindexed", "recipient_suppressed"}


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


PROCESSING_FAILURE_SCHEMA_VERSION = 2
PROCESSING_FAILURE_DOC_PREFIX = "processing-failure-v2-"
FIRESTORE_DOCUMENT_ID_MAX_BYTES = 1500


def _clean_processing_failure_identity_value(value: Any) -> str:
    return str(value or "").strip()


def _processing_failure_identity(
    thread_id: str,
    message_id: Optional[str] = None,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    processing_failure_identity_key: Optional[str] = None,
    processing_failure_identity_kind: Optional[str] = None,
) -> Dict[str, Any]:
    clean_thread_id = _clean_processing_failure_identity_value(thread_id)
    clean_message_id = _clean_processing_failure_identity_value(message_id)
    clean_graph_message_id = _clean_processing_failure_identity_value(
        graph_message_id
    )
    clean_internet_message_id = _clean_processing_failure_identity_value(
        internet_message_id
    )
    clean_source_message_key = _clean_processing_failure_identity_value(
        source_message_key
    )
    clean_identity_key = _clean_processing_failure_identity_value(
        processing_failure_identity_key
    )
    clean_identity_kind = _clean_processing_failure_identity_value(
        processing_failure_identity_kind
    ).lower()
    if not clean_source_message_key:
        clean_source_message_key = (
            clean_message_id
            or clean_internet_message_id
            or clean_graph_message_id
        )
    if not clean_thread_id or not clean_source_message_key:
        raise ValueError(
            "Processing failure identity requires threadId and sourceMessageKey"
        )
    if not clean_internet_message_id and (
        clean_message_id.startswith("<") and clean_message_id.endswith(">")
    ):
        clean_internet_message_id = clean_message_id
    if not clean_identity_key:
        if clean_graph_message_id:
            clean_identity_kind = "graph"
            clean_identity_key = clean_graph_message_id
        elif clean_internet_message_id:
            clean_identity_kind = "internet"
            clean_identity_key = clean_internet_message_id
        else:
            clean_identity_kind = "source"
            clean_identity_key = clean_source_message_key
    if clean_identity_kind not in {"graph", "internet", "source"}:
        raise ValueError("Processing failure identity kind is invalid")
    return {
        "processingFailureSchemaVersion": PROCESSING_FAILURE_SCHEMA_VERSION,
        "threadId": clean_thread_id,
        "sourceMessageKey": clean_source_message_key,
        "graphMessageId": clean_graph_message_id or None,
        "internetMessageId": clean_internet_message_id or None,
        "processingFailureIdentityKind": clean_identity_kind,
        "processingFailureIdentityKey": clean_identity_key,
    }


def _processing_failure_document_id(
    thread_id: str,
    message_id: Optional[str] = None,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    record_key_suffix: Optional[str] = None,
    processing_failure_identity_key: Optional[str] = None,
    processing_failure_identity_kind: Optional[str] = None,
) -> str:
    identity = _processing_failure_identity(
        thread_id,
        message_id,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        source_message_key=source_message_key,
        processing_failure_identity_key=processing_failure_identity_key,
        processing_failure_identity_kind=processing_failure_identity_kind,
    )
    digest_payload = {
        "processingFailureSchemaVersion": PROCESSING_FAILURE_SCHEMA_VERSION,
        "threadId": identity["threadId"],
        "processingFailureIdentityKind": identity[
            "processingFailureIdentityKind"
        ],
        "processingFailureIdentityKey": identity["processingFailureIdentityKey"],
        "recordKeySuffix": (
            _clean_processing_failure_identity_value(record_key_suffix) or None
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"{PROCESSING_FAILURE_DOC_PREFIX}{digest}"


def _processing_failure_document_ids(
    thread_id: str,
    message_id: Optional[str] = None,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    record_key_suffix: Optional[str] = None,
    processing_failure_identity_key: Optional[str] = None,
    processing_failure_identity_kind: Optional[str] = None,
) -> List[str]:
    """Return primary then compatibility v2 IDs for all exact typed aliases."""
    identity = _processing_failure_identity(
        thread_id,
        message_id,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        source_message_key=source_message_key,
        processing_failure_identity_key=processing_failure_identity_key,
        processing_failure_identity_kind=processing_failure_identity_kind,
    )
    pairs = []

    def add(kind: str, value: Any) -> None:
        clean_value = _clean_processing_failure_identity_value(value)
        pair = (kind, clean_value)
        if clean_value and pair not in pairs:
            pairs.append(pair)

    add(
        identity["processingFailureIdentityKind"],
        identity["processingFailureIdentityKey"],
    )
    add("graph", identity.get("graphMessageId"))
    add("internet", identity.get("internetMessageId"))
    add("source", identity.get("sourceMessageKey"))
    # Compatibility for early v2 records created before typed aliases were
    # known: their resource/RFC value may have been classified as `source`.
    add("source", graph_message_id)
    add("source", internet_message_id)
    add("source", message_id)
    return [
        _processing_failure_document_id(
            thread_id,
            message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=source_message_key,
            record_key_suffix=record_key_suffix,
            processing_failure_identity_key=value,
            processing_failure_identity_kind=kind,
        )
        for kind, value in pairs
    ]


def _safe_legacy_processing_failure_document_ids(
    thread_id: str,
    message_id: Optional[str] = None,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    record_key_suffix: Optional[str] = None,
) -> List[str]:
    """Return only legacy IDs that are safe to hand to Firestore.document()."""
    clean_thread_id = _clean_processing_failure_identity_value(thread_id)
    if not clean_thread_id:
        return []
    safe_suffix = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        _clean_processing_failure_identity_value(record_key_suffix),
    ).strip("_")
    candidates = []
    for value in (
        source_message_key,
        message_id,
        internet_message_id,
        graph_message_id,
    ):
        clean_value = _clean_processing_failure_identity_value(value)
        if clean_value and clean_value not in candidates:
            candidates.append(clean_value)

    safe_ids = []
    for candidate in candidates:
        doc_id = f"{clean_thread_id}__{candidate}"
        if safe_suffix:
            doc_id = f"{doc_id}__{safe_suffix}"
        if (
            doc_id in {".", ".."}
            or "/" in doc_id
            or len(doc_id.encode("utf-8")) > FIRESTORE_DOCUMENT_ID_MAX_BYTES
        ):
            continue
        if doc_id not in safe_ids:
            safe_ids.append(doc_id)
    return safe_ids


def _validate_existing_processing_failure_identity(
    ref: Any,
    data: Dict[str, Any],
    thread_id: str,
    message_id: Optional[str] = None,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    record_key_suffix: Optional[str] = None,
) -> None:
    """Reject a candidate whose stored identity does not match its path/request."""
    if not isinstance(data, dict):
        raise ValueError("Processing failure record is malformed")
    requested = _processing_failure_identity(
        thread_id,
        message_id,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        source_message_key=source_message_key,
    )
    stored_thread_id = _clean_processing_failure_identity_value(
        data.get("threadId")
    )
    if stored_thread_id != requested["threadId"]:
        raise ValueError("Processing failure record threadId does not match its path")

    stored_source_message_key = _clean_processing_failure_identity_value(
        data.get("sourceMessageKey") or data.get("messageId")
    )
    requested_aliases = {
        _clean_processing_failure_identity_value(value)
        for value in (
            requested.get("sourceMessageKey"),
            requested.get("graphMessageId"),
            requested.get("internetMessageId"),
            message_id,
        )
        if _clean_processing_failure_identity_value(value)
    }
    if (
        not stored_source_message_key
        or stored_source_message_key not in requested_aliases
    ):
        raise ValueError(
            "Processing failure record source identity does not match the request"
        )

    stored_graph_message_id = _clean_processing_failure_identity_value(
        data.get("graphMessageId")
    )
    stored_internet_message_id = _clean_processing_failure_identity_value(
        data.get("internetMessageId")
    )
    if (
        stored_graph_message_id
        and requested.get("graphMessageId")
        and stored_graph_message_id != requested["graphMessageId"]
    ):
        raise ValueError(
            "Processing failure Graph message identity is contradictory"
        )
    if (
        stored_internet_message_id
        and requested.get("internetMessageId")
        and stored_internet_message_id != requested["internetMessageId"]
    ):
        raise ValueError("Processing failure internet identity is contradictory")

    ref_id = _clean_processing_failure_identity_value(getattr(ref, "id", None))
    is_v2 = bool(
        re.fullmatch(rf"{re.escape(PROCESSING_FAILURE_DOC_PREFIX)}[0-9a-f]{{64}}", ref_id)
    )
    if is_v2:
        stored_identity_kind = _clean_processing_failure_identity_value(
            data.get("processingFailureIdentityKind")
        ).lower()
        stored_identity_key = _clean_processing_failure_identity_value(
            data.get("processingFailureIdentityKey")
        )
        if (
            stored_identity_kind not in {"graph", "internet", "source"}
            or not stored_identity_key
        ):
            raise ValueError("Processing failure v2 identity metadata is malformed")
        if (
            stored_identity_kind == "graph"
            and stored_identity_key != stored_graph_message_id
        ):
            raise ValueError(
                "Processing failure v2 Graph key does not match its stored Graph alias"
            )
        if (
            stored_identity_kind == "internet"
            and stored_identity_key != stored_internet_message_id
        ):
            raise ValueError(
                "Processing failure v2 internet key does not match its stored internet alias"
            )
        if stored_identity_kind == "source" and stored_identity_key not in {
            value
            for value in (
                stored_source_message_key,
                stored_graph_message_id,
                stored_internet_message_id,
            )
            if value
        }:
            raise ValueError(
                "Processing failure v2 source key does not match a stored source alias"
            )
        expected_ref_id = _processing_failure_document_id(
            stored_thread_id,
            stored_source_message_key,
            graph_message_id=stored_graph_message_id,
            internet_message_id=stored_internet_message_id,
            source_message_key=stored_source_message_key,
            record_key_suffix=record_key_suffix,
            processing_failure_identity_key=stored_identity_key,
            processing_failure_identity_kind=stored_identity_kind,
        )
        if ref_id != expected_ref_id:
            raise ValueError(
                "Processing failure v2 identity metadata does not match its path"
            )
        return

    expected_legacy_ids = _safe_legacy_processing_failure_document_ids(
        stored_thread_id,
        stored_source_message_key,
        graph_message_id=stored_graph_message_id,
        internet_message_id=stored_internet_message_id,
        source_message_key=stored_source_message_key,
        record_key_suffix=record_key_suffix,
    )
    if ref_id not in expected_legacy_ids:
        raise ValueError("Legacy processing failure identity does not match its path")


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
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        identity = _processing_failure_identity(
            thread_id,
            message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=source_message_key,
        )
        hashed_doc_ids = _processing_failure_document_ids(
            thread_id,
            message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=source_message_key,
            record_key_suffix=record_key_suffix,
        )
        legacy_doc_ids = _safe_legacy_processing_failure_document_ids(
            thread_id,
            message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=source_message_key,
            record_key_suffix=record_key_suffix,
        )
        failures_ref = (
            _fs.collection("users")
            .document(user_id)
            .collection("processingFailures")
        )
        hashed_refs = [
            failures_ref.document(doc_id) for doc_id in hashed_doc_ids
        ]
        legacy_refs = [failures_ref.document(doc_id) for doc_id in legacy_doc_ids]
        base_identity = dict(identity)

        def persist_failure(transaction) -> bool:
            current_identity = dict(base_identity)
            hashed_snapshots = [
                ref.get(transaction=transaction) for ref in hashed_refs
            ]
            legacy_snapshots = [
                ref.get(transaction=transaction) for ref in legacy_refs
            ]
            existing_hashed = [
                (ref, snapshot)
                for ref, snapshot in zip(hashed_refs, hashed_snapshots)
                if getattr(snapshot, "exists", False)
            ]
            existing_legacy = [
                (ref, snapshot)
                for ref, snapshot in zip(legacy_refs, legacy_snapshots)
                if getattr(snapshot, "exists", False)
            ]
            existing_matches = [*existing_hashed, *existing_legacy]
            if len(existing_matches) > 1:
                raise ValueError(
                    "Multiple processing failure records match the exact identity"
                )
            if existing_matches:
                target_ref, target_snapshot = existing_matches[0]
            else:
                target_ref = hashed_refs[0]
                target_snapshot = hashed_snapshots[0]

            existing = (
                target_snapshot.to_dict() or {}
                if getattr(target_snapshot, "exists", False)
                else {}
            )
            if getattr(target_snapshot, "exists", False):
                _validate_existing_processing_failure_identity(
                    target_ref,
                    existing,
                    thread_id,
                    message_id,
                    graph_message_id=graph_message_id,
                    internet_message_id=internet_message_id,
                    source_message_key=source_message_key,
                    record_key_suffix=record_key_suffix,
                )
            occurrences = existing.get("failureOccurrences")
            if isinstance(occurrences, bool) or not isinstance(occurrences, int):
                occurrences = 0
            occurrences = max(0, occurrences)
            existing_nonretryable = existing.get("retryable") is False
            existing_identity_key = _clean_processing_failure_identity_value(
                existing.get("processingFailureIdentityKey")
            )
            existing_identity_kind = _clean_processing_failure_identity_value(
                existing.get("processingFailureIdentityKind")
            ).lower()
            if (
                existing_identity_key
                and existing_identity_kind in {"graph", "internet", "source"}
            ):
                # Enrichment must never mutate the stable key used by this v2 doc.
                current_identity["processingFailureIdentityKey"] = (
                    existing_identity_key
                )
                current_identity["processingFailureIdentityKind"] = (
                    existing_identity_kind
                )
            payload = {
                "clientId": client_id,
                **current_identity,
                # Preserve the legacy field for readers that have not migrated yet.
                "messageId": current_identity["sourceMessageKey"],
                "reason": reason,
                "retryable": False if existing_nonretryable else bool(retryable),
                "failureOccurrences": occurrences + 1,
                "updatedAt": SERVER_TIMESTAMP,
                "lastFailedAt": SERVER_TIMESTAMP,
            }
            if record_key_suffix:
                payload["processingFailureRecordKeySuffix"] = (
                    _clean_processing_failure_identity_value(record_key_suffix)
                )
            if "createdAt" not in existing:
                payload["createdAt"] = SERVER_TIMESTAMP
            if recovery_status:
                if not (
                    existing_nonretryable
                    and bool(retryable)
                    and existing.get("recoveryStatus")
                ):
                    payload["recoveryStatus"] = recovery_status
            if isinstance(metadata, dict) and metadata:
                payload["metadata"] = metadata
            if isinstance(extra_fields, dict) and extra_fields:
                payload.update(extra_fields)
            transaction.set(target_ref, payload, merge=True)
            return True

        return run_firestore_transaction(_fs, persist_failure)
    except Exception as e:
        print(f"⚠️ Could not record AI processing failure: {e}")
        return False


def _has_processing_failure_record(
    user_id: str,
    thread_id: str,
    message_id: str,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
) -> bool:
    if not thread_id or not (message_id or source_message_key):
        return False
    try:
        failures_ref = (
            _fs.collection("users")
            .document(user_id)
            .collection("processingFailures")
        )
        doc_ids = [
            *_processing_failure_document_ids(
                thread_id,
                message_id,
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=source_message_key,
            ),
            *_safe_legacy_processing_failure_document_ids(
                thread_id,
                message_id,
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=source_message_key,
            ),
        ]
        return any(
            bool(getattr(failures_ref.document(doc_id).get(), "exists", False))
            for doc_id in dict.fromkeys(doc_ids)
        )
    except Exception as e:
        print(f"⚠️ Could not check processing failure retry state: {e}")
        return False


def _record_processing_failure_blocked_by_manual_continuation(
    user_id: str,
    client_id: str,
    thread_id: str,
    message_id: str,
    sent_artifact: Dict[str, Any],
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
) -> bool:
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
    return _record_ai_processing_failure(
        user_id,
        client_id,
        thread_id,
        message_id,
        last_retry_error,
        retryable=False,
        recovery_status=recovery_status,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        source_message_key=source_message_key,
        extra_fields={
            "recoveryArtifactCollection": sent_artifact.get("collection") or "SentItems/manualContinuation",
            "recoverySentMessageId": sent_artifact.get("id") or sent_artifact.get("sentMessageId"),
            "recoverySentInternetMessageId": sent_artifact.get("internetMessageId"),
            "recoveryConversationId": sent_artifact.get("conversationId"),
            "recoverySentDateTime": sent_artifact.get("sentDateTime"),
            "recoveryGuardError": sent_artifact.get("guardError"),
            "lastRetryAt": SERVER_TIMESTAMP,
            "lastRetryError": last_retry_error,
        },
    )


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


def _clear_ai_processing_failure(
    user_id: str,
    thread_id: str,
    message_id: str,
    *,
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    source_message_key: Optional[str] = None,
):
    if not (message_id or source_message_key):
        return
    try:
        failures_ref = (
            _fs.collection("users")
            .document(user_id)
            .collection("processingFailures")
        )
        doc_ids = [
            *_processing_failure_document_ids(
                thread_id,
                message_id,
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=source_message_key,
            ),
            *_safe_legacy_processing_failure_document_ids(
                thread_id,
                message_id,
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=source_message_key,
            ),
        ]
        for doc_id in dict.fromkeys(doc_ids):
            failures_ref.document(doc_id).delete()
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


_SOURCE_MESSAGE_IDENTITY_KEYS = (
    "msgId",
    "replyToMessageId",
    "sourceMessageId",
    "sourceGraphMessageId",
    "sourceInternetMessageId",
    "originalMessageId",
    "currentMsgId",
    "detectedInMessageId",
)
_SOURCE_MESSAGE_IDENTITY_CONTAINERS = (
    "meta",
    "tourInvite",
    "sourceMessage",
    "source",
)


def _source_identity_value_is_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(
            _source_identity_value_is_present(item)
            for item in value.values()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_source_identity_value_is_present(item) for item in value)
    return False


def _source_message_identity_is_present(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict):
        return False
    if any(
        _source_identity_value_is_present(data.get(key))
        for key in _SOURCE_MESSAGE_IDENTITY_KEYS
    ):
        return True
    return any(
        _source_message_identity_is_present(data.get(key))
        for key in _SOURCE_MESSAGE_IDENTITY_CONTAINERS
    )


def _source_message_match(data: Dict[str, Any], candidates: set) -> bool:
    if not candidates:
        return False

    for key in _SOURCE_MESSAGE_IDENTITY_KEYS:
        if _value_matches_message_candidates((data or {}).get(key), candidates):
            return True

    for nested_key in _SOURCE_MESSAGE_IDENTITY_CONTAINERS:
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
        payload["replyToMessageId"] = msg_id
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


def _is_operator_replay_recovery_status(value: Any) -> bool:
    return str(value or "").strip().startswith("operator_replay_")


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
            preserve_operator_replay = _is_operator_replay_recovery_status(
                data.get("recoveryStatus")
            )
            if (
                message_id
                and not preserve_operator_warning
                and not preserve_operator_replay
                and has_processed(user_id, message_id)
            ):
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
    response = exponential_backoff_request(
        lambda: requests.get(
            "https://graph.microsoft.com/v1.0/me/messages/"
            f"{_graph_message_path_segment(message_id)}",
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


def _looks_like_internet_message_id(value: Any) -> bool:
    clean_value = _clean_processing_failure_identity_value(value)
    return clean_value.startswith("<") and clean_value.endswith(">")


def _fetch_graph_message_by_internet_message_id(
    headers: Dict[str, str], internet_message_id: str
) -> Dict[str, Any]:
    exact_id = _clean_processing_failure_identity_value(internet_message_id)
    if not exact_id:
        raise RetryableProcessingError(
            "Processing failure has no RFC internet message id to resolve"
        )
    escaped_id = exact_id.replace("'", "''")
    response = exponential_backoff_request(
        lambda: requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
            headers=headers,
            params={
                "$filter": f"internetMessageId eq '{escaped_id}'",
                "$top": 2,
                "$select": (
                    "id,subject,from,sender,replyTo,toRecipients,ccRecipients,"
                    "receivedDateTime,sentDateTime,conversationId,internetMessageId,"
                    "internetMessageHeaders,bodyPreview,hasAttachments"
                ),
            },
            timeout=30,
        )
    )
    payload = response.json() or {}
    values = payload.get("value", []) if isinstance(payload, dict) else []
    exact_matches = [
        message
        for message in values
        if isinstance(message, dict)
        and message.get("internetMessageId") == exact_id
    ]
    if not exact_matches:
        raise RetryableProcessingError(
            "Graph internetMessageId lookup returned no exact message"
        )
    if len(exact_matches) > 1:
        raise RetryableProcessingError(
            "Graph internetMessageId lookup returned multiple exact messages"
        )
    return exact_matches[0]


def _fetch_graph_message_for_processing_failure(
    headers: Dict[str, str], data: Dict[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    stored_graph_message_id = _clean_processing_failure_identity_value(
        data.get("graphMessageId")
    )
    stored_internet_message_id = _clean_processing_failure_identity_value(
        data.get("internetMessageId")
    )
    legacy_message_id = _clean_processing_failure_identity_value(
        data.get("messageId")
    )
    source_message_key = _clean_processing_failure_identity_value(
        data.get("sourceMessageKey")
    ) or legacy_message_id

    expected_graph_message_id = stored_graph_message_id
    expected_internet_message_id = stored_internet_message_id
    if stored_graph_message_id:
        message = _fetch_graph_message_by_id(headers, stored_graph_message_id)
    elif stored_internet_message_id:
        message = _fetch_graph_message_by_internet_message_id(
            headers, stored_internet_message_id
        )
    elif _looks_like_internet_message_id(source_message_key):
        expected_internet_message_id = source_message_key
        message = _fetch_graph_message_by_internet_message_id(
            headers, source_message_key
        )
    elif source_message_key:
        # Legacy records sometimes stored the Graph resource ID in messageId.
        expected_graph_message_id = source_message_key
        message = _fetch_graph_message_by_id(headers, source_message_key)
    else:
        raise RetryableProcessingError(
            "Processing failure has no resolvable Graph or RFC message identity"
        )

    if not isinstance(message, dict) or not message.get("id"):
        raise RetryableProcessingError("Graph message fetch returned no message id")
    if (
        expected_graph_message_id
        and message.get("id") != expected_graph_message_id
    ):
        raise RetryableProcessingError(
            "Graph message fetch returned a different resource id"
        )
    if (
        expected_internet_message_id
        and message.get("internetMessageId") != expected_internet_message_id
    ):
        raise RetryableProcessingError(
            "Graph message fetch returned a different internetMessageId"
        )

    resolved_graph_message_id = _clean_processing_failure_identity_value(
        message.get("id")
    )
    resolved_internet_message_id = _clean_processing_failure_identity_value(
        message.get("internetMessageId")
    )
    if not source_message_key:
        source_message_key = (
            resolved_internet_message_id or resolved_graph_message_id
        )
    identity = {
        "processingFailureSchemaVersion": PROCESSING_FAILURE_SCHEMA_VERSION,
        "sourceMessageKey": source_message_key,
        "graphMessageId": resolved_graph_message_id,
        "internetMessageId": resolved_internet_message_id or None,
    }
    return message, identity


def _processing_failure_source_alias_message(
    data: Mapping[str, Any],
) -> tuple[Dict[str, str], str, str]:
    """Return only typed source aliases that were durably retained."""
    graph_message_id = _clean_processing_failure_identity_value(
        data.get("graphMessageId")
    )
    internet_message_id = _clean_processing_failure_identity_value(
        data.get("internetMessageId")
    )
    source_message_key = _clean_processing_failure_identity_value(
        data.get("sourceMessageKey") or data.get("messageId")
    )
    if not graph_message_id and source_message_key and not (
        source_message_key.startswith("<") and source_message_key.endswith(">")
    ):
        graph_message_id = source_message_key
    if not internet_message_id and (
        source_message_key.startswith("<") and source_message_key.endswith(">")
    ):
        internet_message_id = source_message_key

    aliases: Dict[str, str] = {}
    if graph_message_id:
        aliases["id"] = graph_message_id
    if internet_message_id:
        aliases["internetMessageId"] = internet_message_id
    return aliases, graph_message_id, internet_message_id


def _strict_source_marker_snapshot(ref, *, label: str) -> Optional[Dict[str, Any]]:
    """Read one exact legacy marker without converting ambiguity to absence."""
    try:
        snapshot = ref.get()
        exists = snapshot.exists
    except Exception as exc:
        raise SourceCoordinatorRetryable(
            f"exact legacy {label} marker is unreadable"
        ) from exc
    if type(exists) is not bool:
        raise SourceCoordinatorAmbiguous(
            f"exact legacy {label} marker existence is ambiguous"
        )
    if exists is False:
        return None
    try:
        data = snapshot.to_dict()
    except Exception as exc:
        raise SourceCoordinatorRetryable(
            f"exact legacy {label} marker payload is unreadable"
        ) from exc
    if type(data) is not dict:
        raise SourceCoordinatorAmbiguous(
            f"exact legacy {label} marker payload is malformed"
        )
    return dict(data)


def _strict_legacy_source_marker_disposition(
    user_id: str,
    thread_id: str,
    *,
    graph_message_id: str,
    internet_message_id: str,
) -> str:
    """Classify exact raw legacy markers without writing or failing open."""
    if type(user_id) is not str or not user_id.strip():
        raise SourceCoordinatorConfigError(
            "legacy source marker inspection requires user id"
        )
    if type(thread_id) is not str or not thread_id.strip():
        raise SourceCoordinatorConfigError(
            "legacy source marker inspection requires thread id"
        )
    exact_ids = tuple(
        dict.fromkeys(
            value
            for value in (
                _clean_processing_failure_identity_value(graph_message_id),
                _clean_processing_failure_identity_value(internet_message_id),
            )
            if value
        )
    )
    if not exact_ids:
        raise SourceCoordinatorConfigError(
            "legacy source marker inspection requires a typed source alias"
        )

    try:
        user_ref = _fs.collection("users").document(user_id)
    except Exception as exc:
        raise SourceCoordinatorRetryable(
            "exact legacy source marker root is unavailable"
        ) from exc

    marker_payloads = []
    for message_id in exact_ids:
        payload = _strict_source_marker_snapshot(
            user_ref.collection("processedMessages").document(
                b64url_id(message_id)
            ),
            label="processed-message",
        )
        if payload is not None:
            marker_payloads.append(payload)

    replay_attempt_ids = set()
    for payload in marker_payloads:
        if payload.get("status") != "operator_replay_in_progress":
            continue
        replay_attempt_id = _clean_processing_failure_identity_value(
            payload.get("replayAttemptId")
        )
        if not replay_attempt_id:
            raise SourceCoordinatorAmbiguous(
                "legacy replay claim is missing its attempt binding"
            )
        replay_attempt_ids.add(replay_attempt_id)
    if len(replay_attempt_ids) > 1:
        raise SourceCoordinatorAmbiguous(
            "legacy replay claims disagree on their attempt binding"
        )
    if replay_attempt_ids:
        return "legacy_replay_claim_quarantined"

    thread_data = _strict_source_marker_snapshot(
        user_ref.collection("threads").document(thread_id),
        label="handled-event thread",
    )
    if thread_data is None:
        raise SourceCoordinatorRetryable(
            "exact legacy handled-event thread is missing"
        )
    handled_events = thread_data.get("handledEvents")
    if handled_events is None:
        handled_events = {}
    if type(handled_events) is not dict:
        raise SourceCoordinatorAmbiguous(
            "legacy handled-event marker map is malformed"
        )
    candidates = _message_identity_candidates(*exact_ids)
    handled_marker_found = False
    for event_data in handled_events.values():
        if type(event_data) is not dict:
            raise SourceCoordinatorAmbiguous(
                "legacy handled-event marker payload is malformed"
            )
        if _source_message_match(event_data, candidates):
            handled_marker_found = True
            continue
        if not _source_message_identity_is_present(event_data):
            handled_marker_found = True

    if marker_payloads or handled_marker_found:
        return "legacy_marker_only_ambiguous"
    return "none"


def _record_enforced_processing_retry_error(
    doc,
    *,
    attempts: int,
    max_attempts: int,
    error: Exception,
) -> None:
    """Keep provider/runtime failures visible without creating marker authority."""
    next_attempts = attempts + 1
    try:
        doc.reference.set(
            {
                "processingAttempts": next_attempts,
                "retryable": next_attempts < max_attempts,
                "lastRetryAt": SERVER_TIMESTAMP,
                "lastRetryError": str(error),
                "updatedAt": SERVER_TIMESTAMP,
            },
            merge=True,
        )
    except Exception as update_error:
        print(
            "⚠️ Could not update enforced processing failure retry state: "
            f"{update_error}"
        )


def _verified_enforced_retry_settlement(
    result: Any,
    *,
    user_id: str,
    thread_id: str,
    canonical_source_id: str,
    message: Mapping[str, Any],
) -> bool:
    if not _is_bound_exact_source_settlement(
        result,
        user_id=user_id,
        thread_id=thread_id,
        message=message,
    ):
        return False
    if result.authority.canonical_source_id != canonical_source_id:
        return False
    return verify_settled_source_dispatch_binding(
        _fs,
        user_id=user_id,
        canonical_source_id=canonical_source_id,
        thread_id=thread_id,
        source_alias_keys=result.source_alias_keys,
        snapshot_hash=result.authority.snapshot_hash,
        selection_hash=result.authority.selection_hash,
        owner_kind=result.authority.owner_kind,
        owner_key=result.authority.owner_key,
        ledger_hash=result.authority.ledger_hash,
        settlement_hash=result.settlement.settlement_hash,
        settlement_revision=result.settlement.settlement_revision,
        alias_projection_count=result.settlement.alias_projection_count,
    )


def _retry_processing_failures_enforced(
    user_id: str,
    headers: Dict[str, str],
    docs: List[Any],
    *,
    max_attempts: int,
    max_failure_age_hours: Optional[float],
) -> Dict[str, int]:
    """Retry only through retained canonical B1 authority."""
    result = {"checked": 0, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 0}
    coordinator = build_source_coordinator(_fs)

    for doc in docs:
        result["checked"] += 1
        try:
            raw_data = doc.to_dict()
        except Exception as read_error:
            result["failed"] += 1
            print(
                "⏸️ Enforced exact-source failure record is unreadable: "
                f"{read_error}"
            )
            continue
        if type(raw_data) is not dict:
            result["failed"] += 1
            continue
        data = dict(raw_data)
        thread_id = _clean_processing_failure_identity_value(data.get("threadId"))
        raw_attempts = data.get("processingAttempts", 0)
        if type(raw_attempts) is not int or raw_attempts < 0:
            result["failed"] += 1
            continue
        attempts = raw_attempts
        if (
            data.get("retryable", True) is not True
            or attempts >= max_attempts
            or _is_operator_replay_recovery_status(data.get("recoveryStatus"))
            or data.get("recoveryStatus") == "asset_warning_persistence_failed"
        ):
            result["skipped"] += 1
            continue
        if max_failure_age_hours and max_failure_age_hours > 0:
            failure_time = _timestamp_to_utc(
                data.get("createdAt") or data.get("updatedAt")
            )
            if (
                failure_time
                and datetime.now(timezone.utc) - failure_time
                > timedelta(hours=max_failure_age_hours)
            ):
                result["skipped"] += 1
                continue

        aliases, graph_message_id, internet_message_id = (
            _processing_failure_source_alias_message(data)
        )
        if not thread_id or not aliases:
            result["failed"] += 1
            continue

        try:
            canonical_source_id = (
                coordinator.resolve_existing_canonical_source_id(
                    user_id=user_id,
                    hydrated_message=aliases,
                    evidence_kind="graph_hydration",
                    thread_id=thread_id,
                )
            )
            retained = None
            if canonical_source_id is not None:
                retained = coordinator.quarantine_retained_terminal_authority(
                    user_id=user_id,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    graph_message_id=graph_message_id or None,
                    internet_message_id=internet_message_id or None,
                )
                if retained.canonical_source_id != canonical_source_id:
                    raise SourceCoordinatorAmbiguous(
                        "retained terminal authority changed canonical source"
                    )
                if retained.state == "legacy_terminal_authority_retained":
                    result["skipped"] += 1
                    continue
                if retained.state not in {
                    "no_retained_terminal_authority",
                    "migrated_b1",
                }:
                    raise SourceCoordinatorAmbiguous(
                        "retained terminal authority returned an unknown state"
                    )

            marker_disposition = _strict_legacy_source_marker_disposition(
                user_id,
                thread_id,
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
            )
            if marker_disposition != "none":
                print(
                    "⏸️ Enforced exact-source retry retained legacy marker state: "
                    f"{marker_disposition}"
                )
                result["skipped"] += 1
                continue
            if canonical_source_id is None:
                raise SourceCoordinatorAmbiguous(
                    "processing failure has no retained canonical source authority"
                )
        except Exception as preflight_error:
            result["failed"] += 1
            print(
                "⏸️ Enforced exact-source retry preflight blocked without effects: "
                f"{preflight_error}"
            )
            continue

        try:
            message, _resolved_identity = (
                _fetch_graph_message_for_processing_failure(headers, data)
            )
        except Exception as fetch_error:
            result["failed"] += 1
            _record_enforced_processing_retry_error(
                doc,
                attempts=attempts,
                max_attempts=max_attempts,
                error=fetch_error,
            )
            continue

        try:
            hydrated_canonical_source_id = (
                coordinator.resolve_existing_canonical_source_id(
                    user_id=user_id,
                    hydrated_message=message,
                    evidence_kind="graph_hydration",
                    thread_id=thread_id,
                )
            )
            if hydrated_canonical_source_id != canonical_source_id:
                raise SourceCoordinatorAmbiguous(
                    "hydrated processing-failure aliases changed canonical source"
                )
            hydrated_marker_disposition = (
                _strict_legacy_source_marker_disposition(
                    user_id,
                    thread_id,
                    graph_message_id=_clean_processing_failure_identity_value(
                        message.get("id")
                    ),
                    internet_message_id=_clean_processing_failure_identity_value(
                        message.get("internetMessageId")
                    ),
                )
            )
            if hydrated_marker_disposition != "none":
                result["skipped"] += 1
                continue
        except Exception as binding_error:
            result["failed"] += 1
            print(
                "⏸️ Enforced exact-source retry binding blocked without effects: "
                f"{binding_error}"
            )
            continue

        result["retried"] += 1
        try:
            processing_result = process_inbox_message(
                user_id,
                headers,
                message,
                expected_canonical_source_id=canonical_source_id,
            )
            if not _verified_enforced_retry_settlement(
                processing_result,
                user_id=user_id,
                thread_id=thread_id,
                canonical_source_id=canonical_source_id,
                message=message,
            ):
                raise RetryableProcessingError(
                    "exact-source processing attempt did not produce a verified "
                    "canonical settlement"
                )
            try:
                doc.reference.delete()
            except Exception as cleanup_error:
                result["failed"] += 1
                print(
                    "⚠️ Exact-source settlement is durable but its stale failure "
                    f"record could not be cleared: {cleanup_error}"
                )
                continue
            result["succeeded"] += 1
        except (SourceCoordinatorConflict, SourceCoordinatorAmbiguous) as authority_error:
            result["failed"] += 1
            _record_enforced_processing_retry_error(
                doc,
                attempts=attempts,
                max_attempts=max_attempts,
                error=authority_error,
            )
            print(
                "⏸️ Enforced exact-source retry authority blocked without effects: "
                f"{authority_error}"
            )
        except Exception as processing_error:
            result["failed"] += 1
            _record_enforced_processing_retry_error(
                doc,
                attempts=attempts,
                max_attempts=max_attempts,
                error=processing_error,
            )

    if result["checked"]:
        print(
            "🔁 Processing failure retry: "
            f"checked={result['checked']}, retried={result['retried']}, "
            f"succeeded={result['succeeded']}, failed={result['failed']}, "
            f"skipped={result['skipped']}"
        )
    return result


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
    source_mode = resolve_source_coordinator_mode(os.environ)
    if source_mode is CoordinatorMode.SHADOW:
        return result
    try:
        failures_ref = _fs.collection("users").document(user_id).collection("processingFailures")
        query = failures_ref.limit(limit) if hasattr(failures_ref, "limit") else failures_ref
        docs = list(query.stream())
    except Exception as e:
        print(f"⚠️ Could not read processing failures for retry: {e}")
        return result

    if source_mode is CoordinatorMode.ENFORCED:
        return _retry_processing_failures_enforced(
            user_id,
            headers,
            docs,
            max_attempts=max_attempts,
            max_failure_age_hours=max_failure_age_hours,
        )

    for doc in docs:
        result["checked"] += 1
        data = doc.to_dict() or {}
        message_id = (
            data.get("sourceMessageKey")
            or data.get("messageId")
            or data.get("internetMessageId")
            or data.get("graphMessageId")
        )
        thread_id = data.get("threadId")
        client_id = data.get("clientId")
        attempts = int(data.get("processingAttempts") or 0)
        terminal_disposition = _terminal_retry_disposition(
            user_id,
            thread_id,
            message_id,
            graph_message_id=data.get("graphMessageId"),
            internet_message_id=data.get("internetMessageId"),
        )
        terminal_kind = terminal_disposition.get("kind")
        terminal_source_exact = (
            terminal_disposition.get("exactSourceConfirmed") is True
        )
        terminal_retry_reserved = terminal_source_exact and terminal_kind in {
            "active",
            "settled",
        }
        exact_terminal_saga = (
            terminal_disposition.get("saga")
            if terminal_retry_reserved and terminal_kind == "active"
            else None
        )
        if (
            terminal_kind == "settled"
            and terminal_source_exact
        ):
            # The authoritative snapshot proved this exact source completed.
            # Clear only its stale retry record; do not evaluate campaign,
            # artifacts, manual continuation, Graph, or generic processing.
            try:
                doc.reference.delete()
            except Exception as cleanup_error:
                print(
                    "⚠️ Could not clear exact settled processing failure: "
                    f"{cleanup_error}"
                )
            result["skipped"] += 1
            continue

        if terminal_kind in {"active", "settled"} and not terminal_source_exact:
            try:
                doc.reference.set({
                    "retryable": False,
                    "recoveryStatus": "terminal_source_identity_unconfirmed",
                    "lastRetryAt": SERVER_TIMESTAMP,
                    "lastRetryError": (
                        "Terminal retry source matched only an untyped alias; "
                        "leaving visible for manual identity review."
                    ),
                    "updatedAt": SERVER_TIMESTAMP,
                }, merge=True)
            except Exception as update_error:
                print(
                    "⚠️ Could not preserve provisional terminal retry: "
                    f"{update_error}"
                )
            result["skipped"] += 1
            continue

        if (
            not terminal_retry_reserved
            and _is_operator_replay_recovery_status(data.get("recoveryStatus"))
        ):
            result["skipped"] += 1
            continue

        if (
            not terminal_retry_reserved
            and data.get("recoveryStatus") == "asset_warning_persistence_failed"
        ):
            result["skipped"] += 1
            continue

        decision = (
            get_client_automation_decision(user_id, client_id)
            if not terminal_retry_reserved
            else None
        )
        suppression_kind = (
            classify_campaign_suppression(decision)
            if decision is not None
            else None
        )
        if suppression_kind and not terminal_retry_reserved:
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

        if not message_id or (
            not terminal_retry_reserved
            and (not data.get("retryable", True) or attempts >= max_attempts)
        ):
            result["skipped"] += 1
            continue

        if not terminal_retry_reserved and has_processed(user_id, message_id):
            doc.reference.delete()
            result["skipped"] += 1
            continue

        if not terminal_retry_reserved:
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

        if not terminal_retry_reserved and max_failure_age_hours and max_failure_age_hours > 0:
            failure_time = _timestamp_to_utc(data.get("createdAt") or data.get("updatedAt"))
            if failure_time and datetime.now(timezone.utc) - failure_time > timedelta(hours=max_failure_age_hours):
                result["skipped"] += 1
                _mark_processing_failure_stale_for_manual_review(doc, max_failure_age_hours)
                continue

        processing_error = None
        msg = None
        try:
            msg, resolved_identity = _fetch_graph_message_for_processing_failure(
                headers, data
            )
            identity_update = {
                **resolved_identity,
                "messageId": resolved_identity["sourceMessageKey"],
                "updatedAt": SERVER_TIMESTAMP,
            }
            identity_needs_update = any(
                data.get(field) != value
                for field, value in resolved_identity.items()
            ) or data.get("messageId") != resolved_identity["sourceMessageKey"]
            terminal_disposition = _terminal_retry_disposition(
                user_id,
                thread_id,
                resolved_identity["sourceMessageKey"],
                graph_message_id=resolved_identity["graphMessageId"],
                internet_message_id=resolved_identity["internetMessageId"],
            )
            terminal_kind = terminal_disposition.get("kind")
            terminal_source_exact = (
                terminal_disposition.get("exactSourceConfirmed") is True
            )
            terminal_retry_reserved = terminal_source_exact and terminal_kind in {
                "active",
                "settled",
            }
            exact_terminal_saga = (
                terminal_disposition.get("saga")
                if terminal_retry_reserved and terminal_kind == "active"
                else None
            )
            if terminal_kind in {"active", "settled"} and not terminal_source_exact:
                raise RetryableProcessingError(
                    "terminal retry source was not exactly confirmed"
                )
            if terminal_kind == "settled":
                try:
                    doc.reference.delete()
                except Exception as cleanup_error:
                    print(
                        "⚠️ Could not clear exact settled processing failure: "
                        f"{cleanup_error}"
                    )
                result["skipped"] += 1
                continue
            if identity_needs_update:
                doc.reference.set(identity_update, merge=True)
            if not exact_terminal_saga:
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
                for key in [
                    resolved_identity["sourceMessageKey"],
                    resolved_identity["graphMessageId"],
                    resolved_identity["internetMessageId"],
                ]
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
    if not _has_processing_failure_record(
        user_id,
        thread_id,
        processed_key,
        graph_message_id=msg.get("id"),
        internet_message_id=msg.get("internetMessageId"),
        source_message_key=processed_key,
    ):
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
    if not _has_processing_failure_record(
        user_id,
        thread_id,
        processed_key,
        graph_message_id=msg.get("id"),
        internet_message_id=msg.get("internetMessageId"),
        source_message_key=processed_key,
    ):
        return False
    disposition = _terminal_retry_disposition(
        user_id,
        thread_id,
        processed_key,
        graph_message_id=msg.get("id"),
        internet_message_id=msg.get("internetMessageId"),
    )
    if (
        disposition.get("kind") in {"active", "settled"}
        and disposition.get("exactSourceConfirmed") is not True
    ):
        return False
    if disposition.get("kind") == "settled":
        return True
    if disposition.get("kind") == "active":
        return False
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
        graph_message_id=msg.get("id"),
        internet_message_id=msg.get("internetMessageId"),
        source_message_key=processed_key,
    )
    mark_processed(user_id, processed_key)
    return True


PDF_LINK_CHANGE_REASON = "Broker PDF attachment uploaded to Drive."
PDF_LINK_COLUMN_ALIASES = {
    "Flyer / Link": ("flyer / link", "flyer/link", "flyer link", "flyer", "flyers", "brochure", "brochures"),
    "Floorplan": ("floorplan", "floorplans", "floor plan", "floor plans", "floor plan / link", "floorplan / link"),
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


def _record_pdf_link_updates(
    sheets,
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
) -> None:
    """Persist AI_META and sheetChangeLog evidence for applied asset links."""
    for column, added_links in (link_updates_by_column or {}).items():
        value = "\n".join(added_links or [])
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
                "source": "pdf_link_write",
            },
        )
        _append_ai_meta(sheets, sheet_id, rownum, column, value, override=False)

    if link_updates_by_column:
        _store_pdf_link_sheet_change(
            user_id,
            client_id,
            sheet_id,
            header,
            rownum,
            rowvals,
            thread_id,
            email,
            pdf_manifest,
            link_updates_by_column,
        )


def _raise_retryable_asset_link_write_failure(
    error: AssetLinkWriteError,
    sheets,
    user_id: str,
    client_id: str,
    sheet_id: str,
    header: List[str],
    rownum: int,
    rowvals: List[str],
    thread_id: str,
    message_id: str,
    email: str,
    pdf_manifest: List[Dict[str, Any]],
    already_applied: Dict[str, List[str]],
) -> None:
    """Reconcile successful cells, expose the failure, and force a safe retry."""
    reconciled = {
        column: list(values or [])
        for column, values in (already_applied or {}).items()
    }
    for column, values in error.applied_updates.items():
        target = reconciled.setdefault(column, [])
        for value in values:
            if value not in target:
                target.append(value)

    if reconciled:
        _record_pdf_link_updates(
            sheets,
            user_id,
            client_id,
            sheet_id,
            header,
            rownum,
            rowvals,
            thread_id,
            email,
            pdf_manifest,
            reconciled,
        )

    _record_ai_processing_failure(
        user_id,
        client_id,
        thread_id,
        message_id,
        str(error),
        retryable=True,
        recovery_status="asset_link_write_partial_failure",
        metadata={
            "appliedAssetLinks": reconciled,
            "createdAssetColumns": error.created_columns,
            "assetColumn": error.canonical_column,
        },
    )
    raise RetryableProcessingError(str(error)) from error


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

def _terminal_thread_blocks_client_completion(doc, data: Dict[str, Any]) -> bool:
    """Fail closed while terminal-protocol work or its permit is unresolved."""
    attempt = data.get("terminalReplyAttempt")
    if (
        data.get("terminalReplyOwed")
        or data.get("terminalNotificationOwed")
        or _has_pending_terminal_saga(data)
        or (
            isinstance(attempt, dict)
            and str(attempt.get("status") or "").strip().lower()
            == "needs_reconciliation"
        )
    ):
        return True

    pointer = data.get("activeGraphSendPermit")
    if pointer is None:
        return False
    if not hasattr(doc, "reference"):
        return True
    try:
        permit = read_active_graph_send_permit(doc.reference, data)
    except Exception:
        return True
    return graph_send_permit_blocks_new_send(permit)


def _maybe_mark_client_completed(
    user_id: str,
    client_id: str,
    *,
    client_ref=None,
    threads_ref=None,
    notifications_ref=None,
    outbox_ref=None,
    pending_responses_ref=None,
    dead_letter_ref=None,
    terminal_graph_reviews_ref=None,
    pending_draft_reviews_ref=None,
) -> bool:
    """Mark a campaign completed once every thread is terminal and no current work remains."""
    if not client_id:
        return False

    try:
        user_ref = None
        if any(ref is None for ref in (
            client_ref,
            threads_ref,
            outbox_ref,
            pending_responses_ref,
            dead_letter_ref,
            terminal_graph_reviews_ref,
            pending_draft_reviews_ref,
        )):
            user_ref = _fs.collection("users").document(user_id)
        if client_ref is None:
            client_ref = user_ref.collection("clients").document(client_id)
        if threads_ref is None:
            threads_ref = user_ref.collection("threads")
        if notifications_ref is None:
            notifications_ref = client_ref.collection("notifications")
        if outbox_ref is None:
            outbox_ref = user_ref.collection("outbox")
        if pending_responses_ref is None:
            pending_responses_ref = user_ref.collection("pendingResponses")
        if dead_letter_ref is None:
            dead_letter_ref = user_ref.collection("deadLetterQueue")
        if terminal_graph_reviews_ref is None:
            terminal_graph_reviews_ref = user_ref.collection(
                "terminalGraphSendReviews"
            )
        if pending_draft_reviews_ref is None:
            pending_draft_reviews_ref = user_ref.collection(
                "graphSendDraftReviews"
            )

        client_snapshot = client_ref.get()
        client_data = client_snapshot.to_dict() if getattr(client_snapshot, "exists", False) else {}
        status = str((client_data or {}).get("status") or "").strip().lower()
        if status in {"stopping", "stopped", "archived", "deleted"}:
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
        unresolved_terminal_protocol_threads = []
        for doc in thread_docs:
            data = doc.to_dict() or {}
            thread_status = str(data.get("status") or THREAD_STATUS["active"]).strip().lower()
            if thread_status in TERMINAL_THREAD_STATUSES:
                terminal_threads.append(doc)
            else:
                active_threads.append(doc)
            if _terminal_thread_blocks_client_completion(doc, data):
                unresolved_terminal_protocol_threads.append(doc)

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

        pending_response_docs = list(
            pending_responses_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        )
        unresolved_dead_letter_docs = []
        for doc in (
            dead_letter_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        ):
            data = doc.to_dict() or {}
            dead_letter_status = str(data.get("status") or "").strip().lower()
            recovery_status = str(data.get("recoveryStatus") or "").strip().lower()
            if (
                dead_letter_status not in RESOLVED_DEAD_LETTER_STATUSES
                and recovery_status not in RESOLVED_DEAD_LETTER_STATUSES
            ):
                unresolved_dead_letter_docs.append(doc)

        unresolved_terminal_graph_reviews = []
        for doc in (
            terminal_graph_reviews_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        ):
            data = doc.to_dict() or {}
            review_status = str(data.get("status") or "").strip().lower()
            if review_status not in RESOLVED_TERMINAL_GRAPH_REVIEW_STATUSES:
                unresolved_terminal_graph_reviews.append(doc)

        unresolved_pending_draft_reviews = []
        for doc in (
            pending_draft_reviews_ref
            .where(filter=FieldFilter("clientId", "==", client_id))
            .stream()
        ):
            data = doc.to_dict() or {}
            review_status = str(data.get("status") or "").strip().lower()
            if review_status not in RESOLVED_PENDING_DRAFT_REVIEW_STATUSES:
                unresolved_pending_draft_reviews.append(doc)

        if (
            active_threads
            or action_docs
            or outbox_docs
            or pending_response_docs
            or unresolved_dead_letter_docs
            or unresolved_terminal_protocol_threads
            or unresolved_terminal_graph_reviews
            or unresolved_pending_draft_reviews
        ):
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
                "pendingResponses": len(pending_response_docs),
                "unresolvedDeadLetters": len(unresolved_dead_letter_docs),
                "unresolvedTerminalProtocolThreads": len(
                    unresolved_terminal_protocol_threads
                ),
                "unresolvedTerminalGraphReviews": len(
                    unresolved_terminal_graph_reviews
                ),
                "unresolvedPendingDraftReviews": len(
                    unresolved_pending_draft_reviews
                ),
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


_ADDRESS_LED_TERMINAL_SEPARATOR_RE = re.compile(
    r",?\s+(?:and|but|or)\s+(?=\d{1,6}\s+[a-z])",
    re.IGNORECASE,
)
_TERMINAL_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])[ \t]+")
_TERMINAL_ABBREVIATION_RE = re.compile(
    r"\b((?:[a-z]\.)*[a-z]+)\.$",
    re.IGNORECASE,
)
_TERMINAL_MEASUREMENT_ABBREVIATIONS = {
    "ft", "in", "sf",
}
_TERMINAL_ABBREVIATED_PROPERTY_CONTINUATIONS = {
    "bldg", "ste", "whse",
}
_TERMINAL_MEASUREMENT_CONTINUATION_WORDS = {
    "across", "at", "building", "buildings", "by", "ceiling", "clear",
    "clearance", "deep", "depth", "door", "doors", "facility", "facilities",
    "high", "height", "listing", "listings", "long", "maximum", "minimum",
    "of", "premises", "property", "properties", "site", "sites", "space",
    "spaces", "suite", "suites", "tall", "unit", "units", "warehouse",
    "warehouses", "wide", "width", "to",
} | _TERMINAL_ABBREVIATED_PROPERTY_CONTINUATIONS
_TERMINAL_TITLE_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof",
}
_TERMINAL_IDENTIFIER_ABBREVIATIONS = {
    "bldg", "ste", "whse",
}
_TERMINAL_SENTENCE_STARTERS = {
    "a", "an", "he", "i", "it", "our", "she", "that", "the", "these",
    "they", "this", "those", "we", "you",
}
_TERMINAL_NUMERIC_NOUN_PHRASE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:both|each)(?:\s+of\s+(?:the\s+)?)?|"
    r"a|an|our|that|the|their|these|this|those|your)\s+(?:±\s*)?\d",
    re.IGNORECASE,
)
_TERMINAL_CONTEXTUAL_IDENTIFIER_TOKEN_PATTERN = r"(?:[A-Z]{1,8}|[?!])"
_TERMINAL_CONTEXTUAL_IDENTIFIER_LINK_PATTERN = (
    r"(?:[ \t]+(?i:[a-z]+(?:[-'’][a-z]+)*))?"
    r"[ \t]+(?i:(?:for|at|across|covering|serving|assigned[ \t]+to))\b"
)
_TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_HEAD_RE = re.compile(
    rf"^(?i:no)\.[ \t]+{_TERMINAL_CONTEXTUAL_IDENTIFIER_TOKEN_PATTERN}"
    rf"{_TERMINAL_CONTEXTUAL_IDENTIFIER_LINK_PATTERN}"
)
_TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_NO_RE = re.compile(
    rf"^{_TERMINAL_CONTEXTUAL_IDENTIFIER_TOKEN_PATTERN}"
    rf"{_TERMINAL_CONTEXTUAL_IDENTIFIER_LINK_PATTERN}"
)
_TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_PUNCTUATION_RE = re.compile(
    r"^(?:(?i:[a-z]+(?:[-'’][a-z]+)*)[ \t]+)?"
    r"(?i:(?:for|at|across|covering|serving|assigned[ \t]+to))\b"
)


def _terminal_boundary_continues_contextual_identifier(
    text: str,
    boundary_start: int,
    boundary_end: int,
    fragment_start: int,
) -> bool:
    """Retain a bounded identifier structurally without promoting its meaning."""
    local_prefix = re.split(
        r"\n+|\s*;\s*|,\s*(?:while|whereas)\s+|"
        r"\s+(?:while|whereas)\s+",
        (text or "")[fragment_start:boundary_start],
        flags=re.IGNORECASE,
    )[-1]
    if not _TERMINAL_NUMERIC_NOUN_PHRASE_PREFIX_RE.search(local_prefix):
        return False

    following = (text or "")[boundary_end:]
    if re.search(r"\b(?:bldg|ste|whse)\.$", local_prefix, re.IGNORECASE):
        return bool(
            _TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_HEAD_RE.match(following)
        )
    if re.search(
        r"\b(?:bldg|ste|whse)\.\s+no\.$",
        local_prefix,
        re.IGNORECASE,
    ):
        return bool(
            _TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_NO_RE.match(following)
        )
    if re.search(
        r"\b(?:bldg|ste|whse)\.\s+no\.\s+[?!]$",
        local_prefix,
        re.IGNORECASE,
    ):
        return bool(
            _TERMINAL_CONTEXTUAL_IDENTIFIER_AFTER_PUNCTUATION_RE.match(
                following
            )
        )
    return False


def _clause_owns_property_assertion(clause: str) -> bool:
    return bool(
        _VIABILITY_RE.search(clause or "")
        or _looks_like_requirements_mismatch_nonviable(clause)
        or any(
            re.search(pattern, clause or "", re.IGNORECASE)
            for _reason, pattern in _UNAVAILABLE_PATTERNS
        )
    )


def _terminal_period_continues_abbreviation(
    text: str,
    boundary_start: int,
    boundary_end: int,
    fragment_start: int,
) -> bool:
    """Keep only context-proven abbreviation periods inside a sentence."""
    abbreviation_match = _TERMINAL_ABBREVIATION_RE.search(
        (text or "")[:boundary_start]
    )
    if not abbreviation_match:
        return False

    abbreviation = abbreviation_match.group(1).replace(".", "").lower()
    following = (text or "")[boundary_end:]
    if (
        abbreviation in _TERMINAL_MEASUREMENT_ABBREVIATIONS
        and re.match(r"(?:[x×/]|[-–—])\s*\d", following, re.IGNORECASE)
    ):
        return True

    next_token_match = re.match(r"(?:#?\d+|[a-z]+)", following, re.IGNORECASE)
    if not next_token_match:
        return False
    next_token = next_token_match.group(0)
    next_token_lower = next_token.lower()

    if abbreviation in {"sq", "cu"}:
        return next_token_lower in {"ft", "feet", "in", "inch", "inches"}
    if abbreviation in _TERMINAL_MEASUREMENT_ABBREVIATIONS:
        if next_token_lower not in _TERMINAL_MEASUREMENT_CONTINUATION_WORDS:
            return False
        if (
            next_token[0].islower()
            and next_token_lower
            not in _TERMINAL_ABBREVIATED_PROPERTY_CONTINUATIONS
        ):
            return True
        local_prefix = re.split(
            r"\n+|\s*;\s*|,\s*(?:while|whereas)\s+|"
            r"\s+(?:while|whereas)\s+",
            (text or "")[fragment_start:boundary_start],
            flags=re.IGNORECASE,
        )[-1]
        return bool(_TERMINAL_NUMERIC_NOUN_PHRASE_PREFIX_RE.search(local_prefix))
    if abbreviation in _TERMINAL_TITLE_ABBREVIATIONS:
        return bool(
            next_token[0].isupper()
            and next_token_lower not in _TERMINAL_SENTENCE_STARTERS
        )
    if len(abbreviation) == 1:
        title_initial_prefix = re.search(
            r"\b(?:dr|mr|mrs|ms|prof)\.\s+(?:[a-z]\.\s+)*$",
            (text or "")[:abbreviation_match.start()],
            re.IGNORECASE,
        )
        return bool(
            title_initial_prefix
            and next_token[0].isupper()
            and next_token_lower not in _TERMINAL_SENTENCE_STARTERS
        )
    if abbreviation == "no":
        identifier_owner = re.search(
            r"\b(?:bldg\.?|building|listing|lot|parcel|property|site|space|"
            r"ste\.?|suite|unit|warehouse|whse\.?)\s*$",
            (text or "")[:abbreviation_match.start()],
            re.IGNORECASE,
        )
        return bool(
            identifier_owner
            and (
                next_token[0].isdigit()
                or next_token.startswith("#")
                or (
                    len(next_token) == 1
                    and next_token.isupper()
                    and next_token != "I"
                )
            )
        )
    if abbreviation in _TERMINAL_IDENTIFIER_ABBREVIATIONS:
        stacked_identifier = re.match(
            r"no\.\s+(#?\d+|[a-z]+)",
            following,
            re.IGNORECASE,
        )
        if stacked_identifier:
            identifier_token = stacked_identifier.group(1)
            if (
                identifier_token[0].isdigit()
                or identifier_token.startswith("#")
                or (
                    len(identifier_token) == 1
                    and identifier_token.isupper()
                    and identifier_token != "I"
                )
            ):
                return True
        return bool(
            next_token[0].isdigit()
            or next_token.startswith("#")
            or (
                len(next_token) == 1
                and next_token.isupper()
            )
        )
    return False


def _terminal_sentence_fragments(message_text: str) -> List[str]:
    """Split real sentence endings while retaining known abbreviation periods."""
    text = message_text or ""
    fragments = []
    fragment_start = 0
    for boundary in _TERMINAL_SENTENCE_BOUNDARY_RE.finditer(text):
        if _terminal_boundary_continues_contextual_identifier(
            text,
            boundary.start(),
            boundary.end(),
            fragment_start,
        ):
            continue
        if (
            text[boundary.start() - 1] == "."
            and _terminal_period_continues_abbreviation(
                text,
                boundary.start(),
                boundary.end(),
                fragment_start,
            )
        ):
            continue
        fragments.append(text[fragment_start:boundary.start()])
        fragment_start = boundary.end()
    fragments.append(text[fragment_start:])
    return fragments


def _terminal_binding_clauses(message_text: str) -> List[str]:
    """Split independent property assertions without separating shared subjects."""
    clauses = [
        clause.strip()
        for sentence in _terminal_sentence_fragments(message_text or "")
        for clause in re.split(
            r"\n+|\s*;\s*|"
            r",\s*(?:while|whereas)\s+|"
            r"\s+(?:while|whereas)\s+|"
            r",\s*and\s+(?=(?:i|we|here|attached|included|please)\b)|"
            r"\s+and\s+(?=(?:i|we|here|attached|included|please)\b)",
            sentence,
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    while True:
        expanded = []
        changed = False
        for clause in clauses:
            for separator in _ADDRESS_LED_TERMINAL_SEPARATOR_RE.finditer(clause):
                left = clause[:separator.start()].strip(" ,")
                right = clause[separator.end():].strip(" ,")
                if (
                    _clause_owns_property_assertion(left)
                    and _clause_owns_property_assertion(right)
                ):
                    expanded.extend((left, right))
                    changed = True
                    break
            else:
                expanded.append(clause)
        clauses = expanded
        if not changed:
            return clauses


_EXPLICIT_OTHER_PROPERTY_RE = re.compile(
    r"\b(?:other|another|alternate|alternative|replacement|different|competing)\s+"
    r"(?:building|property|space|suite|unit|warehouse|facility|center|centre|park|plaza|campus|complex)\b",
    re.IGNORECASE,
)
_PROPER_NAMED_PROPERTY_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&'’-]*\s+){1,5}"
    r"(?:Center|Centre|Park|Plaza|Campus|Complex|Building|Warehouse|Facility)\b",
)
_LOWER_NAMED_CENTER_RE = re.compile(
    r"\b((?:[a-z][a-z0-9&'’-]*\s+){1,5})"
    r"(?:center|centre|park|plaza|campus|complex)\b",
    re.IGNORECASE,
)
_GENERIC_PROPERTY_NAME_TOKENS = {
    "a", "an", "the", "this", "that", "current", "subject", "target",
    "our", "your", "its", "new", "existing", "industrial", "commercial",
    "commerce", "business", "logistics", "distribution", "office", "warehouse",
}


def _explicit_property_bindings(clause: str, row_anchor: str) -> List[tuple]:
    """Return ``(start, end, target|competing)`` for explicit property claims."""
    bindings = []
    for claim in _street_claim_spans(clause):
        start, end = claim[0], claim[1]
        claim_text = clause[start:end]
        binding = (
            "target"
            if _source_mentions_target_property(claim_text, row_anchor)
            else "competing"
        )
        bindings.append((start, end, binding))

    for pattern in (
        _EXPLICIT_OTHER_PROPERTY_RE,
        _PROPER_NAMED_PROPERTY_RE,
        _LOWER_NAMED_CENTER_RE,
    ):
        for match in pattern.finditer(clause or ""):
            match_text = match.group(0)
            if pattern is _LOWER_NAMED_CENTER_RE:
                name_tokens = set(re.findall(r"[a-z0-9]+", match.group(1).lower()))
                if not name_tokens - _GENERIC_PROPERTY_NAME_TOKENS:
                    continue
            binding = (
                "target"
                if _source_mentions_target_property(match_text, row_anchor)
                else "competing"
            )
            bindings.append((match.start(), match.end(), binding))

    return sorted(set(bindings), key=lambda item: (item[0], item[1], item[2]))


def _nearest_property_binding(
    bindings: List[tuple],
    evidence_start: int,
    evidence_end: int,
) -> Optional[str]:
    if not bindings:
        return None

    def distance(binding: tuple) -> int:
        start, end, _kind = binding
        if end <= evidence_start:
            return evidence_start - end
        if start >= evidence_end:
            return start - evidence_end
        return 0

    nearest_distance = min(distance(binding) for binding in bindings)
    nearest_kinds = {
        binding[2]
        for binding in bindings
        if distance(binding) == nearest_distance
    }
    return next(iter(nearest_kinds)) if len(nearest_kinds) == 1 else None


_COORDINATED_LINK_CLAUSE_WORD_RE = re.compile(
    r"\b(?:and|but|or|nor|while|whereas|although|though|because)\b",
    re.IGNORECASE,
)
_COORDINATED_LINK_AUXILIARIES = {
    "am", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had",
    "can", "could", "will", "would", "shall", "should",
    "may", "might", "must", "ought", "to",
    "appear", "appears", "appeared", "seem", "seems", "seemed",
}
_COORDINATED_LINK_IRREGULAR_ADVERBS = {
    "almost", "already", "also", "even", "ever", "just", "maybe",
    "never", "now", "often", "perhaps", "quite", "rather", "since",
    "still", "then", "yet",
}
_COORDINATED_LINK_VIABILITY_QUALIFIERS = (
    _VIABILITY_QUALIFIER_WORDS | {"unlikely"}
)
_PROPERTY_SET_QUANTIFIER_RE = re.compile(r"\b(?:both|each)\b", re.IGNORECASE)
_PROPERTY_SET_SUBJECT_RE = re.compile(
    r"^(?:(?:of\s+)?(?:the\s+|these\s+|those\s+)?)?"
    r"(?:propert(?:y|ies)|buildings?|sites?|spaces?|listings?|"
    r"warehouses?|facilit(?:y|ies)|premises|units?|suites?)\b",
    re.IGNORECASE,
)
_PROPERTY_SET_SUBJECT_NOUNS = {
    "property", "properties", "building", "buildings", "site", "sites",
    "space", "spaces", "listing", "listings",
    "warehouse", "warehouses", "facility", "facilities", "premises",
    "unit", "units", "suite", "suites",
}
_ABBREVIATED_PROPERTY_SET_SUBJECT_NOUNS = {
    "bldg.", "ste.", "whse.",
}
_BOUNDED_QUANTIFIED_SUBJECT_ABBREVIATION_PATTERN = (
    r"(?:(?:sq|cu)\.(?:ft|in)\.|s\.f\.|(?:sq|cu)\.|ft\.|in\.|sf\.|"
    r"(?:bldg|ste|whse)\.|no\.)"
)
_BOUNDED_QUANTIFIED_SUBJECT_WORD_PATTERN = (
    rf"(?:{_BOUNDED_QUANTIFIED_SUBJECT_ABBREVIATION_PATTERN}|"
    r"[a-z]+(?:[-'’][a-z]+)*(?:['’])?)"
)
_BOUNDED_QUANTIFIED_SUBJECT_NUMBER_PATTERN = (
    r"\d+(?:,\d{3})*(?:\.\d+)?"
)
_BOUNDED_QUANTIFIED_SUBJECT_FOOT_MARK_PATTERN = r"['’′]"
_BOUNDED_QUANTIFIED_SUBJECT_INCH_MARK_PATTERN = r'["”″]'
_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_ATOM_PATTERN = (
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_NUMBER_PATTERN}(?:[a-z]+)?"
    rf"(?:{_BOUNDED_QUANTIFIED_SUBJECT_FOOT_MARK_PATTERN}"
    rf"(?:{_BOUNDED_QUANTIFIED_SUBJECT_NUMBER_PATTERN}"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_INCH_MARK_PATTERN})?|"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_INCH_MARK_PATTERN})?"
)
_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_RANGE_PATTERN = (
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_ATOM_PATTERN}"
    rf"(?:[-–—]{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_ATOM_PATTERN})?"
)
_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_COMPOUND_PATTERN = (
    rf"(?:{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_RANGE_PATTERN}"
    rf"(?:[x×]{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_RANGE_PATTERN})?|"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_ATOM_PATTERN}/"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_ATOM_PATTERN})"
)
_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_PATTERN = (
    rf"#?(?:±)?{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_COMPOUND_PATTERN}"
    r"(?:\+)?(?:-[a-z][a-z0-9]*)*"
)
_BOUNDED_QUANTIFIED_SUBJECT_SYMBOL_PATTERN = (
    r"(?:[x×/±+]|[-–—])"
)
_BOUNDED_QUANTIFIED_SUBJECT_TOKEN_PATTERN = (
    rf"(?:{_BOUNDED_QUANTIFIED_SUBJECT_WORD_PATTERN}|"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_PATTERN}|"
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_SYMBOL_PATTERN})"
)
_BOUNDED_QUANTIFIED_SUBJECT_WORD_RE = re.compile(
    _BOUNDED_QUANTIFIED_SUBJECT_WORD_PATTERN,
    re.IGNORECASE,
)
_BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_RE = re.compile(
    _BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_PATTERN,
    re.IGNORECASE,
)
_BOUNDED_QUANTIFIED_SUBJECT_TOKEN_RE = re.compile(
    _BOUNDED_QUANTIFIED_SUBJECT_TOKEN_PATTERN,
    re.IGNORECASE,
)
_BOUNDED_QUANTIFIED_SUBJECT_RE = re.compile(
    rf"{_BOUNDED_QUANTIFIED_SUBJECT_TOKEN_PATTERN}"
    rf"(?:[ \t]+{_BOUNDED_QUANTIFIED_SUBJECT_TOKEN_PATTERN}){{0,9}}",
    re.IGNORECASE,
)
_QUANTIFIED_ADDRESS_LIST_RELATION_RE = re.compile(
    r"\b(?:for|at|across|covering|serving|assigned\s+to)\s*$",
    re.IGNORECASE,
)
_IMPLICIT_QUANTIFIED_ADDRESS_SUBJECT_RE = re.compile(
    r"(?:of(?:\s+(?:the|these|those))?)?",
    re.IGNORECASE,
)


def _is_bounded_coordinated_viability_link(link_text: str) -> bool:
    """Accept a short auxiliary/adverb bridge, but never another clause."""
    normalized = (link_text or "").strip()
    if normalized.startswith(","):
        normalized = normalized[1:].strip()
    if not normalized:
        return True
    if "," in normalized or _COORDINATED_LINK_CLAUSE_WORD_RE.search(normalized):
        return False
    if not re.fullmatch(
        r"[a-z]+(?:[-'’][a-z]+)*"
        r"(?:[ \t]+[a-z]+(?:[-'’][a-z]+)*){0,7}",
        normalized,
        re.IGNORECASE,
    ):
        return False
    words = normalized.lower().replace("’", "'").split()
    if not all(
        word in _COORDINATED_LINK_AUXILIARIES
        or word in _COORDINATED_LINK_IRREGULAR_ADVERBS
        or word in _VIABILITY_NEGATOR_LINK_WORDS
        or word in _COORDINATED_LINK_VIABILITY_QUALIFIERS
        or word.endswith("ly")
        or word.endswith("n't")
        for word in words
    ):
        return False
    total_negators = _viability_lexical_negator_count(normalized)
    if total_negators == 0:
        return True
    scoped_negators = _viability_prefix_negation_count(f"{normalized} ")
    return bool(
        scoped_negators is not None
        and scoped_negators == total_negators
        and scoped_negators % 2 == 0
    )


def _bounded_subject_is_property_set(subject_text: str) -> Optional[bool]:
    """Classify a bounded noun phrase by its final, possibly possessive head."""
    normalized = (subject_text or "").strip()
    if not normalized or not _BOUNDED_QUANTIFIED_SUBJECT_RE.fullmatch(normalized):
        return None
    tokens = list(_BOUNDED_QUANTIFIED_SUBJECT_TOKEN_RE.finditer(normalized))
    if not tokens:
        return None
    token_texts = [token.group(0) for token in tokens]
    if (
        len(token_texts) >= 3
        and token_texts[-2].lower() == "no."
        and _BOUNDED_QUANTIFIED_SUBJECT_NUMERIC_RE.fullmatch(token_texts[-1])
        and token_texts[-3].lower()
        in _ABBREVIATED_PROPERTY_SET_SUBJECT_NOUNS
    ):
        return True
    head_token = token_texts[-1]
    if not _BOUNDED_QUANTIFIED_SUBJECT_WORD_RE.fullmatch(head_token):
        return False
    head = head_token.lower().rstrip("'’")
    return bool(
        head in _PROPERTY_SET_SUBJECT_NOUNS
        or head in _ABBREVIATED_PROPERTY_SET_SUBJECT_NOUNS
    )


def _address_list_subject_is_property_set(subject_text: str) -> Optional[bool]:
    """Classify a quantified address-list subject, failing closed if explicit."""
    normalized = (subject_text or "").strip(" \t,")
    relation = _QUANTIFIED_ADDRESS_LIST_RELATION_RE.search(normalized)
    if relation:
        normalized = normalized[:relation.start()].strip(" \t,")
    if _IMPLICIT_QUANTIFIED_ADDRESS_SUBJECT_RE.fullmatch(normalized):
        return True
    if _is_bounded_coordinated_viability_link(normalized):
        return None
    classified_subject = _bounded_subject_is_property_set(normalized)
    return classified_subject if classified_subject is not None else False


def _quantified_subject_is_property_set(
    clause: str,
    bindings: List[tuple],
    viability_match: re.Match,
) -> Optional[bool]:
    """Classify a bounded ``each``/``both`` subject before viability.

    True means the quantifier governs the property set. False means it governs
    another explicit subject, including one outside the bounded grammar, so the
    viability predicate must not inherit a nearby address. None means there is
    no quantified subject for this predicate and normal binding should apply.
    """
    prefix = (clause or "")[:viability_match.start()]
    quantifiers = list(_PROPERTY_SET_QUANTIFIER_RE.finditer(prefix))
    if not quantifiers:
        return None

    quantifier = quantifiers[-1]
    intervening_bindings = [
        binding
        for binding in bindings
        if binding[0] >= quantifier.end()
        and binding[1] <= viability_match.start()
    ]
    if intervening_bindings:
        first_binding_start = min(binding[0] for binding in intervening_bindings)
        address_list_subject = _address_list_subject_is_property_set(
            prefix[quantifier.end():first_binding_start]
        )
        if address_list_subject is not None:
            return address_list_subject
        return None

    subject_and_link = prefix[quantifier.end():].strip()
    if _is_bounded_coordinated_viability_link(subject_and_link):
        return True

    property_subject = _PROPERTY_SET_SUBJECT_RE.match(subject_and_link)
    if property_subject and _is_bounded_coordinated_viability_link(
        subject_and_link[property_subject.end():]
    ):
        return True

    if not _BOUNDED_QUANTIFIED_SUBJECT_RE.fullmatch(subject_and_link):
        return None

    subject_tokens = list(
        _BOUNDED_QUANTIFIED_SUBJECT_TOKEN_RE.finditer(subject_and_link)
    )
    for subject_token_count in range(1, len(subject_tokens) + 1):
        subject_end = subject_tokens[subject_token_count - 1].end()
        if _is_bounded_coordinated_viability_link(subject_and_link[subject_end:]):
            return _bounded_subject_is_property_set(subject_and_link[:subject_end])
    return None


def _neither_nor_binds_viability(
    clause: str,
    bindings: List[tuple],
    viability_match: re.Match,
) -> bool:
    preceding_bindings = [
        binding
        for binding in bindings
        if binding[1] <= viability_match.start()
    ]
    if len(preceding_bindings) < 2:
        return False

    first, second = preceding_bindings[-2:]
    previous_end = (
        preceding_bindings[-3][1]
        if len(preceding_bindings) > 2
        else 0
    )
    leading_text = (clause or "")[previous_end:first[0]]
    separator = (clause or "")[first[1]:second[0]]
    link_text = (clause or "")[second[1]:viability_match.start()]
    return bool(
        re.search(r"\bneither\s*$", leading_text, re.IGNORECASE)
        and re.search(r"\bnor\s*$", separator, re.IGNORECASE)
        and _is_bounded_coordinated_viability_link(link_text)
    )


def _viability_match_is_negated(
    clause: str,
    bindings: List[tuple],
    viability_match: re.Match,
) -> bool:
    prefix = (clause or "")[:viability_match.start()]
    return bool(
        _viability_prefix_is_lexically_negated(prefix)
        or _neither_nor_binds_viability(clause, bindings, viability_match)
    )


def _viability_is_shared_across_property_bindings(
    clause: str,
    bindings: List[tuple],
    viability_match: re.Match,
) -> bool:
    """Whether one viability predicate governs a coordinated address list."""
    if len(bindings) < 2 or any(
        end > viability_match.start()
        for _start, end, _kind in bindings
    ):
        return False

    conjunctions = []
    for previous, current in zip(bindings, bindings[1:]):
        separator = (clause or "")[previous[1]:current[0]]
        match = re.fullmatch(
            r"\s*,?\s*(and|but|or)\s*",
            separator,
            re.IGNORECASE,
        )
        if not match:
            return False
        conjunctions.append(match.group(1).lower())

    link_text = (clause or "")[bindings[-1][1]:viability_match.start()]
    if not _is_bounded_coordinated_viability_link(link_text):
        return False

    leading_text = (clause or "")[max(0, bindings[0][0] - 12):bindings[0][0]]
    has_both = bool(
        re.search(r"\bboth\s*$", leading_text, re.IGNORECASE)
        or re.search(r"\bboth\b", link_text, re.IGNORECASE)
    )
    viability_text = viability_match.group(0).lower()
    has_plural_link = bool(
        re.search(r"\b(?:are|remain)\b", link_text, re.IGNORECASE)
        or re.match(r"(?:are|remain)\b", viability_text)
    )
    return has_both or (
        all(word == "and" for word in conjunctions)
        and has_plural_link
    )


def _clause_names_competing_property(clause: str, row_anchor: str) -> bool:
    """Whether a clause explicitly names a non-target street or building."""
    if _source_mentions_target_property(clause, row_anchor):
        return False
    if _street_claim_spans(clause):
        return True
    if _EXPLICIT_OTHER_PROPERTY_RE.search(clause) or _PROPER_NAMED_PROPERTY_RE.search(clause):
        return True
    for match in _LOWER_NAMED_CENTER_RE.finditer(clause or ""):
        name_tokens = set(re.findall(r"[a-z0-9]+", match.group(1).lower()))
        if name_tokens - _GENERIC_PROPERTY_NAME_TOKENS:
            return True
    return False


def _clause_has_terminal_property_evidence(
    clause: str,
    unavailable_keywords: List[str],
) -> bool:
    """Recognize market-unavailable and physical-mismatch terminal evidence."""
    if _clause_has_ancillary_requirements_mismatch(clause):
        return False
    if _detect_target_terminal_reason(clause, None):
        return True
    if _looks_like_requirements_mismatch_nonviable(clause):
        return True
    if _clause_has_ancillary_terminal_evidence(clause):
        return False
    clause_norm = _normalize_replacement_match_text(clause)
    return any(keyword in clause_norm for keyword in unavailable_keywords)


def _clause_has_ancillary_terminal_evidence(clause: str) -> bool:
    """Recognize a terminal phrase scoped to a non-target asset or tour slot."""
    text = clause or ""
    if not (
        _ANCILLARY_SUBJECT_RE.search(text)
        or re.search(r"\bleased\s+separately\b", text, re.IGNORECASE)
    ):
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for _reason, pattern in _UNAVAILABLE_PATTERNS)


def _clause_has_ancillary_requirements_mismatch(clause: str) -> bool:
    """Recognize a physical non-fit scoped only to an ancillary asset."""
    return bool(
        _ANCILLARY_SUBJECT_RE.search(clause or "")
        and _looks_like_requirements_mismatch_nonviable(clause)
    )


def _clause_has_target_terminal_after_ancillary(
    clause: str,
    row_anchor: str,
) -> bool:
    """Keep a later explicit target terminal from being masked by an ancillary one."""
    target_bindings = [
        (start, end)
        for start, end, kind in _explicit_property_bindings(clause, row_anchor)
        if kind == "target"
    ]
    ancillary_spans = [match.span() for match in _ANCILLARY_SUBJECT_RE.finditer(clause or "")]
    for _reason, pattern in _UNAVAILABLE_PATTERNS:
        for terminal in re.finditer(pattern, clause or "", re.IGNORECASE):
            pre = (clause or "")[max(0, terminal.start() - 14):terminal.start()]
            if re.search(r"\b(?:not|isn'?t|aren'?t|no)\s*$", pre, re.IGNORECASE):
                continue
            for _target_start, target_end in target_bindings:
                if target_end > terminal.start():
                    continue
                if not any(
                    target_end <= ancillary_start < terminal.start()
                    for ancillary_start, _ancillary_end in ancillary_spans
                ):
                    return True
    return False


def _message_explicitly_keeps_row_viable(message_text: str, row_anchor: str) -> bool:
    last_explicit_binding = None
    last_explicit_kinds = set()
    last_explicit_binding_count = 0
    for sentence in _terminal_binding_clauses(message_text):
        bindings = _explicit_property_bindings(sentence, row_anchor)
        for viability_match in _VIABILITY_RE.finditer(sentence):
            if _viability_match_is_negated(sentence, bindings, viability_match):
                continue
            quantified_property_set = _quantified_subject_is_property_set(
                sentence,
                bindings,
                viability_match,
            )
            preceding_bindings = [
                binding
                for binding in bindings
                if binding[1] <= viability_match.start()
            ]
            quantified_kinds = (
                {kind for _start, _end, kind in preceding_bindings}
                or last_explicit_kinds
            )
            quantified_binding_count = (
                len(preceding_bindings)
                if preceding_bindings
                else last_explicit_binding_count
            )
            if (
                quantified_property_set is True
                and quantified_binding_count >= 2
                and "target" in quantified_kinds
            ):
                return True
            if quantified_property_set is False:
                continue
            if (
                _viability_is_shared_across_property_bindings(
                    sentence,
                    bindings,
                    viability_match,
                )
                and any(kind == "target" for _start, _end, kind in bindings)
            ):
                return True
            binding = _nearest_property_binding(
                bindings,
                viability_match.start(),
                viability_match.end(),
            )
            if binding is None:
                binding = last_explicit_binding or "target"
            if binding == "target":
                return True
        if bindings:
            last_explicit_binding = bindings[-1][2]
            last_explicit_kinds = {
                kind
                for _start, _end, kind in bindings
            }
            last_explicit_binding_count = len(bindings)
    return False


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

    # Market availability contradicts a stale lease/off-market event, but it does
    # not cure an independent physical requirements mismatch on the same target.
    if (
        _nonviable_status_reason(event) != "requirements_mismatch"
        and row_norm
        and message_norm
        and _message_explicitly_keeps_row_viable(message_text, row_anchor)
    ):
        return False

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

    terminal_bindings = []
    ancillary_terminal_seen = False
    last_explicit_binding = None
    for clause in _terminal_binding_clauses(message_text):
        explicit_bindings = _explicit_property_bindings(clause, row_anchor)
        ancillary_terminal = bool(
            _clause_has_ancillary_terminal_evidence(clause)
            or _clause_has_ancillary_requirements_mismatch(clause)
        )
        if ancillary_terminal:
            ancillary_terminal_seen = True
        if (
            ancillary_terminal
            and _clause_has_target_terminal_after_ancillary(clause, row_anchor)
        ):
            terminal_bindings.append("target")
        elif _clause_has_terminal_property_evidence(clause, keywords):
            if _source_mentions_target_property(clause, row_anchor):
                terminal_bindings.append("target")
            elif _clause_names_competing_property(clause, row_anchor):
                terminal_bindings.append("competing")
            elif last_explicit_binding:
                terminal_bindings.append(last_explicit_binding)
            else:
                terminal_bindings.append("addressless")
        if explicit_bindings:
            last_explicit_binding = explicit_bindings[-1][2]

    if "target" in terminal_bindings:
        return True
    if "competing" in terminal_bindings:
        # Bare language alongside an explicitly competing terminal statement is
        # not enough to terminalize the target. A genuinely bare current-context
        # reply has no competing binding and remains accepted below.
        return False
    if "addressless" in terminal_bindings:
        return True
    if ancillary_terminal_seen:
        return False

    # Addressless property_unavailable events are normal model output. Preserve
    # them only when the fresh message contains no explicit competing terminal
    # evidence that can be grounded deterministically.
    return True


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


def _late_reply_after_followup_exhaustion_patch(
    thread_data: Optional[Dict[str, Any]],
    *,
    message_text: str,
    has_attachments: bool,
) -> Optional[Dict[str, Any]]:
    """Reactivate inbound processing without restarting exhausted follow-ups."""
    data = thread_data or {}
    if (
        data.get("status") != THREAD_STATUS["stopped"]
        or data.get("statusReason") != "max_followups_reached"
        or (_is_no_new_reply_text(message_text) and not has_attachments)
    ):
        return None
    return {
        "status": THREAD_STATUS["active"],
        "statusReason": "late_reply_after_max_followups",
        "followUpStatus": "max_reached",
        "hasInboundReply": True,
        "lastInboundAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }


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
    truth_locked_mismatch = scenario.startswith("requirements_mismatch")
    if (
        llm_response_email
        and not truth_locked_mismatch
        and not _response_requests_nonrequestable_fields(
        llm_response_email,
        column_config,
        )
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
        "requirements_mismatch_with_alternative": f"""{greeting}

Thank you for clarifying that the current property does not meet the requirements, and for suggesting the alternative property.

I'll review the alternative and get back to you if I have any questions.""",
        "requirements_mismatch": f"""{greeting}

Thank you for clarifying that the property does not meet the requirements.

Do you have any other properties that might be a better fit?""",
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
    if found_keyword == "requirements_mismatch":
        base = f"[{current_date}] Property does not meet client requirements"
    else:
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


def _terminal_note_date(message: Dict[str, Any]) -> str:
    """Return a retry-stable UTC date from the inbound Graph envelope."""
    for field_name in ("receivedDateTime", "sentDateTime"):
        raw_value = str((message or {}).get(field_name) or "").strip()
        if not raw_value:
            continue
        try:
            normalized = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%m/%d/%Y")
        except (TypeError, ValueError):
            continue
    return datetime.now(timezone.utc).strftime("%m/%d/%Y")


def _terminal_note_range(tab_title: str, notes_column_index: int, row_number: int) -> str:
    """Build a safely quoted A1 range from 1-based row/column indexes."""
    if not isinstance(notes_column_index, int) or notes_column_index < 1:
        raise ValueError("notes_column_index must be a positive 1-based column")
    if not isinstance(row_number, int) or row_number < 1:
        raise ValueError("row_number must be a positive 1-based row")
    safe_title = str(tab_title or "").replace("'", "''")
    if not safe_title:
        raise ValueError("tab_title is required for terminal note persistence")
    return f"'{safe_title}'!{_col_letter(notes_column_index)}{row_number}"


def _terminal_sheet_header_fingerprint(header: List[str]) -> str:
    projection = [str(value or "").strip() for value in (header or [])]
    return hashlib.sha256(
        json.dumps(projection, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_terminal_saga_sheet_layout(
    saga: Dict[str, Any],
    header: List[str],
) -> int:
    notes_index, expected_name, expected_fingerprint = (
        _validate_terminal_saga_sheet_layout_binding(saga)
    )
    if (
        notes_index > len(header or [])
        or str((header or [])[notes_index - 1] or "").strip() != expected_name
        or _terminal_sheet_header_fingerprint(header) != expected_fingerprint
    ):
        raise RetryableProcessingError(
            "terminal saga Sheet header/Notes coordinate drifted"
        )
    return notes_index


def _validate_terminal_saga_sheet_layout_binding(
    saga: Dict[str, Any],
) -> tuple[int, str, str]:
    notes_index = saga.get("notesColumnIndex")
    expected_name = saga.get("notesColumnHeader")
    expected_fingerprint = saga.get("sheetHeaderFingerprint")
    if notes_index is None:
        raise RetryableProcessingError(
            "terminal saga persisted no Notes/Comments column; "
            "automatic coordinate rebinding is forbidden"
        )
    if (
        isinstance(notes_index, bool)
        or not isinstance(notes_index, int)
        or notes_index < 1
        or not str(expected_name or "").strip()
        or not str(expected_fingerprint or "").strip()
    ):
        raise RetryableProcessingError(
            "terminal saga Sheet layout binding is missing or malformed"
        )
    return notes_index, str(expected_name).strip(), str(expected_fingerprint).strip()


def _read_terminal_note(
    sheets,
    spreadsheet_id: str,
    tab_title: str,
    row_number: int,
    notes_column_index: Optional[int],
) -> str:
    note_range = _terminal_note_range(tab_title, notes_column_index, row_number)
    response = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=note_range,
        ),
        "read_terminal_note",
    )
    values = response.get("values", []) if isinstance(response, dict) else []
    if not values or not values[0]:
        return ""
    return str(values[0][0] or "").strip()


def _merge_terminal_note(existing_note: str, terminal_note: str) -> str:
    existing = str(existing_note or "").strip()
    durable_note = str(terminal_note or "").strip()
    if not durable_note:
        raise ValueError("terminal note cannot be blank")
    if durable_note in existing:
        return existing
    return f"{existing} | {durable_note}" if existing else durable_note


def _ensure_terminal_note(
    sheets,
    spreadsheet_id: str,
    tab_title: str,
    row_number: int,
    notes_column_index: int,
    terminal_note: str,
) -> str:
    """Idempotently validate or repair a terminal note on an existing row."""
    existing_note = _read_terminal_note(
        sheets,
        spreadsheet_id,
        tab_title,
        row_number,
        notes_column_index,
    )
    merged_note = _merge_terminal_note(existing_note, terminal_note)
    if merged_note == existing_note:
        return merged_note

    note_range = _terminal_note_range(tab_title, notes_column_index, row_number)
    _execute_with_retry(
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=note_range,
            valueInputOption="RAW",
            body={"values": [[merged_note]]},
        ),
        "repair_terminal_note",
    )
    return merged_note


def _nonviable_status_reason(event: Dict[str, Any]) -> str:
    reason = _event_text(event or {}, "reason") or "property_unavailable"
    normalized = reason.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {
        "requirements_mismatch", "physical_non_fit", "physical_mismatch",
        "bad_fit", "requirements_non_fit",
    }:
        return "requirements_mismatch"
    return normalized


def _pending_nonviable_followup_patch(
    events: List[Dict[str, Any]],
    *,
    row_anchor: str,
    message_text: str,
) -> Optional[Dict[str, Any]]:
    """Build the legacy fail-closed follow-up patch for terminal intent.

    The immutable saga owns persistence in the full pipeline; this pure helper
    remains the shared semantic contract for follow-up guards and regressions.
    """
    for event in events or []:
        if (event or {}).get("type") != "property_unavailable":
            continue
        if not _property_unavailable_event_applies_to_row(
            event,
            row_anchor=row_anchor,
            message_text=message_text,
            unavailable_keywords=PROPERTY_UNAVAILABLE_KEYWORDS,
        ):
            continue
        return {
            "followUpStatus": "stopped",
            "followUpConfig.nextFollowUpAt": None,
            "followUpConfig.processingBy": None,
            "followUpConfig.processingAt": None,
            "pendingTerminalReason": _nonviable_status_reason(event),
            "pendingTerminalAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        }
    return None


TERMINAL_SAGA_VERSION = 2
FIRESTORE_BATCH_WRITE_LIMIT = 500
TERMINAL_SAGA_EXECUTION_LEASE_SECONDS = 300
TERMINAL_SHEET_MUTATION_VERSION = 2
TERMINAL_SHEET_PROVIDER_DEADLINE_SECONDS = 60
TERMINAL_SHEET_READBACK_DEADLINE_SECONDS = 30
TERMINAL_SHEET_MUTATION_ATTEMPT_LIMIT = 8
TERMINAL_SHEET_MUTATION_HISTORY_LIMIT = TERMINAL_SHEET_MUTATION_ATTEMPT_LIMIT - 1


@dataclass(frozen=True)
class TerminalSagaExecution:
    owner: str
    fencing_token: int


def _terminal_source_message_key(message_id: str, internet_message_id: str) -> str:
    source_key = str(internet_message_id or message_id or "").strip()
    if not source_key:
        raise ValueError("terminal saga requires an exact source message identity")
    return source_key


def _terminal_event_key_for_source(
    event_type: str,
    message_id: str,
    internet_message_id: str,
) -> str:
    """Bind terminal event admission to one immutable inbound source."""
    source_key = _terminal_source_message_key(message_id, internet_message_id)
    source_hash = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"{event_type}:source:{source_hash}"


def _thread_handled_event_record(
    thread_data: Dict[str, Any],
    event_key: str,
) -> Any:
    """Read both native Firestore maps and flattened local-test projections."""
    nested = (thread_data or {}).get("handledEvents")
    if isinstance(nested, dict) and event_key in nested:
        return nested.get(event_key)
    return (thread_data or {}).get(f"handledEvents.{event_key}")


def _is_explicit_same_contact_replacement_reactivation(
    thread_data: Dict[str, Any],
) -> bool:
    data = thread_data or {}
    replacement = _active_replacement_context(data)
    return bool(
        data.get("status") == THREAD_STATUS["active"]
        and data.get("statusReason") == "same_contact_replacement_reply"
        and replacement is not None
        and data.get("rowNumber") == replacement.get("rowNumber")
    )


def _terminal_event_is_handled_for_source(
    thread_data: Dict[str, Any],
    event_type: str,
    event_key: str,
    message_id: str,
    internet_message_id: str,
) -> bool:
    """Honor exact v2 markers and narrowly scoped legacy terminal markers.

    Legacy thread-wide markers remain effective for their recorded source, and
    unsourced evidence remains fail-closed while any terminal evidence remains.
    Only an explicitly sourced mismatch can be scoped away after an exact
    same-contact replacement reactivation/rebind.
    """
    if _thread_handled_event_record(thread_data, event_key) is not None:
        return True

    legacy = _thread_handled_event_record(thread_data, event_type)
    if legacy is None:
        return False
    candidates = _message_identity_candidates(message_id, internet_message_id)
    if isinstance(legacy, dict) and _source_message_match(legacy, candidates):
        return True
    if (
        isinstance(legacy, dict)
        and _source_message_identity_is_present(legacy)
        and _is_explicit_same_contact_replacement_reactivation(thread_data)
    ):
        # The active row is a new property generation. Historical terminal
        # timestamps from the prior row cannot broaden an explicitly sourced
        # legacy marker to this different inbound message. Unsourced legacy
        # evidence remains fail-closed below.
        return False

    still_terminal = (
        (thread_data or {}).get("status") == THREAD_STATUS["stopped"]
        or bool((thread_data or {}).get("nonViableAt"))
        or bool((thread_data or {}).get("nonViableReason"))
    )
    return still_terminal


def _validate_terminal_saga_immutable_hash(saga: Dict[str, Any]) -> None:
    expected_hash = str((saga or {}).get("immutableHash") or "").strip()
    immutable_payload = {
        key: value
        for key, value in (saga or {}).items()
        if key not in {"immutableHash", "phase", "finalRow"}
    }
    actual_hash = hashlib.sha256(
        json.dumps(immutable_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if not expected_hash or actual_hash != expected_hash:
        raise RetryableProcessingError(
            "immutable terminal saga hash does not match persisted payload"
        )


def _terminal_saga_for_source(
    thread_data: Dict[str, Any],
    message_id: str,
    internet_message_id: str,
) -> Optional[Dict[str, Any]]:
    saga = (thread_data or {}).get("terminalSaga")
    if not isinstance(saga, dict):
        return None
    expected_key = _terminal_source_message_key(message_id, internet_message_id)
    if saga.get("sourceMessageKey") != expected_key:
        return None
    if saga.get("sourceGraphMessageId") and saga.get("sourceGraphMessageId") != message_id:
        return None
    if (
        saga.get("sourceInternetMessageId")
        and saga.get("sourceInternetMessageId") != internet_message_id
    ):
        return None
    _validate_terminal_saga_immutable_hash(saga)
    return dict(saga)


def _has_pending_terminal_saga(thread_data: Optional[Dict[str, Any]]) -> bool:
    """Fail closed while another exact source owns a staged/finalized saga."""
    data = thread_data or {}
    return bool(
        data.get("terminalSagaKey")
        or data.get("pendingTerminalReason")
        or isinstance(data.get("terminalSaga"), dict)
        or isinstance(data.get("terminalSagaClaim"), dict)
    )


def _terminal_saga_for_retry_source(
    user_id: str,
    thread_id: str,
    *message_ids: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper over the authoritative retry disposition."""
    disposition = _terminal_retry_disposition(
        user_id,
        thread_id,
        *message_ids,
    )
    return (
        disposition.get("saga")
        if disposition.get("kind") == "active"
        else None
    )


def _terminal_retry_disposition(
    user_id: str,
    thread_id: str,
    *message_ids: Optional[str],
    graph_message_id: Optional[str] = None,
    internet_message_id: Optional[str] = None,
    firestore_client=None,
    transaction=None,
) -> Dict[str, Any]:
    """Classify one source from one authoritative thread snapshot.

    ``active`` must resume the frozen saga, ``settled`` must perform no generic
    retry/batch work, and ``ordinary`` may enter the normal pipeline.  Every
    retained settlement is fully self-validated even when it does not match.
    """
    if not user_id or not thread_id:
        return {"kind": "ordinary", "saga": None, "settlement": None}
    candidates = {
        str(value).strip()
        for value in message_ids
        if str(value or "").strip()
    }
    graph_source = str(graph_message_id or "").strip()
    internet_source = str(internet_message_id or "").strip()
    if not candidates and not graph_source and not internet_source:
        return {"kind": "ordinary", "saga": None, "settlement": None}
    try:
        active_firestore = (
            _fs if firestore_client is None else firestore_client
        )
        thread_ref = (
            active_firestore.collection("users")
            .document(user_id)
            .collection("threads")
            .document(thread_id)
        )
        snapshot = (
            thread_ref.get()
            if transaction is None
            else thread_ref.get(transaction=transaction)
        )
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal retry disposition lookup failed: {exc}"
        ) from exc
    if snapshot.exists is not True:
        raise RetryableProcessingError(
            "authoritative terminal thread is missing during retry disposition"
        )
    data = snapshot.to_dict() or {}
    records = []
    saga = (data or {}).get("terminalSaga")
    if isinstance(saga, dict):
        _validate_terminal_saga_immutable_hash(saga)
        records.append(("active", dict(saga)))
    elif _has_pending_terminal_saga(data):
        raise RetryableProcessingError(
            "terminal retry disposition found partial active-saga markers"
        )

    for settlement in _validate_terminal_settlement_history(
        (data or {}).get("terminalSettlements")
    ):
        records.append(("settled", dict(settlement)))

    def aliases(record):
        return {
            str(value).strip()
            for value in (
                record.get("sourceMessageKey"),
                record.get("sourceGraphMessageId"),
                record.get("sourceInternetMessageId"),
            )
            if str(value or "").strip()
        }

    untyped_matches = {
        index
        for index, (_kind, record) in enumerate(records)
        if candidates.intersection(aliases(record))
    }
    graph_matches = {
        index
        for index, (_kind, record) in enumerate(records)
        if graph_source
        and str(record.get("sourceGraphMessageId") or "").strip()
        == graph_source
    }
    internet_matches = {
        index
        for index, (_kind, record) in enumerate(records)
        if internet_source
        and str(record.get("sourceInternetMessageId") or "").strip()
        == internet_source
    }
    typed_source_provided = bool(graph_source or internet_source)
    matches = (
        graph_matches | internet_matches | untyped_matches
        if typed_source_provided
        else untyped_matches
    )
    if len(matches) > 1:
        raise RetryableProcessingError(
            "terminal retry disposition received contradictory source aliases"
        )
    if not matches:
        return {
            "kind": "ordinary",
            "saga": None,
            "settlement": None,
            "exactSourceConfirmed": False,
        }

    matched_index = next(iter(matches))
    matched_kind, matched_record = records[matched_index]
    matched_aliases = aliases(matched_record)
    persisted_graph = str(
        matched_record.get("sourceGraphMessageId") or ""
    ).strip()
    persisted_internet = str(
        matched_record.get("sourceInternetMessageId") or ""
    ).strip()
    exact_source_confirmed = False
    if typed_source_provided:
        if (
            (graph_source and persisted_graph and graph_source != persisted_graph)
            or (
                internet_source
                and persisted_internet
                and internet_source != persisted_internet
            )
            or any(candidate not in matched_aliases for candidate in candidates)
        ):
            raise RetryableProcessingError(
                "terminal retry disposition received contradictory source aliases"
            )
        graph_exact = bool(graph_source) and persisted_graph == graph_source
        internet_exact = (
            bool(internet_source) and persisted_internet == internet_source
        )
        every_supplied_type_exact = (
            (not graph_source or graph_exact)
            and (not internet_source or internet_exact)
        )
        exact_source_confirmed = bool(
            (graph_exact or internet_exact) and every_supplied_type_exact
        )

    return {
        "kind": matched_kind,
        "saga": dict(matched_record) if matched_kind == "active" else None,
        "settlement": (
            dict(matched_record) if matched_kind == "settled" else None
        ),
        "exactSourceConfirmed": exact_source_confirmed,
    }


def _load_retained_terminal_authority(
    fs_client,
    user_id: str,
    thread_id: str,
    *,
    graph_message_id: Optional[str],
    internet_message_id: Optional[str],
    transaction,
) -> Dict[str, Any]:
    """Reviewed production bridge to the strict retained Terminal A loader."""
    return _terminal_retry_disposition(
        user_id,
        thread_id,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        firestore_client=fs_client,
        transaction=transaction,
    )


def build_source_coordinator(fs_client) -> "SourceCoordinator":
    """Construct the production B1 coordinator with reviewed dependencies."""
    def retained_terminal_authority_loader(
        user_id: str,
        thread_id: str,
        *,
        graph_message_id: Optional[str],
        internet_message_id: Optional[str],
        transaction,
    ) -> Dict[str, Any]:
        return _load_retained_terminal_authority(
            fs_client,
            user_id,
            thread_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            transaction=transaction,
        )

    return SourceCoordinator(
        fs_client,
        uuid_factory=lambda: str(uuid4()),
        now_factory=lambda: datetime.now(timezone.utc),
        local_source_policy_verifier=_verify_local_source_policy,
        retained_terminal_authority_loader=retained_terminal_authority_loader,
    )


def _preview_nonviable_divider(
    sheets,
    spreadsheet_id: str,
    tab_title: str,
) -> Dict[str, Any]:
    """Read the existing or would-be divider row without mutating the Sheet."""
    safe_title = str(tab_title or "").replace("'", "''")
    response = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{safe_title}'!A:A",
        ),
        "preview_nonviable_divider",
    )
    rows = response.get("values", []) if isinstance(response, dict) else []
    for row_number, row in enumerate(rows, start=1):
        if row and str(row[0]).strip().upper() == "NON-VIABLE":
            return {"dividerRow": row_number, "exists": True}
    return {"dividerRow": (len(rows) + 1) if rows else 1, "exists": False}


def _terminal_plan_members(
    snapshots,
    *,
    client_id: str,
    source_row: int,
    divider_row: int,
) -> tuple[List[str], List[Dict[str, Any]]]:
    terminal_ids = []
    row_shifts = []
    for snapshot in snapshots:
        data = snapshot.to_dict() or {}
        if data.get("clientId") != client_id:
            continue
        current_row = data.get("rowNumber")
        if current_row == source_row:
            terminal_ids.append(snapshot.id)
        elif (
            isinstance(current_row, int)
            and source_row < current_row <= divider_row
        ):
            row_shifts.append({
                "threadId": snapshot.id,
                "fromRow": current_row,
                "toRow": current_row - 1,
            })
    terminal_ids.sort()
    row_shifts.sort(key=lambda item: item["threadId"])
    return terminal_ids, row_shifts


def _build_terminal_finalization_plan(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    *,
    source_row: int,
    divider_row: int,
) -> Dict[str, Any]:
    threads_ref = (
        _fs.collection("users").document(user_id).collection("threads")
    )
    terminal_ids, row_shifts = _terminal_plan_members(
        list(threads_ref.stream()),
        client_id=client_id,
        source_row=source_row,
        divider_row=divider_row,
    )
    if not terminal_ids or current_thread_id not in terminal_ids:
        raise RetryableProcessingError(
            "terminal preflight could not identify the exact current row roots"
        )
    write_count = len(terminal_ids) + len(row_shifts)
    if write_count > FIRESTORE_BATCH_WRITE_LIMIT:
        raise RetryableProcessingError(
            "terminal finalization preflight exceeds Firestore 500-write limit: "
            f"{write_count} writes"
        )
    return {
        "dividerRow": divider_row,
        "finalRow": source_row if source_row > divider_row else divider_row,
        "claimThreadId": terminal_ids[0],
        "terminalThreadIds": terminal_ids,
        "rowShifts": row_shifts,
        "writeCount": write_count,
    }


def _verify_terminal_finalization_plan(
    user_id: str,
    client_id: str,
    saga: Dict[str, Any],
) -> None:
    plan = saga.get("finalizationPlan") or {}
    threads_ref = (
        _fs.collection("users").document(user_id).collection("threads")
    )
    actual_ids, actual_shifts = _terminal_plan_members(
        list(threads_ref.stream()),
        client_id=client_id,
        source_row=saga.get("sourceRow"),
        divider_row=plan.get("dividerRow"),
    )
    if actual_ids != plan.get("terminalThreadIds") or actual_shifts != plan.get("rowShifts"):
        raise RetryableProcessingError(
            "terminal finalization plan drifted after immutable preflight"
        )
    if plan.get("writeCount") != len(actual_ids) + len(actual_shifts):
        raise RetryableProcessingError("terminal finalization plan write count drifted")


def _terminal_saga_claim_ref(user_id: str, saga: Dict[str, Any]):
    claim_thread_id = (saga.get("finalizationPlan") or {}).get("claimThreadId")
    if not claim_thread_id:
        raise RetryableProcessingError("terminal saga has no canonical claim root")
    return (
        _fs.collection("users").document(user_id).collection("threads")
        .document(claim_thread_id)
    )


def _validate_terminal_saga_execution_claim(
    claim: Any,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    _validate_terminal_saga_immutable_hash(saga)
    if not isinstance(terminal_saga_owner, TerminalSagaExecution):
        raise RetryableProcessingError("terminal saga execution owner is missing")
    if not isinstance(claim, dict):
        raise RetryableProcessingError("terminal saga canonical claim is missing")
    if (
        claim.get("sagaKey") != saga.get("sagaKey")
        or claim.get("immutableHash") != saga.get("immutableHash")
    ):
        raise RetryableProcessingError("terminal saga canonical claim drifted")
    claim_token = claim.get("fencingToken")
    if (
        claim.get("owner") != terminal_saga_owner.owner
        or isinstance(claim_token, bool)
        or claim_token != terminal_saga_owner.fencing_token
    ):
        raise RetryableProcessingError("terminal saga execution ownership changed")
    if (
        not terminal_saga_owner.owner
        or isinstance(terminal_saga_owner.fencing_token, bool)
        or terminal_saga_owner.fencing_token < 1
    ):
        raise RetryableProcessingError("terminal saga execution owner is malformed")
    lease_until = _timestamp_to_utc(claim.get("leaseUntil"))
    now = now or datetime.now(timezone.utc)
    if lease_until is None or lease_until <= now:
        raise RetryableProcessingError(
            "terminal saga execution lease is missing, malformed, or expired"
        )
    return claim


def _renew_terminal_saga_execution(
    user_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
) -> datetime:
    """Transactionally assert the canonical fence and renew before an effect."""
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    now = datetime.now(timezone.utc)
    renewed_until = now + timedelta(seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS)

    def renew_claim(transaction) -> datetime:
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        transaction.update(claim_ref, {
            "terminalSagaClaim": {
                **claim,
                "leaseUntil": renewed_until,
                "renewedAt": now,
            },
            "updatedAt": SERVER_TIMESTAMP,
        })
        return renewed_until

    try:
        return run_firestore_transaction(_fs, renew_claim)
    except RetryableProcessingError:
        raise
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal saga execution lease renewal failed: {exc}"
        ) from exc


def _fenced_terminal_thread_update(
    user_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    patch: Dict[str, Any],
    *,
    renew_lease: bool = False,
    failure_label: str,
) -> None:
    """Apply an intent/outcome only while the caller owns the canonical fence."""
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    current_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(current_thread_id)
    )
    now = datetime.now(timezone.utc)

    def fenced_update(transaction) -> None:
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        if renew_lease:
            renewed_claim = {
                **claim,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "renewedAt": now,
            }
            claim_thread_id = (saga.get("finalizationPlan") or {}).get(
                "claimThreadId"
            )
            if current_thread_id == claim_thread_id:
                transaction.update(current_ref, {
                    **patch,
                    "terminalSagaClaim": renewed_claim,
                })
            else:
                transaction.update(claim_ref, {
                    "terminalSagaClaim": renewed_claim,
                    "updatedAt": SERVER_TIMESTAMP,
                })
                transaction.update(current_ref, patch)
        else:
            transaction.update(current_ref, patch)

    try:
        run_firestore_transaction(_fs, fenced_update)
    except RetryableProcessingError:
        raise
    except Exception as exc:
        raise RetryableProcessingError(f"{failure_label}: {exc}") from exc


def _read_validated_terminal_staging_plan(
    transaction,
    threads_ref,
    *,
    client_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
) -> tuple[List[str], str, Dict[str, Any]]:
    """Read and validate every frozen staging target in one transaction."""
    _validate_terminal_saga_immutable_hash(saga)
    plan = saga.get("finalizationPlan")
    source_row = saga.get("sourceRow")
    if (
        saga.get("phase") != "staged"
        or saga.get("clientId") != client_id
        or not isinstance(plan, dict)
        or type(source_row) is not int
        or source_row < 1
    ):
        raise RetryableProcessingError(
            "terminal staging plan identity or source geometry is malformed"
        )

    divider_row = plan.get("dividerRow")
    final_row = plan.get("finalRow")
    write_count = plan.get("writeCount")
    terminal_ids = plan.get("terminalThreadIds")
    row_shifts = plan.get("rowShifts")
    claim_thread_id = plan.get("claimThreadId")
    if (
        type(divider_row) is not int
        or divider_row < 1
        or type(final_row) is not int
        or final_row != (
            source_row if source_row > divider_row else divider_row
        )
        or type(write_count) is not int
        or not isinstance(terminal_ids, list)
        or not isinstance(row_shifts, list)
    ):
        raise RetryableProcessingError(
            "terminal staging plan geometry is malformed"
        )

    if (
        not terminal_ids
        or any(
            not isinstance(thread_id, str) or not thread_id.strip()
            for thread_id in terminal_ids
        )
        or terminal_ids != sorted(set(terminal_ids))
        or current_thread_id not in terminal_ids
        or claim_thread_id != terminal_ids[0]
    ):
        raise RetryableProcessingError(
            "terminal staging plan membership is malformed"
        )

    validated_shifts = []
    shift_ids = []
    for shift in row_shifts:
        if not isinstance(shift, dict) or set(shift) != {
            "threadId",
            "fromRow",
            "toRow",
        }:
            raise RetryableProcessingError(
                "terminal staging row-shift entry is malformed"
            )
        shift_id = shift.get("threadId")
        from_row = shift.get("fromRow")
        to_row = shift.get("toRow")
        if (
            not isinstance(shift_id, str)
            or not shift_id.strip()
            or type(from_row) is not int
            or type(to_row) is not int
            or not (source_row < from_row <= divider_row)
            or to_row != from_row - 1
        ):
            raise RetryableProcessingError(
                "terminal staging row-shift geometry is malformed"
            )
        shift_ids.append(shift_id)
        validated_shifts.append(shift)
    if (
        shift_ids != sorted(set(shift_ids))
        or set(terminal_ids) & set(shift_ids)
        or write_count != len(terminal_ids) + len(validated_shifts)
        or write_count > FIRESTORE_BATCH_WRITE_LIMIT
    ):
        raise RetryableProcessingError(
            "terminal staging plan target membership drifted"
        )

    expected_rows = {
        **{thread_id: source_row for thread_id in terminal_ids},
        **{
            shift["threadId"]: shift["fromRow"]
            for shift in validated_shifts
        },
    }
    target_snapshots = {}
    for target_id, expected_row in expected_rows.items():
        target_ref = threads_ref.document(target_id)
        if getattr(target_ref, "id", None) != target_id:
            raise RetryableProcessingError(
                f"terminal staging root identity drifted: {target_id}"
            )
        snapshot = target_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        live_row = (data or {}).get("rowNumber")
        if (
            not snapshot.exists
            or (data or {}).get("clientId") != client_id
            or type(live_row) is not int
            or live_row != expected_row
        ):
            raise RetryableProcessingError(
                f"terminal staging root drifted: {target_id}"
            )
        target_snapshots[target_id] = snapshot

    return list(terminal_ids), claim_thread_id, target_snapshots


def _stage_terminal_saga(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
) -> tuple[Dict[str, Any], TerminalSagaExecution]:
    """Atomically claim the row and persist one immutable source-message saga."""
    try:
        threads_ref = (
            _fs.collection("users").document(user_id).collection("threads")
        )
        owner = f"terminal-saga-{uuid4().hex}"
        now = datetime.now(timezone.utc)
        common_patch = {
            "followUpStatus": "stopped",
            "followUpConfig.nextFollowUpAt": None,
            "followUpConfig.processingBy": None,
            "followUpConfig.processingAt": None,
            "pendingTerminalReason": saga.get("reason"),
            "pendingTerminalAt": SERVER_TIMESTAMP,
            "pendingTerminalSourceRow": saga.get("sourceRow"),
            "terminalSagaKey": saga.get("sagaKey"),
            "updatedAt": SERVER_TIMESTAMP,
        }
        claim_ref = None
        fencing_token = None
        terminal_saga_owner = None
        commit_attempted = False

        def stage(transaction):
            nonlocal claim_ref
            nonlocal fencing_token
            nonlocal terminal_saga_owner
            nonlocal commit_attempted
            commit_attempted = False
            terminal_ids, claim_thread_id, target_snapshots = (
                _read_validated_terminal_staging_plan(
                    transaction,
                    threads_ref,
                    client_id=client_id,
                    current_thread_id=current_thread_id,
                    saga=saga,
                )
            )
            claim_ref = threads_ref.document(claim_thread_id)
            claim_snapshot = target_snapshots[claim_thread_id]
            claim_data = claim_snapshot.to_dict() if claim_snapshot.exists else {}
            current_snapshot = target_snapshots[current_thread_id]
            current_data = (
                current_snapshot.to_dict() if current_snapshot.exists else {}
            )
            # Every frozen alias/root is about to receive terminal ownership
            # markers. Linearize against a pending/reply send permit on each
            # one, not only the source root, before staging any write.
            for terminal_id in terminal_ids:
                try:
                    assert_terminal_staging_allowed(
                        transaction,
                        threads_ref.document(terminal_id),
                    )
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        "terminal saga staging blocked by Graph send permit on "
                        f"root {terminal_id}: {exc}"
                    ) from exc
            settlements = _validate_terminal_settlement_history(
                (current_data or {}).get("terminalSettlements")
            )
            if len(settlements) >= TERMINAL_SETTLEMENT_HISTORY_LIMIT:
                raise RetryableProcessingError(
                    "terminal settlement retention limit reached before staging; "
                    "operator review is required"
                )
            expected_settlement_ordinal = len(settlements) + 1
            if saga.get("settlementOrdinal") != expected_settlement_ordinal:
                raise RetryableProcessingError(
                    "terminal saga settlement ordinal drifted before staging"
                )
            if (current_data or {}).get("terminalReplyAttempt") is not None:
                raise RetryableProcessingError(
                    "terminal saga cannot replace an uncleared prior reply attempt"
                )
            existing_claim = (claim_data or {}).get("terminalSagaClaim")
            if isinstance(existing_claim, dict) and existing_claim.get("sagaKey"):
                raise RetryableProcessingError(
                    "terminal saga row claim conflict; losing source remains retryable"
                )
            prior_fence = (claim_data or {}).get("terminalSagaFence", 0)
            if (
                isinstance(prior_fence, bool)
                or not isinstance(prior_fence, int)
                or prior_fence < 0
            ):
                raise RetryableProcessingError(
                    "terminal saga fencing counter is malformed"
                )
            fencing_token = prior_fence + 1
            terminal_saga_owner = TerminalSagaExecution(owner, fencing_token)
            claim = {
                "sagaKey": saga.get("sagaKey"),
                "immutableHash": saga.get("immutableHash"),
                "sourceMessageKey": saga.get("sourceMessageKey"),
                "currentThreadId": current_thread_id,
                "owner": owner,
                "fencingToken": fencing_token,
                "claimedAt": now,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "status": "processing",
            }
            for terminal_id in terminal_ids:
                root_patch = dict(common_patch)
                if terminal_id == current_thread_id:
                    root_patch["terminalSaga"] = dict(saga)
                if terminal_id == claim_thread_id:
                    root_patch["terminalSagaClaim"] = claim
                    root_patch["terminalSagaFence"] = fencing_token
                transaction.update(threads_ref.document(terminal_id), root_patch)
            commit_attempted = True
            return dict(saga), terminal_saga_owner

        try:
            return run_firestore_transaction(_fs, stage)
        except Exception as exc:
            if not commit_attempted or claim_ref is None:
                raise
            committed_snapshot = claim_ref.get()
            committed_data = (
                committed_snapshot.to_dict() if committed_snapshot.exists else {}
            )
            committed_claim = committed_data.get("terminalSagaClaim")
            if not (
                isinstance(committed_claim, dict)
                and committed_claim.get("owner") == owner
                and committed_claim.get("fencingToken") == fencing_token
                and committed_claim.get("immutableHash") == saga.get("immutableHash")
            ):
                if isinstance(committed_claim, dict) and committed_claim.get("sagaKey"):
                    raise RetryableProcessingError(
                        "terminal saga row claim conflict; losing source remains retryable"
                    ) from exc
                raise
            return dict(saga), terminal_saga_owner
    except RetryableProcessingError:
        raise
    except Exception as exc:
        raise RetryableProcessingError(
            f"Row thread terminal staging failed: {exc}"
        ) from exc


def _release_terminal_saga_execution_claim(
    user_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
) -> None:
    if not terminal_saga_owner:
        return
    plan = saga.get("finalizationPlan") or {}
    claim_thread_id = plan.get("claimThreadId")
    if not claim_thread_id:
        return
    claim_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(claim_thread_id)
    )

    def release_claim(transaction) -> None:
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = (data or {}).get("terminalSagaClaim")
        _validate_terminal_saga_execution_claim(
            claim,
            saga,
            terminal_saga_owner,
        )
        transaction.update(claim_ref, {
            "terminalSagaClaim": {
                **claim,
                "owner": None,
                "leaseUntil": None,
                "status": "retryable",
                "releasedAt": SERVER_TIMESTAMP,
            },
            "updatedAt": SERVER_TIMESTAMP,
        })

    try:
        run_firestore_transaction(_fs, release_claim)
    except Exception as exc:
        print(f"⚠️ Could not release terminal saga execution claim: {exc}")


def _claim_existing_terminal_saga_execution(
    user_id: str,
    current_thread_id: str,
    thread_data: Dict[str, Any],
    saga: Dict[str, Any],
) -> TerminalSagaExecution:
    """Serialize exact-source recovery against the original row winner."""
    plan = saga.get("finalizationPlan") or {}
    claim_thread_id = plan.get("claimThreadId")
    if not claim_thread_id:
        raise RetryableProcessingError("terminal saga has no canonical claim root")
    claim_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(claim_thread_id)
    )
    owner = f"terminal-saga-recovery-{uuid4().hex}"
    now = datetime.now(timezone.utc)

    def claim_existing(transaction) -> TerminalSagaExecution:
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = (data or {}).get("terminalSagaClaim")
        if not isinstance(claim, dict):
            raise RetryableProcessingError(
                "terminal saga canonical claim is missing"
            )
        if (
            claim.get("sagaKey") != saga.get("sagaKey")
            or claim.get("immutableHash") != saga.get("immutableHash")
        ):
            raise RetryableProcessingError("terminal saga canonical claim drifted")

        existing_owner = claim.get("owner")
        lease_until = _timestamp_to_utc(claim.get("leaseUntil"))
        if existing_owner:
            if lease_until is None:
                raise RetryableProcessingError(
                    "terminal saga execution lease is missing, malformed, or expired"
                )
            if lease_until > now:
                raise RetryableProcessingError(
                    "terminal saga is already owned by another active worker"
                )
        fence_values = (
            claim.get("fencingToken", 0),
            (data or {}).get("terminalSagaFence", 0),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in fence_values
        ):
            raise RetryableProcessingError(
                "terminal saga fencing counter is malformed"
            )
        prior_fence = max(fence_values)
        fencing_token = prior_fence + 1
        terminal_saga_owner = TerminalSagaExecution(owner, fencing_token)

        transaction.update(claim_ref, {
            "terminalSagaClaim": {
                **claim,
                "owner": owner,
                "fencingToken": fencing_token,
                "claimedAt": now,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "status": "recovering",
            },
            "terminalSagaFence": fencing_token,
            "updatedAt": SERVER_TIMESTAMP,
        })
        return terminal_saga_owner

    try:
        return run_firestore_transaction(_fs, claim_existing)
    except RetryableProcessingError:
        raise
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal saga recovery claim failed: {exc}"
        ) from exc


_TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS = (
    "version",
    "sagaKey",
    "sagaImmutableHash",
    "attemptId",
    "ordinal",
    "previousAttemptId",
    "previousAttemptHash",
    "mutationKind",
    "sourceRow",
    "finalRow",
    "rowAnchor",
    "noteHash",
    "owner",
    "fencingToken",
    "requestStartedAt",
    "providerDeadline",
)
_TERMINAL_SHEET_APPLIED_STATUSES = {"applied", "reconciled_applied"}
_TERMINAL_SHEET_ATTEMPT_COMMON_FIELDS = frozenset({
    *_TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS,
    "attemptImmutableHash",
    "attemptHash",
    "status",
})
_TERMINAL_SHEET_ATTEMPT_STATUS_FIELDS = {
    "request_started": frozenset(),
    "applied": frozenset({
        "appliedByOwner",
        "appliedByFencingToken",
        "providerCompletedAt",
        "operatorReviewRequired",
    }),
    "reconciled_applied": frozenset({
        "reconciledByOwner",
        "reconciledByFencingToken",
        "reconciledAt",
        "reconciliationEvidence",
        "operatorReviewRequired",
    }),
    "needs_operator_review": frozenset({
        "operatorReviewRequired",
        "reviewReason",
        "reviewEvidence",
        "providerError",
        "reviewedByOwner",
        "reviewedByFencingToken",
        "reviewedAt",
    }),
    "definitely_not_applied": frozenset({
        "providerStatusCode",
        "providerError",
        "definitelyNotAppliedAt",
        "operatorReviewRequired",
    }),
}
_TERMINAL_SHEET_ATTEMPT_LEGAL_TRANSITIONS = {
    "request_started": frozenset({
        "applied",
        "reconciled_applied",
        "needs_operator_review",
        "definitely_not_applied",
    }),
    "needs_operator_review": frozenset({"reconciled_applied"}),
    "applied": frozenset(),
    "reconciled_applied": frozenset(),
    "definitely_not_applied": frozenset(),
}
_TERMINAL_SHEET_MUTATION_REVIEW_FIELDS = frozenset({
    "sagaKey",
    "attemptId",
    "attemptHash",
    "reason",
    "observedByOwner",
    "observedByFencingToken",
    "requestedAt",
})


def _terminal_sheet_attempt_immutable_hash(attempt: Dict[str, Any]) -> str:
    immutable = {
        field: attempt.get(field)
        for field in _TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS
    }
    return hashlib.sha256(
        json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _terminal_sheet_attempt_full_hash(attempt: Dict[str, Any]) -> str:
    state = {
        field: value
        for field, value in attempt.items()
        if field != "attemptHash"
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _terminal_sheet_attempt_observer_is_valid(
    attempt: Dict[str, Any],
    *,
    owner_field: str,
    fencing_field: str,
) -> bool:
    observer_owner = attempt.get(owner_field)
    observer_fence = attempt.get(fencing_field)
    attempt_fence = attempt.get("fencingToken")
    if (
        not isinstance(observer_owner, str)
        or not observer_owner.strip()
        or type(observer_fence) is not int
        or observer_fence < attempt_fence
    ):
        return False
    return not (
        observer_fence == attempt_fence
        and observer_owner != attempt.get("owner")
    )


def _terminal_sheet_attempt_datetime_is_valid(value: Any) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        return value.tzinfo is not None and value.utcoffset() is not None
    except Exception:
        return False


def _terminal_sheet_attempt_timestamp_is_valid(
    attempt: Dict[str, Any],
    field: str,
) -> bool:
    raw_value = attempt.get(field)
    raw_started_at = attempt.get("requestStartedAt")
    if (
        not _terminal_sheet_attempt_datetime_is_valid(raw_value)
        or not _terminal_sheet_attempt_datetime_is_valid(raw_started_at)
    ):
        return False
    value = _timestamp_to_utc(raw_value)
    started_at = _timestamp_to_utc(raw_started_at)
    return value is not None and started_at is not None and value >= started_at


def _terminal_sheet_attempt_with_status(
    current: Dict[str, Any],
    status: str,
    outcome_fields: Dict[str, Any],
) -> Dict[str, Any]:
    updated = {
        field: current[field]
        for field in _TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS
    }
    updated["attemptImmutableHash"] = current["attemptImmutableHash"]
    updated["status"] = status
    updated.update(outcome_fields)
    updated["attemptHash"] = _terminal_sheet_attempt_full_hash(updated)
    return updated


def _terminal_sheet_mutation_geometry_from_saga(
    saga: Dict[str, Any],
) -> tuple[int, int, str]:
    plan = saga.get("finalizationPlan")
    source_row = saga.get("sourceRow")
    final_row = plan.get("finalRow") if isinstance(plan, dict) else None
    if (
        type(source_row) is not int
        or source_row < 1
        or type(final_row) is not int
        or final_row < 1
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation saga geometry is malformed"
        )
    phase = saga.get("phase")
    if phase == "staged":
        if "finalRow" in saga:
            raise RetryableProcessingError(
                "staged terminal Sheet mutation saga contains mutable finalRow"
            )
    elif phase == "finalized":
        if (
            type(saga.get("finalRow")) is not int
            or saga.get("finalRow") != final_row
        ):
            raise RetryableProcessingError(
                "finalized terminal Sheet mutation saga finalRow drifted from plan"
            )
    else:
        raise RetryableProcessingError(
            "terminal Sheet mutation saga phase is malformed"
        )
    mutation_kind = (
        "ensure_note"
        if source_row == final_row
        else "move_with_note"
    )
    return source_row, final_row, mutation_kind


def _terminal_sheet_mutation_kind_from_saga(
    saga: Dict[str, Any],
) -> str:
    return _terminal_sheet_mutation_geometry_from_saga(saga)[2]


def _validate_terminal_sheet_mutation_attempt(
    attempt: Any,
    saga: Dict[str, Any],
    *,
    mutation_kind: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(attempt, dict):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt is missing or malformed"
        )
    status = attempt.get("status")
    status_fields = _TERMINAL_SHEET_ATTEMPT_STATUS_FIELDS.get(status)
    if status_fields is None:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt status is malformed"
        )
    expected_fields = _TERMINAL_SHEET_ATTEMPT_COMMON_FIELDS | status_fields
    if set(attempt) != expected_fields:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt status schema has missing, extra, "
            "or cross-status fields"
        )
    immutable = {
        field: attempt.get(field)
        for field in _TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS
    }
    actual_immutable_hash = _terminal_sheet_attempt_immutable_hash(attempt)
    actual_full_hash = _terminal_sheet_attempt_full_hash(attempt)
    plan = saga.get("finalizationPlan") or {}
    expected = {
        "version": TERMINAL_SHEET_MUTATION_VERSION,
        "sagaKey": saga.get("sagaKey"),
        "sagaImmutableHash": saga.get("immutableHash"),
        "sourceRow": saga.get("sourceRow"),
        "finalRow": plan.get("finalRow"),
        "rowAnchor": saga.get("rowAnchor"),
        "noteHash": hashlib.sha256(
            str(saga.get("note") or "").encode("utf-8")
        ).hexdigest(),
    }
    if any(immutable.get(field) != value for field, value in expected.items()):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt drifted from immutable saga"
        )
    expected_mutation_kind = _terminal_sheet_mutation_kind_from_saga(saga)
    if immutable.get("mutationKind") != expected_mutation_kind:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt kind drifted from immutable "
            "saga geometry"
        )
    if mutation_kind and immutable.get("mutationKind") != mutation_kind:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt kind drifted"
        )
    if immutable.get("mutationKind") not in {"move_with_note", "ensure_note"}:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt kind is malformed"
        )
    if (
        type(immutable.get("version")) is not int
        or type(immutable.get("sourceRow")) is not int
        or immutable.get("sourceRow") < 1
        or type(immutable.get("finalRow")) is not int
        or immutable.get("finalRow") < 1
        or not isinstance(immutable.get("attemptId"), str)
        or not immutable.get("attemptId").strip()
        or type(immutable.get("ordinal")) is not int
        or immutable.get("ordinal") < 1
        or not isinstance(immutable.get("owner"), str)
        or not immutable.get("owner").strip()
        or type(immutable.get("fencingToken")) is not int
        or immutable.get("fencingToken") < 1
        or attempt.get("attemptImmutableHash") != actual_immutable_hash
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt immutable hash is malformed"
        )
    if attempt.get("attemptHash") != actual_full_hash:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt full hash is malformed"
        )
    if immutable.get("ordinal") == 1 and (
        immutable.get("previousAttemptId") is not None
        or immutable.get("previousAttemptHash") is not None
    ):
        raise RetryableProcessingError(
            "first terminal Sheet mutation attempt has malformed lineage"
    )
    if immutable.get("ordinal") > 1 and (
        not isinstance(immutable.get("previousAttemptId"), str)
        or not immutable.get("previousAttemptId").strip()
        or not isinstance(immutable.get("previousAttemptHash"), str)
        or not immutable.get("previousAttemptHash").strip()
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt lineage is malformed"
        )
    started_at = _timestamp_to_utc(immutable.get("requestStartedAt"))
    provider_deadline = _timestamp_to_utc(immutable.get("providerDeadline"))
    if (
        not _terminal_sheet_attempt_datetime_is_valid(
            immutable.get("requestStartedAt")
        )
        or not _terminal_sheet_attempt_datetime_is_valid(
            immutable.get("providerDeadline")
        )
        or started_at is None
        or provider_deadline is None
        or provider_deadline <= started_at
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt provider deadline is malformed"
        )
    if status == "applied" and (
        attempt.get("appliedByOwner") != immutable.get("owner")
        or type(attempt.get("appliedByFencingToken")) is not int
        or attempt.get("appliedByFencingToken")
        != immutable.get("fencingToken")
        or not _terminal_sheet_attempt_timestamp_is_valid(
            attempt,
            "providerCompletedAt",
        )
        or attempt.get("operatorReviewRequired") is not False
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt applied outcome is malformed"
        )
    if status == "reconciled_applied" and (
        not _terminal_sheet_attempt_observer_is_valid(
            attempt,
            owner_field="reconciledByOwner",
            fencing_field="reconciledByFencingToken",
        )
        or not _terminal_sheet_attempt_timestamp_is_valid(
            attempt,
            "reconciledAt",
        )
        or not isinstance(attempt.get("reconciliationEvidence"), str)
        or not attempt.get("reconciliationEvidence").strip()
        or attempt.get("operatorReviewRequired") is not False
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt reconciled outcome is malformed"
        )
    if status == "needs_operator_review" and (
        attempt.get("operatorReviewRequired") is not True
        or not isinstance(attempt.get("reviewReason"), str)
        or not attempt.get("reviewReason").strip()
        or attempt.get("reviewEvidence")
        not in {"absent", "partial", "unreadable"}
        or (
            attempt.get("providerError") is not None
            and (
                not isinstance(attempt.get("providerError"), str)
                or not attempt.get("providerError").strip()
            )
        )
        or not _terminal_sheet_attempt_observer_is_valid(
            attempt,
            owner_field="reviewedByOwner",
            fencing_field="reviewedByFencingToken",
        )
        or not _terminal_sheet_attempt_timestamp_is_valid(
            attempt,
            "reviewedAt",
        )
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt operator-review outcome is malformed"
        )
    if status == "definitely_not_applied" and (
        type(attempt.get("providerStatusCode")) is not int
        or attempt.get("providerStatusCode") != 429
        or not isinstance(attempt.get("providerError"), str)
        or not attempt.get("providerError").strip()
        or not _terminal_sheet_attempt_timestamp_is_valid(
            attempt,
            "definitelyNotAppliedAt",
        )
        or attempt.get("operatorReviewRequired") is not False
    ):
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt definite-429 outcome is malformed"
        )
    return dict(attempt)


def _validate_terminal_sheet_mutation_review(
    review: Any,
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if attempt.get("status") != "needs_operator_review":
        if review is not None:
            raise RetryableProcessingError(
                "terminal Sheet mutation review exists for a non-review attempt"
            )
        return None
    if not isinstance(review, dict) or set(review) != _TERMINAL_SHEET_MUTATION_REVIEW_FIELDS:
        raise RetryableProcessingError(
            "terminal Sheet mutation review is missing or malformed"
        )
    expected = {
        "sagaKey": saga.get("sagaKey"),
        "attemptId": attempt.get("attemptId"),
        "attemptHash": attempt.get("attemptHash"),
        "reason": attempt.get("reviewReason"),
        "observedByOwner": attempt.get("reviewedByOwner"),
        "observedByFencingToken": attempt.get("reviewedByFencingToken"),
        "requestedAt": attempt.get("reviewedAt"),
    }
    if review != expected:
        raise RetryableProcessingError(
            "terminal Sheet mutation review drifted from the exact attempt hash"
        )
    return dict(review)


def _validate_terminal_sheet_mutation_history(
    history: Any,
    saga: Dict[str, Any],
    *,
    mutation_kind: str,
    active_attempt: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if history is None:
        history = []
    if not isinstance(history, list) or len(history) > TERMINAL_SHEET_MUTATION_HISTORY_LIMIT:
        raise RetryableProcessingError(
            "terminal Sheet mutation attempt history is malformed or over limit"
        )
    validated: List[Dict[str, Any]] = []
    previous = None
    seen_attempt_ids = set()
    for index, raw_attempt in enumerate(history, start=1):
        attempt = _validate_terminal_sheet_mutation_attempt(
            raw_attempt,
            saga,
            mutation_kind=mutation_kind,
        )
        if attempt.get("ordinal") != index:
            raise RetryableProcessingError(
                "terminal Sheet mutation attempt history ordinal drifted"
            )
        if attempt.get("attemptId") in seen_attempt_ids:
            raise RetryableProcessingError(
                "terminal Sheet mutation attempt history contains a duplicate ID"
            )
        if previous is not None and (
            attempt.get("previousAttemptId") != previous.get("attemptId")
            or attempt.get("previousAttemptHash") != previous.get("attemptHash")
        ):
            raise RetryableProcessingError(
                "terminal Sheet mutation attempt history lineage drifted"
            )
        if previous is not None and (
            attempt.get("owner") == previous.get("owner")
            or attempt.get("fencingToken") <= previous.get("fencingToken")
        ):
            raise RetryableProcessingError(
                "terminal Sheet mutation history owner/fence did not advance"
            )
        if attempt.get("status") != "definitely_not_applied":
            raise RetryableProcessingError(
                "terminal Sheet mutation history contains an ambiguous attempt"
            )
        validated.append(attempt)
        seen_attempt_ids.add(attempt.get("attemptId"))
        previous = attempt

    if active_attempt is not None:
        if active_attempt.get("ordinal") != len(validated) + 1:
            raise RetryableProcessingError(
                "active terminal Sheet mutation attempt ordinal drifted"
            )
        expected_previous_id = previous.get("attemptId") if previous else None
        expected_previous_hash = previous.get("attemptHash") if previous else None
        if (
            active_attempt.get("previousAttemptId") != expected_previous_id
            or active_attempt.get("previousAttemptHash") != expected_previous_hash
        ):
            raise RetryableProcessingError(
                "active terminal Sheet mutation attempt lineage drifted"
            )
        if active_attempt.get("attemptId") in seen_attempt_ids:
            raise RetryableProcessingError(
                "active terminal Sheet mutation attempt ID is duplicated"
            )
        if previous is not None and (
            active_attempt.get("owner") == previous.get("owner")
            or active_attempt.get("fencingToken")
            <= previous.get("fencingToken")
        ):
            raise RetryableProcessingError(
                "active terminal Sheet mutation owner/fence did not advance"
            )
    return validated


def _begin_terminal_sheet_mutation_attempt(
    user_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    mutation_kind: str,
    *,
    allow_create: bool = True,
) -> tuple[Dict[str, Any], bool]:
    """Persist request_started under the current fence before any Sheet write."""
    _source_row, expected_final_row, derived_mutation_kind = (
        _terminal_sheet_mutation_geometry_from_saga(saga)
    )
    if mutation_kind != derived_mutation_kind:
        raise RetryableProcessingError(
            "terminal Sheet mutation caller kind disagrees with immutable "
            "saga geometry"
    )
    mutation_kind = derived_mutation_kind
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    now = datetime.now(timezone.utc)
    attempt_id = f"terminal-sheet-{uuid4().hex}"
    transaction_state = {
        "attempt": None,
        "commitAttempted": False,
        "expectedHistory": None,
        "expectedReview": None,
        "expectedClaim": None,
    }

    def begin_attempt(transaction) -> tuple[Dict[str, Any], bool]:
        transaction_state.update({
            "attempt": None,
            "commitAttempted": False,
            "expectedHistory": None,
            "expectedReview": None,
            "expectedClaim": None,
        })
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        existing = (data or {}).get("terminalSheetMutationAttempt")
        if existing is not None:
            existing = _validate_terminal_sheet_mutation_attempt(
                existing,
                saga,
                mutation_kind=mutation_kind,
            )
            _validate_terminal_sheet_mutation_review(
                (data or {}).get("terminalSheetMutationReview"),
                saga,
                existing,
            )
            history = _validate_terminal_sheet_mutation_history(
                (data or {}).get("terminalSheetMutationHistory"),
                saga,
                mutation_kind=mutation_kind,
                active_attempt=existing,
            )
            if existing.get("status") != "definitely_not_applied":
                return existing, False
            if not allow_create:
                raise RetryableProcessingError(
                    "terminal Sheet mutation attempt creation is disabled "
                    "during read-only recovery"
                )
            if (
                terminal_saga_owner.owner == existing.get("owner")
                or terminal_saga_owner.fencing_token
                <= existing.get("fencingToken", 0)
            ):
                raise RetryableProcessingError(
                    "a definitely-not-applied Sheet mutation requires a new fenced "
                    "owner with a strictly higher fencing token"
                )
            if existing.get("ordinal") >= TERMINAL_SHEET_MUTATION_ATTEMPT_LIMIT:
                raise RetryableProcessingError(
                    "terminal Sheet mutation definite-retry limit was reached"
                )
            previous_attempt = existing
            next_ordinal = existing.get("ordinal") + 1
        else:
            if not allow_create:
                raise RetryableProcessingError(
                    "finalized terminal Sheet recovery is missing its durable "
                    "mutation attempt; reconstruction is forbidden"
                )
            if (data or {}).get("terminalSheetMutationReview") is not None:
                raise RetryableProcessingError(
                    "terminal Sheet mutation review exists without an active attempt"
                )
            history = _validate_terminal_sheet_mutation_history(
                (data or {}).get("terminalSheetMutationHistory"),
                saga,
                mutation_kind=mutation_kind,
            )
            if history:
                raise RetryableProcessingError(
                    "terminal Sheet mutation history exists without an active attempt"
                )
            previous_attempt = None
            next_ordinal = 1

        renewed_until = now + timedelta(
            seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
        )
        provider_deadline = now + timedelta(
            seconds=TERMINAL_SHEET_PROVIDER_DEADLINE_SECONDS
        )
        if provider_deadline >= renewed_until:
            raise RetryableProcessingError(
                "terminal Sheet provider deadline must be shorter than its lease"
            )
        immutable = {
            "version": TERMINAL_SHEET_MUTATION_VERSION,
            "sagaKey": saga.get("sagaKey"),
            "sagaImmutableHash": saga.get("immutableHash"),
            "attemptId": attempt_id,
            "ordinal": next_ordinal,
            "previousAttemptId": (
                previous_attempt.get("attemptId") if previous_attempt else None
            ),
            "previousAttemptHash": (
                previous_attempt.get("attemptHash") if previous_attempt else None
            ),
            "mutationKind": derived_mutation_kind,
            "sourceRow": saga.get("sourceRow"),
            "finalRow": expected_final_row,
            "rowAnchor": saga.get("rowAnchor"),
            "noteHash": hashlib.sha256(
                str(saga.get("note") or "").encode("utf-8")
            ).hexdigest(),
            "owner": terminal_saga_owner.owner,
            "fencingToken": terminal_saga_owner.fencing_token,
            "requestStartedAt": now,
            "providerDeadline": provider_deadline,
        }
        attempt = {
            **immutable,
            "attemptImmutableHash": _terminal_sheet_attempt_immutable_hash(
                immutable
            ),
            "status": "request_started",
        }
        attempt["attemptHash"] = _terminal_sheet_attempt_full_hash(attempt)
        patch = {
            "terminalSheetMutationAttempt": attempt,
            "terminalSagaClaim": {
                **claim,
                "leaseUntil": renewed_until,
                "renewedAt": now,
            },
            "updatedAt": SERVER_TIMESTAMP,
        }
        expected_history = copy.deepcopy(
            (data or {}).get("terminalSheetMutationHistory")
        )
        expected_review = copy.deepcopy(
            (data or {}).get("terminalSheetMutationReview")
        )
        if previous_attempt is not None:
            patch["terminalSheetMutationHistory"] = [
                *history,
                previous_attempt,
            ]
            patch["terminalSheetMutationReview"] = None
            expected_history = copy.deepcopy(
                patch["terminalSheetMutationHistory"]
            )
            expected_review = None
        transaction_state.update({
            "attempt": attempt,
            "expectedHistory": expected_history,
            "expectedReview": expected_review,
            "expectedClaim": copy.deepcopy(patch["terminalSagaClaim"]),
        })
        transaction.update(claim_ref, patch)
        transaction_state["commitAttempted"] = True
        return attempt, True

    try:
        return run_firestore_transaction(_fs, begin_attempt)
    except Exception as exc:
        attempt = transaction_state["attempt"]
        if transaction_state["commitAttempted"] and attempt is not None:
            try:
                readback = claim_ref.get()
                readback_data = readback.to_dict() if readback.exists else {}
                readback_claim = _validate_terminal_saga_execution_claim(
                    readback_data.get("terminalSagaClaim"),
                    saga,
                    terminal_saga_owner,
                )
                committed = _validate_terminal_sheet_mutation_attempt(
                    readback_data.get("terminalSheetMutationAttempt"),
                    saga,
                    mutation_kind=mutation_kind,
                )
                _validate_terminal_sheet_mutation_history(
                    readback_data.get("terminalSheetMutationHistory"),
                    saga,
                    mutation_kind=mutation_kind,
                    active_attempt=committed,
                )
                _validate_terminal_sheet_mutation_review(
                    readback_data.get("terminalSheetMutationReview"),
                    saga,
                    committed,
                )
                if (
                    committed == attempt
                    and readback_claim == transaction_state["expectedClaim"]
                    and readback_data.get("terminalSheetMutationHistory")
                    == transaction_state["expectedHistory"]
                    and readback_data.get("terminalSheetMutationReview")
                    == transaction_state["expectedReview"]
                ):
                    return attempt, True
            except Exception:
                pass
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"terminal Sheet mutation intent persistence failed: {exc}"
        ) from exc


def _record_terminal_sheet_mutation_state(
    user_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    attempt: Dict[str, Any],
    status: str,
    **outcome_fields,
) -> Dict[str, Any]:
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    now = datetime.now(timezone.utc)
    transaction_state = {
        "updatedAttempt": None,
        "commitAttempted": False,
        "expectedReview": None,
        "expectedHistory": None,
        "expectedClaim": None,
        "validatedHistory": None,
    }

    def record_state(transaction) -> Dict[str, Any]:
        transaction_state.update({
            "updatedAttempt": None,
            "commitAttempted": False,
            "expectedReview": None,
            "expectedHistory": None,
            "expectedClaim": None,
            "validatedHistory": None,
        })
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        current = _validate_terminal_sheet_mutation_attempt(
            (data or {}).get("terminalSheetMutationAttempt"),
            saga,
            mutation_kind=attempt.get("mutationKind"),
        )
        _validate_terminal_sheet_mutation_review(
            (data or {}).get("terminalSheetMutationReview"),
            saga,
            current,
        )
        expected_history = copy.deepcopy(
            (data or {}).get("terminalSheetMutationHistory")
        )
        history = _validate_terminal_sheet_mutation_history(
            expected_history,
            saga,
            mutation_kind=current.get("mutationKind"),
            active_attempt=current,
        )
        if (
            current.get("attemptId") != attempt.get("attemptId")
            or current.get("attemptHash") != attempt.get("attemptHash")
        ):
            raise RetryableProcessingError(
                "terminal Sheet mutation attempt changed before outcome persistence"
            )
        updated_attempt = _terminal_sheet_attempt_with_status(
            current,
            status,
            outcome_fields,
        )
        updated_attempt = _validate_terminal_sheet_mutation_attempt(
            updated_attempt,
            saga,
            mutation_kind=current.get("mutationKind"),
        )
        current_status = current.get("status")
        if current_status == status:
            if updated_attempt != current:
                raise RetryableProcessingError(
                    "terminal Sheet mutation same-state outcome rewrite is forbidden"
                )
            return current
        if status not in _TERMINAL_SHEET_ATTEMPT_LEGAL_TRANSITIONS.get(
            current_status,
            frozenset(),
        ):
            raise RetryableProcessingError(
                "terminal Sheet mutation status transition is forbidden"
            )
        if status in {"applied", "definitely_not_applied"} and (
            terminal_saga_owner.owner != current.get("owner")
            or terminal_saga_owner.fencing_token
            != current.get("fencingToken")
        ):
            raise RetryableProcessingError(
                "terminal Sheet provider outcome fence identity is malformed"
            )
        if status == "reconciled_applied" and (
            outcome_fields.get("reconciledByOwner")
            != terminal_saga_owner.owner
            or outcome_fields.get("reconciledByFencingToken")
            != terminal_saga_owner.fencing_token
        ):
            raise RetryableProcessingError(
                "terminal Sheet reconciliation fence identity is malformed"
            )
        if status == "needs_operator_review" and (
            outcome_fields.get("reviewedByOwner")
            != terminal_saga_owner.owner
            or outcome_fields.get("reviewedByFencingToken")
            != terminal_saga_owner.fencing_token
        ):
            raise RetryableProcessingError(
                "terminal Sheet review fence identity is malformed"
            )
        patch = {
            "terminalSheetMutationAttempt": updated_attempt,
            "terminalSagaClaim": {
                **claim,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "renewedAt": now,
            },
            "updatedAt": SERVER_TIMESTAMP,
        }
        expected_review = None
        if status == "needs_operator_review":
            expected_review = {
                "sagaKey": saga.get("sagaKey"),
                "attemptId": updated_attempt.get("attemptId"),
                "attemptHash": updated_attempt.get("attemptHash"),
                "reason": updated_attempt.get("reviewReason"),
                "observedByOwner": updated_attempt.get("reviewedByOwner"),
                "observedByFencingToken": updated_attempt.get(
                    "reviewedByFencingToken"
                ),
                "requestedAt": updated_attempt.get("reviewedAt"),
            }
        patch["terminalSheetMutationReview"] = expected_review
        transaction_state.update({
            "updatedAttempt": updated_attempt,
            "expectedReview": expected_review,
            "expectedHistory": expected_history,
            "expectedClaim": copy.deepcopy(patch["terminalSagaClaim"]),
            "validatedHistory": history,
        })
        transaction.update(claim_ref, patch)
        transaction_state["commitAttempted"] = True
        return updated_attempt

    try:
        return run_firestore_transaction(_fs, record_state)
    except Exception as exc:
        updated_attempt = transaction_state["updatedAttempt"]
        if transaction_state["commitAttempted"] and updated_attempt is not None:
            try:
                readback = claim_ref.get()
                data = readback.to_dict() if readback.exists else {}
                readback_claim = _validate_terminal_saga_execution_claim(
                    data.get("terminalSagaClaim"),
                    saga,
                    terminal_saga_owner,
                )
                committed = _validate_terminal_sheet_mutation_attempt(
                    data.get("terminalSheetMutationAttempt"),
                    saga,
                    mutation_kind=attempt.get("mutationKind"),
                )
                committed_history = _validate_terminal_sheet_mutation_history(
                    data.get("terminalSheetMutationHistory"),
                    saga,
                    mutation_kind=attempt.get("mutationKind"),
                    active_attempt=committed,
                )
                _validate_terminal_sheet_mutation_review(
                    data.get("terminalSheetMutationReview"),
                    saga,
                    committed,
                )
                if (
                    committed == updated_attempt
                    and readback_claim == transaction_state["expectedClaim"]
                    and data.get("terminalSheetMutationHistory")
                    == transaction_state["expectedHistory"]
                    and committed_history
                    == transaction_state["validatedHistory"]
                    and data.get("terminalSheetMutationReview")
                    == transaction_state["expectedReview"]
                ):
                    return updated_attempt
            except Exception:
                pass
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"terminal Sheet mutation outcome persistence failed: {exc}"
        ) from exc


def _record_terminal_sheet_mutation_review(
    user_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    reason: str,
) -> None:
    """Make malformed/non-reconcilable attempts visible without rewriting them."""
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    now = datetime.now(timezone.utc)

    def record_review(transaction) -> None:
        snapshot = claim_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        attempt = (data or {}).get("terminalSheetMutationAttempt")
        transaction.update(claim_ref, {
            "terminalSheetMutationReview": {
                "sagaKey": saga.get("sagaKey"),
                "attemptId": (
                    attempt.get("attemptId") if isinstance(attempt, dict) else None
                ),
                "attemptHash": (
                    attempt.get("attemptHash") if isinstance(attempt, dict) else None
                ),
                "reason": reason,
                "observedByOwner": terminal_saga_owner.owner,
                "observedByFencingToken": terminal_saga_owner.fencing_token,
                "requestedAt": now,
            },
            "terminalSagaClaim": {
                **claim,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "renewedAt": now,
            },
            "updatedAt": SERVER_TIMESTAMP,
        })

    try:
        run_firestore_transaction(_fs, record_review)
    except Exception as exc:
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"terminal Sheet operator-review persistence failed: {exc}"
        ) from exc


def _terminal_sheet_persisted_state_requires_operator_review(
    user_id: str,
    saga: Dict[str, Any],
) -> bool:
    """Classify only malformed persisted Sheet state as operator-visible."""
    try:
        snapshot = _terminal_saga_claim_ref(user_id, saga).get()
        data = snapshot.to_dict() if snapshot.exists else {}
    except Exception:
        return False
    attempt = (data or {}).get("terminalSheetMutationAttempt")
    history = (data or {}).get("terminalSheetMutationHistory")
    review = (data or {}).get("terminalSheetMutationReview")
    if attempt is None:
        return review is not None or history not in (None, [])
    try:
        mutation_kind = _terminal_sheet_mutation_kind_from_saga(saga)
        validated_attempt = _validate_terminal_sheet_mutation_attempt(
            attempt,
            saga,
            mutation_kind=mutation_kind,
        )
        _validate_terminal_sheet_mutation_history(
            history,
            saga,
            mutation_kind=mutation_kind,
            active_attempt=validated_attempt,
        )
        _validate_terminal_sheet_mutation_review(
            review,
            saga,
            validated_attempt,
        )
    except RetryableProcessingError:
        return True
    return False


def _read_terminal_sheet_mutation_effect(
    user_id: str,
    current_thread_id: str,
    sheets,
    sheet_id: str,
    tab_title: str,
    header: List[str],
    notes_column_index: int,
    saga: Dict[str, Any],
) -> tuple[str, str]:
    """Read exact row-plus-note evidence without issuing a Sheet mutation."""
    try:
        with terminal_sheets_provider_window(
            TERMINAL_SHEET_READBACK_DEADLINE_SECONDS
        ):
            rownum, rowvals = _find_row_by_anchor(
                user_id,
                current_thread_id,
                sheets,
                sheet_id,
                tab_title,
                header,
                saga.get("replyRecipient") or "",
            )
            if rownum is None:
                return "absent", "persisted row anchor was not found"
            live_anchor = get_row_anchor(rowvals or [], header)
            if (
                saga.get("rowAnchor")
                and live_anchor
                and _normalize_replacement_match_text(live_anchor)
                != _normalize_replacement_match_text(saga.get("rowAnchor"))
            ):
                return "partial", "row anchor drifted"
            expected_final_row = (saga.get("finalizationPlan") or {}).get("finalRow")
            if rownum != expected_final_row:
                return (
                    "absent" if rownum == saga.get("sourceRow") else "partial",
                    f"row is {rownum}; expected exact final row {expected_final_row}",
                )
            if not _is_row_below_nonviable(
                sheets,
                sheet_id,
                tab_title,
                rownum,
            ):
                return "partial", "row is not below the NON-VIABLE divider"
            note = _read_terminal_note(
                sheets,
                sheet_id,
                tab_title,
                rownum,
                notes_column_index,
            )
            stable_note = str(saga.get("note") or "").strip()
            if not stable_note or stable_note not in note:
                return "partial", "exact terminal note is not present"
            return "applied", "exact final row and terminal note are present"
    except Exception as exc:
        return "unreadable", f"Sheet effect readback failed: {exc}"


def _execute_or_reconcile_terminal_sheet_mutation(
    user_id: str,
    current_thread_id: str,
    sheets,
    sheet_id: str,
    tab_title: str,
    header: List[str],
    notes_column_index: int,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    mutation_kind: str,
    *,
    allow_provider_mutation: bool = True,
) -> int:
    _source_row, expected_final_row, derived_mutation_kind = (
        _terminal_sheet_mutation_geometry_from_saga(saga)
    )
    if mutation_kind != derived_mutation_kind:
        raise RetryableProcessingError(
            "terminal Sheet mutation caller kind disagrees with immutable "
            "saga geometry"
        )
    mutation_kind = derived_mutation_kind
    try:
        attempt, created = _begin_terminal_sheet_mutation_attempt(
            user_id,
            saga,
            terminal_saga_owner,
            mutation_kind,
            allow_create=allow_provider_mutation,
        )
    except Exception as exc:
        malformed_state = (
            _terminal_sheet_persisted_state_requires_operator_review(
                user_id,
                saga,
            )
        )
        reason = (
            f"malformed terminal Sheet mutation attempt: {exc}"
            if malformed_state
            else f"terminal Sheet mutation attempt preparation failed: {exc}"
        )
        if malformed_state:
            _record_terminal_sheet_mutation_review(
                user_id,
                saga,
                terminal_saga_owner,
                reason,
            )
        raise RetryableProcessingError(reason) from exc

    if attempt.get("status") in _TERMINAL_SHEET_APPLIED_STATUSES:
        return expected_final_row
    if not created or not allow_provider_mutation:
        evidence, evidence_reason = _read_terminal_sheet_mutation_effect(
            user_id,
            current_thread_id,
            sheets,
            sheet_id,
            tab_title,
            header,
            notes_column_index,
            saga,
        )
        if evidence == "applied":
            _record_terminal_sheet_mutation_state(
                user_id,
                saga,
                terminal_saga_owner,
                attempt,
                "reconciled_applied",
                reconciledByOwner=terminal_saga_owner.owner,
                reconciledByFencingToken=terminal_saga_owner.fencing_token,
                reconciledAt=datetime.now(timezone.utc),
                reconciliationEvidence=evidence_reason,
                operatorReviewRequired=False,
            )
            return expected_final_row
        if attempt.get("status") == "needs_operator_review":
            raise RetryableProcessingError(
                "terminal Sheet mutation requires operator review: "
                f"{evidence_reason}; no second mutation was authorized"
            )
        _record_terminal_sheet_mutation_state(
            user_id,
            saga,
            terminal_saga_owner,
            attempt,
            "needs_operator_review",
            operatorReviewRequired=True,
            reviewReason=evidence_reason,
            reviewEvidence=evidence,
            providerError=None,
            reviewedByOwner=terminal_saga_owner.owner,
            reviewedByFencingToken=terminal_saga_owner.fencing_token,
            reviewedAt=datetime.now(timezone.utc),
        )
        raise RetryableProcessingError(
            "terminal Sheet mutation requires operator review: "
            f"{evidence_reason}; no second mutation was authorized"
        )

    provider_deadline = _timestamp_to_utc(attempt.get("providerDeadline"))
    remaining_seconds = (
        (provider_deadline - datetime.now(timezone.utc)).total_seconds()
        if provider_deadline is not None
        else 0
    )
    try:
        with terminal_sheets_provider_window(remaining_seconds):
            live_header = _read_header_row2(sheets, sheet_id, tab_title)
            _validate_terminal_saga_sheet_layout(saga, live_header)
            if derived_mutation_kind == "ensure_note":
                _ensure_terminal_note(
                    sheets,
                    sheet_id,
                    tab_title,
                    expected_final_row,
                    notes_column_index,
                    saga.get("note"),
                )
                final_row = expected_final_row
            else:
                plan = saga.get("finalizationPlan") or {}
                planned_divider = plan.get("dividerRow")
                if saga.get("dividerExists", True):
                    live_divider = _preview_nonviable_divider(
                        sheets,
                        sheet_id,
                        tab_title,
                    )
                    if (
                        not live_divider.get("exists")
                        or live_divider.get("dividerRow") != planned_divider
                    ):
                        raise RetryableProcessingError(
                            "existing NON-VIABLE divider drifted after preflight"
                        )
                    divider_row = planned_divider
                else:
                    live_divider = _preview_nonviable_divider(
                        sheets,
                        sheet_id,
                        tab_title,
                    )
                    if (
                        live_divider.get("exists")
                        or live_divider.get("dividerRow") != planned_divider
                    ):
                        raise RetryableProcessingError(
                            "missing NON-VIABLE divider plan drifted after preflight"
                        )
                    final_row = move_row_below_new_divider_atomic(
                        sheets,
                        sheet_id,
                        tab_title,
                        saga.get("sourceRow"),
                        planned_divider,
                        notes_column_index=notes_column_index,
                        notes_value=saga.get("note"),
                    )
                    if final_row != expected_final_row:
                        raise RetryableProcessingError(
                            "atomic missing-divider Sheet mutation returned an "
                            "unexpected final row"
                        )
                if saga.get("dividerExists", True):
                    final_row = move_row_below_divider(
                        sheets,
                        sheet_id,
                        tab_title,
                        saga.get("sourceRow"),
                        divider_row,
                        notes_column_index=notes_column_index,
                        notes_value=saga.get("note"),
                    )
                if final_row != expected_final_row:
                    raise RetryableProcessingError(
                        "Sheet mutation returned an unexpected final row"
                    )
        _record_terminal_sheet_mutation_state(
            user_id,
            saga,
            terminal_saga_owner,
            attempt,
            "applied",
            appliedByOwner=terminal_saga_owner.owner,
            appliedByFencingToken=terminal_saga_owner.fencing_token,
            providerCompletedAt=datetime.now(timezone.utc),
            operatorReviewRequired=False,
        )
        return final_row
    except Exception as provider_exc:
        provider_status = (
            getattr(getattr(provider_exc, "resp", None), "status", None)
            if isinstance(provider_exc, HttpError)
            else None
        )
        if type(provider_status) is int and provider_status == 429:
            _record_terminal_sheet_mutation_state(
                user_id,
                saga,
                terminal_saga_owner,
                attempt,
                "definitely_not_applied",
                providerStatusCode=429,
                providerError=str(provider_exc)[:1500],
                definitelyNotAppliedAt=datetime.now(timezone.utc),
                operatorReviewRequired=False,
            )
            raise RetryableProcessingError(
                "terminal Sheet provider rejected the mutation with 429 before "
                "acceptance; a new fenced owner may issue one linked attempt"
            ) from provider_exc
        evidence, evidence_reason = _read_terminal_sheet_mutation_effect(
            user_id,
            current_thread_id,
            sheets,
            sheet_id,
            tab_title,
            header,
            notes_column_index,
            saga,
        )
        if evidence == "applied":
            _record_terminal_sheet_mutation_state(
                user_id,
                saga,
                terminal_saga_owner,
                attempt,
                "reconciled_applied",
                reconciledByOwner=terminal_saga_owner.owner,
                reconciledByFencingToken=terminal_saga_owner.fencing_token,
                reconciledAt=datetime.now(timezone.utc),
                reconciliationEvidence=evidence_reason,
                operatorReviewRequired=False,
            )
            return expected_final_row
        _record_terminal_sheet_mutation_state(
            user_id,
            saga,
            terminal_saga_owner,
            attempt,
            "needs_operator_review",
            operatorReviewRequired=True,
            reviewReason=evidence_reason,
            reviewEvidence=evidence,
            providerError=str(provider_exc)[:1500],
            reviewedByOwner=terminal_saga_owner.owner,
            reviewedByFencingToken=terminal_saga_owner.fencing_token,
            reviewedAt=datetime.now(timezone.utc),
        )
        raise RetryableProcessingError(
            "terminal Sheet mutation requires operator review after an ambiguous "
            f"provider outcome: {evidence_reason}"
        ) from provider_exc


def _finalize_terminal_thread_roots(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
    *,
    final_row: int,
    terminal_saga_owner: TerminalSagaExecution,
) -> Dict[str, Any]:
    """Use only the persisted bounded plan to atomically terminalize row roots."""
    try:
        _validate_terminal_saga_immutable_hash(saga)
        plan = saga.get("finalizationPlan") or {}
        exact_ids = list(plan.get("terminalThreadIds") or [])
        row_shifts = list(plan.get("rowShifts") or [])
        if not exact_ids or current_thread_id not in exact_ids:
            raise ValueError("exact staged thread roots are required for finalization")
        if final_row != plan.get("finalRow"):
            raise ValueError("Sheet move result does not match immutable final row")
        if len(exact_ids) + len(row_shifts) != plan.get("writeCount"):
            raise ValueError("immutable finalization write count is inconsistent")
        if plan.get("writeCount") > FIRESTORE_BATCH_WRITE_LIMIT:
            raise ValueError("immutable finalization plan exceeds Firestore batch limit")

        threads_ref = (
            _fs.collection("users").document(user_id).collection("threads")
        )
        claim_thread_id = plan.get("claimThreadId")
        if claim_thread_id not in exact_ids:
            raise ValueError("canonical claim root is outside the terminal plan")
        claim_ref = threads_ref.document(claim_thread_id)
        now = datetime.now(timezone.utc)

        def finalize_roots(transaction) -> Dict[str, Any]:
            claim_snapshot = claim_ref.get(transaction=transaction)
            claim_data = claim_snapshot.to_dict() if claim_snapshot.exists else {}
            claim = _validate_terminal_saga_execution_claim(
                (claim_data or {}).get("terminalSagaClaim"),
                saga,
                terminal_saga_owner,
                now=now,
            )
            sheet_attempt = _validate_terminal_sheet_mutation_attempt(
                (claim_data or {}).get("terminalSheetMutationAttempt"),
                saga,
            )
            _validate_terminal_sheet_mutation_history(
                (claim_data or {}).get("terminalSheetMutationHistory"),
                saga,
                mutation_kind=sheet_attempt.get("mutationKind"),
                active_attempt=sheet_attempt,
            )
            _validate_terminal_sheet_mutation_review(
                (claim_data or {}).get("terminalSheetMutationReview"),
                saga,
                sheet_attempt,
            )
            if sheet_attempt.get("status") not in _TERMINAL_SHEET_APPLIED_STATUSES:
                raise RetryableProcessingError(
                    "terminal Sheet mutation is not durably applied or reconciled"
                )

            # Read every planned target in this transaction before any writes.
            # A concurrent sibling row change then creates a transaction
            # conflict instead of being blindly overwritten.
            target_snapshots = {claim_thread_id: claim_snapshot}
            for target_id in [
                *exact_ids,
                *(shift.get("threadId") for shift in row_shifts),
            ]:
                if target_id not in target_snapshots:
                    target_snapshots[target_id] = (
                        threads_ref.document(target_id).get(
                            transaction=transaction
                        )
                    )
            for terminal_id in exact_ids:
                snapshot = target_snapshots[terminal_id]
                data = snapshot.to_dict() if snapshot.exists else {}
                if (
                    not snapshot.exists
                    or data.get("clientId") != client_id
                    or data.get("rowNumber") != saga.get("sourceRow")
                    or data.get("terminalSagaKey") != saga.get("sagaKey")
                ):
                    raise RetryableProcessingError(
                        f"terminal finalization root drifted: {terminal_id}"
                    )
                if terminal_id == current_thread_id and (
                    (data.get("terminalSaga") or {}).get("immutableHash")
                    != saga.get("immutableHash")
                ):
                    raise RetryableProcessingError(
                        "terminal current-root saga drifted before finalization"
                    )
            for shift in row_shifts:
                snapshot = target_snapshots[shift["threadId"]]
                data = snapshot.to_dict() if snapshot.exists else {}
                if (
                    not snapshot.exists
                    or data.get("clientId") != client_id
                    or data.get("rowNumber") != shift.get("fromRow")
                ):
                    raise RetryableProcessingError(
                        f"terminal row-shift root drifted: {shift['threadId']}"
                    )
            finalized_saga = {
                **saga,
                "phase": "finalized",
                "finalRow": final_row,
            }
            terminal_patch = {
                "rowNumber": final_row,
                "status": THREAD_STATUS["stopped"],
                "statusReason": saga.get("reason"),
                "statusUpdatedAt": SERVER_TIMESTAMP,
                "nonViableAt": SERVER_TIMESTAMP,
                "nonViableReason": saga.get("reason"),
                "followUpStatus": "stopped",
                "followUpConfig.nextFollowUpAt": None,
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "updatedAt": SERVER_TIMESTAMP,
            }
            root_patches: Dict[str, Dict[str, Any]] = {}
            for terminal_id in exact_ids:
                root_patch = dict(terminal_patch)
                if terminal_id == current_thread_id:
                    root_patch.update({
                        "terminalSaga": finalized_saga,
                        "terminalNotificationOwed": bool(
                            saga.get("notificationRequired", True)
                        ),
                        "terminalNotificationOutcome": None,
                        "terminalReplyOwed": (
                            saga.get("responseScenario") != "none"
                        ),
                        "terminalReplyOutcome": None,
                    })
                root_patches[terminal_id] = root_patch
            for shift in row_shifts:
                root_patches[shift["threadId"]] = {
                    "rowNumber": shift["toRow"],
                    "updatedAt": SERVER_TIMESTAMP,
                }
            root_patches[claim_thread_id]["terminalSagaClaim"] = {
                **claim,
                "leaseUntil": now + timedelta(
                    seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
                ),
                "renewedAt": now,
            }
            for root_id, root_patch in root_patches.items():
                transaction.update(threads_ref.document(root_id), root_patch)
            return finalized_saga

        return run_firestore_transaction(_fs, finalize_roots)
    except Exception as exc:
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"terminal Firestore finalization failed: {exc}"
        ) from exc


def _settle_terminal_notification_obligation(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    recipient: str,
    saga: Dict[str, Any],
    *,
    terminal_saga_owner: TerminalSagaExecution,
) -> None:
    """Create the idempotent notification only after terminal state commits."""
    thread_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(current_thread_id)
    )
    snapshot = thread_ref.get()
    data = snapshot.to_dict() if snapshot.exists else {}
    if not data or (data.get("terminalSaga") or {}).get("sagaKey") != saga.get("sagaKey"):
        raise RetryableProcessingError("terminal notification saga state is unavailable")
    if not data.get("terminalNotificationOwed"):
        if saga.get("notificationRequired", True):
            return
        try:
            _fenced_terminal_thread_update(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner,
                {
                f"handledEvents.{saga['eventKey']}": {
                    "detectedAt": SERVER_TIMESTAMP,
                    "detectedInMessageId": saga.get("sourceGraphMessageId"),
                    "notificationId": None,
                },
                "terminalNotificationOutcome": "not_required_already_nonviable",
                "updatedAt": SERVER_TIMESTAMP,
                },
                failure_label="terminal handled-marker persistence failed",
            )
            return
        except Exception as exc:
            raise RetryableProcessingError(
                f"terminal handled-marker persistence failed: {exc}"
            ) from exc

    try:
        _renew_terminal_saga_execution(user_id, saga, terminal_saga_owner)
        _source_row, terminal_final_row, _mutation_kind = (
            _terminal_sheet_mutation_geometry_from_saga(saga)
        )
        notification_id = write_notification(
            user_id,
            client_id,
            kind="property_unavailable",
            priority="important",
            email=recipient,
            thread_id=current_thread_id,
            row_number=terminal_final_row,
            row_anchor=saga.get("rowAnchor"),
            meta={
                "address": saga.get("eventAddress", ""),
                "city": saga.get("eventCity", ""),
                "reason": saga.get("reason"),
                "sourceMessageKey": saga.get("sourceMessageKey"),
            },
            dedupe_key=f"property_unavailable:{current_thread_id}:{saga.get('sagaKey')}",
        )
        if not notification_id:
            raise ValueError("notification write returned no durable identifier")
        _fenced_terminal_thread_update(
            user_id,
            current_thread_id,
            saga,
            terminal_saga_owner,
            {
            f"handledEvents.{saga['eventKey']}": {
                "detectedAt": SERVER_TIMESTAMP,
                "detectedInMessageId": saga.get("sourceGraphMessageId"),
                "notificationId": notification_id,
            },
            "terminalNotificationOwed": False,
            "terminalNotificationId": notification_id,
            "terminalNotificationOutcome": "created",
            "updatedAt": SERVER_TIMESTAMP,
            },
            failure_label="terminal notification outcome persistence failed",
        )
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal notification persistence failed: {exc}"
        ) from exc


TERMINAL_SETTLEMENT_VERSION = 2
TERMINAL_SETTLEMENT_HISTORY_LIMIT = 8


def _terminal_reply_attempt_archive_hash(attempt: Any) -> Optional[str]:
    if attempt is None:
        return None
    if not isinstance(attempt, dict):
        raise RetryableProcessingError(
            "terminal settlement reply attempt is malformed"
        )
    return hashlib.sha256(
        json.dumps(attempt, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _validate_terminal_settlement_projection(
    projection: Any,
    *,
    saga: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not isinstance(projection, dict):
        raise RetryableProcessingError(
            "terminal settlement projection is missing or malformed"
        )
    expected_hash = str(projection.get("projectionHash") or "").strip()
    immutable = {
        key: value
        for key, value in projection.items()
        if key != "projectionHash"
    }
    actual_hash = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if (
        projection.get("version") != TERMINAL_SETTLEMENT_VERSION
        or isinstance(projection.get("settlementOrdinal"), bool)
        or not isinstance(projection.get("settlementOrdinal"), int)
        or projection.get("settlementOrdinal") < 1
        or not str(projection.get("sagaKey") or "").strip()
        or not str(projection.get("sourceMessageKey") or "").strip()
        or not expected_hash
        or actual_hash != expected_hash
    ):
        raise RetryableProcessingError(
            "terminal settlement projection immutable hash does not match"
        )
    archived_saga = projection.get("sagaSnapshot")
    if not isinstance(archived_saga, dict):
        raise RetryableProcessingError(
            "terminal settlement is missing its immutable saga snapshot"
        )
    _validate_terminal_saga_immutable_hash(archived_saga)
    archived_final_row = (archived_saga.get("finalizationPlan") or {}).get(
        "finalRow"
    )
    if (
        archived_saga.get("phase") != "finalized"
        or archived_saga.get("finalRow") != archived_final_row
    ):
        raise RetryableProcessingError(
            "terminal settlement saga snapshot is not durably finalized"
        )
    if saga is not None and (
        archived_saga.get("sagaKey") != saga.get("sagaKey")
        or archived_saga.get("immutableHash") != saga.get("immutableHash")
    ):
        raise RetryableProcessingError(
            "terminal settlement archived saga does not match active saga"
        )
    validation_saga = archived_saga
    expected = {
        "sagaKey": validation_saga.get("sagaKey"),
        "sagaImmutableHash": validation_saga.get("immutableHash"),
        "sourceMessageKey": validation_saga.get("sourceMessageKey"),
        "sourceGraphMessageId": validation_saga.get("sourceGraphMessageId"),
        "sourceInternetMessageId": validation_saga.get(
            "sourceInternetMessageId"
        ),
        "finalRow": archived_final_row,
    }
    if validation_saga.get("settlementOrdinal") is not None:
        expected["settlementOrdinal"] = validation_saga.get(
            "settlementOrdinal"
        )
    actual = {key: projection.get(key) for key in expected}
    if actual != expected:
        raise RetryableProcessingError(
            "terminal settlement projection does not match immutable saga"
        )
    attempt = _validate_terminal_sheet_mutation_attempt(
        projection.get("sheetMutationAttempt"),
        validation_saga,
    )
    if attempt.get("status") not in _TERMINAL_SHEET_APPLIED_STATUSES:
        raise RetryableProcessingError(
            "terminal settlement does not archive an applied Sheet attempt"
        )
    _validate_terminal_sheet_mutation_history(
        projection.get("sheetMutationHistory"),
        validation_saga,
        mutation_kind=attempt.get("mutationKind"),
        active_attempt=attempt,
    )
    if projection.get("sheetMutationReview") is not None:
        raise RetryableProcessingError(
            "applied terminal settlement unexpectedly archives active Sheet review"
        )

    notification_outcome = projection.get("notificationOutcome")
    if validation_saga.get("notificationRequired"):
        if notification_outcome != "created":
            raise RetryableProcessingError(
                "terminal settlement notification outcome is not durable"
            )
    elif notification_outcome not in {
        "not_required",
        "not_required_already_nonviable",
    }:
        raise RetryableProcessingError(
            "terminal settlement no-notification outcome is malformed"
        )

    archived_reply_attempt = projection.get("terminalReplyAttempt")
    archived_reply_attempt_hash = projection.get("terminalReplyAttemptHash")
    if archived_reply_attempt is None:
        if archived_reply_attempt_hash is not None:
            raise RetryableProcessingError(
                "terminal settlement no-attempt reply hash is malformed"
            )
    else:
        if (
            archived_reply_attempt_hash
            != _terminal_reply_attempt_archive_hash(archived_reply_attempt)
            or archived_reply_attempt.get("sourceMessageKey")
            != validation_saga.get("sourceMessageKey")
            or archived_reply_attempt.get("sourceGraphMessageId")
            != validation_saga.get("sourceGraphMessageId")
            or archived_reply_attempt.get("conversationId")
            != validation_saga.get("sourceConversationId")
            or str(archived_reply_attempt.get("recipient") or "").strip().lower()
            != str(validation_saga.get("replyRecipient") or "").strip().lower()
        ):
            raise RetryableProcessingError(
                "terminal settlement reply attempt hash or source binding drifted"
            )
    reply_outcome = projection.get("replyOutcome")
    if validation_saga.get("responseScenario") != "none":
        if reply_outcome == "campaign_stopped":
            if archived_reply_attempt is not None:
                raise RetryableProcessingError(
                    "campaign-stopped terminal settlement unexpectedly archives "
                    "a provider reply attempt"
                )
        elif (
            not isinstance(archived_reply_attempt, dict)
            or archived_reply_attempt.get("sagaKey")
            != validation_saga.get("sagaKey")
            or archived_reply_attempt.get("status")
            not in {"committed", "reconciled"}
            or archived_reply_attempt.get("outcome") != reply_outcome
            or reply_outcome
            not in {
                "sent_indexed",
                "sent_unindexed",
                "sent_reconciled",
                "queued_retry",
                "recipient_suppressed",
                "draft_needs_review",
            }
        ):
            raise RetryableProcessingError(
                "terminal settlement does not archive the exact resolved reply attempt"
            )
        else:
            _validate_terminal_reply_attempt_body(
                validation_saga,
                archived_reply_attempt,
            )
    elif archived_reply_attempt is not None or reply_outcome != "not_required":
        raise RetryableProcessingError(
            "no-reply terminal settlement archives a malformed reply outcome"
        )
    return dict(projection)


def _validate_terminal_settlement_history(
    settlements: Any,
) -> List[Dict[str, Any]]:
    if settlements is None:
        return []
    if (
        not isinstance(settlements, list)
        or len(settlements) > TERMINAL_SETTLEMENT_HISTORY_LIMIT
    ):
        raise RetryableProcessingError(
            "terminal settlement history is malformed or over its retention limit"
        )
    validated: List[Dict[str, Any]] = []
    saga_keys = set()
    source_keys = set()
    for ordinal, raw_projection in enumerate(settlements, start=1):
        projection = _validate_terminal_settlement_projection(raw_projection)
        if projection.get("settlementOrdinal") != ordinal:
            raise RetryableProcessingError(
                "terminal settlement history ordinal drifted"
            )
        saga_key = projection.get("sagaKey")
        source_key = projection.get("sourceMessageKey")
        if saga_key in saga_keys or source_key in source_keys:
            raise RetryableProcessingError(
                "terminal settlement history contains a duplicate generation"
            )
        saga_keys.add(saga_key)
        source_keys.add(source_key)
        validated.append(projection)
    return validated


def _terminal_settlement_for_source(
    thread_data: Dict[str, Any],
    message_id: str,
    internet_message_id: str,
) -> Optional[Dict[str, Any]]:
    settlements = _validate_terminal_settlement_history(
        (thread_data or {}).get("terminalSettlements")
    )
    if not settlements:
        return None
    expected_key = _terminal_source_message_key(message_id, internet_message_id)
    matches = []
    for projection in settlements:
        persisted_sources = {
            str(value).strip()
            for value in (
                projection.get("sourceMessageKey"),
                projection.get("sourceGraphMessageId"),
                projection.get("sourceInternetMessageId"),
            )
            if str(value or "").strip()
        }
        if expected_key not in persisted_sources:
            continue
        if (
            projection.get("sourceGraphMessageId")
            and projection.get("sourceGraphMessageId") != message_id
        ):
            continue
        if (
            projection.get("sourceInternetMessageId")
            and projection.get("sourceInternetMessageId") != internet_message_id
        ):
            continue
        matches.append(projection)
    if len(matches) > 1:
        raise RetryableProcessingError(
            "multiple terminal settlements match the same exact source"
        )
    return matches[0] if matches else None


_CLIENT_COMPLETION_INELIGIBLE_STATUSES = frozenset({
    "stopping",
    "stopped",
    "archived",
    "deleted",
})


def _client_status_for_terminal_completion_replay(client_ref) -> str:
    try:
        snapshot = client_ref.get()
        if getattr(snapshot, "exists", False) is not True:
            raise RetryableProcessingError(
                "terminal completion replay client is missing"
            )
        data = snapshot.to_dict() or {}
        return str(data.get("status") or "").strip().lower()
    except RetryableProcessingError:
        raise
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal completion replay client read failed: {exc}"
        ) from exc


def _require_terminal_client_completion(
    user_id: str,
    client_id: str,
) -> None:
    """Fail retryably until a post-terminal local completion is durable."""
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id:
        raise RetryableProcessingError(
            "terminal completion client binding is missing"
        )
    client_ref = (
        _fs.collection("users").document(user_id)
        .collection("clients").document(normalized_client_id)
    )
    status = _client_status_for_terminal_completion_replay(client_ref)
    if status == "completed" or status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
        return
    try:
        completed = _maybe_mark_client_completed(
            user_id,
            normalized_client_id,
            client_ref=client_ref,
        )
    except Exception:
        completed = False
    if completed is True:
        return
    status = _client_status_for_terminal_completion_replay(client_ref)
    if status == "completed" or status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
        return
    raise RetryableProcessingError(
        "terminal settlement client completion obligation remains unresolved"
    )


def _replay_terminal_completion_obligation(
    user_id: str,
    thread_data: Dict[str, Any],
    settlement: Dict[str, Any],
    message_id: str,
    internet_message_id: str,
) -> None:
    """Replay only the local client-completion intent from an exact tombstone."""
    projection = _validate_terminal_settlement_projection(settlement)
    expected_source_key = _terminal_source_message_key(
        message_id,
        internet_message_id,
    )
    persisted_sources = {
        str(value).strip()
        for value in (
            projection.get("sourceMessageKey"),
            projection.get("sourceGraphMessageId"),
            projection.get("sourceInternetMessageId"),
        )
        if str(value or "").strip()
    }
    if (
        expected_source_key not in persisted_sources
        or (
            projection.get("sourceGraphMessageId")
            and projection.get("sourceGraphMessageId") != message_id
        )
        or (
            projection.get("sourceInternetMessageId")
            and projection.get("sourceInternetMessageId")
            != internet_message_id
        )
    ):
        raise RetryableProcessingError(
            "terminal completion replay settlement source drifted"
        )

    saga_snapshot = projection.get("sagaSnapshot")
    completion_required = saga_snapshot.get("completeClientAfterReply")
    if type(completion_required) is not bool:
        raise RetryableProcessingError(
            "terminal completion replay obligation is malformed"
        )
    if not completion_required:
        return

    client_id = str(saga_snapshot.get("clientId") or "").strip()
    thread_client_id = str((thread_data or {}).get("clientId") or "").strip()
    if not client_id or (
        thread_client_id and thread_client_id != client_id
    ):
        raise RetryableProcessingError(
            "terminal completion replay client binding drifted"
        )

    client_ref = (
        _fs.collection("users").document(user_id)
        .collection("clients").document(client_id)
    )
    status = _client_status_for_terminal_completion_replay(client_ref)
    if status == "completed" or status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
        return

    try:
        completed = _maybe_mark_client_completed(
            user_id,
            client_id,
            client_ref=client_ref,
        )
    except Exception:
        completed = False
    if completed is True:
        return

    status = _client_status_for_terminal_completion_replay(client_ref)
    if status == "completed" or status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
        return
    raise RetryableProcessingError(
        "terminal settlement client completion obligation remains unresolved"
    )


def _persist_terminal_settlement_projection(
    user_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
) -> Dict[str, Any]:
    """Persist immutable exact-source settlement before active pointers clear."""
    _validate_terminal_saga_immutable_hash(saga)
    threads_ref = (
        _fs.collection("users").document(user_id).collection("threads")
    )
    claim_thread_id = (saga.get("finalizationPlan") or {}).get("claimThreadId")
    claim_ref = threads_ref.document(claim_thread_id)
    current_ref = threads_ref.document(current_thread_id)
    now = datetime.now(timezone.utc)
    transaction_state = {"commitAttempted": False}

    def persist_projection(transaction) -> Dict[str, Any]:
        transaction_state["commitAttempted"] = False
        claim_snapshot = claim_ref.get(transaction=transaction)
        claim_data = claim_snapshot.to_dict() if claim_snapshot.exists else {}
        claim = _validate_terminal_saga_execution_claim(
            (claim_data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
            now=now,
        )
        current_snapshot = current_ref.get(transaction=transaction)
        current_data = current_snapshot.to_dict() if current_snapshot.exists else {}
        if (
            not current_snapshot.exists
            or (current_data.get("terminalSaga") or {}).get("sagaKey")
            != saga.get("sagaKey")
            or current_data.get("terminalNotificationOwed")
            or current_data.get("terminalReplyOwed")
        ):
            raise RetryableProcessingError(
                "terminal settlement requires the exact resolved current saga"
            )
        try:
            assert_terminal_reply_permit_settled(
                transaction,
                current_ref,
                thread_data=current_data,
            )
        except GraphSendPermitBlocked as exc:
            raise RetryableProcessingError(
                f"terminal settlement blocked by Graph send permit: {exc}"
            ) from exc

        settlements = _validate_terminal_settlement_history(
            current_data.get("terminalSettlements")
        )
        for existing in settlements:
            if existing.get("sagaKey") == saga.get("sagaKey"):
                return _validate_terminal_settlement_projection(
                    existing,
                    saga=saga,
                )
        new_source_values = {
            str(value).strip()
            for value in (
                saga.get("sourceMessageKey"),
                saga.get("sourceGraphMessageId"),
                saga.get("sourceInternetMessageId"),
            )
            if str(value or "").strip()
        }
        for existing in settlements:
            existing_source_values = {
                str(value).strip()
                for value in (
                    existing.get("sourceMessageKey"),
                    existing.get("sourceGraphMessageId"),
                    existing.get("sourceInternetMessageId"),
                )
                if str(value or "").strip()
            }
            if new_source_values & existing_source_values:
                raise RetryableProcessingError(
                    "terminal settlement source already belongs to another generation"
                )
        if len(settlements) >= TERMINAL_SETTLEMENT_HISTORY_LIMIT:
            raise RetryableProcessingError(
                "terminal settlement retention limit reached; operator review is required"
            )
        reserved_settlement_ordinal = saga.get("settlementOrdinal")
        if reserved_settlement_ordinal is None and saga.get("version") == 1:
            reserved_settlement_ordinal = len(settlements) + 1
        if reserved_settlement_ordinal != len(settlements) + 1:
            raise RetryableProcessingError(
                "terminal settlement ordinal no longer matches its staged reservation"
            )

        sheet_attempt = _validate_terminal_sheet_mutation_attempt(
            (claim_data or {}).get("terminalSheetMutationAttempt"),
            saga,
        )
        if sheet_attempt.get("status") not in _TERMINAL_SHEET_APPLIED_STATUSES:
            raise RetryableProcessingError(
                "terminal settlement requires an applied Sheet mutation attempt"
            )
        sheet_history = _validate_terminal_sheet_mutation_history(
            (claim_data or {}).get("terminalSheetMutationHistory"),
            saga,
            mutation_kind=sheet_attempt.get("mutationKind"),
            active_attempt=sheet_attempt,
        )
        _source_row, terminal_final_row, _mutation_kind = (
            _terminal_sheet_mutation_geometry_from_saga(saga)
        )

        immutable = {
            "version": TERMINAL_SETTLEMENT_VERSION,
            "settlementOrdinal": reserved_settlement_ordinal,
            "sagaKey": saga.get("sagaKey"),
            "sagaImmutableHash": saga.get("immutableHash"),
            "sourceMessageKey": saga.get("sourceMessageKey"),
            "sourceGraphMessageId": saga.get("sourceGraphMessageId"),
            "sourceInternetMessageId": saga.get("sourceInternetMessageId"),
            "finalRow": terminal_final_row,
            "notificationOutcome": (
                current_data.get("terminalNotificationOutcome")
                or "not_required"
            ),
            "replyOutcome": (
                current_data.get("terminalReplyOutcome") or "not_required"
            ),
            "sagaSnapshot": copy.deepcopy(saga),
            "terminalReplyAttempt": current_data.get("terminalReplyAttempt"),
            "terminalReplyAttemptHash": _terminal_reply_attempt_archive_hash(
                current_data.get("terminalReplyAttempt")
            ),
            "sheetMutationAttempt": sheet_attempt,
            "sheetMutationHistory": sheet_history,
            "sheetMutationReview": (claim_data or {}).get(
                "terminalSheetMutationReview"
            ),
            "settledAt": now,
        }
        projection = {
            **immutable,
            "projectionHash": hashlib.sha256(
                json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }
        projection = _validate_terminal_settlement_projection(
            projection,
            saga=saga,
        )
        renewed_claim = {
            **claim,
            "leaseUntil": now + timedelta(
                seconds=TERMINAL_SAGA_EXECUTION_LEASE_SECONDS
            ),
            "renewedAt": now,
        }
        updated_settlements = [*settlements, projection]
        if claim_thread_id == current_thread_id:
            transaction.update(current_ref, {
                "terminalSettlements": updated_settlements,
                "terminalSettlement": None,
                "terminalSagaClaim": renewed_claim,
                "updatedAt": SERVER_TIMESTAMP,
            })
        else:
            transaction.update(claim_ref, {
                "terminalSagaClaim": renewed_claim,
                "updatedAt": SERVER_TIMESTAMP,
            })
            transaction.update(current_ref, {
                "terminalSettlements": updated_settlements,
                "terminalSettlement": None,
                "updatedAt": SERVER_TIMESTAMP,
            })
        transaction_state["commitAttempted"] = True
        return projection

    try:
        return run_firestore_transaction(_fs, persist_projection)
    except Exception as exc:
        if not transaction_state["commitAttempted"]:
            if isinstance(exc, RetryableProcessingError):
                raise
            raise RetryableProcessingError(
                f"terminal settlement projection persistence failed: {exc}"
            ) from exc
        try:
            readback = current_ref.get()
            readback_data = readback.to_dict() if readback.exists else {}
            committed = _terminal_settlement_for_source(
                readback_data,
                saga.get("sourceGraphMessageId"),
                saga.get("sourceInternetMessageId"),
            )
            if committed is None:
                raise RetryableProcessingError(
                    "terminal settlement commit readback did not find exact source"
                )
            return _validate_terminal_settlement_projection(committed, saga=saga)
        except Exception:
            if isinstance(exc, RetryableProcessingError):
                raise
            raise RetryableProcessingError(
                f"terminal settlement projection persistence failed: {exc}"
            ) from exc


def _terminal_cleanup_readback_is_exact(
    threads_ref,
    current_thread_id: str,
    saga: Dict[str, Any],
) -> bool:
    plan = saga.get("finalizationPlan") or {}
    exact_ids = list(plan.get("terminalThreadIds") or [])
    claim_thread_id = plan.get("claimThreadId")
    for terminal_id in exact_ids:
        snapshot = threads_ref.document(terminal_id).get()
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if any(
            data.get(field) is not None
            for field in (
                "terminalSagaKey",
                "pendingTerminalReason",
                "pendingTerminalAt",
                "pendingTerminalSourceRow",
            )
        ):
            return False
        if terminal_id == claim_thread_id:
            if data.get("terminalSagaClaim") is not None:
                return False
            if any(
                data.get(field) is not None
                for field in (
                    "terminalSheetMutationAttempt",
                    "terminalSheetMutationHistory",
                    "terminalSheetMutationReview",
                )
            ):
                return False
        if terminal_id == current_thread_id:
            if (
                data.get("terminalSaga") is not None
                or data.get("terminalNotificationOwed")
                or data.get("terminalReplyOwed")
                or data.get("terminalReplyAttempt") is not None
            ):
                return False
            try:
                settlement = _terminal_settlement_for_source(
                    data,
                    saga.get("sourceGraphMessageId"),
                    saga.get("sourceInternetMessageId"),
                )
                if settlement is None:
                    return False
                _validate_terminal_settlement_projection(settlement, saga=saga)
            except RetryableProcessingError:
                return False
    return True


def _clear_resolved_terminal_saga(
    user_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
    *,
    terminal_saga_owner: TerminalSagaExecution,
) -> None:
    threads_ref = (
        _fs.collection("users").document(user_id).collection("threads")
    )
    plan = saga.get("finalizationPlan") or {}
    exact_ids = list(plan.get("terminalThreadIds") or [])
    claim_thread_id = plan.get("claimThreadId")
    if not exact_ids or len(exact_ids) > FIRESTORE_BATCH_WRITE_LIMIT:
        raise RetryableProcessingError("terminal saga cleanup plan is invalid")
    if current_thread_id not in exact_ids or claim_thread_id not in exact_ids:
        raise RetryableProcessingError("terminal saga cleanup roots are invalid")
    _persist_terminal_settlement_projection(
        user_id,
        current_thread_id,
        saga,
        terminal_saga_owner,
    )
    claim_ref = threads_ref.document(claim_thread_id)
    current_ref = threads_ref.document(current_thread_id)
    transaction_state = {"commitAttempted": False}

    def clear_saga(transaction) -> None:
        transaction_state["commitAttempted"] = False
        claim_snapshot = claim_ref.get(transaction=transaction)
        claim_data = claim_snapshot.to_dict() if claim_snapshot.exists else {}
        _validate_terminal_saga_execution_claim(
            (claim_data or {}).get("terminalSagaClaim"),
            saga,
            terminal_saga_owner,
        )
        current_snapshot = current_ref.get(transaction=transaction)
        current_data = current_snapshot.to_dict() if current_snapshot.exists else {}
        if (
            not current_snapshot.exists
            or (current_data.get("terminalSaga") or {}).get("sagaKey")
            != saga.get("sagaKey")
            or current_data.get("terminalNotificationOwed")
            or current_data.get("terminalReplyOwed")
        ):
            raise RetryableProcessingError(
                "terminal saga still has unresolved obligations"
            )
        try:
            assert_terminal_reply_permit_settled(
                transaction,
                current_ref,
                thread_data=current_data,
            )
        except GraphSendPermitBlocked as exc:
            raise RetryableProcessingError(
                f"terminal saga cleanup blocked by Graph send permit: {exc}"
            ) from exc
        settlement = _terminal_settlement_for_source(
            current_data,
            saga.get("sourceGraphMessageId"),
            saga.get("sourceInternetMessageId"),
        )
        if settlement is None:
            raise RetryableProcessingError(
                "terminal settlement projection disappeared before cleanup"
            )
        _validate_terminal_settlement_projection(settlement, saga=saga)
        exact_snapshots = {}
        for terminal_id in exact_ids:
            if terminal_id == current_thread_id:
                exact_snapshots[terminal_id] = current_snapshot
                continue
            if terminal_id == claim_thread_id:
                exact_snapshots[terminal_id] = claim_snapshot
                continue
            exact_snapshots[terminal_id] = threads_ref.document(terminal_id).get(
                transaction=transaction
            )
        for terminal_id, snapshot in exact_snapshots.items():
            data = snapshot.to_dict() if snapshot.exists else {}
            if (
                not snapshot.exists
                or data.get("terminalSagaKey") != saga.get("sagaKey")
            ):
                raise RetryableProcessingError(
                    f"terminal saga cleanup root drifted: {terminal_id}"
                )
        for terminal_id in exact_ids:
            patch = {
                "terminalSagaKey": None,
                "pendingTerminalReason": None,
                "pendingTerminalAt": None,
                "pendingTerminalSourceRow": None,
                "updatedAt": SERVER_TIMESTAMP,
            }
            if terminal_id == current_thread_id:
                patch["terminalSaga"] = None
                patch["terminalReplyAttempt"] = None
            if terminal_id == claim_thread_id:
                patch["terminalSagaClaim"] = None
                patch["terminalSheetMutationAttempt"] = None
                patch["terminalSheetMutationHistory"] = None
                patch["terminalSheetMutationReview"] = None
            transaction.update(threads_ref.document(terminal_id), patch)
        transaction_state["commitAttempted"] = True

    try:
        run_firestore_transaction(_fs, clear_saga)
    except Exception as exc:
        if transaction_state["commitAttempted"]:
            if _terminal_cleanup_readback_is_exact(
                threads_ref,
                current_thread_id,
                saga,
            ):
                return
        raise RetryableProcessingError(
            f"terminal saga cleanup failed: {exc}"
        ) from exc


def _terminal_reply_will_queue_after_definite_send_failure(
    send_outcome: ReplySendOutcome,
) -> bool:
    """Mirror the generic retry helper's branches that create pending work."""
    if (
        send_outcome.sent_but_unindexed
        or send_outcome.outcome == "sent_but_unindexed"
        or send_outcome.campaign_suppression_kind == "terminal"
        or send_outcome.outcome == "blocked_campaign_terminal"
        or send_outcome.outcome == "suppressed_recipient_optout"
    ):
        return False
    return True


def _terminal_reply_body_hash(saga: Dict[str, Any]) -> str:
    """Hash the immutable, validated saga body that is actually sent or queued."""
    response_body = str((saga or {}).get("responseBody") or "").strip()
    if not response_body:
        raise RetryableProcessingError("terminal reply obligation has no persisted body")
    return hashlib.sha256(response_body.encode("utf-8")).hexdigest()


def _validate_terminal_reply_attempt_body(
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
) -> str:
    expected_body_hash = _terminal_reply_body_hash(saga)
    if str((attempt or {}).get("responseBodyHash") or "") != expected_body_hash:
        raise RetryableProcessingError(
            "terminal reply attempt body hash does not match immutable saga response body"
        )
    return expected_body_hash


def _terminal_pending_response_ref(user_id: str, current_thread_id: str):
    return (
        _fs.collection("users").document(user_id)
        .collection("pendingResponses").document(current_thread_id)
    )


def _validate_exact_terminal_pending_response_data(
    pending: Dict[str, Any],
    current_thread_id: str,
    client_id: str,
    recipient: str,
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Dict[str, Any]:
    expected_body_hash = _validate_terminal_reply_attempt_body(saga, attempt)
    body_hash = hashlib.sha256(
        str((pending or {}).get("responseBody") or "").encode("utf-8")
    ).hexdigest()
    expected = {
        "threadId": current_thread_id,
        "msgId": saga.get("sourceGraphMessageId"),
        "recipient": str(recipient or "").strip().lower(),
        "clientId": client_id,
        "conversationId": (
            attempt.get("conversationId") or saga.get("sourceConversationId")
        ),
        "responseBodyHash": expected_body_hash,
    }
    actual = {
        "threadId": (pending or {}).get("threadId"),
        "msgId": (pending or {}).get("msgId"),
        "recipient": str((pending or {}).get("recipient") or "").strip().lower(),
        "clientId": (pending or {}).get("clientId"),
        "conversationId": (pending or {}).get("conversationId"),
        "responseBodyHash": body_hash,
    }
    if actual != expected:
        raise RetryableProcessingError(
            "terminal pending response evidence does not match immutable reply intent"
        )
    return pending


def _exact_terminal_pending_response(
    user_id: str,
    current_thread_id: str,
    client_id: str,
    recipient: str,
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return only the deterministic pending doc for this immutable reply intent."""
    _validate_terminal_reply_attempt_body(saga, attempt)
    try:
        snapshot = _terminal_pending_response_ref(user_id, current_thread_id).get()
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal pending response lookup failed: {exc}"
        ) from exc
    if not snapshot.exists:
        return None
    return _validate_exact_terminal_pending_response_data(
        snapshot.to_dict() or {},
        current_thread_id,
        client_id,
        recipient,
        saga,
        attempt,
    )


def _terminal_pending_response_payload(
    current_thread_id: str,
    client_id: str,
    recipient: str,
    response_body: str,
    saga: Dict[str, Any],
    *,
    error: str,
    subject: Optional[str] = None,
    conversation_id: Optional[str] = None,
    last_send_attempt_at: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the deterministic pending projection used by the terminal CAS."""
    payload = {
        "threadId": current_thread_id,
        "msgId": saga.get("sourceGraphMessageId"),
        "recipient": recipient,
        "responseBody": response_body,
        "clientId": client_id,
        "attempts": 1,
        "lastError": error,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    resolved_conversation_id = (
        conversation_id or saga.get("sourceConversationId")
    )
    if resolved_conversation_id:
        payload["conversationId"] = resolved_conversation_id
    if subject:
        payload["subject"] = subject
    if last_send_attempt_at:
        payload["lastSendAttemptAt"] = last_send_attempt_at
    return payload


def _terminal_reply_reconciliation_document(
    user_id: str,
    current_thread_id: str,
    saga: Dict[str, Any],
    permit: Dict[str, Any],
    *,
    kind: str,
    already_sent: Optional[bool],
    provider_send_started: bool,
    reason: str,
) -> tuple[Any, Dict[str, Any]]:
    identity = hashlib.sha256(
        f"{saga.get('sagaKey')}:{permit.get('permitId')}:{kind}".encode("utf-8")
    ).hexdigest()
    ref = (
        _fs.collection("users").document(user_id)
        .collection("terminalGraphSendReviews")
        .document(f"terminal-reply-{identity}")
    )
    if kind == "send_needs_reconciliation" and already_sent is not None:
        raise RetryableProcessingError(
            "ambiguous terminal send review must preserve tri-state Sent evidence"
        )
    payload = {
        "threadId": current_thread_id,
        "msgId": saga.get("sourceGraphMessageId"),
        "recipient": saga.get("replyRecipient"),
        "responseBody": saga.get("responseBody"),
        "clientId": saga.get("clientId"),
        "conversationId": saga.get("sourceConversationId"),
        "source": "terminalGraphSendProtocol",
        "authoritative": True,
        "status": (
            "needs_reconciliation" if provider_send_started
            else "manual_review"
        ),
        "alreadySent": already_sent,
        "providerSendStarted": bool(provider_send_started),
        "sendOutcomeUnknown": bool(
            provider_send_started and already_sent is None
        ),
        "retryAllowed": False,
        "failureReason": reason,
        "graphSendPermitId": permit.get("permitId"),
        "graphSendPermitHash": permit.get("immutableHash"),
        "preparedEnvelopeHash": (
            permit.get("sendPreparedEnvelopeHash")
            or (permit.get("preparedEnvelope") or {}).get(
                "preparedEnvelopeHash"
            )
        ),
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "deadLetteredAt": SERVER_TIMESTAMP,
    }
    if kind == "draft_needs_review":
        preparation = dict(permit.get("draftPreparation") or {})
        resolution_evidence = dict(permit.get("resolutionEvidence") or {})
        payload.update({
            "draftId": preparation.get("draftId"),
            "draftMutationState": preparation.get("state"),
            "draftResolutionEvidenceHash": permit.get(
                "resolutionEvidenceHash"
            ),
            "automaticDeleteAttempted": resolution_evidence.get(
                "automaticDeleteAttempted",
                False,
            ),
        })
    return ref, payload


def _cas_terminal_reply_transition(*args, **kwargs) -> bool:
    try:
        return _cas_graph_terminal_reply_transition(*args, **kwargs)
    except GraphSendPermitBlocked as exc:
        raise RetryableProcessingError(
            f"terminal reply Graph permit CAS failed: {exc}"
        ) from exc
    except Exception as exc:
        raise RetryableProcessingError(
            f"terminal reply outcome persistence failed: {exc}"
        ) from exc


def _fenced_reconcile_terminal_reply_sent(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    recipient: str,
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
    sent_match: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
) -> None:
    """Settle retained send evidence and the terminal outcome in one CAS."""
    claim_ref = _terminal_saga_claim_ref(user_id, saga)
    current_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(current_thread_id)
    )
    pending_ref = _terminal_pending_response_ref(user_id, current_thread_id)
    try:
        current_attempt = dict(attempt or {})
        _validate_terminal_reply_attempt_body(saga, current_attempt)
        body_hash = current_attempt.get("responseBodyHash")
        retained_permit = read_active_terminal_reply_permit(
            _fs,
            current_ref,
            current_attempt,
            saga,
        )
        prepared_envelope_hash = (
            retained_permit.get("sendPreparedEnvelopeHash")
            or (retained_permit.get("preparedEnvelope") or {}).get(
                "preparedEnvelopeHash"
            )
        )
        reconciled_attempt = {
            **current_attempt,
            "status": "reconciled",
            "outcome": "sent_reconciled",
            "reconciledAt": SERVER_TIMESTAMP,
            "sentMessageId": (
                sent_match.get("sentMessageId") or sent_match.get("id")
            ),
            "sentInternetMessageId": sent_match.get("internetMessageId"),
            "sentConversationId": sent_match.get("conversationId"),
            "sentDateTime": sent_match.get("sentDateTime"),
        }
        _cas_terminal_reply_transition(
            _fs,
            current_ref,
            claim_ref,
            saga,
            terminal_saga_owner.owner,
            terminal_saga_owner.fencing_token,
            expected_attempt_status=current_attempt.get("status"),
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_reconciled",
                "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                "terminalReplyAttempt": reconciled_attempt,
                "updatedAt": SERVER_TIMESTAMP,
            },
            permit_settlement="settled_sent",
            sent_evidence={
                **dict(sent_match or {}),
                "sentMessageId": (
                    sent_match.get("sentMessageId") or sent_match.get("id")
                ),
                "recipient": recipient,
                "bodyHash": body_hash,
                "conversationId": (
                    sent_match.get("conversationId")
                    or saga.get("sourceConversationId")
                ),
                "permitId": retained_permit.get("permitId"),
                "sourceGraphMessageId": retained_permit.get(
                    "sourceGraphMessageId"
                ),
                "preparedEnvelopeHash": prepared_envelope_hash,
            },
            pending_delete_ref=pending_ref,
        )
    except Exception as exc:
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"terminal reply Sent reconciliation persistence failed: {exc}"
        ) from exc


def _ensure_terminal_reply_queue(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    recipient: str,
    response_body: str,
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
    terminal_saga_owner: TerminalSagaExecution,
    *,
    error: str,
    subject: Optional[str] = None,
    last_send_attempt_at: Optional[Any] = None,
    intent_already_renewed: bool = False,
    canonical_source_id: Optional[str] = None,
    work_key: Optional[str] = None,
    proposal_hash: Optional[str] = None,
    selection_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the deterministic pending item at most once after a fenced intent."""
    pending_binding = _pending_response_source_binding(
        canonical_source_id=canonical_source_id,
        work_key=work_key,
        proposal_hash=proposal_hash,
        selection_hash=selection_hash,
    )
    pending = _exact_terminal_pending_response(
        user_id,
        current_thread_id,
        client_id,
        recipient,
        saga,
        attempt,
    )
    if pending is not None:
        return attempt

    renewed_attempt = attempt
    if not intent_already_renewed:
        renewed_attempt = {
            **attempt,
            "queueIntentRenewedAt": SERVER_TIMESTAMP,
        }
        _fenced_terminal_thread_update(
            user_id,
            current_thread_id,
            saga,
            terminal_saga_owner,
            {
                "terminalReplyAttempt": renewed_attempt,
                "updatedAt": SERVER_TIMESTAMP,
            },
            renew_lease=True,
            failure_label="terminal reply queue-intent renewal failed",
        )

    queue_pending_response(
        user_id,
        current_thread_id,
        saga.get("sourceGraphMessageId"),
        recipient,
        response_body,
        client_id,
        error=error,
        subject=subject,
        conversation_id=(
            renewed_attempt.get("conversationId")
            or saga.get("sourceConversationId")
        ),
        last_send_attempt_at=last_send_attempt_at,
        **pending_binding,
    )
    if _exact_terminal_pending_response(
        user_id,
        current_thread_id,
        client_id,
        recipient,
        saga,
        renewed_attempt,
    ) is None:
        raise RetryableProcessingError(
            "terminal reply queue did not create durable exact pending evidence"
        )
    return renewed_attempt


def _settle_terminal_reply_obligation(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    headers: Dict[str, str],
    recipient: str,
    saga: Dict[str, Any],
    *,
    terminal_saga_owner: TerminalSagaExecution,
) -> str:
    """Require a durable sent, pending, reconciliation, or suppression outcome."""
    thread_ref = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(current_thread_id)
    )
    snapshot = thread_ref.get()
    data = snapshot.to_dict() if snapshot.exists else {}
    unissued_terminal_attempt = None
    if data.get("terminalReplyOwed"):
        durable_attempt = data.get("terminalReplyAttempt")
        if durable_attempt is not None and not isinstance(durable_attempt, dict):
            raise RetryableProcessingError(
                "terminal reply attempt is malformed before saga renewal"
            )
        if isinstance(durable_attempt, dict):
            has_permit_id = bool(durable_attempt.get("graphSendPermitId"))
            has_permit_hash = bool(
                durable_attempt.get("graphSendPermitHash")
            )
            if has_permit_id != has_permit_hash:
                raise RetryableProcessingError(
                    "terminal reply attempt has partial retained permit proof"
                )
            has_complete_permit_link = has_permit_id and has_permit_hash
            if (
                durable_attempt.get("status")
                != "queueing_campaign_suppression"
                and not has_complete_permit_link
            ):
                try:
                    validated_unissued_attempt = (
                        validate_unissued_terminal_reply_attempt(
                            saga,
                            durable_attempt,
                        )
                    )
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        f"terminal unissued reply attempt is not exact: {exc}"
                    ) from exc
                try:
                    active_permit = read_active_graph_send_permit(
                        thread_ref,
                        data,
                    )
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        "terminal unissued reply attempt has malformed retained "
                        f"Graph permit state: {exc}"
                    ) from exc
                if active_permit is None:
                    # The issuance transaction re-proves this exact source and
                    # the absence of a newly concurrent active permit.
                    unissued_terminal_attempt = validated_unissued_attempt
    _renew_terminal_saga_execution(user_id, saga, terminal_saga_owner)
    if data.get("terminalReplyOwed"):
        response_body = str(saga.get("responseBody") or "").strip()
        body_hash = _terminal_reply_body_hash(saga)
        attempt = data.get("terminalReplyAttempt")
        if unissued_terminal_attempt is not None:
            # A crash may leave the durable send intent immediately before
            # permit issuance. Preserve it rather than constructing a new one.
            attempt = None

        if attempt is not None:
            if not isinstance(attempt, dict) or attempt.get("sagaKey") != saga.get(
                "sagaKey"
            ):
                raise RetryableProcessingError(
                    "terminal reply attempt does not belong to the immutable saga"
                )
            status = attempt.get("status")
            if status not in {
                "sending",
                "needs_reconciliation",
                "queueing_response_retry",
                "queueing_campaign_suppression",
            }:
                raise RetryableProcessingError(
                    f"terminal reply attempt has unsupported owed status: {status}"
                )
            _validate_terminal_reply_attempt_body(saga, attempt)
            retained_permit = None
            permit_id = attempt.get("graphSendPermitId")
            permit_hash = attempt.get("graphSendPermitHash")
            if bool(permit_id) != bool(permit_hash):
                raise RetryableProcessingError(
                    "terminal reply attempt has partial retained permit proof"
                )
            if permit_id:
                try:
                    retained_permit = read_active_terminal_reply_permit(
                        _fs,
                        thread_ref,
                        attempt,
                        saga,
                    )
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        "terminal reply retained permit validation failed before "
                        f"Sent lookup: {exc}"
                    ) from exc
            sent_match = None
            permit_status = (retained_permit or {}).get("status")
            pre_send_recovery_kind = (
                expired_graph_send_pre_send_recovery_kind(
                    retained_permit
                )
                if status == "sending" and retained_permit is not None
                else None
            )
            if (
                status == "sending"
                and retained_permit is not None
                and (
                    pre_send_recovery_kind == "draft_needs_review"
                    or (
                        permit_status == "needs_reconciliation"
                        and retained_permit.get("requestStartedAt") is None
                    )
                )
            ):
                outcome = "draft_needs_review"
                resolution_evidence = dict(
                    retained_permit.get("resolutionEvidence") or {}
                )
                evidence_document = _terminal_reply_reconciliation_document(
                    user_id,
                    current_thread_id,
                    saga,
                    retained_permit,
                    kind="draft_needs_review",
                    already_sent=False,
                    provider_send_started=False,
                    reason=(
                        resolution_evidence.get("reason")
                        or "Expired retained Graph draft requires authoritative manual review"
                    ),
                )
                committed_attempt = {
                    **attempt,
                    "status": "committed",
                    "outcome": outcome,
                    "committedAt": SERVER_TIMESTAMP,
                }
                _cas_terminal_reply_transition(
                    _fs,
                    thread_ref,
                    _terminal_saga_claim_ref(user_id, saga),
                    saga,
                    terminal_saga_owner.owner,
                    terminal_saga_owner.fencing_token,
                    expected_attempt_status="sending",
                    thread_patch={
                        "terminalReplyOwed": False,
                        "terminalReplyOutcome": outcome,
                        "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                        "terminalReplyAttempt": committed_attempt,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    permit_settlement="settled_draft_needs_review",
                    side_documents=(evidence_document,),
                )
                _clear_resolved_terminal_saga(
                    user_id,
                    current_thread_id,
                    saga,
                    terminal_saga_owner=terminal_saga_owner,
                )
                return outcome
            if (
                status == "sending"
                and retained_permit is not None
                and pre_send_recovery_kind == "definitely_not_started"
            ):
                error = (
                    "Recovering an expired Graph permit that never started "
                    "provider work before deterministic queue creation"
                )
                attempt = {
                    **attempt,
                    "status": "queueing_response_retry",
                    "queueDocumentId": current_thread_id,
                    "conversationId": (
                        attempt.get("conversationId")
                        or saga.get("sourceConversationId")
                    ),
                    "sendFailure": error,
                    "definiteUnsentAt": datetime.now(timezone.utc),
                }
                _cas_terminal_reply_transition(
                    _fs,
                    thread_ref,
                    _terminal_saga_claim_ref(user_id, saga),
                    saga,
                    terminal_saga_owner.owner,
                    terminal_saga_owner.fencing_token,
                    expected_attempt_status="sending",
                    thread_patch={
                        "terminalReplyAttempt": attempt,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    permit_settlement="settled_definitely_not_sent",
                    pending_upsert=(
                        _terminal_pending_response_ref(
                            user_id,
                            current_thread_id,
                        ),
                        _terminal_pending_response_payload(
                            current_thread_id,
                            client_id,
                            recipient,
                            response_body,
                            saga,
                            error=error,
                            conversation_id=attempt.get("conversationId"),
                        ),
                    ),
                )
                status = "queueing_response_retry"
                permit_status = "settled_definitely_not_sent"
            if status == "queueing_response_retry" and (
                retained_permit is None
                or permit_status != "settled_definitely_not_sent"
            ):
                raise RetryableProcessingError(
                    "terminal queueing_response_retry lacks retained "
                    "definitely-not-sent permit proof"
                )
            if status == "queueing_campaign_suppression":
                if retained_permit is not None:
                    raise RetryableProcessingError(
                        "terminal pre-send campaign queue unexpectedly carries a permit"
                    )
                try:
                    active_permit = read_active_graph_send_permit(thread_ref, data)
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        "terminal pre-send campaign queue permit state is malformed: "
                        f"{exc}"
                    ) from exc
                if active_permit is not None:
                    raise RetryableProcessingError(
                        "terminal pre-send campaign queue has retained Graph activity"
                    )
            if retained_permit is not None and permit_status in {
                "request_started",
                "needs_reconciliation",
                "accepted",
                "reconciled_sent",
            }:
                sent_after = (
                    retained_permit.get("requestStartedAt")
                    if retained_permit is not None
                    else sent_after_from_retry_data(attempt)
                )
                if retained_permit is not None and not sent_after:
                    raise RetryableProcessingError(
                        "terminal send reconciliation is missing requestStartedAt"
                    )
                try:
                    prepared_envelope = (
                        retained_permit.get("preparedEnvelope") or {}
                    )
                    sent_match = find_exact_sent_message_by_immutable_id(
                        headers,
                        prepared_envelope.get("draftId"),
                        recipient=recipient,
                        to_recipients=prepared_envelope.get("toRecipients"),
                        cc_recipients=prepared_envelope.get("ccRecipients"),
                        require_no_bcc=True,
                        require_attachment_proof=True,
                        canonical_body_hash=prepared_envelope.get("htmlBodyHash"),
                        subject=prepared_envelope.get("subject"),
                        conversation_id=saga.get("sourceConversationId"),
                        attempts=2,
                    )
                except Exception as exc:
                    raise RetryableProcessingError(
                        "terminal reply Sent Items reconciliation failed closed: "
                        f"{exc}"
                    ) from exc
            if sent_match:
                _fenced_reconcile_terminal_reply_sent(
                    user_id,
                    client_id,
                    current_thread_id,
                    recipient,
                    saga,
                    attempt,
                    sent_match,
                    terminal_saga_owner,
                )
                outcome = "sent_reconciled"
                _clear_resolved_terminal_saga(
                    user_id,
                    current_thread_id,
                    saga,
                    terminal_saga_owner=terminal_saga_owner,
                )
                return outcome

            if status == "sending":
                try:
                    retained_permit = read_active_terminal_reply_permit(
                        _fs,
                        thread_ref,
                        attempt,
                        saga,
                    )
                except GraphSendPermitBlocked as exc:
                    raise RetryableProcessingError(
                        "terminal reply has a send intent without exact retained "
                        f"permit evidence: {exc}"
                    ) from exc
                if retained_permit.get("status") == "definitely_not_sent":
                    error = (
                        "Recovering retained definitely-unsent terminal reply "
                        "before deterministic queue creation"
                    )
                    attempt = {
                        **attempt,
                        "status": "queueing_response_retry",
                        "queueDocumentId": current_thread_id,
                        "conversationId": (
                            attempt.get("conversationId")
                            or saga.get("sourceConversationId")
                        ),
                        "sendFailure": error,
                        "definiteUnsentAt": SERVER_TIMESTAMP,
                    }
                    _cas_terminal_reply_transition(
                        _fs,
                        thread_ref,
                        _terminal_saga_claim_ref(user_id, saga),
                        saga,
                        terminal_saga_owner.owner,
                        terminal_saga_owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyAttempt": attempt,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        permit_settlement="settled_definitely_not_sent",
                        pending_upsert=(
                            _terminal_pending_response_ref(
                                user_id,
                                current_thread_id,
                            ),
                            _terminal_pending_response_payload(
                                current_thread_id,
                                client_id,
                                recipient,
                                response_body,
                                saga,
                                error=error,
                                conversation_id=attempt.get("conversationId"),
                            ),
                        ),
                    )
                    status = "queueing_response_retry"
                else:
                    raise RetryableProcessingError(
                        "terminal reply has a durable send intent but no confirmed "
                        "Sent Items match; retained permit remains unresolved and "
                        "duplicate send is refused"
                    )

            if status == "needs_reconciliation":
                raise RetryableProcessingError(
                    "terminal reply remains reconciliation-only without an exact "
                    "Sent Items match; duplicate send is refused"
                )

            error = (
                "Recovering durable definite-unsent terminal reply queue intent"
                if status == "queueing_response_retry"
                else "Recovering durable terminal reply campaign queue intent"
            )
            attempt = _ensure_terminal_reply_queue(
                user_id,
                client_id,
                current_thread_id,
                recipient,
                response_body,
                saga,
                attempt,
                terminal_saga_owner,
                error=error,
            )
            outcome = "queued_retry"
            _fenced_terminal_thread_update(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner,
                {
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": outcome,
                    "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": outcome,
                        "committedAt": SERVER_TIMESTAMP,
                    },
                    "updatedAt": SERVER_TIMESTAMP,
                },
                failure_label=(
                    "terminal reply queued outcome persistence failed"
                    if status == "queueing_response_retry"
                    else "terminal reply suppression outcome persistence failed"
                ),
            )
            _clear_resolved_terminal_saga(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner=terminal_saga_owner,
            )
            return outcome

        campaign_decision = get_client_automation_decision(user_id, client_id)
        campaign_suppression_kind = classify_campaign_suppression(campaign_decision)
        if campaign_suppression_kind:
            if campaign_suppression_kind == "terminal":
                outcome = "campaign_stopped"
                committed_attempt = None
            else:
                attempt = {
                    "sagaKey": saga.get("sagaKey"),
                    "sourceMessageKey": saga.get("sourceMessageKey"),
                    "sourceGraphMessageId": saga.get("sourceGraphMessageId"),
                    "conversationId": saga.get("sourceConversationId"),
                    "recipient": recipient,
                    "responseBodyHash": body_hash,
                    "status": "queueing_campaign_suppression",
                    "startedAt": SERVER_TIMESTAMP,
                }
                _fenced_terminal_thread_update(
                    user_id,
                    current_thread_id,
                    saga,
                    terminal_saga_owner,
                    {
                        "terminalReplyAttempt": attempt,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    renew_lease=True,
                    failure_label="terminal reply queue-intent persistence failed",
                )
                attempt = _ensure_terminal_reply_queue(
                    user_id,
                    client_id,
                    current_thread_id,
                    recipient,
                    response_body,
                    saga,
                    attempt,
                    terminal_saga_owner,
                    error=(
                        "Terminal reply queued while campaign automation is "
                        f"{campaign_suppression_kind}: {campaign_decision.reason}"
                    ),
                    intent_already_renewed=True,
                )
                outcome = "queued_retry"
                committed_attempt = {
                    **attempt,
                    "status": "committed",
                    "outcome": outcome,
                    "committedAt": SERVER_TIMESTAMP,
                }
            _fenced_terminal_thread_update(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner,
                {
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": outcome,
                    "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                    "terminalReplyAttempt": committed_attempt,
                    "updatedAt": SERVER_TIMESTAMP,
                },
                failure_label="terminal reply suppression outcome persistence failed",
            )
            _clear_resolved_terminal_saga(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner=terminal_saga_owner,
            )
            return outcome

        if unissued_terminal_attempt is None:
            attempt = {
                "sagaKey": saga.get("sagaKey"),
                "sourceMessageKey": saga.get("sourceMessageKey"),
                "sourceGraphMessageId": saga.get("sourceGraphMessageId"),
                "conversationId": saga.get("sourceConversationId"),
                "recipient": str(recipient or "").strip().lower(),
                "responseBodyHash": body_hash,
                "status": "sending",
                "startedAt": datetime.now(timezone.utc),
            }
            _fenced_terminal_thread_update(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner,
                {
                    "terminalReplyAttempt": attempt,
                    "updatedAt": SERVER_TIMESTAMP,
                },
                renew_lease=True,
                failure_label="terminal reply send-intent persistence failed",
            )
        else:
            attempt = unissued_terminal_attempt
        graph_send_capability = issue_terminal_graph_send_permit(
            _fs,
            thread_ref,
            _terminal_saga_claim_ref(user_id, saga),
            saga,
            terminal_saga_owner.owner,
            terminal_saga_owner.fencing_token,
        )
        attempt = {
            **attempt,
            "graphSendPermitId": graph_send_capability.permit_id,
            "graphSendPermitHash": graph_send_capability.immutable_hash,
        }
        sent = send_reply_in_thread(
            user_id,
            headers,
            response_body,
            saga.get("sourceGraphMessageId"),
            recipient,
            current_thread_id,
            graph_send_capability=graph_send_capability,
        )
        send_outcome = _get_reply_send_outcome()
        outcome_committed_with_permit = False
        if sent:
            if not isinstance(send_outcome.exact_sent_evidence, dict):
                raise RetryableProcessingError(
                    "terminal indexed reply lacks exact immutable Sent evidence"
                )
            outcome = "sent_indexed"
            committed_attempt = {
                **attempt,
                "status": "committed",
                "outcome": outcome,
                "committedAt": SERVER_TIMESTAMP,
            }
            _cas_terminal_reply_transition(
                _fs,
                thread_ref,
                _terminal_saga_claim_ref(user_id, saga),
                saga,
                terminal_saga_owner.owner,
                terminal_saga_owner.fencing_token,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": outcome,
                    "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                    "terminalReplyAttempt": committed_attempt,
                    "updatedAt": SERVER_TIMESTAMP,
                },
                permit_settlement="settled_sent",
                capability=graph_send_capability,
                sent_evidence=send_outcome.exact_sent_evidence,
            )
            attempt = committed_attempt
            outcome_committed_with_permit = True
        else:
            retained_permit = read_permit(graph_send_capability)
            permit_status = retained_permit.get("status")
            if (
                permit_status == "definitely_not_sent"
                and _terminal_reply_will_queue_after_definite_send_failure(
                    send_outcome
                )
            ):
                failure_reason = (
                    send_outcome.error or "send_reply_in_thread returned False"
                )
                attempt = {
                    **attempt,
                    "status": "queueing_response_retry",
                    "queueDocumentId": current_thread_id,
                    "conversationId": (
                        send_outcome.conversation_id
                        or saga.get("sourceConversationId")
                    ),
                    "sendFailure": send_outcome.error,
                    "definiteUnsentAt": SERVER_TIMESTAMP,
                }
                _cas_terminal_reply_transition(
                    _fs,
                    thread_ref,
                    _terminal_saga_claim_ref(user_id, saga),
                    saga,
                    terminal_saga_owner.owner,
                    terminal_saga_owner.fencing_token,
                    expected_attempt_status="sending",
                    thread_patch={
                        "terminalReplyAttempt": attempt,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    permit_settlement="settled_definitely_not_sent",
                    capability=graph_send_capability,
                    pending_upsert=(
                        _terminal_pending_response_ref(
                            user_id,
                            current_thread_id,
                        ),
                        _terminal_pending_response_payload(
                            current_thread_id,
                            client_id,
                            recipient,
                            response_body,
                            saga,
                            error=failure_reason,
                            subject=send_outcome.subject,
                            conversation_id=attempt.get("conversationId"),
                            last_send_attempt_at=send_outcome.send_attempt_at,
                        ),
                    ),
                )
                if _exact_terminal_pending_response(
                    user_id,
                    current_thread_id,
                    client_id,
                    recipient,
                    saga,
                    attempt,
                ) is None:
                    raise RetryableProcessingError(
                        "terminal definite-unsent CAS did not retain exact pending work"
                    )
                outcome = "queued_retry"
            else:
                if (
                    permit_status == "accepted"
                    and isinstance(send_outcome.exact_sent_evidence, dict)
                ):
                    outcome = "sent_unindexed"
                    committed_attempt = {
                        **attempt,
                        "status": "committed",
                        "outcome": outcome,
                        "committedAt": SERVER_TIMESTAMP,
                    }
                    _cas_terminal_reply_transition(
                        _fs,
                        thread_ref,
                        _terminal_saga_claim_ref(user_id, saga),
                        saga,
                        terminal_saga_owner.owner,
                        terminal_saga_owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyOwed": False,
                            "terminalReplyOutcome": outcome,
                            "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                            "terminalReplyAttempt": committed_attempt,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        permit_settlement="settled_sent",
                        capability=graph_send_capability,
                        sent_evidence=send_outcome.exact_sent_evidence,
                    )
                    attempt = committed_attempt
                    outcome_committed_with_permit = True
                elif permit_status == "definitely_not_sent":
                    outcome = _queue_response_retry_or_reconciliation(
                        user_id,
                        current_thread_id,
                        saga.get("sourceGraphMessageId"),
                        recipient,
                        response_body,
                        client_id,
                        source_context="terminalSaga",
                    )
                    committed_attempt = {
                        **attempt,
                        "status": "committed",
                        "outcome": outcome,
                        "committedAt": SERVER_TIMESTAMP,
                    }
                    _cas_terminal_reply_transition(
                        _fs,
                        thread_ref,
                        _terminal_saga_claim_ref(user_id, saga),
                        saga,
                        terminal_saga_owner.owner,
                        terminal_saga_owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyOwed": False,
                            "terminalReplyOutcome": outcome,
                            "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                            "terminalReplyAttempt": committed_attempt,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        permit_settlement="settled_definitely_not_sent",
                        capability=graph_send_capability,
                    )
                    attempt = committed_attempt
                    outcome_committed_with_permit = True
                elif (
                    send_outcome.outcome
                    == "draft_mutation_needs_reconciliation"
                    and permit_status == "needs_reconciliation"
                    and retained_permit.get("requestStartedAt") is None
                ):
                    outcome = "draft_needs_review"
                    evidence_document = _terminal_reply_reconciliation_document(
                        user_id,
                        current_thread_id,
                        saga,
                        retained_permit,
                        kind="draft_needs_review",
                        already_sent=False,
                        provider_send_started=False,
                        reason=(
                            send_outcome.error
                            or "Graph draft mutation became ambiguous before /send"
                        ),
                    )
                    committed_attempt = {
                        **attempt,
                        "status": "committed",
                        "outcome": outcome,
                        "committedAt": SERVER_TIMESTAMP,
                    }
                    _cas_terminal_reply_transition(
                        _fs,
                        thread_ref,
                        _terminal_saga_claim_ref(user_id, saga),
                        saga,
                        terminal_saga_owner.owner,
                        terminal_saga_owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyOwed": False,
                            "terminalReplyOutcome": outcome,
                            "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                            "terminalReplyAttempt": committed_attempt,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        permit_settlement="settled_draft_needs_review",
                        capability=graph_send_capability,
                        side_documents=(evidence_document,),
                    )
                    attempt = committed_attempt
                    outcome_committed_with_permit = True
                elif permit_status in {
                    "accepted",
                    "request_started",
                    "needs_reconciliation",
                }:
                    outcome = "needs_reconciliation"
                    evidence_document = _terminal_reply_reconciliation_document(
                        user_id,
                        current_thread_id,
                        saga,
                        retained_permit,
                        kind="send_needs_reconciliation",
                        already_sent=None,
                        provider_send_started=True,
                        reason=(
                            send_outcome.error
                            or "Graph send outcome is ambiguous"
                        ),
                    )
                    reconciliation_attempt = {
                        **attempt,
                        "status": "needs_reconciliation",
                        "outcome": outcome,
                        "reconciliationRecordedAt": SERVER_TIMESTAMP,
                    }
                    _cas_terminal_reply_transition(
                        _fs,
                        thread_ref,
                        _terminal_saga_claim_ref(user_id, saga),
                        saga,
                        terminal_saga_owner.owner,
                        terminal_saga_owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyAttempt": reconciliation_attempt,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        permit_settlement="reconciliation_recorded",
                        capability=graph_send_capability,
                        side_documents=(evidence_document,),
                    )
                    raise RetryableProcessingError(
                        "terminal Graph send remains reconciliation-only; duplicate "
                        "send is refused"
                    )
                else:
                    raise RetryableProcessingError(
                        "terminal Graph permit has unsupported reply outcome: "
                        f"{permit_status}"
                    )
        if outcome not in {
            "sent_indexed",
            "sent_unindexed",
            "queued_retry",
            "recipient_suppressed",
            "campaign_stopped",
            "draft_needs_review",
        }:
            raise RetryableProcessingError(
                f"terminal reply has no durable outcome: {outcome}"
            )
        if not outcome_committed_with_permit:
            _fenced_terminal_thread_update(
                user_id,
                current_thread_id,
                saga,
                terminal_saga_owner,
                {
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": outcome,
                    "terminalReplyResolvedAt": SERVER_TIMESTAMP,
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": outcome,
                        "committedAt": SERVER_TIMESTAMP,
                    },
                    "updatedAt": SERVER_TIMESTAMP,
                },
                failure_label="terminal reply outcome persistence failed",
            )
    else:
        outcome = data.get("terminalReplyOutcome") or "not_required"

    _clear_resolved_terminal_saga(
        user_id,
        current_thread_id,
        saga,
        terminal_saga_owner=terminal_saga_owner,
    )
    return outcome


def _build_terminal_saga(
    user_id: str,
    client_id: str,
    current_thread_id: str,
    *,
    message: Dict[str, Any],
    internet_message_id: str,
    conversation_id: str,
    sheet_id: str,
    tab_title: str,
    source_row: int,
    row_anchor: str,
    notes_column_index: int,
    sheet_header: List[str],
    terminal_reason: str,
    terminal_note: str,
    event_key: str,
    event: Dict[str, Any],
    divider_row: int,
    divider_exists: bool,
    row_already_nonviable: bool,
    has_alternative_path: bool,
    llm_response_email: Optional[str],
    column_config: Optional[Dict[str, Any]],
    contact_name: Optional[str],
    reply_recipient: str,
) -> Dict[str, Any]:
    source_graph_message_id = str(message.get("id") or "").strip()
    if (
        isinstance(notes_column_index, bool)
        or not isinstance(notes_column_index, int)
        or notes_column_index < 1
        or notes_column_index > len(sheet_header or [])
    ):
        raise RetryableProcessingError(
            "terminal saga requires an exact Notes/Comments coordinate"
        )
    notes_column_header = str(
        (sheet_header or [])[notes_column_index - 1] or ""
    ).strip()
    if not notes_column_header:
        raise RetryableProcessingError(
            "terminal saga Notes/Comments header is blank"
        )
    source_message_key = _terminal_source_message_key(
        source_graph_message_id,
        internet_message_id,
    )
    current_snapshot = (
        _fs.collection("users").document(user_id).collection("threads")
        .document(current_thread_id).get()
    )
    if not current_snapshot.exists:
        raise RetryableProcessingError(
            "terminal saga current root disappeared before settlement preflight"
        )
    prior_settlements = _validate_terminal_settlement_history(
        (current_snapshot.to_dict() or {}).get("terminalSettlements")
    )
    if len(prior_settlements) >= TERMINAL_SETTLEMENT_HISTORY_LIMIT:
        raise RetryableProcessingError(
            "terminal settlement retention limit reached before staging; "
            "operator review is required"
        )
    settlement_ordinal = len(prior_settlements) + 1
    saga_key = hashlib.sha256(
        f"{current_thread_id}\0{source_message_key}\0property_unavailable".encode("utf-8")
    ).hexdigest()
    plan = _build_terminal_finalization_plan(
        user_id,
        client_id,
        current_thread_id,
        source_row=source_row,
        divider_row=divider_row,
    )
    if row_already_nonviable:
        response_scenario = "none"
        response_body = None
    else:
        if terminal_reason == "requirements_mismatch":
            response_scenario = (
                "requirements_mismatch_with_alternative"
                if has_alternative_path
                else "requirements_mismatch"
            )
        else:
            response_scenario = (
                "nonviable_with_alternative"
                if has_alternative_path
                else "nonviable"
            )
        response_body = _select_automatic_response_body(
            response_scenario,
            _align_response_greeting(llm_response_email, contact_name),
            column_config,
            contact_name,
        )

    immutable = {
        "version": TERMINAL_SAGA_VERSION,
        "settlementOrdinal": settlement_ordinal,
        "sagaKey": saga_key,
        "sourceMessageKey": source_message_key,
        "sourceGraphMessageId": source_graph_message_id,
        "sourceInternetMessageId": internet_message_id,
        "sourceConversationId": conversation_id,
        "sourceReceivedAt": message.get("receivedDateTime"),
        "reason": terminal_reason,
        "note": terminal_note,
        "eventKey": event_key,
        "sourceRow": source_row,
        "rowAnchor": row_anchor,
        "responseScenario": response_scenario,
        "responseBody": response_body,
        "completeClientAfterReply": (
            response_scenario in {"nonviable", "requirements_mismatch"}
            or (response_scenario == "none" and not has_alternative_path)
        ),
        "replyRecipient": reply_recipient,
        "notificationRequired": not row_already_nonviable,
        "eventAddress": _event_text(event, "address"),
        "eventCity": _event_text(event, "city"),
        "clientId": client_id,
        "sheetId": sheet_id,
        "tabTitle": tab_title,
        "notesColumnIndex": notes_column_index,
        "notesColumnHeader": notes_column_header,
        "sheetHeaderFingerprint": _terminal_sheet_header_fingerprint(
            sheet_header
        ),
        "dividerExists": bool(divider_exists),
        "finalizationPlan": plan,
    }
    immutable_hash = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return {
        **immutable,
        "immutableHash": immutable_hash,
        "phase": "staged",
    }


def _resume_exact_terminal_saga(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    thread_data: Dict[str, Any],
    saga: Dict[str, Any],
) -> None:
    """Advance only persisted terminal evidence; skip the generic inbox pipeline."""
    _validate_terminal_saga_immutable_hash(saga)
    source_row, expected_final_row, mutation_kind = (
        _terminal_sheet_mutation_geometry_from_saga(saga)
    )
    # Validate the immutable coordinate commitment before claim/fence churn or
    # any Sheets client/read.  Legacy missing bindings are operator-repair only.
    _validate_terminal_saga_sheet_layout_binding(saga)
    client_id = saga.get("clientId") or (thread_data or {}).get("clientId")
    if not client_id or (
        saga.get("clientId")
        and (thread_data or {}).get("clientId")
        and saga.get("clientId") != (thread_data or {}).get("clientId")
    ):
        raise RetryableProcessingError("terminal saga client context drift")
    sheet_id = saga.get("sheetId")
    tab_title = saga.get("tabTitle")
    if not sheet_id or not tab_title:
        raise RetryableProcessingError("terminal saga Sheet context is incomplete")

    owner = _claim_existing_terminal_saga_execution(
        user_id,
        thread_id,
        thread_data,
        saga,
    )
    try:
        sheets = _sheets_client()
        live_tab_title = _get_first_tab_title(sheets, sheet_id)
        if live_tab_title != tab_title:
            raise RetryableProcessingError(
                "terminal saga tab context drift: "
                f"expected {tab_title!r}, found {live_tab_title!r}"
            )
        header = _read_header_row2(sheets, sheet_id, tab_title)
        live_notes_column_index = find_notes_comment_column_index(header)
        persisted_notes_column_index = saga.get("notesColumnIndex")
        if live_notes_column_index != persisted_notes_column_index:
            raise RetryableProcessingError(
                "terminal saga notes-column context drift: "
                f"expected {persisted_notes_column_index}, "
                f"found {live_notes_column_index}"
            )
        notes_column_index = _validate_terminal_saga_sheet_layout(saga, header)

        rownum, rowvals = _find_row_by_anchor(
            user_id,
            thread_id,
            sheets,
            sheet_id,
            tab_title,
            header,
            saga.get("replyRecipient") or "",
        )
        if rownum is None:
            raise RetryableProcessingError(
                "terminal saga could not locate its persisted Sheet row"
            )
        live_anchor = get_row_anchor(rowvals or [], header)
        if (
            saga.get("rowAnchor")
            and live_anchor
            and _normalize_replacement_match_text(live_anchor)
            != _normalize_replacement_match_text(saga.get("rowAnchor"))
        ):
            raise RetryableProcessingError("terminal saga row-anchor context drift")

        if rownum not in {source_row, expected_final_row}:
            raise RetryableProcessingError(
                "terminal saga row context drift: "
                f"expected {source_row} or {expected_final_row}, found {rownum}"
            )

        phase = saga.get("phase")
        if phase == "staged":
            allow_provider_mutation = rownum == source_row
            if mutation_kind == "move_with_note" and allow_provider_mutation:
                _verify_terminal_finalization_plan(user_id, client_id, saga)
            final_row = _execute_or_reconcile_terminal_sheet_mutation(
                user_id,
                thread_id,
                sheets,
                sheet_id,
                tab_title,
                header,
                notes_column_index,
                saga,
                owner,
                mutation_kind,
                allow_provider_mutation=allow_provider_mutation,
            )
            saga = _finalize_terminal_thread_roots(
                user_id,
                client_id,
                thread_id,
                saga,
                final_row=final_row,
                terminal_saga_owner=owner,
            )
        elif phase == "finalized":
            final_row = expected_final_row
            _execute_or_reconcile_terminal_sheet_mutation(
                user_id,
                thread_id,
                sheets,
                sheet_id,
                tab_title,
                header,
                notes_column_index,
                saga,
                owner,
                mutation_kind,
                allow_provider_mutation=False,
            )
        else:
            raise RetryableProcessingError(
                f"unsupported terminal saga phase: {phase}"
            )

        recipient = saga.get("replyRecipient") or ""
        _settle_terminal_notification_obligation(
            user_id,
            client_id,
            thread_id,
            recipient,
            saga,
            terminal_saga_owner=owner,
        )
        reply_outcome = _settle_terminal_reply_obligation(
            user_id,
            client_id,
            thread_id,
            headers,
            recipient,
            saga,
            terminal_saga_owner=owner,
        )
        print(f"📧 Terminal saga recovery reached durable outcome: {reply_outcome}")
        if saga.get("completeClientAfterReply"):
            _require_terminal_client_completion(user_id, client_id)
    except Exception:
        current_ref = (
            _fs.collection("users").document(user_id).collection("threads")
            .document(thread_id)
        )
        current_snapshot = current_ref.get()
        current_data = current_snapshot.to_dict() if current_snapshot.exists else {}
        attempt = current_data.get("terminalReplyAttempt")
        if not (
            isinstance(attempt, dict)
            and attempt.get("sagaKey") == saga.get("sagaKey")
        ):
            _release_terminal_saga_execution_claim(user_id, saga, owner)
        raise


_PROPERTY_ANCHOR_STOPWORDS = {
    "adjacent", "building", "built", "city", "development", "location",
    "new", "newly", "park", "property", "tbd", "the", "to", "town",
}


def _attachment_source_text(attachment: Dict[str, Any]) -> str:
    return "\n".join((
        str((attachment or {}).get("name") or ""),
        str((attachment or {}).get("text") or ""),
    ))


def _attachment_matches_event_property(
    attachment: Dict[str, Any],
    event: Dict[str, Any],
) -> bool:
    event_anchor = ", ".join(
        value for value in (
            _event_text(event, "address"),
            _event_text(event, "city"),
        ) if value
    )
    source = _attachment_source_text(attachment)
    if event_anchor and _source_mentions_target_property(source, event_anchor):
        return True

    anchor_tokens = {
        token for token in re.findall(r"[a-z0-9]+", event_anchor.lower())
        if len(token) >= 3 and token not in _PROPERTY_ANCHOR_STOPWORDS
    }
    source_tokens = set(re.findall(r"[a-z0-9]+", source.lower()))
    return len(anchor_tokens & source_tokens) >= 2


def _attachment_event_match_score(
    attachment: Dict[str, Any],
    event: Dict[str, Any],
) -> int:
    """Score address evidence without letting a shared city decide ownership."""
    source = _attachment_source_text(attachment)
    address = _event_text(event, "address")
    if not address:
        return 0
    if _target_street_identity(address) and _source_mentions_target_property(source, address):
        return 1000

    address_tokens = [
        token for token in re.findall(r"[a-z0-9]+", address.lower())
        if token != "tbd" and token not in _PROPERTY_ANCHOR_STOPWORDS
    ]
    if len(address_tokens) < 2:
        return 0
    source_token_list = re.findall(r"[a-z0-9]+", source.lower())
    if any(
        source_token_list[index:index + len(address_tokens)] == address_tokens
        for index in range(len(source_token_list) - len(address_tokens) + 1)
    ):
        return 500 + len(address_tokens)
    source_tokens = set(source_token_list)
    matched = sum(token in source_tokens for token in address_tokens)
    ratio = matched / len(address_tokens)
    if matched < 2 or ratio < 0.6:
        return 0
    return int(ratio * 100) + matched


def _partition_property_attachments(
    pdf_manifest: List[Dict[str, Any]],
    *,
    current_anchor: str,
    events: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
    """Partition assets between the current row and pending replacement rows."""
    new_property_events = [
        event for event in (events or [])
        if (event or {}).get("type") == "new_property"
    ]
    if not new_property_events:
        if any(
            (event or {}).get("type") == "needs_user_input"
            and (event or {}).get("reason") == "multi_property_attachment"
            for event in (events or [])
        ):
            return [
                attachment for attachment in (pdf_manifest or [])
                if _source_mentions_target_property(
                    _attachment_source_text(attachment),
                    current_anchor,
                )
            ], []
        return list(pdf_manifest or []), []

    current_assets: List[Dict[str, Any]] = []
    event_assets: List[List[Dict[str, Any]]] = [[] for _ in new_property_events]
    for attachment in (pdf_manifest or []):
        source = _attachment_source_text(attachment)
        current_match = _source_mentions_target_property(source, current_anchor)
        scores = [
            _attachment_event_match_score(attachment, event)
            for event in new_property_events
        ]
        best_score = max(scores, default=0)
        best_indexes = [
            index for index, score in enumerate(scores)
            if score == best_score and score > 0
        ]

        # A brochure that names both the established row and an alternate stays
        # attached to the message for review; it must not seed either row.
        if current_match and best_indexes:
            continue
        if current_match:
            current_assets.append(attachment)
            continue
        if len(best_indexes) == 1:
            event_assets[best_indexes[0]].append(attachment)

    return current_assets, event_assets


def _categorize_property_asset_links(
    pdf_manifest: List[Dict[str, Any]],
) -> tuple[List[str], List[str]]:
    flyer_links: List[str] = []
    floorplan_links: List[str] = []
    for attachment in (pdf_manifest or []):
        link = attachment.get("drive_link")
        if not link:
            continue
        if is_floorplan_filename(attachment.get("name", "")):
            floorplan_links.append(link)
        else:
            flyer_links.append(link)
    return flyer_links, floorplan_links


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


_NON_PERSON_CONTACT_TOKENS = frozenset({
    "asset", "broker", "brokerage", "colliers", "commercial", "company",
    "corp", "corporation", "cushman", "director", "group", "holdings", "inc",
    "international", "leasing", "llc", "management", "manager", "managing",
    "office", "owner", "partners", "principal", "properties", "property",
    "realty", "services", "team", "wakefield",
})


def _safe_reply_greeting_first_name(contact_name: Optional[str]) -> Optional[str]:
    candidate = (contact_name or "").strip()
    if not candidate or "," in candidate or "@" in candidate:
        return None
    candidate_parts = candidate.split()
    if len(candidate_parts) > 2:
        return None
    tokens = [re.sub(r"[^a-z]", "", token.lower()) for token in candidate_parts]
    if any(token in _NON_PERSON_CONTACT_TOKENS for token in tokens):
        return None
    first_name = candidate_parts[0]
    if len(first_name) > 1 and first_name.isupper():
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z.'-]{0,63}", first_name):
        return None
    return first_name


def _build_greeting(contact_name: Optional[str]) -> str:
    """Build a personalized greeting using the contact's first name, or generic 'Hi,' if no name."""
    first_name = _safe_reply_greeting_first_name(contact_name)
    if first_name:
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
    """Align named or neutral model greetings with the resolved reply identity."""
    if not response_body:
        return response_body

    expected = _build_greeting(contact_name)
    greeting_re = re.compile(
        r"^(\s*)(?:(?:hi|hello|hey)"
        r"(?:\s+[a-z][a-z'’.-]*(?:\s+[a-z][a-z'’.-]*)?)?"
        r"|(?:thanks|thank you)(?:\s+[a-z][a-z'’.-]*)?)"
        r"\s*(?:,|[-–—])"
        r"(?=\s*(?:\r?\n|$))",
        re.IGNORECASE,
    )
    return greeting_re.sub(lambda match: f"{match.group(1)}{expected}", response_body, count=1)


def _should_defer_client_completion_for_closing_reply(proposal: Optional[Dict[str, Any]]) -> bool:
    """Keep the campaign live until a prepared terminal reply reaches a send outcome."""
    proposal = proposal or {}
    response_email = proposal.get("response_email")
    return bool(str(response_email or "").strip()) and not bool(proposal.get("skip_response"))


def _mark_reply_sent_but_unindexed(reason: str) -> bool:
    _set_reply_send_outcome(
        error=reason,
        sent_but_unindexed=True,
        outcome="sent_but_unindexed",
    )
    print(f"   ⚠️ SENT-BUT-UNINDEXED: {reason}")
    return False


def _mark_reply_accepted_unconfirmed(reason: str) -> bool:
    _set_reply_send_outcome(
        error=reason,
        sent_but_unindexed=False,
        outcome="accepted_needs_reconciliation",
        exact_sent_evidence=None,
    )
    print(f"   ⚠️ ACCEPTED-BUT-UNCONFIRMED: {reason}")
    return False


def _mark_draft_mutation_needs_reconciliation(reason: str) -> bool:
    _set_reply_send_outcome(
        error=reason,
        sent_but_unindexed=False,
        outcome="draft_mutation_needs_reconciliation",
    )
    print(f"   ⚠️ DRAFT-RECONCILIATION: {reason}")
    return False


def _persist_exact_graph_completion(completer, *args, **kwargs):
    """Retry only the same local evidence write after an ambiguous commit."""
    try:
        return completer(*args, **kwargs)
    except Exception as first_error:
        try:
            return completer(*args, **kwargs)
        except Exception as readback_error:
            raise readback_error from first_error


def _retain_graph_reply_draft_for_review(
    graph_send_capability,
    draft_id: str,
    *,
    reason: str,
    phase: str,
) -> bool:
    """Retain one capability-owned draft; Graph DELETE has no CAS precondition."""
    normalized_draft_id = str(draft_id or "").strip()
    if not normalized_draft_id:
        raise GraphSendPermitBlocked(
            "capability-owned Graph draft review is missing its draft id"
        )
    evidence = {
        "reason": str(reason or "").strip(),
        "phase": str(phase or "").strip(),
        "draftId": normalized_draft_id,
        "providerSendStarted": False,
        "automaticDeleteAttempted": False,
    }
    _persist_exact_graph_completion(
        resolve_graph_send_permit,
        graph_send_capability,
        "needs_reconciliation",
        evidence=evidence,
    )
    return _mark_draft_mutation_needs_reconciliation(evidence["reason"])


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


def send_reply_in_thread(
    user_id: str,
    headers: dict,
    body: str,
    current_msg_id: str,
    recipient: str,
    thread_id: str,
    *,
    graph_send_capability=None,
) -> bool:
    """Send a reply to the current message being processed and index it for future replies"""
    _reset_reply_send_outcome()
    outbound_mode = resolve_outbound_mode()
    if outbound_mode != OUTBOUND_MODE_LIVE:
        reason = (
            "suppressed_by_kill_switch "
            f"(SITESIFT_OUTBOUND_MODE={outbound_mode})"
        )
        _set_reply_send_outcome(
            error=reason,
            outcome="suppressed_by_kill_switch",
        )
        _kill_switch_suppressed(
            outbound_mode,
            context=f"send_reply_in_thread thread {thread_id}",
        )
        if graph_send_capability is not None:
            resolve_graph_send_permit(
                graph_send_capability,
                "definitely_not_sent",
                evidence={"reason": reason, "phase": "preflight"},
            )
        return False
    body_validation = validate_outbound_body(body)
    if not body_validation.is_safe:
        _set_reply_send_outcome(
            error=f"{body_validation.reason}; manual review required before auto-reply",
            outcome="blocked_unsafe_body",
        )
        print(f"   🛑 Blocked unsafe auto-reply body: {body_validation.reason}")
        if graph_send_capability is not None:
            resolve_graph_send_permit(
                graph_send_capability,
                "definitely_not_sent",
                evidence={
                    "reason": body_validation.reason,
                    "phase": "body_validation",
                },
            )
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
        if graph_send_capability is not None:
            resolve_graph_send_permit(
                graph_send_capability,
                "definitely_not_sent",
                evidence={"reason": "user_not_allowlisted", "phase": "preflight"},
            )
        return False
    provider_mutation_phase = None
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
            if graph_send_capability is not None:
                resolve_graph_send_permit(
                    graph_send_capability,
                    "definitely_not_sent",
                    evidence={"reason": decision.reason, "phase": "campaign_gate"},
                )
            return False

        base = "https://graph.microsoft.com/v1.0"
        graph_headers = graph_headers_with_immutable_id(headers)
        current_meta = {}

        try:
            current_meta_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages/{_graph_message_path_segment(current_msg_id)}",
                    headers=graph_headers,
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

        # Freeze and validate the complete attachment plan before the first
        # provider mutation. Its size determines the retained permit history
        # budget, and the same list is reused through PATCH, attachment POSTs,
        # /send, and exact Sent reconciliation.
        signature_attachments = []
        if needs_signature_attachments(
            signature_mode,
            user_signature,
            user_email=user_email,
        ):
            signature_attachments = get_signature_attachments(
                user_signature,
                signature_mode,
                user_email=user_email,
            )
        planned_attachment_count = validate_graph_draft_attachment_plan(
            signature_attachments
        )

        # Track if reply was sent successfully
        reply_sent_successfully = False
        reply_sent_after = None

        if graph_send_capability is not None:
            create_timeout = begin_graph_draft_creation(
                graph_send_capability,
                current_msg_id,
                planned_attachment_count=planned_attachment_count,
            )
            provider_mutation_phase = "create_reply"
            try:
                create_reply_resp = requests.post(
                    f"{base}/me/messages/{_graph_message_path_segment(current_msg_id)}/createReplyAll",
                    headers=graph_headers,
                    timeout=create_timeout,
                )
            except Exception as exc:
                _persist_exact_graph_completion(
                    complete_graph_draft_creation,
                    graph_send_capability,
                    outcome="needs_reconciliation",
                    evidence={"reason": str(exc)[:1500], "phase": "create_reply"},
                )
                return _mark_draft_mutation_needs_reconciliation(str(exc))
        else:
            provider_mutation_phase = "create_reply"
            create_reply_resp = exponential_backoff_request(
                lambda: requests.post(
                    f"{base}/me/messages/{_graph_message_path_segment(current_msg_id)}/createReplyAll",
                    headers=graph_headers,
                    timeout=30,
                ),
                max_retries=GRAPH_SEND_MAX_RETRIES,
            )
        if not create_reply_resp or create_reply_resp.status_code not in [200, 201]:
            failure_reason = f"createReplyAll failed: {create_reply_resp.status_code if create_reply_resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            if graph_send_capability is not None:
                _persist_exact_graph_completion(
                    complete_graph_draft_creation,
                    graph_send_capability,
                    outcome="needs_reconciliation",
                    evidence={"reason": failure_reason, "phase": "create_reply"},
                )
                return _mark_draft_mutation_needs_reconciliation(failure_reason)
            print(f"   ❌ {failure_reason}")
            return False

        reply_draft = create_reply_resp.json() or {}
        reply_draft_id = reply_draft.get("id")
        if not reply_draft_id:
            _set_reply_send_outcome(
                error="createReplyAll returned no draft id",
                outcome="send_failed",
            )
            if graph_send_capability is not None:
                _persist_exact_graph_completion(
                    complete_graph_draft_creation,
                    graph_send_capability,
                    outcome="needs_reconciliation",
                    evidence={
                        "reason": "createReplyAll returned no draft id",
                        "phase": "create_reply",
                    },
                )
                return _mark_draft_mutation_needs_reconciliation(
                    "createReplyAll returned no draft id"
                )
            print("   ❌ createReplyAll returned no draft id")
            return False
        if graph_send_capability is not None:
            _persist_exact_graph_completion(
                complete_graph_draft_creation,
                graph_send_capability,
                draft_id=reply_draft_id,
                outcome="created",
                evidence={
                    "httpStatus": getattr(create_reply_resp, "status_code", None),
                    "phase": "create_reply",
                    "draftId": reply_draft_id,
                },
            )

        reply_draft = _hydrate_reply_all_draft_recipients(
            graph_headers,
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
        reply_subject = reply_draft.get("subject")
        if not isinstance(reply_subject, str) or not reply_subject.strip():
            failure_reason = (
                "createReplyAll draft has no exact provider-inherited subject"
            )
            _set_reply_send_outcome(
                error=failure_reason,
                outcome="send_failed",
            )
            if graph_send_capability is not None:
                return _retain_graph_reply_draft_for_review(
                    graph_send_capability,
                    reply_draft_id,
                    reason=failure_reason,
                    phase="draft_subject",
                )
            _delete_graph_reply_draft(
                graph_headers,
                reply_draft_id,
                base=base,
            )
            return False
        _set_reply_send_outcome(subject=reply_subject)

        recipient_result = _filter_reply_all_draft_recipients(
            user_id,
            reply_draft,
            user_email=user_email,
        )
        recipient_payload = recipient_result["payload"]
        if not (recipient_payload["toRecipients"] or recipient_payload["ccRecipients"]):
            opted_out = (recipient_result.get("skipped") or {}).get("optedOut") or []
            if opted_out:
                _set_reply_send_outcome(
                    error="All safe reply-all recipients opted out",
                    outcome="suppressed_recipient_optout",
                )
                print("   ⏭️ Reply suppressed because all safe recipients opted out")
                if graph_send_capability is not None:
                    return _retain_graph_reply_draft_for_review(
                        graph_send_capability,
                        reply_draft_id,
                        reason="recipient_optout",
                        phase="draft",
                    )
                _delete_graph_reply_draft(
                    graph_headers, reply_draft_id, base=base
                )
                return False
            _set_reply_send_outcome(
                error="No safe reply-all recipients remained after filtering",
                outcome="send_failed",
            )
            print("   ❌ No safe reply-all recipients remained after filtering")
            if graph_send_capability is not None:
                return _retain_graph_reply_draft_for_review(
                    graph_send_capability,
                    reply_draft_id,
                    reason="no_safe_recipients",
                    phase="draft",
                )
            _delete_graph_reply_draft(graph_headers, reply_draft_id, base=base)
            return False

        patch_payload = {
            "subject": reply_subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": recipient_payload["toRecipients"],
            "ccRecipients": recipient_payload["ccRecipients"],
        }
        prepared_graph_envelope = None
        if graph_send_capability is not None:
            prepared_graph_envelope = begin_graph_draft_patch(
                graph_send_capability,
                source_graph_message_id=current_msg_id,
                draft_id=reply_draft_id,
                subject=reply_subject,
                html_body=html_body,
                to_recipients=recipient_payload["toRecipients"],
                cc_recipients=recipient_payload["ccRecipients"],
                attachments=signature_attachments,
            )
            provider_mutation_phase = "patch_draft"
            try:
                patch_resp = requests.patch(
                    f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}",
                    headers=graph_headers,
                    json=patch_payload,
                    timeout=prepared_graph_envelope["timeoutSeconds"],
                )
            except Exception as exc:
                _persist_exact_graph_completion(
                    complete_graph_draft_patch,
                    graph_send_capability,
                    prepared_envelope_hash=prepared_graph_envelope[
                        "preparedEnvelopeHash"
                    ],
                    outcome="needs_reconciliation",
                    evidence={"reason": str(exc)[:1500], "phase": "patch_draft"},
                )
                return _mark_draft_mutation_needs_reconciliation(str(exc))
        else:
            provider_mutation_phase = "patch_draft"
            patch_resp = exponential_backoff_request(
                lambda: requests.patch(
                    f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}",
                    headers=graph_headers,
                    json=patch_payload,
                    timeout=30
                ),
                max_retries=GRAPH_SEND_MAX_RETRIES,
            )
        if not patch_resp or patch_resp.status_code not in [200, 202, 204]:
            failure_reason = f"Reply-all draft patch failed: {patch_resp.status_code if patch_resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            if graph_send_capability is not None:
                _persist_exact_graph_completion(
                    complete_graph_draft_patch,
                    graph_send_capability,
                    prepared_envelope_hash=prepared_graph_envelope[
                        "preparedEnvelopeHash"
                    ],
                    outcome="needs_reconciliation",
                    evidence={"reason": failure_reason, "phase": "patch_draft"},
                )
                return _mark_draft_mutation_needs_reconciliation(failure_reason)
            print(f"   ❌ {failure_reason}")
            return False
        if graph_send_capability is not None:
            _persist_exact_graph_completion(
                complete_graph_draft_patch,
                graph_send_capability,
                prepared_envelope_hash=prepared_graph_envelope[
                    "preparedEnvelopeHash"
                ],
                outcome="applied",
                evidence={
                    "httpStatus": getattr(patch_resp, "status_code", None),
                    "phase": "patch_draft",
                    "draftId": reply_draft_id,
                    "preparedEnvelopeHash": prepared_graph_envelope[
                        "preparedEnvelopeHash"
                    ],
                },
            )

        for attachment_index, attachment in enumerate(signature_attachments):
            try:
                if graph_send_capability is not None:
                    attachment_operation = begin_graph_draft_attachment(
                        graph_send_capability,
                        prepared_envelope_hash=prepared_graph_envelope[
                            "preparedEnvelopeHash"
                        ],
                        attachment_index=attachment_index,
                        attachment=attachment,
                    )
                    provider_mutation_phase = "attach_draft"
                    att_resp = requests.post(
                        f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}/attachments",
                        headers=graph_headers,
                        json=attachment,
                        timeout=attachment_operation["timeoutSeconds"],
                    )
                else:
                    provider_mutation_phase = "attach_draft"
                    att_resp = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}/attachments",
                            headers=graph_headers,
                            json=att,
                            timeout=30
                        ),
                        max_retries=GRAPH_SEND_MAX_RETRIES,
                    )
                if att_resp.status_code in [200, 201]:
                    if graph_send_capability is not None:
                        attachment_response = att_resp.json() or {}
                        provider_attachment_id = str(
                            attachment_response.get("id") or ""
                        ).strip()
                        if not provider_attachment_id:
                            failure_reason = (
                                "Reply-all attachment success returned no "
                                "provider attachment id"
                            )
                            _persist_exact_graph_completion(
                                complete_graph_draft_attachment,
                                graph_send_capability,
                                prepared_envelope_hash=prepared_graph_envelope[
                                    "preparedEnvelopeHash"
                                ],
                                attachment_index=attachment_index,
                                outcome="needs_reconciliation",
                                evidence={
                                    "reason": failure_reason,
                                    "phase": "attach_draft",
                                },
                            )
                            return _mark_draft_mutation_needs_reconciliation(
                                failure_reason
                            )
                        _persist_exact_graph_completion(
                            complete_graph_draft_attachment,
                            graph_send_capability,
                            prepared_envelope_hash=prepared_graph_envelope[
                                "preparedEnvelopeHash"
                            ],
                            attachment_index=attachment_index,
                            outcome="applied",
                            evidence={
                                "httpStatus": att_resp.status_code,
                                "phase": "attach_draft",
                                "draftId": attachment_operation["draftId"],
                                "attachmentIndex": attachment_operation[
                                    "attachmentIndex"
                                ],
                                "attachmentHash": attachment_operation[
                                    "attachmentHash"
                                ],
                                "providerAttachmentId": provider_attachment_id,
                            },
                        )
                    print(f"   📎 Attached {attachment['name']}")
                elif graph_send_capability is not None:
                    failure_reason = (
                        "Reply-all attachment failed: "
                        f"{getattr(att_resp, 'status_code', None)}"
                    )
                    _persist_exact_graph_completion(
                        complete_graph_draft_attachment,
                        graph_send_capability,
                        prepared_envelope_hash=prepared_graph_envelope[
                            "preparedEnvelopeHash"
                        ],
                        attachment_index=attachment_index,
                        outcome="needs_reconciliation",
                        evidence={
                            "reason": failure_reason,
                            "phase": "attach_draft",
                        },
                    )
                    return _mark_draft_mutation_needs_reconciliation(
                        failure_reason
                    )
            except GraphSendPermitLocalRetryable:
                # The attachment request has not started when begin_* reports
                # an exact local commit failure.  Preserve that distinction
                # for the outer permit resolver instead of recording an
                # unknown provider mutation.
                raise
            except Exception as e:
                if graph_send_capability is not None:
                    try:
                        _persist_exact_graph_completion(
                            complete_graph_draft_attachment,
                            graph_send_capability,
                            prepared_envelope_hash=prepared_graph_envelope[
                                "preparedEnvelopeHash"
                            ],
                            attachment_index=attachment_index,
                            outcome="needs_reconciliation",
                            evidence={
                                "reason": str(e)[:1500],
                                "phase": "attach_draft",
                            },
                        )
                    except Exception:
                        # Propagate to the outer exact-permit resolver.  Returning
                        # here could strand an issued parent after begin_* failed.
                        raise
                    return _mark_draft_mutation_needs_reconciliation(str(e))
                print(f"   ⚠️ Error attaching {attachment['name']}: {e}")

        if graph_send_capability is not None:
            finalize_graph_draft_preparation(
                graph_send_capability,
                prepared_envelope_hash=prepared_graph_envelope[
                    "preparedEnvelopeHash"
                ],
            )

        decision = get_client_automation_decision(user_id, client_id)
        if decision.denies_autonomous_work:
            _set_reply_campaign_suppression(decision)
            if graph_send_capability is not None:
                print(f"   🛑 {_get_reply_send_outcome().error}")
                return _retain_graph_reply_draft_for_review(
                    graph_send_capability,
                    reply_draft_id,
                    reason=decision.reason,
                    phase="final_campaign_gate",
                )
            _delete_graph_reply_draft(graph_headers, reply_draft_id, base=base)
            print(f"   🛑 {_get_reply_send_outcome().error}")
            return False

        outbound_mode = resolve_outbound_mode()
        if outbound_mode != OUTBOUND_MODE_LIVE:
            reason = (
                "suppressed_by_kill_switch "
                f"(SITESIFT_OUTBOUND_MODE={outbound_mode})"
            )
            _set_reply_send_outcome(
                error=reason,
                outcome="suppressed_by_kill_switch",
            )
            _kill_switch_suppressed(
                outbound_mode,
                context=f"send_reply_in_thread thread {thread_id} at Graph send",
            )
            if graph_send_capability is not None:
                return _retain_graph_reply_draft_for_review(
                    graph_send_capability,
                    reply_draft_id,
                    reason=reason,
                    phase="final_kill_switch",
                )
            _delete_graph_reply_draft(graph_headers, reply_draft_id, base=base)
            return False

        reply_sent_after = datetime.now(timezone.utc) - timedelta(seconds=3)
        _set_reply_send_outcome(send_attempt_at=reply_sent_after)
        if graph_send_capability is not None:
            send_timeout = consume_graph_send_capability(
                graph_send_capability,
                source_graph_message_id=current_msg_id,
                draft_id=reply_draft_id,
                subject=reply_subject,
                html_body=html_body,
                to_recipients=recipient_payload["toRecipients"],
                cc_recipients=recipient_payload["ccRecipients"],
                attachments=signature_attachments,
            )
            provider_mutation_phase = "send"
            try:
                resp = requests.post(
                    f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}/send",
                    headers=graph_headers,
                    timeout=send_timeout,
                )
            except Exception as exc:
                resolve_graph_send_permit(
                    graph_send_capability,
                    "needs_reconciliation",
                    evidence={"reason": str(exc)[:1500], "phase": "send"},
                )
                _set_reply_send_outcome(
                    error=str(exc),
                    outcome="graph_permit_needs_reconciliation",
                    sent_but_unindexed=True,
                )
                raise
            permit_status = (
                "accepted"
                if resp and resp.status_code in [200, 202]
                else "needs_reconciliation"
            )
            resolve_graph_send_permit(
                graph_send_capability,
                permit_status,
                evidence={
                    "httpStatus": getattr(resp, "status_code", None),
                    "phase": "send",
                },
            )
        else:
            resp = exponential_backoff_request(
                lambda: requests.post(
                    f"{base}/me/messages/{_graph_message_path_segment(reply_draft_id)}/send",
                    headers=graph_headers,
                    timeout=30,
                ),
                max_retries=1,
                operation="graph_send",
            )
        reply_sent_successfully = resp and resp.status_code in [200, 202]
        if reply_sent_successfully:
            print(f"   ✅ Sent reply via createReplyAll draft")

        if not reply_sent_successfully:
            failure_reason = f"Reply-all draft send failed: {resp.status_code if resp else 'no response'}"
            _set_reply_send_outcome(error=failure_reason, outcome="send_failed")
            if graph_send_capability is not None:
                _set_reply_send_outcome(
                    error=failure_reason,
                    outcome="graph_permit_needs_reconciliation",
                    sent_but_unindexed=True,
                )
            print(f"   ❌ {failure_reason}")
            return False

        # Reply was accepted.  The immutable draft ID names the same provider
        # object after Graph moves it to Sent Items, so only that exact object
        # may be indexed or used as successful-send evidence.
        try:
            time.sleep(1)
            expected_conversation_id = current_meta.get("conversationId")
            sent_msg = find_exact_sent_message_by_immutable_id(
                graph_headers,
                reply_draft_id,
                recipient=recipient,
                to_recipients=recipient_payload["toRecipients"],
                cc_recipients=recipient_payload["ccRecipients"],
                require_no_bcc=True,
                require_attachment_proof=True,
                canonical_body_hash=(prepared_graph_envelope or {}).get(
                    "htmlBodyHash"
                ),
                subject=(
                    (prepared_graph_envelope or {}).get("subject")
                    or reply_subject
                ),
                conversation_id=expected_conversation_id,
                base=base,
                attempts=4,
            )
            if not sent_msg:
                return _mark_reply_accepted_unconfirmed(
                    "Exact immutable Sent message is not yet readable; "
                    "provider acceptance remains reconciliation-only"
                )
            exact_sent_evidence = {
                **dict(sent_msg),
                "sentMessageId": sent_msg.get("id"),
                "recipient": str(recipient or "").strip().lower(),
                "bodyHash": hashlib.sha256(
                    str(body or "").encode("utf-8")
                ).hexdigest(),
                "conversationId": sent_msg.get("conversationId"),
                "permitId": (
                    graph_send_capability.permit_id
                    if graph_send_capability is not None
                    else None
                ),
                "sourceGraphMessageId": current_msg_id,
                "preparedEnvelopeHash": (
                    (prepared_graph_envelope or {}).get(
                        "preparedEnvelopeHash"
                    )
                ),
            }
            _set_reply_send_outcome(
                exact_sent_evidence=exact_sent_evidence,
            )
            conversation_id = (
                sent_msg.get("conversationId") or expected_conversation_id
            )
            if conversation_id:
                _set_reply_send_outcome(conversation_id=conversation_id)
            else:
                return _mark_reply_sent_but_unindexed("Could not get conversationId to index sent message")

            sent_internet_msg_id = sent_msg.get("internetMessageId")
            if not sent_internet_msg_id:
                return _mark_reply_sent_but_unindexed(
                    "Exact Sent message has no internetMessageId, cannot index"
                )
            normalized_id = normalize_message_id(sent_internet_msg_id)
            max_index_retries = 3
            msg_indexed = False
            for attempt in range(max_index_retries):
                if index_message_id(user_id, sent_internet_msg_id, thread_id):
                    time.sleep(0.2)
                    if (
                        lookup_thread_by_message_id(
                            user_id, sent_internet_msg_id
                        )
                        == thread_id
                    ):
                        msg_indexed = True
                        break
                print(
                    "   ⚠️ Reply index attempt "
                    f"{attempt + 1}/{max_index_retries} failed, retrying..."
                )
                time.sleep(0.5 * (attempt + 1))
            if not msg_indexed:
                error_msg = (
                    f"Failed to index reply after {max_index_retries} attempts"
                )
                print(
                    f"   ⚠️ CRITICAL: {error_msg} - future replies may be orphaned"
                )
                return _mark_reply_sent_but_unindexed(error_msg)

            to_recipients = [
                item.get("emailAddress", {}).get("address", "")
                for item in sent_msg.get("toRecipients", [])
            ]
            cc_recipients = [
                item.get("emailAddress", {}).get("address", "")
                for item in sent_msg.get("ccRecipients", [])
            ]
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
                    "inReplyTo": None,
                    "references": [],
                },
                "body": {
                    "contentType": body_obj.get("contentType", "HTML"),
                    "content": body_content,
                    "preview": (
                        sent_msg.get("bodyPreview", "")[:200]
                        or safe_preview(body_content)
                    ),
                },
            }
            save_message(user_id, thread_id, normalized_id, message_record)
            for attempt in range(max_index_retries):
                if index_conversation_id(user_id, conversation_id, thread_id):
                    break
                time.sleep(0.5 * (attempt + 1))
            print(
                "   📝 Indexed exact immutable sent reply message: "
                f"{sent_internet_msg_id[:50]}..."
            )
        except Exception as e:
            if _get_reply_send_outcome().exact_sent_evidence:
                return _mark_reply_sent_but_unindexed(
                    f"Failed to index exact sent reply: {e}"
                )
            return _mark_reply_accepted_unconfirmed(
                f"Exact immutable Sent confirmation failed closed: {e}"
            )

        _set_reply_send_outcome(outcome="sent_indexed")
        return True

    except GraphSendPermitLocalRetryable as e:
        if graph_send_capability is None:
            raise
        retained_permit = read_permit(graph_send_capability)
        preparation = dict(retained_permit.get("draftPreparation") or {})
        preparation_state = preparation.get("state")
        failure_reason = str(e)[:1500]
        if preparation_state is None:
            _persist_exact_graph_completion(
                resolve_graph_send_permit,
                graph_send_capability,
                "definitely_not_sent",
                evidence={
                    "reason": failure_reason,
                    "phase": "local_transition_commit",
                },
            )
            _set_reply_send_outcome(
                error=failure_reason,
                sent_but_unindexed=False,
                outcome="local_transition_definitely_not_started",
            )
            print(f"   ❌ Failed before any Graph provider mutation: {e}")
            return False
        if preparation_state in {
            "draft_created",
            "patch_applied",
            "attachment_applied",
            "prepared",
        }:
            evidence = {
                "reason": failure_reason,
                "phase": "local_transition_commit",
                "draftId": preparation.get("draftId"),
                "providerSendStarted": False,
                "automaticDeleteAttempted": False,
            }
            _persist_exact_graph_completion(
                resolve_graph_send_permit,
                graph_send_capability,
                "needs_reconciliation",
                evidence=evidence,
            )
            return _mark_draft_mutation_needs_reconciliation(
                evidence["reason"]
            )
        raise
    except Exception as e:
        send_request_may_have_started = provider_mutation_phase == "send"
        draft_mutation_may_have_started = provider_mutation_phase in {
            "create_reply",
            "patch_draft",
            "attach_draft",
        }
        if graph_send_capability is not None and not send_request_may_have_started:
            retained_permit = read_permit(graph_send_capability)
            preparation = dict(retained_permit.get("draftPreparation") or {})
            if (
                retained_permit.get("requestStartedAt") is None
                and preparation.get("state") in {
                    "draft_created",
                    "patch_applied",
                    "attachment_applied",
                    "prepared",
                }
            ):
                failure_reason = str(e)[:1500]
                evidence = {
                    "reason": failure_reason,
                    "phase": "pre_send_permit",
                    "draftId": preparation.get("draftId"),
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                }
                _persist_exact_graph_completion(
                    resolve_graph_send_permit,
                    graph_send_capability,
                    "needs_reconciliation",
                    evidence=evidence,
                )
                return _mark_draft_mutation_needs_reconciliation(
                    failure_reason
                )
        if graph_send_capability is not None:
            try:
                resolve_graph_send_permit(
                    graph_send_capability,
                    (
                        "needs_reconciliation"
                        if send_request_may_have_started
                        or draft_mutation_may_have_started
                        else "definitely_not_sent"
                    ),
                    evidence={
                        "reason": str(e)[:1500],
                        "phase": provider_mutation_phase or "preflight_exception",
                    },
                )
            except Exception:
                pass
        _set_reply_send_outcome(
            error=str(e),
            sent_but_unindexed=bool(
                graph_send_capability and send_request_may_have_started
            ),
            outcome=(
                "graph_permit_needs_reconciliation"
                if graph_send_capability and send_request_may_have_started
                else "draft_mutation_needs_reconciliation"
                if graph_send_capability and draft_mutation_may_have_started
                else "send_failed"
            ),
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


def _persist_inbound_message_history(
    user_id: str,
    thread_id: str,
    graph_message_id: str,
    internet_message_id: str,
    message_record: Dict[str, Any],
    thread_ref,
    source_envelope: Dict[str, Any],
    *,
    strict: bool,
) -> None:
    """Persist the exact inbound history/index/envelope without downstream effects."""
    max_retries = 3
    durable_message_id = internet_message_id or graph_message_id
    message_saved = False
    if durable_message_id:
        for attempt_number in range(max_retries):
            try:
                message_saved = bool(
                    save_message(
                        user_id,
                        thread_id,
                        durable_message_id,
                        message_record,
                    )
                )
            except Exception as exc:
                message_saved = False
                print(f"⚠️ Inbound message save raised: {exc}")
            if message_saved:
                break
            print(
                "⚠️ Inbound message save attempt "
                f"{attempt_number + 1}/{max_retries} failed, retrying..."
            )
            time.sleep(0.5 * (attempt_number + 1))
    if strict and not message_saved:
        raise RetryableProcessingError(
            "different-source inbound history save could not be verified"
        )

    if internet_message_id:
        message_indexed = False
        for attempt_number in range(max_retries):
            try:
                if index_message_id(
                    user_id,
                    internet_message_id,
                    thread_id,
                ):
                    time.sleep(0.2)
                    message_indexed = (
                        lookup_thread_by_message_id(
                            user_id,
                            internet_message_id,
                        )
                        == thread_id
                    )
                    if message_indexed:
                        break
            except Exception as exc:
                print(f"⚠️ Inbound message index verification raised: {exc}")
            print(
                "⚠️ Inbound message index attempt "
                f"{attempt_number + 1}/{max_retries} failed, retrying..."
            )
            time.sleep(0.5 * (attempt_number + 1))
        if not message_indexed:
            print(
                f"⚠️ Failed to index inbound message after {max_retries} attempts"
            )
            if strict:
                raise RetryableProcessingError(
                    "different-source inbound message index could not be verified"
                )

    try:
        update_payload = {"updatedAt": SERVER_TIMESTAMP}
        if source_envelope:
            update_payload["lastInboundEnvelope"] = source_envelope
        thread_ref.set(update_payload, merge=True)
        if strict:
            verification_snapshot = thread_ref.get()
            verification_data = (
                verification_snapshot.to_dict()
                if verification_snapshot.exists
                else {}
            )
            if "updatedAt" not in verification_data or (
                source_envelope
                and verification_data.get("lastInboundEnvelope") != source_envelope
            ):
                raise RetryableProcessingError(
                    "different-source inbound timestamp/envelope could not be verified"
                )
    except Exception as exc:
        if strict:
            if isinstance(exc, RetryableProcessingError):
                raise
            raise RetryableProcessingError(
                f"different-source inbound timestamp/envelope persistence failed: {exc}"
            ) from exc
        print(f"⚠️ Failed to update thread timestamp: {exc}")


def _source_processing_datetime(value: Any, *, field_name: str) -> datetime:
    if type(value) is not str or not value.strip():
        raise SourceCoordinatorConfigError(
            f"exact-source {field_name} must be an ISO datetime"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceCoordinatorConfigError(
            f"exact-source {field_name} is not an ISO datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceCoordinatorConfigError(
            f"exact-source {field_name} must be timezone-aware"
        )
    return parsed


def _persist_strict_source_history_and_index(
    *,
    user_id: str,
    thread_id: str,
    canonical_source_id: str,
    message_record: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Retain first-observed history while binding retries by semantic content."""
    incoming_message = copy.deepcopy(dict(message_record))
    semantic_message = copy.deepcopy(incoming_message)
    semantic_headers = semantic_message.get("headers")
    if isinstance(semantic_headers, dict):
        semantic_headers.pop("internetMessageId", None)
    semantic_envelope = semantic_message.get("sourceMessage")
    if isinstance(semantic_envelope, dict):
        semantic_envelope.pop("graphMessageId", None)
        semantic_envelope.pop("internetMessageId", None)
    history_material = {
        "schemaVersion": 1,
        "canonicalSourceId": canonical_source_id,
        "threadId": thread_id,
        "message": incoming_message,
    }
    history_hash = canonical_json_hash(history_material)
    semantic_history_hash = canonical_json_hash(
        {
            "schemaVersion": 1,
            "canonicalSourceId": canonical_source_id,
            "threadId": thread_id,
            "message": semantic_message,
        }
    )
    history_document = {
        **incoming_message,
        "canonicalSourceId": canonical_source_id,
        "historyHash": history_hash,
        "semanticHistoryHash": semantic_history_hash,
    }
    history_ref = (
        _fs.collection("users")
        .document(user_id)
        .collection("threads")
        .document(thread_id)
        .collection("messages")
        .document(canonical_source_id)
    )
    try:
        before = history_ref.get()
        before_data = before.to_dict() if before.exists else None
    except Exception as exc:
        raise SourceCoordinatorRetryable(
            "exact-source inbound history is unreadable"
        ) from exc
    retained_data = before_data
    if before_data is None:
        try:
            history_ref.create(copy.deepcopy(history_document))
            retained_data = history_document
        except Exception as create_error:
            try:
                readback = history_ref.get()
                readback_data = readback.to_dict() if readback.exists else None
            except Exception as readback_error:
                raise SourceCoordinatorAmbiguous(
                    "exact-source inbound history create outcome is unreadable"
                ) from readback_error
            if readback_data is None:
                raise SourceCoordinatorRetryable(
                    "exact-source inbound history create was not applied"
                ) from create_error
            retained_data = readback_data

    def validate_retained_history(data: Any) -> str:
        if type(data) is not dict:
            raise SourceCoordinatorAmbiguous(
                "exact-source inbound history is malformed"
            )
        retained = copy.deepcopy(data)
        retained_canonical_source_id = retained.pop(
            "canonicalSourceId",
            None,
        )
        retained_history_hash = retained.pop("historyHash", None)
        retained_semantic_hash = retained.pop("semanticHistoryHash", None)
        retained_semantic_message = copy.deepcopy(retained)
        retained_headers = retained_semantic_message.get("headers")
        if isinstance(retained_headers, dict):
            retained_headers.pop("internetMessageId", None)
        retained_envelope = retained_semantic_message.get("sourceMessage")
        if isinstance(retained_envelope, dict):
            retained_envelope.pop("graphMessageId", None)
            retained_envelope.pop("internetMessageId", None)
        expected_history_hash = canonical_json_hash(
            {
                "schemaVersion": 1,
                "canonicalSourceId": canonical_source_id,
                "threadId": thread_id,
                "message": retained,
            }
        )
        expected_semantic_hash = canonical_json_hash(
            {
                "schemaVersion": 1,
                "canonicalSourceId": canonical_source_id,
                "threadId": thread_id,
                "message": retained_semantic_message,
            }
        )
        if (
            retained_canonical_source_id != canonical_source_id
            or retained_history_hash != expected_history_hash
            or retained_semantic_hash != expected_semantic_hash
            or retained_semantic_hash != semantic_history_hash
        ):
            raise SourceCoordinatorAmbiguous(
                "exact-source inbound history conflicts with retained evidence"
            )
        return retained_history_hash

    retained_history_hash = validate_retained_history(retained_data)

    try:
        verified = history_ref.get()
        verified_data = verified.to_dict() if verified.exists else None
    except Exception as exc:
        raise SourceCoordinatorAmbiguous(
            "exact-source inbound history readback is unavailable"
        ) from exc
    verified_history_hash = validate_retained_history(verified_data)
    if verified_data != retained_data or verified_history_hash != retained_history_hash:
        raise SourceCoordinatorAmbiguous(
            "exact-source inbound history readback differs from authority"
        )

    saved_history_binding = {
        "schemaVersion": 1,
        "canonicalSourceId": canonical_source_id,
        "threadId": thread_id,
        "historyDocumentId": canonical_source_id,
        "historyHash": retained_history_hash,
    }
    index_binding = {
        "schemaVersion": 1,
        "canonicalSourceId": canonical_source_id,
        "threadId": thread_id,
        "identityDocumentId": canonical_source_id,
    }
    return saved_history_binding, index_binding


def _read_source_classification_input(
    *,
    canonical_source_id: str,
    thread_id: str,
    hydrated_message: Mapping[str, Any],
    message_text: str,
    internet_message_headers,
    local_source_disposition: str | None = None,
) -> Mapping[str, Any]:
    """Acquire the exact, provider-free input later fenced by classification."""
    from_info = hydrated_message.get("from", {}).get("emailAddress", {})
    stable_headers = []
    for header in internet_message_headers or []:
        if not isinstance(header, Mapping):
            stable_headers.append(copy.deepcopy(header))
            continue
        if str(header.get("name", "")).strip().lower() == "message-id":
            continue
        stable_headers.append(copy.deepcopy(dict(header)))
    classification_input = {
        "schemaVersion": 1,
        "canonicalSourceId": canonical_source_id,
        "message": {
            "threadId": thread_id,
            "subject": hydrated_message.get("subject", ""),
            "from": from_info.get("address", ""),
            "body": message_text,
            "hasAttachments": bool(hydrated_message.get("hasAttachments")),
            "internetMessageHeaders": stable_headers,
        },
    }
    if local_source_disposition is not None:
        if local_source_disposition not in {
            "ignored_auto_reply",
            "ignored_self_sender",
        }:
            raise SourceCoordinatorConfigError(
                "exact-source local disposition is unsupported"
            )
        classification_input["localSourceDisposition"] = (
            local_source_disposition
        )
    return classification_input


def _classify_source_proposal(
    classification_input: Mapping[str, Any],
):
    """B1 has no production model adapter; tests inject the fenced callback."""
    raise SourceCoordinatorConfigError(
        "exact-source classifier adapter is unavailable until B4"
    )


def _verify_local_source_policy(
    classification_input: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Verify provider-free local ignore policy before any model request starts."""
    local_source_disposition = classification_input.get(
        "localSourceDisposition"
    )
    evidence_kind = {
        "ignored_auto_reply": "local_ignore_auto_reply",
        "ignored_self_sender": "local_ignore_self_sender",
    }.get(local_source_disposition)
    if evidence_kind is None:
        return None
    return {
        "schemaVersion": 1,
        "evidenceKind": evidence_kind,
        "evidenceHash": canonical_json_hash(
            {
                "hashKind": "source-local-policy-v1",
                "canonicalSourceId": classification_input.get(
                    "canonicalSourceId"
                ),
                "localSourceDisposition": local_source_disposition,
            }
        ),
    }


def _consume_source_authority(
    _authority: SourceProcessingAuthority,
    _snapshot,
    _ledger,
) -> Mapping[str, Any]:
    """B1 defaults to a durable block until downstream adapters are adopted."""
    return {
        "state": "blocked",
        "reason": "exact-source downstream adapter is unavailable until B4",
    }


def _source_authority_consumer_available() -> bool:
    """B1 exposes no effectful downstream adapter before B3/B4 adoption."""
    return False


def _consume_durable_source_resume_context(
    context,
    execution_ledger,
) -> Mapping[str, Any]:
    authority = SourceProcessingAuthority(
        canonical_source_id=context.canonical_source_id,
        snapshot_hash=context.snapshot.snapshot_immutable_hash,
        selection_hash=context.snapshot.selection_hash,
        owner_kind=context.owner["ownerKind"],
        owner_key=context.owner["ownerKey"],
        ledger_hash=context.ledger["ledgerHash"],
    )
    return _consume_source_authority(
        authority,
        context.snapshot,
        execution_ledger,
    )


def _drain_durable_source_queue(user_id: str) -> int:
    """Recover retained releases and source work without Graph message state."""
    release_settled_source_generations(
        _fs,
        user_id=user_id,
        max_records=MAX_UNSETTLED_SOURCE_ADMISSIONS,
    )
    processed_count = 0
    remaining_budget = MAX_UNSETTLED_SOURCE_ADMISSIONS
    attempted_source_ids = set()
    while remaining_budget > 0:
        contexts = durable_source_resume_contexts(
            _fs,
            user_id=user_id,
            max_records=MAX_UNSETTLED_SOURCE_ADMISSIONS,
        )
        unattempted = [
            context
            for context in contexts
            if context.canonical_source_id not in attempted_source_ids
        ]
        if not unattempted:
            break
        for context in unattempted:
            if remaining_budget <= 0:
                break
            remaining_budget -= 1
            attempted_source_ids.add(context.canonical_source_id)
            resume_result = consume_durable_source_resume_context(
                _fs,
                user_id=user_id,
                context=context,
                consumer=(
                    _consume_durable_source_resume_context
                    if _source_authority_consumer_available()
                    else None
                ),
            )
            if resume_result.state != "settled":
                continue
            settlement = resume_result.settlement
            valid_settlement = (
                settlement is not None
                and verify_settled_source_dispatch_binding(
                    _fs,
                    user_id=user_id,
                    canonical_source_id=context.canonical_source_id,
                    thread_id=context.thread_id,
                    source_alias_keys=context.source_alias_keys,
                    snapshot_hash=context.snapshot.snapshot_immutable_hash,
                    selection_hash=context.snapshot.selection_hash,
                    owner_kind=context.owner["ownerKind"],
                    owner_key=context.owner["ownerKey"],
                    ledger_hash=context.ledger["ledgerHash"],
                    settlement_hash=settlement.settlement_hash,
                    settlement_revision=settlement.settlement_revision,
                    alias_projection_count=settlement.alias_projection_count,
                )
            )
            if not valid_settlement:
                raise SourceCoordinatorAmbiguous(
                    "durable source resume settlement failed readback"
                )
            processed_count += 1
    if remaining_budget == 0:
        remaining = durable_source_resume_contexts(
            _fs,
            user_id=user_id,
            max_records=MAX_UNSETTLED_SOURCE_ADMISSIONS,
        )
        if any(
            context.canonical_source_id not in attempted_source_ids
            for context in remaining
        ):
            raise SourceCoordinatorConfigError(
                "durable source resume exceeded its safe bound"
            )
    return processed_count


def process_inbox_message(
    user_id: str,
    headers: Dict[str, str],
    msg: Dict[str, Any],
    *,
    allow_outbound_reply: bool = True,
    operator_replay_attempt_id: Optional[str] = None,
    expected_canonical_source_id: Optional[str] = None,
):
    """ENHANCED: Process a single inbox message with full pipeline including events."""
    source_mode = resolve_source_coordinator_mode(os.environ)
    if source_mode is CoordinatorMode.SHADOW:
        return SourceProcessingDisposition(
            mode=source_mode,
            state="shadow_no_effect",
        )
    if source_mode is CoordinatorMode.ENFORCED:
        coordinator = build_source_coordinator(_fs)
        if (
            expected_canonical_source_id is not None
            and (
                type(expected_canonical_source_id) is not str
                or not expected_canonical_source_id
                or expected_canonical_source_id != expected_canonical_source_id.strip()
            )
        ):
            raise SourceCoordinatorConfigError(
                "expected canonical source id is malformed"
            )

    # A worker may process many sources in one context.  Never let a prior
    # source's send/suppression result influence the next exact source.
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
        if source_mode is CoordinatorMode.DISABLED:
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
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{_graph_message_path_segment(msg_id)}",
                headers=headers,
                params={"$select": "body,hasAttachments,sender,replyTo,ccRecipients"},
                timeout=30
            )
        ).json() or {}
        if (
            source_mode is CoordinatorMode.ENFORCED
            and (
                type(full_msg) is not dict
                or type(full_msg.get("body")) is not dict
            )
        ):
            raise SourceCoordinatorRetryable(
                "exact-source Graph hydration returned no authoritative body"
            )
        full_body_resp = full_msg.get("body", {}) or {}
        has_attachments = bool(has_attachments or full_msg.get("hasAttachments"))
        _raw_content = full_body_resp.get("content", "") or ""
        _ctype = (full_body_resp.get("contentType") or "Text").upper()
        _full_text = strip_html_tags(_raw_content) if _ctype == "HTML" else _raw_content
    except Exception as e:
        if source_mode is CoordinatorMode.ENFORCED:
            if isinstance(e, SourceCoordinatorRetryable):
                raise
            raise SourceCoordinatorRetryable(
                "exact-source Graph body hydration failed"
            ) from e
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
                    "https://graph.microsoft.com/v1.0/me/messages/"
                    f"{_graph_message_path_segment(msg_id)}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            response_status = getattr(response, "status_code", None)
            if (
                source_mode is CoordinatorMode.ENFORCED
                and type(response_status) is int
                and not 200 <= response_status < 300
            ):
                raise SourceCoordinatorRetryable(
                    "exact-source Graph header hydration was not successful"
                )
            header_payload = response.json()
            if source_mode is CoordinatorMode.ENFORCED:
                if (
                    type(header_payload) is not dict
                    or "internetMessageHeaders" not in header_payload
                    or type(header_payload["internetMessageHeaders"]) is not list
                ):
                    raise SourceCoordinatorRetryable(
                        "exact-source Graph header hydration was not authoritative"
                    )
                internet_message_headers = header_payload[
                    "internetMessageHeaders"
                ]
            else:
                internet_message_headers = header_payload.get(
                    "internetMessageHeaders", []
                )
        except Exception as e:
            if source_mode is CoordinatorMode.ENFORCED:
                if isinstance(e, SourceCoordinatorRetryable):
                    raise
                raise SourceCoordinatorRetryable(
                    "exact-source Graph header hydration failed"
                ) from e
            print(f"⚠️ Could not fetch headers for {msg_id}: {e}")
            internet_message_headers = []

    if source_mode is CoordinatorMode.ENFORCED:
        if type(internet_message_headers) is not list or any(
            type(header) is not dict
            or type(header.get("name")) is not str
            or type(header.get("value")) is not str
            for header in internet_message_headers
        ):
            raise SourceCoordinatorRetryable(
                "exact-source Graph headers are malformed"
            )
    
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

    local_source_disposition = None

    # SAFETY: Disabled mode retains the legacy early return. Enforced mode still
    # admits and settles the exact source under an empty local-policy proposal.
    if is_auto_reply:
        print(f"⏭️ Skipping auto-reply from {from_addr}: {subject}")
        print(f"   Auto-reply emails are not processed to prevent data corruption")
        if source_mode is not CoordinatorMode.ENFORCED:
            return
        local_source_disposition = "ignored_auto_reply"

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
            if source_mode is CoordinatorMode.ENFORCED and type(my_data) is not dict:
                raise SourceCoordinatorRetryable(
                    "exact-source self identity response is malformed"
                )
            my_email_value = (
                my_data.get("mail") or my_data.get("userPrincipalName") or ""
            )
            if isinstance(my_email_value, str):
                my_email = my_email_value.strip().lower()

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
                if source_mode is CoordinatorMode.ENFORCED and type(sent_data) is not dict:
                    raise SourceCoordinatorRetryable(
                        "exact-source sent identity response is malformed"
                    )
                sent_values = sent_data.get("value")
                if isinstance(sent_values, list) and sent_values:
                    sent_email_value = (
                        sent_values[0]
                        .get("from", {})
                        .get("emailAddress", {})
                        .get("address")
                        or ""
                    )
                    if isinstance(sent_email_value, str):
                        my_email = sent_email_value.strip().lower()

        if source_mode is CoordinatorMode.ENFORCED and not my_email:
            raise SourceCoordinatorRetryable(
                "exact-source self identity could not be verified"
            )

        if my_email and from_addr.lower() == my_email:
            print(f"⏭️ Skipping self-email (forwarded back): {subject}")
            print(f"   Sender {from_addr} matches our own address - likely auto-forwarded")
            if source_mode is not CoordinatorMode.ENFORCED:
                return
            if local_source_disposition is None:
                local_source_disposition = "ignored_self_sender"
    except Exception as e:
        if source_mode is CoordinatorMode.ENFORCED:
            raise SourceCoordinatorRetryable(
                "exact-source self-sender verification failed"
            ) from e
        # Don't fail the whole legacy process if this check fails.
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
        if thread_doc.exists is not True:
            raise RetryableProcessingError(
                "matched authoritative thread root is missing"
            )
        thread_data = thread_doc.to_dict() or {}
    except Exception as e:
        if isinstance(e, RetryableProcessingError):
            raise
        raise RetryableProcessingError(
            f"authoritative terminal thread read failed: {e}"
        ) from e

    if source_mode is CoordinatorMode.ENFORCED:
        strict_message_record = {
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
                "references": references,
            },
            "body": {
                "contentType": "Text",
                "content": _full_text,
                "preview": safe_preview(_full_text),
            },
            "hasAttachments": has_attachments,
            "sourceMessage": source_envelope,
        }
        existing_canonical_source_id = (
            coordinator.resolve_existing_canonical_source_id(
                user_id=user_id,
                hydrated_message=merged_msg,
                evidence_kind="graph_hydration",
                thread_id=thread_id,
            )
        )
        if (
            expected_canonical_source_id is not None
            and existing_canonical_source_id != expected_canonical_source_id
        ):
            raise SourceCoordinatorAmbiguous(
                "source retry/replay canonical authority is missing or changed"
            )
        if existing_canonical_source_id is not None:
            saved_history_binding, index_binding = (
                _persist_strict_source_history_and_index(
                    user_id=user_id,
                    thread_id=thread_id,
                    canonical_source_id=existing_canonical_source_id,
                    message_record=strict_message_record,
                )
            )
        identity = coordinator.admit_or_repair_source_identity(
            user_id=user_id,
            hydrated_message=merged_msg,
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        if (
            existing_canonical_source_id is not None
            and identity.canonical_source_id != existing_canonical_source_id
        ):
            raise SourceCoordinatorAmbiguous(
                "source identity changed after retained history validation"
            )
        disposition_binding = {
            "thread_id": thread_id,
            "source_alias_keys": _source_alias_keys_for_message(
                user_id,
                merged_msg,
            ),
        }
        retained = coordinator.quarantine_retained_terminal_authority(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            thread_id=thread_id,
            graph_message_id=msg_id,
            internet_message_id=internet_message_id,
        )
        if retained.canonical_source_id != identity.canonical_source_id:
            raise SourceCoordinatorAmbiguous(
                "retained terminal authority changed canonical source"
            )
        if retained.state == "legacy_terminal_authority_retained":
            return SourceProcessingDisposition(
                mode=source_mode,
                state=retained.state,
                **disposition_binding,
            )
        if retained.state == "no_retained_terminal_authority":
            marker_disposition = _strict_legacy_source_marker_disposition(
                user_id,
                thread_id,
                graph_message_id=msg_id,
                internet_message_id=internet_message_id,
            )
            if marker_disposition != "none":
                return SourceProcessingDisposition(
                    mode=source_mode,
                    state=marker_disposition,
                    **disposition_binding,
                )
        elif retained.state != "migrated_b1":
            raise SourceCoordinatorAmbiguous(
                "retained terminal authority returned an unknown state"
            )
        if existing_canonical_source_id is None:
            saved_history_binding, index_binding = (
                _persist_strict_source_history_and_index(
                    user_id=user_id,
                    thread_id=thread_id,
                    canonical_source_id=identity.canonical_source_id,
                    message_record=strict_message_record,
                )
            )
        classification_input = _read_source_classification_input(
            canonical_source_id=identity.canonical_source_id,
            thread_id=thread_id,
            hydrated_message=merged_msg,
            message_text=_text_for_ai,
            internet_message_headers=internet_message_headers,
            local_source_disposition=local_source_disposition,
        )
        snapshot = coordinator.classify_source_once(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input=classification_input,
            classifier=lambda: _classify_source_proposal(classification_input),
        )
        owner = coordinator.elect_transition_owner_from_snapshot(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        received_at = _source_processing_datetime(
            received_dt,
            field_name="receivedDateTime",
        )
        sent_at = _source_processing_datetime(
            sent_dt,
            field_name="sentDateTime",
        )
        authority = SourceProcessingAuthority(
            canonical_source_id=identity.canonical_source_id,
            snapshot_hash=snapshot.snapshot_immutable_hash,
            selection_hash=snapshot.selection_hash,
            owner_kind=owner["ownerKind"],
            owner_key=owner["ownerKey"],
            ledger_hash=ledger["ledgerHash"],
        )
        required_alias_key = next(
            (
                alias.key
                for alias in identity.aliases
                if alias.alias_type == "graph"
            ),
            identity.aliases[0].key,
        )

        blocker_canonical_source_id = None
        if owner["ownerKind"] == "none":
            admission = coordinator.admit_pending_inbound(
                user_id=user_id,
                canonical_source_id=identity.canonical_source_id,
                received_at=received_at,
                sent_at=sent_at,
                saved_history_binding=saved_history_binding,
                index_binding=index_binding,
            )
            if admission.state == "settled":
                settlement = coordinator.settle_source_markers_if_ready(
                    user_id=user_id,
                    canonical_source_id=identity.canonical_source_id,
                    ledger_hash=ledger["ledgerHash"],
                    required_source_alias_key=required_alias_key,
                )
                return SourceProcessingDisposition(
                    mode=source_mode,
                    state="settled",
                    authority=authority,
                    settlement=settlement,
                    **disposition_binding,
                )
        else:
            try:
                retained_settlement = coordinator.settle_source_markers_if_ready(
                    user_id=user_id,
                    canonical_source_id=identity.canonical_source_id,
                    ledger_hash=ledger["ledgerHash"],
                    required_source_alias_key=required_alias_key,
                )
            except SourceSettlementNotReady:
                retained_settlement = None
            if retained_settlement is not None:
                coordinator.release_settled_generation_if_needed(
                    user_id=user_id,
                    thread_id=thread_id,
                    canonical_source_id=identity.canonical_source_id,
                )
                return SourceProcessingDisposition(
                    mode=source_mode,
                    state="settled",
                    authority=authority,
                    settlement=retained_settlement,
                    **disposition_binding,
                )
            transition = coordinator.claim_or_resume_thread_transition(
                user_id=user_id,
                canonical_source_id=identity.canonical_source_id,
                received_at=received_at,
                sent_at=sent_at,
                saved_history_binding=saved_history_binding,
                index_binding=index_binding,
            )
            blocker_canonical_source_id = transition.blocker_canonical_source_id
            if transition.disposition == "blocked":
                return SourceProcessingDisposition(
                    mode=source_mode,
                    state="blocked",
                    authority=authority,
                    blocker_canonical_source_id=blocker_canonical_source_id,
                    **disposition_binding,
                )

        if all(
            entry["state"] in {"completed", "delegated", "dominated"}
            for entry in ledger["entries"]
        ):
            settlement = coordinator.settle_source_markers_if_ready(
                user_id=user_id,
                canonical_source_id=identity.canonical_source_id,
                ledger_hash=ledger["ledgerHash"],
                required_source_alias_key=required_alias_key,
            )
            if owner["ownerKind"] != "none":
                coordinator.release_settled_generation_if_needed(
                    user_id=user_id,
                    thread_id=thread_id,
                    canonical_source_id=identity.canonical_source_id,
                )
            return SourceProcessingDisposition(
                mode=source_mode,
                state="settled",
                authority=authority,
                settlement=settlement,
                **disposition_binding,
            )

        work_result = coordinator.consume_source_work_once(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            consumer=(
                (
                    lambda execution_ledger: _consume_source_authority(
                        authority,
                        snapshot,
                        execution_ledger,
                    )
                )
                if _source_authority_consumer_available()
                else None
            ),
        )
        if work_result["state"] == "blocked":
            return SourceProcessingDisposition(
                mode=source_mode,
                state="blocked",
                authority=authority,
                blocker_canonical_source_id=blocker_canonical_source_id,
                **disposition_binding,
            )

        settlement = coordinator.settle_source_markers_if_ready(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            required_source_alias_key=required_alias_key,
        )
        if owner["ownerKind"] != "none":
            coordinator.release_settled_generation_if_needed(
                user_id=user_id,
                thread_id=thread_id,
                canonical_source_id=identity.canonical_source_id,
            )
        return SourceProcessingDisposition(
            mode=source_mode,
            state="settled",
            authority=authority,
            settlement=settlement,
            **disposition_binding,
        )

    exact_terminal_saga = _terminal_saga_for_source(
        thread_data,
        msg_id,
        internet_message_id,
    )
    if exact_terminal_saga:
        print(
            "🔁 Resuming exact immutable terminal saga before the generic inbox pipeline"
        )
        return _resume_exact_terminal_saga(
            user_id,
            headers,
            thread_id,
            thread_data,
            exact_terminal_saga,
        )

    exact_terminal_settlement = _terminal_settlement_for_source(
        thread_data,
        msg_id,
        internet_message_id,
    )
    if exact_terminal_settlement:
        _replay_terminal_completion_obligation(
            user_id,
            thread_data,
            exact_terminal_settlement,
            msg_id,
            internet_message_id,
        )
        print(
            "✅ Exact terminal source already has an immutable settlement; "
            "skipping replacement and generic processing"
        )
        return

    retained_terminal_settlements = _validate_terminal_settlement_history(
        (thread_data or {}).get("terminalSettlements")
    )
    if len(retained_terminal_settlements) >= TERMINAL_SETTLEMENT_HISTORY_LIMIT:
        # This source is not one of the eight exact retained generations.  Stop
        # before history writes, follow-up mutation, attachment reads, Sheets,
        # model/proposal work, notifications, or any Graph send.
        raise RetryableProcessingError(
            "terminal settlement retention limit reached before generic source "
            "admission; operator review is required"
        )

    pending_terminal_other_source = _has_pending_terminal_saga(thread_data)
    if pending_terminal_other_source:
        print(
            "⏸️ A different exact source owns the pending terminal saga; "
            "saving this message for history only and leaving it retryable"
        )

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
            "references": references,
        },
        "body": {
            "contentType": "Text",
            "content": _full_text,
            "preview": safe_preview(_full_text),
        },
        "hasAttachments": has_attachments,
        "sourceMessage": source_envelope,
    }

    if pending_terminal_other_source:
        _persist_inbound_message_history(
            user_id,
            thread_id,
            msg_id,
            internet_message_id,
            message_record,
            thread_ref,
            source_envelope,
            strict=True,
        )
        raise RetryableProcessingError(
            "terminal saga transition is pending for a different source message; "
            "history was saved but downstream processing remains blocked"
        )

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
    if (
        thread_status == THREAD_STATUS["paused"]
        and not client_denied
        and not pending_terminal_other_source
    ):
        if _resume_paused_thread_after_manual_continuation(
            user_id, headers, thread_id, thread_data, msg
        ):
            thread_data["status"] = THREAD_STATUS["active"]
            thread_data["statusReason"] = "manual_continuation_resumed"
            thread_status = THREAD_STATUS["active"]

    late_reply_patch = _late_reply_after_followup_exhaustion_patch(
        thread_data,
        message_text=_text_for_ai,
        has_attachments=has_attachments,
    )
    if (
        late_reply_patch
        and not client_denied
        and not exact_terminal_saga
        and not pending_terminal_other_source
    ):
        thread_ref.set(late_reply_patch, merge=True)
        thread_data.update(late_reply_patch)
        thread_status = THREAD_STATUS["active"]
        print(
            f"↩️ Reactivated thread {thread_id[:20]}... for a broker reply received "
            "after follow-ups were exhausted"
        )

    # Terminal threads keep late replies for history but must not generate new AI work or auto-replies,
    # except when the user approved a same-contact replacement property in this email thread.
    replacement_context = _active_replacement_context(thread_data, _full_text)
    if (
        replacement_context
        and thread_status == THREAD_STATUS["stopped"]
        and not client_denied
        and not exact_terminal_saga
        and not pending_terminal_other_source
    ):
        replacement_subject = replacement_context["address"]
        if replacement_context.get("city"):
            replacement_subject = f"{replacement_subject}, {replacement_context['city']}"
        thread_patch = {
            "rowNumber": replacement_context["rowNumber"],
            "subject": replacement_subject,
            "status": THREAD_STATUS["active"],
            "followUpStatus": "waiting",
            "statusReason": "same_contact_replacement_reply",
            "pendingTerminalReason": None,
            "pendingTerminalAt": None,
            "updatedAt": SERVER_TIMESTAMP,
        }
        thread_ref.set(thread_patch, merge=True)
        thread_data.update(thread_patch)
        thread_status = THREAD_STATUS["active"]
        print(
            f"🔁 Reactivated stopped thread for replacement property "
            f"{replacement_subject} row {replacement_context['rowNumber']}"
        )

    terminal_thread_skip = _should_skip_processing_for_terminal_thread(
        thread_status,
        thread_data,
        _full_text,
    )
    if (
        client_denied
        or pending_terminal_other_source
        or (terminal_thread_skip and not exact_terminal_saga)
    ):
        reason_label = (
            f"campaign automation is {campaign_suppression_kind}"
            if client_denied
            else (
                "another source owns a pending terminal saga"
                if pending_terminal_other_source
                else f"thread is {thread_status}"
            )
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

    _persist_inbound_message_history(
        user_id,
        thread_id,
        msg_id,
        internet_message_id,
        message_record,
        thread_ref,
        source_envelope,
        strict=False,
    )

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
        if exact_terminal_saga and (
            thread_data.get("terminalNotificationOwed")
            or thread_data.get("terminalReplyOwed")
        ):
            raise RetryableProcessingError(
                "exact terminal source still has unresolved obligations"
            )
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
        defer_client_completion_for_closing_reply = False
        active_terminal_saga = dict(exact_terminal_saga) if exact_terminal_saga else None

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
        if active_terminal_saga:
            proposal = {
                "updates": [],
                "events": [{
                    "type": "property_unavailable",
                    "reason": active_terminal_saga.get("reason"),
                    "_terminalSagaResume": True,
                }],
                "response_email": active_terminal_saga.get("responseBody"),
            }
            print(
                "🔁 Resuming immutable terminal saga for the exact source message "
                "without requesting fresh model output"
            )
        else:
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
            if active_terminal_saga:
                row_anchor = active_terminal_saga.get("rowAnchor") or row_anchor

            events = _order_events_for_processing(_proposal_events(proposal))
            current_pdf_manifest, new_property_pdf_groups = _partition_property_attachments(
                pdf_manifest,
                current_anchor=row_anchor,
                events=events,
            )
            new_property_events = [
                event for event in events
                if (event or {}).get("type") == "new_property"
            ]
            new_property_pdf_by_event = {
                id(event): new_property_pdf_groups[index]
                for index, event in enumerate(new_property_events)
            }
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
                if event_type == "property_unavailable":
                    event_key = _terminal_event_key_for_source(
                        event_type,
                        msg_id,
                        internet_message_id,
                    )
                print(f"   Event key: {event_key}")

                # Check if this event was already handled - prevents duplicate notifications
                # when AI re-detects the same event from conversation history
                event_already_handled = (
                    _terminal_event_is_handled_for_source(
                        thread_data,
                        event_type,
                        event_key,
                        msg_id,
                        internet_message_id,
                    )
                    if event_type == "property_unavailable"
                    else is_event_handled(user_id, thread_id, event_key)
                )
                if (
                    event_already_handled
                    and not active_terminal_saga
                ):
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
                    if not active_terminal_saga and not _property_unavailable_event_applies_to_row(
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

                    terminal_saga_owner = None
                    try:
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        comments_col_idx = find_notes_comment_column_index(header)
                        row_already_nonviable = _is_row_below_nonviable(
                            sheets,
                            sheet_id,
                            tab_title,
                            rownum,
                        )
                        if active_terminal_saga:
                            saga = dict(active_terminal_saga)
                            _validate_terminal_saga_sheet_layout(saga, header)
                            terminal_reason = saga.get("reason")
                            terminal_note = saga.get("note")
                            event_key = saga.get("eventKey")
                            comments_col_idx = saga.get("notesColumnIndex")
                            print(
                                "🔁 Processing persisted property_unavailable saga "
                                f"({saga.get('sagaKey', '')[:12]})"
                            )
                        else:
                            terminal_reason = _nonviable_status_reason(event)
                            message_content = _full_text.lower()
                            found_keyword = next(
                                (
                                    keyword
                                    for keyword in PROPERTY_UNAVAILABLE_KEYWORDS
                                    if keyword in message_content
                                ),
                                "AI-detected unavailability",
                            )
                            comment_reason = (
                                terminal_reason
                                if terminal_reason == "requirements_mismatch"
                                else found_keyword
                            )
                            print(
                                "🔍 Processing property_unavailable event "
                                f"(trigger: '{found_keyword}')"
                            )
                            unavailable_comment = _build_property_unavailable_comment(
                                _terminal_note_date(msg),
                                comment_reason,
                                events,
                            )
                            if comments_col_idx is None:
                                # Refuse before staging any immutable saga or
                                # stopping row roots.  A later-added column may
                                # never retroactively become this source's
                                # coordinate.
                                raise ValueError(
                                    "Notes/Comments column is required for a "
                                    "terminal transition"
                                )
                            existing_comment = _read_terminal_note(
                                sheets,
                                sheet_id,
                                tab_title,
                                rownum,
                                comments_col_idx,
                            )
                            terminal_note = _merge_terminal_note(
                                existing_comment,
                                unavailable_comment,
                            )
                            if row_already_nonviable:
                                divider_preview = {
                                    "dividerRow": max(1, rownum - 1),
                                    "exists": True,
                                }
                            else:
                                divider_preview = _preview_nonviable_divider(
                                    sheets,
                                    sheet_id,
                                    tab_title,
                                )
                            saga = _build_terminal_saga(
                                user_id,
                                client_id,
                                thread_id,
                                message=msg,
                                internet_message_id=internet_message_id,
                                conversation_id=conversation_id,
                                sheet_id=sheet_id,
                                tab_title=tab_title,
                                source_row=rownum,
                                row_anchor=row_anchor,
                                notes_column_index=comments_col_idx,
                                sheet_header=header,
                                terminal_reason=terminal_reason,
                                terminal_note=terminal_note,
                                event_key=event_key,
                                event=event,
                                divider_row=divider_preview["dividerRow"],
                                divider_exists=divider_preview["exists"],
                                row_already_nonviable=row_already_nonviable,
                                has_alternative_path=_has_new_property_path(
                                    events,
                                    new_row_created=new_row_created,
                                    new_property_pending_created=(
                                        new_property_pending_created
                                    ),
                                ),
                                llm_response_email=proposal.get("response_email"),
                                column_config=column_config,
                                contact_name=contact_name,
                                reply_recipient=to_addr_lower,
                            )
                            saga, terminal_saga_owner = _stage_terminal_saga(
                                user_id,
                                client_id,
                                thread_id,
                                saga,
                            )
                            active_terminal_saga = dict(saga)
                            print(
                                "🛑 Persisted immutable terminal saga and stopped "
                                f"{len(saga['finalizationPlan']['terminalThreadIds'])} "
                                "exact row root(s)"
                            )
                        (
                            terminal_source_row,
                            expected_final_row,
                            mutation_kind,
                        ) = _terminal_sheet_mutation_geometry_from_saga(saga)
                        if rownum not in {terminal_source_row, expected_final_row}:
                            raise ValueError(
                                "live terminal row does not match the immutable saga"
                            )
                        saga_phase = saga.get("phase")
                        if saga_phase == "staged":
                            if mutation_kind == "move_with_note":
                                _verify_terminal_finalization_plan(
                                    user_id,
                                    client_id,
                                    saga,
                                )
                            final_row = _execute_or_reconcile_terminal_sheet_mutation(
                                user_id,
                                thread_id,
                                sheets,
                                sheet_id,
                                tab_title,
                                header,
                                comments_col_idx,
                                saga,
                                terminal_saga_owner,
                                mutation_kind,
                                allow_provider_mutation=True,
                            )
                            saga = _finalize_terminal_thread_roots(
                                user_id,
                                client_id,
                                thread_id,
                                saga,
                                final_row=final_row,
                                terminal_saga_owner=terminal_saga_owner,
                            )
                            active_terminal_saga = dict(saga)
                        elif saga_phase == "finalized":
                            final_row = expected_final_row
                            _execute_or_reconcile_terminal_sheet_mutation(
                                user_id,
                                thread_id,
                                sheets,
                                sheet_id,
                                tab_title,
                                header,
                                comments_col_idx,
                                saga,
                                terminal_saga_owner,
                                mutation_kind,
                                allow_provider_mutation=False,
                            )
                        else:
                            raise ValueError(f"unsupported terminal saga phase: {saga_phase}")

                        _settle_terminal_notification_obligation(
                            user_id,
                            client_id,
                            thread_id,
                            to_addr_lower,
                            saga,
                            terminal_saga_owner=terminal_saga_owner,
                        )

                        # Reformatting is cosmetic and must not turn a committed
                        # terminal transition into a retry hidden by stopped state.
                        try:
                            format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                        except Exception as format_error:
                            print(f"⚠️ Could not reformat terminal row: {format_error}")

                        old_row_became_nonviable = True
                        rownum = final_row
                        try:
                            clear_row_highlight(sheet_id, final_row)
                        except Exception as e:
                            print(f"⚠️ Could not clear row highlight: {e}")
                        print(
                            "🚫 Terminal Sheet/state evidence committed; notification "
                            "obligation settled"
                        )
                    except Exception as e:
                        if terminal_saga_owner and "saga" in locals():
                            _release_terminal_saga_execution_claim(
                                user_id,
                                saga,
                                terminal_saga_owner,
                            )
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
                        event_pdf_manifest = new_property_pdf_by_event.get(id(event), [])
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
                            event_pdf_manifest,
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
                                "pdfLinks": [p.get('drive_link') for p in event_pdf_manifest if p.get('drive_link')],
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
                                    for p in event_pdf_manifest
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
                        if _should_defer_client_completion_for_closing_reply(proposal):
                            defer_client_completion_for_closing_reply = True
                            print("   ⏳ Deferring campaign completion until the closing reply is resolved")
                        else:
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

            has_new_property_path = _has_new_property_path(
                events,
                new_row_created=new_row_created,
                new_property_pending_created=new_property_pending_created,
            )
            current_flyer_links, current_floorplan_links = _categorize_property_asset_links(
                current_pdf_manifest
            )
            if current_pdf_manifest:
                pdf_link_updates_for_results: Dict[str, List[str]] = {}
                try:
                    sheets = _sheets_client()
                    property_image_candidate = select_property_image_candidate(
                        current_pdf_manifest,
                        address=row_anchor,
                    )
                    property_image_updates_for_results: Dict[str, List[str]] = {}

                    if current_flyer_links:
                        flyer_updates = append_links_to_flyer_link_column(sheets, sheet_id, header, rownum, current_flyer_links)
                        for column, added_flyer_links in flyer_updates.items():
                            pdf_link_updates_for_results[column] = added_flyer_links
                        print(f"   🔗 Applied {len(current_flyer_links)} flyer link(s) to current row")

                    # Delay between writes to avoid Google Sheets API rate limits
                    if current_flyer_links and current_floorplan_links:
                        print("   ⏳ Waiting 30s before next sheet write to avoid rate limits...")
                        time.sleep(30)

                    if current_floorplan_links:
                        floorplan_updates = append_links_to_floorplan_column(sheets, sheet_id, header, rownum, current_floorplan_links)
                        for column, added_floorplan_links in floorplan_updates.items():
                            pdf_link_updates_for_results[column] = added_floorplan_links
                        print(f"   📐 Applied {len(current_floorplan_links)} floorplan link(s) to current row")

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
                    if current_flyer_links or current_floorplan_links or property_image_updates_for_results:
                        try:
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            header = _read_header_row2(sheets, sheet_id, tab_title)
                            format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                        except Exception as _e:
                            print(f"ℹ️ Skipped re-format after link append: {_e}")

                    if pdf_link_updates_for_results:
                        _record_pdf_link_updates(
                            sheets,
                            user_id,
                            client_id,
                            sheet_id,
                            header,
                            rownum,
                            rowvals,
                            thread_id,
                            to_addr_lower,
                            current_pdf_manifest,
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
                except AssetLinkWriteError as e:
                    _raise_retryable_asset_link_write_failure(
                        e,
                        sheets,
                        user_id,
                        client_id,
                        sheet_id,
                        header,
                        rownum,
                        rowvals,
                        thread_id,
                        internet_message_id or msg_id,
                        to_addr_lower,
                        current_pdf_manifest,
                        pdf_link_updates_for_results,
                    )
                except Exception as e:
                    print(f"⚠️ Failed to write PDF link/property image metadata to sheet: {e}")
            elif pdf_manifest and has_new_property_path:
                print("   ℹ️ Attachment links were preserved with pending replacement properties")

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
                if active_terminal_saga:
                    raise RetryableProcessingError(
                        "terminal saga reply remains owed in extraction-only replay"
                    )
                return

            if active_terminal_saga:
                reply_outcome = _settle_terminal_reply_obligation(
                    user_id,
                    client_id,
                    thread_id,
                    headers,
                    active_terminal_saga.get("replyRecipient") or to_addr_lower,
                    active_terminal_saga,
                    terminal_saga_owner=terminal_saga_owner,
                )
                print(f"📧 Terminal saga reply reached durable outcome: {reply_outcome}")
                if active_terminal_saga.get("completeClientAfterReply"):
                    _require_terminal_client_completion(user_id, client_id)
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
                requirements_mismatch_nonviable = any(
                    (event or {}).get("type") == "property_unavailable"
                    and _nonviable_status_reason(event) == "requirements_mismatch"
                    for event in events
                )

                # Scenario 1: Property became non-viable AND new property was suggested
                if old_row_became_nonviable and has_new_property_path:
                    print(f"   📍 SCENARIO 1: Non-viable + new property suggested")
                    response_scenario = (
                        "requirements_mismatch_with_alternative"
                        if requirements_mismatch_nonviable
                        else "nonviable_with_alternative"
                    )
                    response_body = _select_automatic_response_body(
                        response_scenario,
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
                    response_scenario = (
                        "requirements_mismatch"
                        if requirements_mismatch_nonviable
                        else "nonviable"
                    )
                    response_body = _select_automatic_response_body(
                        response_scenario,
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
                    if response_sent:
                        _maybe_mark_client_completed(user_id, client_id)
                
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
                                if not defer_client_completion_for_closing_reply:
                                    _maybe_mark_client_completed(user_id, client_id)
                            else:
                                response_sent = _handle_auto_response_send_failure(
                                    user_id, thread_id, msg_id, to_addr_lower, response_body, client_id,
                                    failure_label="closing email"
                                )
                        
            except Exception as e:
                print(f"❌ Failed to send automatic response: {e}")
            finally:
                if defer_client_completion_for_closing_reply:
                    send_outcome = _get_reply_send_outcome()
                    if send_outcome.campaign_suppression_kind == "terminal":
                        print("⏹️ Campaign became terminal before closing reply; completion update skipped")
                    else:
                        _maybe_mark_client_completed(user_id, client_id)
        
        else:
            print("ℹ️ No proposal generated; nothing to apply.")
            _record_ai_processing_failure(
                user_id, client_id, thread_id, msg_id,
                "OpenAI proposal was unavailable or invalid JSON"
            )
            raise RetryableProcessingError("OpenAI proposal was unavailable or invalid JSON")


def _validated_enforced_inbox_next_link(
    value: Any,
    *,
    seen_page_urls: set[str],
    completed_page_count: int,
) -> str | None:
    """Accept only a bounded, acyclic continuation of the exact Graph inbox query."""
    if value is None:
        return None
    if type(value) is not str or not value or value != value.strip():
        raise SourceCoordinatorRetryable(
            "exact-source Graph inbox pagination is malformed"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as parse_error:
        raise SourceCoordinatorRetryable(
            "exact-source Graph inbox pagination is malformed"
        ) from parse_error
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "graph.microsoft.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path.rstrip("/") != _GRAPH_INBOX_MESSAGES_PATH
    ):
        raise SourceCoordinatorRetryable(
            "exact-source Graph inbox pagination left the approved resource"
        )
    if value in seen_page_urls:
        raise SourceCoordinatorRetryable(
            "exact-source Graph inbox pagination repeated a page"
        )
    if completed_page_count >= MAX_ENFORCED_INBOX_SCAN_PAGES:
        raise SourceCoordinatorRetryable(
            "exact-source Graph inbox pagination exceeded its safe bound"
        )
    return value

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """
    Idempotent scan of inbox for replies with early exit on processed messages.

    BATCHING: Groups multiple unprocessed messages in the same thread together
    to prevent conflicting auto-responses when contact sends multiple emails quickly.
    """
    source_mode = resolve_source_coordinator_mode(os.environ)
    if source_mode is CoordinatorMode.SHADOW:
        return SourceProcessingDisposition(
            mode=source_mode,
            state="shadow_no_effect",
        )

    durable_processed_before_scan = 0
    if source_mode is CoordinatorMode.ENFORCED:
        try:
            durable_processed_before_scan = _drain_durable_source_queue(user_id)
        except Exception as exc:
            return {
                "status": "error",
                "operation": "inbox_scan",
                "error": f"durable source recovery failed: {exc}",
                "scanned": 0,
                "processed": 0,
                "batched": 0,
                "skipped": 0,
                "orphaned": 0,
                "unsettled": 1,
            }

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
    thread_message_dispositions = {}
    orphan_messages = []  # Messages we couldn't match to a thread
    provisional_terminal_messages = []

    scanned_count = 0
    skipped_count = 0

    try:
        url = f"{base}/me/mailFolders/Inbox/messages"
        seen_page_urls: set[str] = set()
        completed_page_count = 0

        while url:
            if source_mode is CoordinatorMode.ENFORCED:
                if url in seen_page_urls:
                    raise SourceCoordinatorRetryable(
                        "exact-source Graph inbox pagination repeated a page"
                    )
                if completed_page_count >= MAX_ENFORCED_INBOX_SCAN_PAGES:
                    raise SourceCoordinatorRetryable(
                        "exact-source Graph inbox pagination exceeded its safe bound"
                    )
                seen_page_urls.add(url)
            response = exponential_backoff_request(
                lambda: requests.get(url, headers=headers, params=params, timeout=30)
            )
            completed_page_count += 1
            data = response.json()
            if source_mode is CoordinatorMode.ENFORCED and (
                type(data) is not dict
                or "value" not in data
                or type(data["value"]) is not list
            ):
                raise SourceCoordinatorRetryable(
                    "exact-source Graph inbox page is malformed"
                )
            messages = data.get("value", [])
            if source_mode is CoordinatorMode.ENFORCED and any(
                type(message) is not dict
                or not any(
                    type(message.get(field)) is str
                    and bool(message.get(field).strip())
                    for field in ("id", "internetMessageId")
                )
                for message in messages
            ):
                raise SourceCoordinatorRetryable(
                    "exact-source Graph inbox message identity is malformed"
                )
            next_url = data.get("@odata.nextLink")
            if source_mode is CoordinatorMode.ENFORCED:
                next_url = _validated_enforced_inbox_next_link(
                    next_url,
                    seen_page_urls=seen_page_urls,
                    completed_page_count=completed_page_count,
                )
                if (
                    scanned_count + len(messages)
                    > MAX_ENFORCED_INBOX_SCAN_MESSAGES
                ):
                    raise SourceCoordinatorRetryable(
                        "exact-source Graph inbox scan exceeded its message bound"
                    )

            if not messages:
                if next_url:
                    url = next_url
                    params = {}
                    continue
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

                # Resolve active/settled/ordinary from one authoritative thread
                # snapshot before generic processed/manual/batch behavior.
                thread_id = _match_message_to_thread(
                    user_id,
                    msg,
                    headers,
                    strict=source_mode is CoordinatorMode.ENFORCED,
                )
                if source_mode is CoordinatorMode.ENFORCED:
                    if thread_id:
                        thread_messages[thread_id].append(msg)
                    else:
                        skipped_count += 1
                    continue

                disposition = {"kind": "ordinary"}
                if thread_id:
                    disposition = _terminal_retry_disposition(
                        user_id,
                        thread_id,
                        processed_key,
                        graph_message_id=msg.get("id"),
                        internet_message_id=msg.get("internetMessageId"),
                    )
                    if (
                        disposition.get("kind") in {"active", "settled"}
                        and disposition.get("exactSourceConfirmed") is not True
                    ):
                        print(
                            "⏸️ Terminal source matched only an untyped alias; "
                            "leaving the inbox message unprocessed for identity review"
                        )
                        provisional_terminal_messages.append(
                            (thread_id, msg, processed_key, disposition.get("kind"))
                        )
                        skipped_count += 1
                        continue
                    if disposition.get("kind") == "settled":
                        skipped_count += 1
                        continue

                already_processed = has_processed(user_id, processed_key)
                if already_processed and disposition.get("kind") != "active":
                    skipped_count += 1
                    continue

                if thread_id:
                    thread_message_dispositions[
                        (thread_id, processed_key)
                    ] = disposition.get("kind")
                    thread_messages[thread_id].append(msg)
                else:
                    orphan_messages.append(msg)

            # Handle pagination
            url = next_url
            if url:
                params = {}  # nextLink includes all parameters

    except Exception as e:
        state = _graph_operation_error_state("inbox_scan", e)
        print(f"❌ Failed to scan inbox: {state.get('error')}")
        return state

    if source_mode is CoordinatorMode.ENFORCED:
        processed_count = durable_processed_before_scan
        unsettled_count = 0
        first_unsettled_error = None
        globally_ordered_messages = sorted(
            (
                (thread_id, message)
                for thread_id, messages in thread_messages.items()
                for message in messages
            ),
            key=lambda item: (
                item[1].get("receivedDateTime")
                or item[1].get("sentDateTime")
                or "",
                item[1].get("id") or "",
            ),
        )
        blocked_thread_ids = set()
        for thread_id, message in globally_ordered_messages:
            if thread_id in blocked_thread_ids:
                continue
            processed_key = (
                message.get("internetMessageId") or message.get("id")
            )
            try:
                result = process_inbox_message(user_id, headers, message)
                valid_settlement = _is_bound_exact_source_settlement(
                    result,
                    user_id=user_id,
                    thread_id=thread_id,
                    message=message,
                )
                if valid_settlement:
                    valid_settlement = verify_settled_source_dispatch_binding(
                        _fs,
                        user_id=user_id,
                        canonical_source_id=(
                            result.authority.canonical_source_id
                        ),
                        thread_id=thread_id,
                        source_alias_keys=result.source_alias_keys,
                        snapshot_hash=result.authority.snapshot_hash,
                        selection_hash=result.authority.selection_hash,
                        owner_kind=result.authority.owner_kind,
                        owner_key=result.authority.owner_key,
                        ledger_hash=result.authority.ledger_hash,
                        settlement_hash=result.settlement.settlement_hash,
                        settlement_revision=(
                            result.settlement.settlement_revision
                        ),
                        alias_projection_count=(
                            result.settlement.alias_projection_count
                        ),
                    )
                if not valid_settlement:
                    unsettled_count += 1
                    blocked_thread_ids.add(thread_id)
                    first_unsettled_error = first_unsettled_error or (
                        "exact source did not produce a canonical settlement"
                    )
                    print(
                        "⏸️ Exact source remains unsettled; leaving it and every "
                        "later same-thread source enumerable"
                    )
                    continue
                processed_count += 1
            except Exception as exc:
                unsettled_count += 1
                blocked_thread_ids.add(thread_id)
                first_unsettled_error = first_unsettled_error or str(exc)
                print(f"❌ Failed to settle exact source {processed_key}: {exc}")

        try:
            processed_count += _drain_durable_source_queue(user_id)
        except Exception as exc:
            unsettled_count += 1
            first_unsettled_error = first_unsettled_error or (
                f"durable source recovery failed: {exc}"
            )

        result = {
            "status": "healthy" if unsettled_count == 0 else "error",
            "operation": "inbox_scan",
            "scanned": scanned_count,
            "processed": processed_count,
            "batched": 0,
            "skipped": skipped_count,
            "orphaned": len(orphan_messages),
        }
        if unsettled_count:
            result.update(
                {
                    "error": first_unsettled_error
                    or "one or more exact sources remain unsettled",
                    "unsettled": unsettled_count,
                }
            )
            return result

        try:
            outstanding_admissions = advance_scan_cursor_if_source_authority_clear(
                _fs,
                user_id=user_id,
                last_scan_iso=now_iso.replace("+00:00", "Z"),
            )
        except Exception as exc:
            result.update(
                {
                    "status": "error",
                    "error": f"durable source admission audit failed: {exc}",
                    "unsettled": 1,
                }
            )
            return result
        if outstanding_admissions:
            result.update(
                {
                    "status": "error",
                    "error": (
                        "durable exact sources remain unsettled outside the "
                        "current Graph scan window"
                    ),
                    "unsettled": len(outstanding_admissions),
                }
            )
            return result

        print(
            f"📥 Scanned {scanned_count}; independently settled "
            f"{processed_count} exact sources; skipped {skipped_count}"
        )
        return result

    # PHASE 2: Process messages - batched by thread
    processed_count = 0
    batched_count = 0
    failure_visibility_lost = 0

    for thread_id, msg, processed_key, terminal_kind in provisional_terminal_messages:
        failure_recorded = _record_ai_processing_failure(
            user_id,
            _client_id_for_processing_failure(user_id, thread_id),
            thread_id,
            processed_key,
            (
                f"{terminal_kind} terminal source matched only an untyped alias; "
                "manual identity review is required"
            ),
            retryable=False,
            recovery_status="terminal_source_identity_unconfirmed",
            graph_message_id=msg.get("id"),
            internet_message_id=msg.get("internetMessageId"),
            source_message_key=processed_key,
        )
        if failure_recorded is not True:
            failure_visibility_lost += 1

    # Process thread batches (multiple messages in same thread)
    # Add delay between processing to avoid Google Sheets rate limits (60 reads/min)
    RATE_LIMIT_DELAY = 3  # seconds between processing each thread

    thread_list = list(thread_messages.items())
    for idx, (thread_id, messages) in enumerate(thread_list):
        exact_recovery_messages = []
        remaining_messages = list(messages)
        if len(messages) > 1:
            remaining_messages = []
            for candidate in messages:
                candidate_key = (
                    candidate.get("internetMessageId") or candidate.get("id")
                )
                if thread_message_dispositions.get(
                    (thread_id, candidate_key)
                ) == "active":
                    exact_recovery_messages.append(candidate)
                else:
                    remaining_messages.append(candidate)

        exact_recovery_failed = False
        for recovery_msg in exact_recovery_messages:
            processing_error = None
            recovery_key = (
                recovery_msg.get("internetMessageId") or recovery_msg.get("id")
            )
            try:
                process_inbox_message(user_id, headers, recovery_msg)
                processed_count += 1
                _clear_ai_processing_failure(
                    user_id,
                    thread_id,
                    recovery_key,
                    graph_message_id=recovery_msg.get("id"),
                    internet_message_id=recovery_msg.get("internetMessageId"),
                    source_message_key=recovery_key,
                )
            except Exception as exc:
                processing_error = exc
                exact_recovery_failed = True
                print(f"❌ Failed to recover exact terminal saga message: {exc}")
                failure_recorded = _record_ai_processing_failure(
                    user_id,
                    _client_id_for_processing_failure(user_id, thread_id),
                    thread_id,
                    recovery_key,
                    str(exc),
                    graph_message_id=recovery_msg.get("id"),
                    internet_message_id=recovery_msg.get("internetMessageId"),
                    source_message_key=recovery_key,
                )
                if failure_recorded is not True:
                    failure_visibility_lost += 1
            finally:
                if _should_mark_processed_after_error(processing_error):
                    mark_processed(user_id, recovery_key)
                else:
                    print(f"🔁 Leaving exact terminal source retryable: {recovery_key}")
            if exact_recovery_failed:
                break

        if exact_recovery_failed:
            print(
                "⏸️ Exact terminal recovery failed; leaving every later message "
                "in this thread unprocessed until the saga resolves"
            )
            if idx < len(thread_list) - 1:
                time.sleep(RATE_LIMIT_DELAY)
            continue

        messages = remaining_messages
        if not messages:
            if idx < len(thread_list) - 1:
                time.sleep(RATE_LIMIT_DELAY)
            continue

        if len(messages) > 1:
            # BATCH PROCESSING: Multiple messages in same thread
            print(f"📦 Batching {len(messages)} messages for thread {thread_id[:20]}...")
            batched_count += len(messages) - 1  # Count the extras

            # Process only the LAST message (most recent), but include all message content
            # in the conversation history (which is already handled by build_conversation_payload)
            # First, save all the messages to Firestore so they appear in conversation
            batch_prerequisite_failed = False
            for msg in messages[:-1]:  # All but the last
                try:
                    _save_message_to_thread(user_id, thread_id, msg, headers)
                    processed_key = msg.get("internetMessageId") or msg.get("id")
                    mark_processed(user_id, processed_key)
                except Exception as e:
                    print(f"⚠️ Failed to save batched message: {e}")
                    failed_key = msg.get("internetMessageId") or msg.get("id")
                    failure_recorded = _record_ai_processing_failure(
                        user_id,
                        _client_id_for_processing_failure(user_id, thread_id),
                        thread_id,
                        failed_key,
                        f"Conversation history persistence failed: {e}",
                        graph_message_id=msg.get("id"),
                        internet_message_id=msg.get("internetMessageId"),
                        source_message_key=failed_key,
                    )
                    if failure_recorded is not True:
                        failure_visibility_lost += 1
                    batch_prerequisite_failed = True
                    break

            if batch_prerequisite_failed:
                print(
                    "⏸️ Earlier batched message was not durably saved; "
                    "leaving the latest message unprocessed to preserve ordering"
                )
                if idx < len(thread_list) - 1:
                    time.sleep(RATE_LIMIT_DELAY)
                continue

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
                _clear_ai_processing_failure(
                    user_id,
                    thread_id,
                    processed_key,
                    graph_message_id=last_msg.get("id"),
                    internet_message_id=last_msg.get("internetMessageId"),
                    source_message_key=processed_key,
                )
            except Exception as e:
                processing_error = e
                print(f"❌ Failed to process batched message: {e}")
                failure_recorded = _record_ai_processing_failure(
                    user_id,
                    _client_id_for_processing_failure(user_id, thread_id),
                    thread_id,
                    processed_key,
                    str(e),
                    graph_message_id=last_msg.get("id"),
                    internet_message_id=last_msg.get("internetMessageId"),
                    source_message_key=processed_key,
                )
                if failure_recorded is not True:
                    failure_visibility_lost += 1
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
                _clear_ai_processing_failure(
                    user_id,
                    thread_id,
                    processed_key,
                    graph_message_id=msg.get("id"),
                    internet_message_id=msg.get("internetMessageId"),
                    source_message_key=processed_key,
                )
            except Exception as e:
                processing_error = e
                print(f"❌ Failed to process message {msg.get('id', 'unknown')}: {e}")
                failure_recorded = _record_ai_processing_failure(
                    user_id,
                    _client_id_for_processing_failure(user_id, thread_id),
                    thread_id,
                    processed_key,
                    str(e),
                    graph_message_id=msg.get("id"),
                    internet_message_id=msg.get("internetMessageId"),
                    source_message_key=processed_key,
                )
                if failure_recorded is not True:
                    failure_visibility_lost += 1
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
            failure_recorded = _record_ai_processing_failure(
                user_id,
                "unknown",
                "orphan",
                processed_key,
                str(e),
                graph_message_id=msg.get("id"),
                internet_message_id=msg.get("internetMessageId"),
                source_message_key=processed_key,
            )
            if failure_recorded is not True:
                failure_visibility_lost += 1
        finally:
            if _should_mark_processed_after_error(processing_error):
                mark_processed(user_id, processed_key)
            else:
                print(f"🔁 Leaving orphan message retryable: {processed_key}")

        # Rate limit delay between orphan messages (skip delay after last one)
        if idx < len(orphan_messages) - 1:
            time.sleep(RATE_LIMIT_DELAY)

    if failure_visibility_lost:
        return {
            "status": "error",
            "operation": "inbox_scan",
            "error": (
                "One or more processing failures could not be durably recorded"
            ),
            "failureVisibilityLost": failure_visibility_lost,
            "scanned": scanned_count,
            "processed": processed_count,
            "batched": batched_count,
            "skipped": skipped_count,
            "orphaned": len(orphan_messages),
        }

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


def _strict_thread_index_read(
    user_id: str,
    *,
    collection_name: str,
    document_id: str,
) -> str | None:
    snapshot = (
        _fs.collection("users")
        .document(user_id)
        .collection(collection_name)
        .document(document_id)
        .get()
    )
    if snapshot.exists is False:
        return None
    if snapshot.exists is not True:
        raise SourceCoordinatorRetryable(
            "exact-source thread index returned ambiguous existence"
        )
    data = snapshot.to_dict()
    if (
        type(data) is not dict
        or type(data.get("threadId")) is not str
        or not data["threadId"]
    ):
        raise SourceCoordinatorRetryable(
            "exact-source thread index is malformed"
        )
    return data["threadId"]


def _strict_lookup_thread_by_message_id(
    user_id: str,
    message_id: str,
) -> str | None:
    normalized_message_id = normalize_message_id(message_id)
    legacy_raw_message_id = (message_id or "").strip()
    candidates = []
    for candidate in (normalized_message_id, legacy_raw_message_id):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        thread_id = _strict_thread_index_read(
            user_id,
            collection_name="msgIndex",
            document_id=b64url_id(candidate),
        )
        if thread_id is not None:
            return thread_id
    return None


def _match_message_to_thread(
    user_id: str,
    msg: dict,
    headers: dict,
    *,
    strict: bool = False,
) -> Optional[str]:
    """
    Try to match an inbox message to an existing thread.
    Returns thread_id if found, None after an authoritative no-match.

    The enforced scanner sets ``strict=True`` so unreadable or malformed
    message headers cannot collapse into the same result as a confirmed
    untracked message.
    """
    if type(strict) is not bool:
        raise SourceCoordinatorConfigError(
            "strict inbox thread matching must be a boolean"
        )
    # Get headers if not present
    internet_message_headers = msg.get("internetMessageHeaders")
    needs_header_hydration = (
        "internetMessageHeaders" not in msg
        if strict
        else not internet_message_headers
    )
    if needs_header_hydration:
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    "https://graph.microsoft.com/v1.0/me/messages/"
                    f"{_graph_message_path_segment(msg.get('id'))}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            payload = response.json()
            if strict and (
                type(payload) is not dict
                or "internetMessageHeaders" not in payload
                or type(payload["internetMessageHeaders"]) is not list
            ):
                raise SourceCoordinatorRetryable(
                    "exact-source Graph header hydration was not authoritative"
                )
            internet_message_headers = payload.get("internetMessageHeaders", [])
        except Exception as header_error:
            if strict:
                if isinstance(header_error, SourceCoordinatorRetryable):
                    raise
                raise SourceCoordinatorRetryable(
                    "exact-source Graph header hydration failed during matching"
                ) from header_error
            internet_message_headers = []

    if strict and (
        type(internet_message_headers) is not list
        or any(
            type(header) is not dict
            or type(header.get("name")) is not str
            or type(header.get("value")) is not str
            for header in internet_message_headers
        )
    ):
        raise SourceCoordinatorRetryable(
            "exact-source Graph headers are malformed during matching"
        )

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

    try:
        message_index_lookup = (
            _strict_lookup_thread_by_message_id
            if strict
            else lookup_thread_by_message_id
        )

        # Try In-Reply-To first
        if in_reply_to:
            thread_id = message_index_lookup(user_id, in_reply_to)
            if thread_id:
                return thread_id

        # Try References (newest to oldest)
        if references:
            for ref in reversed(references):
                ref = normalize_message_id(ref)
                thread_id = message_index_lookup(user_id, ref)
                if thread_id:
                    return thread_id

        # Fallback to conversation ID
        if conversation_id:
            if strict:
                thread_id = _strict_thread_index_read(
                    user_id,
                    collection_name="convIndex",
                    document_id=conversation_id,
                )
            else:
                thread_id = lookup_thread_by_conversation_id(
                    user_id,
                    conversation_id,
                )
            if thread_id:
                return thread_id
    except Exception as lookup_error:
        if strict:
            raise SourceCoordinatorRetryable(
                "exact-source thread authority lookup failed"
            ) from lookup_error
        raise

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
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{_graph_message_path_segment(msg.get('id'))}",
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
