"""Durable, projection-only review records for policy-blocked inbox replies.

This module deliberately owns no provider or outbox capability.  It converts a
deterministic local policy outcome into one atomic operator-visible Firestore
projection while keeping all autonomous continuation paused.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP


# Kept overrideable for hermetic tests; production resolves the shared client
# lazily so importing this pure contract module never initializes credentials.
_fs = None


POLICY_BLOCK_FAILURE_CODE = "blocked_auto_reply_policy"
PROJECTION_ONLY_MODE = "projection_only"
REPLY_REVIEW_RECORD_TYPE = "reply_review"
REPLY_REVIEW_SCHEMA_VERSION = 1
REPLY_REVIEW_STATUS = "needs_review"
REPLY_REVIEW_ID_PREFIX = "blocked-auto-reply:v1"
REPLY_REVIEW_NOTIFICATION_PREFIX = "reply-review-required:v1"

MAX_FIRESTORE_ID_LENGTH = 1_500
MAX_RECIPIENT_LENGTH = 320
MAX_SUBJECT_LENGTH = 998
MAX_RESPONSE_BODY_LENGTH = 100_000
MAX_TERMINAL_REASON_LENGTH = 500


@dataclass(frozen=True)
class ReplyReviewProjection:
    review_id: str
    notification_id: str
    status: str


class ReplyReviewProjectionError(RuntimeError):
    """Raised when a review cannot be projected without losing safety."""


class ReplyReviewConflict(ReplyReviewProjectionError):
    """Raised when a stable review identity points at different content."""


def _required_string(name: str, value: Any, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length {max_length}")
    return value


def _optional_string(name: str, value: Any, max_length: int) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length {max_length}")
    return value or None


def _terminal_disposition(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("terminal_disposition must be a mapping or None")
    if set(value) != {"status", "reason", "rowNumber"}:
        raise ValueError(
            "terminal_disposition must contain exactly status, reason, and rowNumber"
        )
    if value.get("status") != "completed":
        raise ValueError("terminal_disposition status must be completed")
    reason = _required_string(
        "terminal_disposition.reason",
        value.get("reason"),
        MAX_TERMINAL_REASON_LENGTH,
    )
    row_number = value.get("rowNumber")
    if isinstance(row_number, bool) or not isinstance(row_number, int) or row_number < 1:
        raise ValueError("terminal_disposition.rowNumber must be a positive integer")
    return {"status": "completed", "reason": reason, "rowNumber": row_number}


def build_policy_blocked_reply_review_id(
    *,
    thread_id: str,
    source_message_id: str,
) -> str:
    thread_id = _required_string("thread_id", thread_id, MAX_FIRESTORE_ID_LENGTH)
    source_message_id = _required_string(
        "source_message_id", source_message_id, MAX_FIRESTORE_ID_LENGTH
    )
    identity = f"{REPLY_REVIEW_ID_PREFIX}\n{thread_id}\n{source_message_id}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_policy_blocked_reply_review_notification_id(review_id: str) -> str:
    review_id = _required_string("review_id", review_id, MAX_FIRESTORE_ID_LENGTH)
    dedupe_key = f"{REPLY_REVIEW_NOTIFICATION_PREFIX}\n{review_id}"
    return hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()


def build_policy_blocked_reply_intent_hash(intent: Mapping[str, Any]) -> str:
    if not isinstance(intent, Mapping):
        raise ValueError("intent must be a mapping")
    try:
        canonical = json.dumps(
            dict(intent),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("intent must be JSON-serializable") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_intent(
    *,
    user_id: Any,
    client_id: Any,
    thread_id: Any,
    source_message_id: Any,
    recipient: Any,
    response_body: Any,
    subject: Any,
    conversation_id: Any,
    terminal_disposition: Any,
) -> Dict[str, Any]:
    user_id = _required_string("user_id", user_id, MAX_FIRESTORE_ID_LENGTH)
    client_id = _required_string("client_id", client_id, MAX_FIRESTORE_ID_LENGTH)
    thread_id = _required_string("thread_id", thread_id, MAX_FIRESTORE_ID_LENGTH)
    source_message_id = _required_string(
        "source_message_id", source_message_id, MAX_FIRESTORE_ID_LENGTH
    )
    recipient = _required_string("recipient", recipient, MAX_RECIPIENT_LENGTH)
    response_body = _required_string(
        "response_body", response_body, MAX_RESPONSE_BODY_LENGTH
    )
    subject = _optional_string("subject", subject, MAX_SUBJECT_LENGTH)
    conversation_id = _optional_string(
        "conversation_id", conversation_id, MAX_FIRESTORE_ID_LENGTH
    )
    terminal_disposition = _terminal_disposition(terminal_disposition)
    return {
        "clientId": client_id,
        "conversationId": conversation_id,
        "recipient": recipient,
        "responseBody": response_body,
        "sourceMessageId": source_message_id,
        "subject": subject,
        "terminalDisposition": terminal_disposition,
        "threadId": thread_id,
    }


def _build_review_document(
    *,
    review_id: str,
    notification_id: str,
    intent: Mapping[str, Any],
    intent_hash: str,
) -> Dict[str, Any]:
    return {
        "recordType": REPLY_REVIEW_RECORD_TYPE,
        "schemaVersion": REPLY_REVIEW_SCHEMA_VERSION,
        "reviewId": review_id,
        "failureCode": POLICY_BLOCK_FAILURE_CODE,
        "status": REPLY_REVIEW_STATUS,
        "recoveryStatus": REPLY_REVIEW_STATUS,
        "manualActionRequired": True,
        "automaticRetryAllowed": False,
        "alreadySent": False,
        "source": "autoResponse",
        "clientId": intent["clientId"],
        "threadId": intent["threadId"],
        "sourceMessageId": intent["sourceMessageId"],
        "conversationId": intent["conversationId"],
        "recipient": intent["recipient"],
        "subject": intent["subject"],
        "responseBody": intent["responseBody"],
        "terminalDisposition": deepcopy(intent["terminalDisposition"]),
        "draftVersion": 1,
        "intentHash": intent_hash,
        "notificationId": notification_id,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }


def _build_notification_document(
    *,
    review_id: str,
    intent: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "kind": "action_needed",
        "priority": "important",
        "threadId": intent["threadId"],
        "meta": {
            "reason": "reply_review_required",
            "failureCode": POLICY_BLOCK_FAILURE_CODE,
            "reviewActionMode": PROJECTION_ONLY_MODE,
            "reviewId": review_id,
            "sourceMessageId": intent["sourceMessageId"],
            "suggestedEmail": {
                "to": [intent["recipient"]],
                "subject": intent["subject"],
                "body": intent["responseBody"],
            },
        },
        "createdAt": SERVER_TIMESTAMP,
    }


def _existing_review_matches(
    current: Mapping[str, Any],
    *,
    review_id: str,
    notification_id: str,
    intent_hash: str,
) -> bool:
    return (
        current.get("recordType") == REPLY_REVIEW_RECORD_TYPE
        and current.get("schemaVersion") == REPLY_REVIEW_SCHEMA_VERSION
        and current.get("reviewId") == review_id
        and current.get("failureCode") == POLICY_BLOCK_FAILURE_CODE
        and current.get("intentHash") == intent_hash
        and current.get("notificationId") == notification_id
    )


def _existing_notification_matches(
    current: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    return (
        current.get("kind") == expected["kind"]
        and current.get("priority") == expected["priority"]
        and current.get("threadId") == expected["threadId"]
        and current.get("meta") == expected["meta"]
    )


def create_policy_blocked_reply_review(
    *,
    user_id: str,
    client_id: str,
    thread_id: str,
    source_message_id: str,
    recipient: str,
    response_body: str,
    subject: Optional[str] = None,
    conversation_id: Optional[str] = None,
    terminal_disposition: Optional[Mapping[str, Any]] = None,
) -> ReplyReviewProjection:
    """Create one policy-blocked review and notification transactionally."""
    user_id = _required_string("user_id", user_id, MAX_FIRESTORE_ID_LENGTH)
    intent = _validated_intent(
        user_id=user_id,
        client_id=client_id,
        thread_id=thread_id,
        source_message_id=source_message_id,
        recipient=recipient,
        response_body=response_body,
        subject=subject,
        conversation_id=conversation_id,
        terminal_disposition=terminal_disposition,
    )
    review_id = build_policy_blocked_reply_review_id(
        thread_id=intent["threadId"],
        source_message_id=intent["sourceMessageId"],
    )
    notification_id = build_policy_blocked_reply_review_notification_id(review_id)
    intent_hash = build_policy_blocked_reply_intent_hash(intent)
    review_document = _build_review_document(
        review_id=review_id,
        notification_id=notification_id,
        intent=intent,
        intent_hash=intent_hash,
    )
    notification_document = _build_notification_document(
        review_id=review_id,
        intent=intent,
    )

    firestore_client = _fs
    if firestore_client is None:
        from .clients import _fs as firestore_client

    user_ref = firestore_client.collection("users").document(user_id)
    client_ref = user_ref.collection("clients").document(intent["clientId"])
    thread_ref = user_ref.collection("threads").document(intent["threadId"])
    review_ref = user_ref.collection("deadLetterQueue").document(review_id)
    notification_ref = client_ref.collection("notifications").document(notification_id)

    @firestore.transactional
    def project(transaction):
        client_snapshot = client_ref.get(transaction=transaction)
        thread_snapshot = thread_ref.get(transaction=transaction)
        review_snapshot = review_ref.get(transaction=transaction)
        notification_snapshot = notification_ref.get(transaction=transaction)

        if not client_snapshot.exists:
            raise ReplyReviewProjectionError("reply review client does not exist")
        if not thread_snapshot.exists:
            raise ReplyReviewProjectionError("reply review thread does not exist")

        thread_data = thread_snapshot.to_dict() or {}
        bound_client_id = thread_data.get("clientId")
        if bound_client_id != intent["clientId"]:
            raise ReplyReviewConflict("reply review thread belongs to a different client")

        if review_snapshot.exists or notification_snapshot.exists:
            if not review_snapshot.exists or not notification_snapshot.exists:
                raise ReplyReviewConflict(
                    "reply review projection is incomplete or uses a conflicting identity"
                )
            if not _existing_review_matches(
                review_snapshot.to_dict() or {},
                review_id=review_id,
                notification_id=notification_id,
                intent_hash=intent_hash,
            ):
                raise ReplyReviewConflict("reply review identity has a different intent")
            if not _existing_notification_matches(
                notification_snapshot.to_dict() or {}, notification_document
            ):
                raise ReplyReviewConflict("reply review notification conflicts")
            return ReplyReviewProjection(review_id, notification_id, "existing")

        client_data = client_snapshot.to_dict() or {}
        notif_counts = dict(client_data.get("notifCounts") or {})
        notif_counts["action_needed"] = int(notif_counts.get("action_needed") or 0) + 1
        client_rollups = {
            "notificationsUnread": int(client_data.get("notificationsUnread") or 0) + 1,
            "newUpdateCount": int(client_data.get("newUpdateCount") or 0),
            "notifCounts": notif_counts,
        }
        thread_pause = {
            "status": "action_needed",
            "statusReason": POLICY_BLOCK_FAILURE_CODE,
            "followUpStatus": "stopped",
            "followUpConfig.enabled": False,
            "followUpConfig.nextFollowUpAt": None,
            "followUpConfig.processingBy": None,
            "followUpConfig.processingAt": None,
            "updatedAt": SERVER_TIMESTAMP,
        }

        transaction.set(review_ref, review_document)
        transaction.set(notification_ref, notification_document)
        transaction.set(client_ref, client_rollups, merge=True)
        transaction.update(thread_ref, thread_pause)
        return ReplyReviewProjection(review_id, notification_id, "created")

    try:
        return project(firestore_client.transaction())
    except ReplyReviewProjectionError:
        raise
    except Exception as exc:
        raise ReplyReviewProjectionError(
            "policy-blocked reply review projection failed"
        ) from exc
