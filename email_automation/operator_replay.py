"""Exact, lease-guarded operator replay for one failed Baylor/BP21 message."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote
from uuid import uuid4

import requests
from google.cloud.firestore import SERVER_TIMESTAMP

from .campaign_safety import get_client_automation_decision
from .sent_mail_guard import coerce_utc_datetime, sent_after_from_retry_data
from .utils import (
    b64url_id,
    exponential_backoff_request,
    normalize_message_id,
    parse_references_header,
)


APPROVED_BAYLOR_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
APPROVED_OPERATOR_RECIPIENT = "baylor.freelance@outlook.com"
APPROVED_BP21_LOCAL_PART = "bp21harrison"
APPROVED_BP21_DOMAIN = "gmail.com"
ALLOWED_THREAD_STATUSES = {"active", "paused"}


class ReplayRefused(RuntimeError):
    """Raised when any exact-identity or safety preflight does not pass."""


@dataclass(frozen=True)
class ReplayRequest:
    uid: str
    client_id: str
    thread_id: str
    graph_message_id: str
    internet_message_id: str
    sender: str
    operator_recipient: str


@dataclass(frozen=True)
class ReplayResult:
    status: str
    applied: bool
    uid: str
    client_id: str
    thread_id: str
    graph_message_id: str
    internet_message_id: str
    sender: str
    operator_recipient: str
    conversation_id: str
    failure_id: str
    client_status: str
    thread_status: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_email(value: Any) -> str:
    return _clean(value).lower()


def _is_bp21_sender(value: Any) -> bool:
    email = _normalize_email(value)
    if email.count("@") != 1:
        return False
    local_part, domain = email.split("@", 1)
    return domain == APPROVED_BP21_DOMAIN and (
        local_part == APPROVED_BP21_LOCAL_PART
        or local_part.startswith(f"{APPROVED_BP21_LOCAL_PART}+")
    )


def _is_legacy_asset_failure(failure: Dict[str, Any]) -> bool:
    return _clean(failure.get("reason")).startswith(
        "Broker asset extraction failed for "
    )


def validate_approved_lane(request: ReplayRequest) -> None:
    """Reject any request outside the initial Baylor/BP21 recovery lane."""
    if _clean(request.uid) != APPROVED_BAYLOR_UID:
        raise ReplayRefused("Replay requires the approved Baylor UID")
    if not _is_bp21_sender(request.sender):
        raise ReplayRefused("Replay requires an exact BP21 sender address")
    if _normalize_email(request.operator_recipient) != APPROVED_OPERATOR_RECIPIENT:
        raise ReplayRefused("Replay requires the approved Baylor operator recipient")

    required = {
        "client": request.client_id,
        "thread": request.thread_id,
        "Graph message": request.graph_message_id,
        "RFC internet message": request.internet_message_id,
    }
    for label, value in required.items():
        if not _clean(value):
            raise ReplayRefused(f"Replay requires an exact {label} ID")


def _email_address(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_email(value)
    if isinstance(value, dict):
        return _normalize_email(((value.get("emailAddress") or {}).get("address")))
    return ""


def _email_addresses(values: Any) -> list[str]:
    addresses = []
    for value in values or []:
        address = _email_address(value)
        if address and address not in addresses:
            addresses.append(address)
    return addresses


def _fetch_exact_graph_message(
    headers: Dict[str, str], graph_message_id: str
) -> Dict[str, Any]:
    encoded_id = quote(_clean(graph_message_id), safe="")
    response = exponential_backoff_request(
        lambda: requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages/{encoded_id}",
            headers=headers,
            params={
                "$select": (
                    "id,subject,from,sender,replyTo,toRecipients,ccRecipients,"
                    "receivedDateTime,sentDateTime,conversationId,internetMessageId,"
                    "internetMessageHeaders,body,bodyPreview,hasAttachments"
                )
            },
            timeout=30,
        )
    )
    if response.status_code != 200:
        raise ReplayRefused(
            f"Exact Graph message fetch failed with HTTP {response.status_code}"
        )
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise ReplayRefused("Exact Graph message fetch returned an invalid payload")
    return payload


def _user_ref(fs_client, uid: str):
    return fs_client.collection("users").document(uid)


def _processed_ref(fs_client, uid: str, message_id: str):
    return (
        _user_ref(fs_client, uid)
        .collection("processedMessages")
        .document(b64url_id(message_id))
    )


def _failure_ref(fs_client, request: ReplayRequest):
    failure_id = f"{request.thread_id}__{request.internet_message_id}"
    return (
        _user_ref(fs_client, request.uid)
        .collection("processingFailures")
        .document(failure_id)
    )


def _read_required(ref, label: str) -> Dict[str, Any]:
    snapshot = ref.get()
    if not getattr(snapshot, "exists", False):
        raise ReplayRefused(f"Exact {label} does not exist")
    data = snapshot.to_dict()
    if not isinstance(data, dict):
        raise ReplayRefused(f"Exact {label} is malformed")
    return data


def _validate_graph_identity(
    request: ReplayRequest, message: Dict[str, Any]
) -> None:
    if _clean(message.get("id")) != request.graph_message_id:
        raise ReplayRefused("Graph message ID does not match the requested ID")
    if _clean(message.get("internetMessageId")) != request.internet_message_id:
        raise ReplayRefused("RFC internet message ID does not match the requested ID")

    from_address = _email_address(message.get("from"))
    sender_address = _email_address(message.get("sender"))
    expected_sender = _normalize_email(request.sender)
    if from_address != expected_sender:
        raise ReplayRefused("Graph sender does not match the exact requested sender")
    if sender_address and sender_address != expected_sender:
        raise ReplayRefused("Graph envelope sender does not match the exact sender")

    to_recipients = _email_addresses(message.get("toRecipients"))
    expected_operator = _normalize_email(request.operator_recipient)
    if to_recipients != [expected_operator]:
        raise ReplayRefused("Graph recipient does not match the exact operator recipient")

    cc_recipients = _email_addresses(message.get("ccRecipients"))
    if cc_recipients:
        raise ReplayRefused("Graph message has recipients outside the exact safe lane")

    reply_to = _email_addresses(message.get("replyTo"))
    if reply_to and reply_to != [expected_sender]:
        raise ReplayRefused("Graph reply-to does not match the exact BP21 sender")


def _validate_failure(request: ReplayRequest, failure: Dict[str, Any]) -> None:
    expected = {
        "clientId": request.client_id,
        "threadId": request.thread_id,
        "messageId": request.internet_message_id,
    }
    for field, value in expected.items():
        if _clean(failure.get(field)) != value:
            raise ReplayRefused(f"Exact failure {field} does not match the request")
    recorded_graph_id = _clean(failure.get("graphMessageId"))
    if not recorded_graph_id and not _is_legacy_asset_failure(failure):
        raise ReplayRefused("Exact failure is missing its Graph message ID")
    if recorded_graph_id and recorded_graph_id != request.graph_message_id:
        raise ReplayRefused("Exact failure Graph message ID does not match the request")
    if failure.get("retryable") is not True:
        raise ReplayRefused("Exact failure is not currently retryable")
    if _clean(failure.get("recoveryStatus")).startswith("blocked_"):
        raise ReplayRefused("Exact failure is already blocked for manual review")


def _validate_thread_and_indexes(
    fs_client,
    request: ReplayRequest,
    thread: Dict[str, Any],
    message: Dict[str, Any],
) -> str:
    if _clean(thread.get("clientId")) != request.client_id:
        raise ReplayRefused("Exact thread does not belong to the requested client")
    thread_status = _clean(thread.get("status")).lower()
    if thread_status not in ALLOWED_THREAD_STATUSES:
        raise ReplayRefused(
            f"Exact thread state is not replayable: {thread_status or 'unknown'}"
        )

    participants = thread.get("email") or []
    if isinstance(participants, str):
        participants = [participants]
    normalized_participants = {
        _normalize_email(value) for value in participants if _normalize_email(value)
    }
    if normalized_participants and not all(
        _is_bp21_sender(value) for value in normalized_participants
    ):
        raise ReplayRefused(
            "Exact thread participants do not stay inside the BP21 mailbox family"
        )

    canonical_rfc_id = normalize_message_id(request.internet_message_id)
    msg_index = _read_required(
        _user_ref(fs_client, request.uid)
        .collection("msgIndex")
        .document(b64url_id(canonical_rfc_id)),
        "message index",
    )
    if _clean(msg_index.get("threadId")) != request.thread_id:
        raise ReplayRefused("Exact message index points to a different thread")

    conversation_id = _clean(message.get("conversationId"))
    if not conversation_id:
        raise ReplayRefused("Graph message has no exact conversation ID")
    conv_index = _read_required(
        _user_ref(fs_client, request.uid)
        .collection("convIndex")
        .document(conversation_id),
        "conversation index",
    )
    if _clean(conv_index.get("threadId")) != request.thread_id:
        raise ReplayRefused("Exact conversation index points to a different thread")
    return thread_status


def _validate_reply_header_indexes(
    fs_client,
    request: ReplayRequest,
    message: Dict[str, Any],
) -> None:
    headers = {
        _clean(item.get("name")).lower(): _clean(item.get("value"))
        for item in message.get("internetMessageHeaders") or []
        if isinstance(item, dict)
    }
    candidates = []
    in_reply_to = headers.get("in-reply-to")
    if in_reply_to:
        candidates.append(in_reply_to)
    candidates.extend(parse_references_header(headers.get("references", "")))

    for message_id in dict.fromkeys(candidates):
        canonical_id = normalize_message_id(message_id)
        if not canonical_id:
            continue
        snapshot = (
            _user_ref(fs_client, request.uid)
            .collection("msgIndex")
            .document(b64url_id(canonical_id))
            .get()
        )
        if not getattr(snapshot, "exists", False):
            continue
        indexed = snapshot.to_dict() or {}
        if _clean(indexed.get("threadId")) != request.thread_id:
            raise ReplayRefused(
                "A reply header message index points to a different thread"
            )


def _sent_items_search_start(
    failure: Dict[str, Any],
    message: Dict[str, Any],
):
    failure_start = sent_after_from_retry_data(failure)
    received_at = coerce_utc_datetime(message.get("receivedDateTime"))
    if not received_at:
        return failure_start
    return min(failure_start, received_at - timedelta(seconds=30))


def _verify_degraded_asset_postcondition(
    request: ReplayRequest,
    fs_client,
    attempt_started_at: datetime,
) -> bool:
    from .processing import _get_reply_send_outcome

    send_outcome = _get_reply_send_outcome()
    if (
        send_outcome.error
        or send_outcome.sent_but_unindexed
        or send_outcome.outcome != "suppressed_operator_replay_no_send"
    ):
        return False

    freshness_floor = attempt_started_at - timedelta(seconds=5)

    def fresh_matching(collection_name: str, predicate) -> bool:
        snapshots = (
            _user_ref(fs_client, request.uid)
            .collection(collection_name)
            .stream()
        )
        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            updated_at = coerce_utc_datetime(
                data.get("updatedAt") or data.get("createdAt")
            )
            if updated_at and updated_at >= freshness_floor and predicate(data):
                return True
        return False

    warning_is_fresh = fresh_matching(
        "assetWarnings",
        lambda data: (
            _clean(data.get("clientId")) == request.client_id
            and _clean(data.get("threadId")) == request.thread_id
            and _clean(data.get("messageId")) == request.internet_message_id
            and data.get("status") == "degraded_text_processed"
        ),
    )
    sheet_evidence_is_fresh = fresh_matching(
        "sheetChangeLog",
        lambda data: (
            _clean(data.get("clientId")) == request.client_id
            and _clean(data.get("threadId")) == request.thread_id
            and data.get("status") == "applied"
            and isinstance(data.get("applied"), dict)
        ),
    )
    return warning_is_fresh and sheet_evidence_is_fresh


def _begin_replay_claim(fs_client, request: ReplayRequest, failure_ref):
    attempt_id = uuid4().hex
    started_at = datetime.now(timezone.utc)
    batch = fs_client.batch()
    for message_id in (request.graph_message_id, request.internet_message_id):
        batch.set(
            _processed_ref(fs_client, request.uid, message_id),
            {
                "status": "operator_replay_in_progress",
                "replayAttemptId": attempt_id,
                "claimedAt": SERVER_TIMESTAMP,
            },
            merge=False,
        )
    batch.set(
        failure_ref,
        {
            "recoveryStatus": "operator_replay_in_progress",
            "replayAttemptId": attempt_id,
            "graphMessageId": request.graph_message_id,
            "replayStartedAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        },
        merge=True,
    )
    batch.commit()
    return attempt_id, started_at


def _complete_replay_claim(
    fs_client,
    request: ReplayRequest,
    failure_ref,
    attempt_id: str,
) -> None:
    batch = fs_client.batch()
    for message_id in (request.graph_message_id, request.internet_message_id):
        batch.set(
            _processed_ref(fs_client, request.uid, message_id),
            {
                "status": "processed",
                "replayAttemptId": attempt_id,
                "processedAt": SERVER_TIMESTAMP,
            },
            merge=True,
        )
    batch.delete(failure_ref)
    batch.commit()


def replay_exact_message(
    request: ReplayRequest,
    headers: Dict[str, str],
    *,
    apply: bool = False,
    fs_client=None,
    fetch_message: Optional[Callable[[Dict[str, str], str], Dict[str, Any]]] = None,
    process_message: Optional[
        Callable[[str, Dict[str, str], Dict[str, Any]], Any]
    ] = None,
    find_existing_artifact: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    find_manual_continuation: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    find_recipient_continuation: Optional[
        Callable[..., Optional[Dict[str, Any]]]
    ] = None,
    verify_postcondition: Optional[
        Callable[[ReplayRequest, Any, datetime], bool]
    ] = None,
    lease_runner: Optional[Callable[..., bool]] = None,
) -> ReplayResult:
    """Preflight and optionally process exactly one failed inbox message.

    Dry-run is the default. Both dry-run and apply execute inside the existing
    per-user lease so state cannot change between identity checks and replay.
    """
    validate_approved_lane(request)
    authorization = _clean((headers or {}).get("Authorization"))
    if not authorization.startswith("Bearer ") or len(authorization) <= len("Bearer "):
        raise ReplayRefused("Graph authorization headers are unavailable")

    if fs_client is None:
        from .clients import _fs

        fs_client = _fs
    if fetch_message is None:
        fetch_message = _fetch_exact_graph_message
    if process_message is None:
        from .processing import process_inbox_message

        def process_message(uid, graph_headers, message):
            return process_inbox_message(
                uid,
                graph_headers,
                message,
                allow_outbound_reply=False,
            )
    if find_existing_artifact is None:
        from .processing import _find_existing_retry_artifact_for_message

        find_existing_artifact = _find_existing_retry_artifact_for_message
    if find_manual_continuation is None:
        from .sent_mail_guard import find_sent_conversation_continuation_for_retry

        find_manual_continuation = find_sent_conversation_continuation_for_retry
    if find_recipient_continuation is None:
        from .sent_mail_guard import find_sent_recipient_continuation_for_retry

        find_recipient_continuation = find_sent_recipient_continuation_for_retry
    if verify_postcondition is None:
        verify_postcondition = _verify_degraded_asset_postcondition
    if lease_runner is None:
        from .scheduler_lease import run_with_user_lease

        lease_runner = run_with_user_lease

    result: Optional[ReplayResult] = None

    def _under_lease() -> None:
        nonlocal result

        thread_ref = (
            _user_ref(fs_client, request.uid)
            .collection("threads")
            .document(request.thread_id)
        )
        thread = _read_required(thread_ref, "thread")

        decision = get_client_automation_decision(
            request.uid,
            request.client_id,
            firestore_client=fs_client,
        )
        if not decision.allows_autonomous_work:
            raise ReplayRefused(
                f"Exact client state does not allow replay: {decision.reason or decision.state}"
            )

        failure_ref = _failure_ref(fs_client, request)
        failure = _read_required(
            failure_ref,
            "processing failure for the RFC internet message ID",
        )
        _validate_failure(request, failure)

        for label, message_id in (
            ("Graph", request.graph_message_id),
            ("RFC", request.internet_message_id),
        ):
            if _processed_ref(fs_client, request.uid, message_id).get().exists:
                raise ReplayRefused(f"Exact {label} message is already processed")

        message = fetch_message(headers, request.graph_message_id)
        _validate_graph_identity(request, message)
        thread_status = _validate_thread_and_indexes(
            fs_client, request, thread, message
        )
        _validate_reply_header_indexes(fs_client, request, message)

        existing_artifact = find_existing_artifact(
            request.uid,
            request.thread_id,
            request.internet_message_id,
            request.client_id,
            additional_message_ids=[
                request.graph_message_id,
                request.internet_message_id,
                message.get("conversationId"),
            ],
        )
        if existing_artifact:
            raise ReplayRefused(
                "An existing recovery artifact already targets this exact message"
            )

        sent_after = _sent_items_search_start(failure, message)
        continuation = find_manual_continuation(
            headers,
            conversation_id=message.get("conversationId"),
            sent_after=sent_after,
        )
        if continuation:
            raise ReplayRefused(
                "Sent Items shows a manual continuation or an uncertain continuation state"
            )

        recipient_continuation = find_recipient_continuation(
            headers,
            recipient=request.sender,
            sent_after=sent_after,
        )
        if recipient_continuation:
            raise ReplayRefused(
                "Sent Items shows a newer recipient continuation outside the exact thread"
            )

        failure_id = f"{request.thread_id}__{request.internet_message_id}"
        if apply:
            attempt_id, attempt_started_at = _begin_replay_claim(
                fs_client,
                request,
                failure_ref,
            )
            try:
                process_message(request.uid, headers, message)
            except Exception as exc:
                failure_ref.set(
                    {
                        "recoveryStatus": "operator_replay_failed",
                        "replayErrorClass": type(exc).__name__,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
                raise
            post_process_artifact = find_existing_artifact(
                request.uid,
                request.thread_id,
                request.internet_message_id,
                request.client_id,
                additional_message_ids=[
                    request.graph_message_id,
                    request.internet_message_id,
                    message.get("conversationId"),
                ],
            )
            if post_process_artifact:
                failure_ref.set(
                    {
                        "recoveryStatus": "operator_replay_blocked_artifact",
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
                raise ReplayRefused(
                    "A post-processing artifact requires manual review before completion"
                )
            if not verify_postcondition(request, fs_client, attempt_started_at):
                failure_ref.set(
                    {
                        "recoveryStatus": "operator_replay_blocked_postcondition",
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    merge=True,
                )
                raise ReplayRefused(
                    "Durable degraded-asset replay postcondition was not satisfied"
                )
            _complete_replay_claim(
                fs_client,
                request,
                failure_ref,
                attempt_id,
            )

        result = ReplayResult(
            status="applied" if apply else "verified",
            applied=apply,
            uid=request.uid,
            client_id=request.client_id,
            thread_id=request.thread_id,
            graph_message_id=request.graph_message_id,
            internet_message_id=request.internet_message_id,
            sender=_normalize_email(request.sender),
            operator_recipient=_normalize_email(request.operator_recipient),
            conversation_id=_clean(message.get("conversationId")),
            failure_id=failure_id,
            client_status=_clean(decision.client_data.get("status")).lower(),
            thread_status=thread_status,
        )

    acquired = lease_runner(
        request.uid,
        _under_lease,
        fs_client=fs_client,
        ttl_seconds=30 * 60,
    )
    if not acquired:
        raise ReplayRefused("The existing per-user lease is held; replay refused")
    if result is None:
        raise ReplayRefused("Per-user lease callback did not complete")
    return result


__all__ = [
    "APPROVED_BAYLOR_UID",
    "APPROVED_OPERATOR_RECIPIENT",
    "ReplayRefused",
    "ReplayRequest",
    "ReplayResult",
    "replay_exact_message",
    "validate_approved_lane",
]
