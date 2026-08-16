import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from .clients import _fs
from google.cloud import firestore
from .manual_reply import (
    canonical_email_address,
    canonical_graph_message_id,
    is_canonical_document_id,
    manual_reply_authority_key,
    normalize_internet_message_id,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManualReplySource:
    """Trusted notification-time inputs for one possible manual reply."""

    graph_lookup_message_id: str
    immutable_graph_message_id: Optional[str]
    internet_message_id: str
    conversation_id: str
    authenticated_mailbox_address: str
    from_addresses: Tuple[str, ...]
    sender_addresses: Tuple[str, ...]
    reply_to_addresses: Tuple[str, ...]
    to_addresses: Tuple[str, ...]
    cc_addresses: Tuple[str, ...]
    bcc_addresses: Tuple[str, ...]


def _canonical_opaque(value: object, maximum_bytes: int = 2048) -> Optional[str]:
    if type(value) is not str or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if not 1 <= len(encoded) <= maximum_bytes:
        return None
    if value != value.strip(" \t\r\n"):
        return None
    if any(unicodedata.category(character) == "Cc" for character in value):
        return None
    return value


def _canonical_address_tuple(values: object) -> Optional[Tuple[str, ...]]:
    if type(values) is not tuple:
        return None
    try:
        canonical = tuple(canonical_email_address(value) for value in values)
    except (TypeError, ValueError):
        return None
    return canonical


def _manual_reply_authority_document(
    *,
    uid: str,
    client_id: str,
    thread_id: str,
    notification_id: str,
    kind: str,
    email: str,
    meta: object,
    source: Optional[ManualReplySource],
) -> Optional[Dict[str, Any]]:
    if kind != "action_needed" or source is None or type(meta) is not dict:
        return None

    reason = meta.get("reason")
    if (
        type(reason) is not str
        or not reason.startswith("needs_user_input:")
        or not reason.removeprefix("needs_user_input:")
    ):
        return None
    if not is_canonical_document_id(thread_id):
        return None

    conversation_id = _canonical_opaque(source.conversation_id)
    try:
        graph_lookup_message_id = canonical_graph_message_id(
            source.graph_lookup_message_id,
            "graph_lookup_message_id",
        )
        immutable_graph_message_id = source.immutable_graph_message_id
        if immutable_graph_message_id is not None:
            immutable_graph_message_id = canonical_graph_message_id(
                immutable_graph_message_id,
                "immutable_graph_message_id",
            )
    except (TypeError, ValueError):
        return None
    if conversation_id is None:
        return None

    try:
        normalized_internet_message_id = normalize_internet_message_id(
            source.internet_message_id
        )
        meta_internet_message_id = normalize_internet_message_id(
            meta.get("sourceInternetMessageId")
        )
    except (TypeError, ValueError):
        return None
    if normalized_internet_message_id != meta_internet_message_id:
        return None

    if any(
        meta.get(field) != graph_lookup_message_id
        for field in (
            "replyToMessageId",
            "sourceMessageId",
            "sourceGraphMessageId",
        )
    ):
        return None

    try:
        authenticated_mailbox = canonical_email_address(
            source.authenticated_mailbox_address,
            "authenticated_mailbox_address",
        )
        notification_recipient = canonical_email_address(
            email,
            "notification_recipient",
        )
    except (TypeError, ValueError):
        return None
    from_addresses = _canonical_address_tuple(source.from_addresses)
    sender_addresses = _canonical_address_tuple(source.sender_addresses)
    reply_to_addresses = _canonical_address_tuple(source.reply_to_addresses)
    to_addresses = _canonical_address_tuple(source.to_addresses)
    cc_addresses = _canonical_address_tuple(source.cc_addresses)
    bcc_addresses = _canonical_address_tuple(source.bcc_addresses)
    if (
        authenticated_mailbox is None
        or notification_recipient is None
        or from_addresses is None
        or sender_addresses is None
        or reply_to_addresses is None
        or to_addresses is None
        or cc_addresses is None
        or bcc_addresses is None
        or len(from_addresses) != 1
        or len(sender_addresses) != 1
        or from_addresses != sender_addresses
        or notification_recipient != from_addresses[0]
        or reply_to_addresses
        or to_addresses != (authenticated_mailbox,)
        or cc_addresses
        or bcc_addresses
    ):
        return None

    authority_doc: Dict[str, Any] = {
        "schemaVersion": 1,
        "status": "eligible",
        "uid": uid,
        "clientId": client_id,
        "threadId": thread_id,
        "notificationId": notification_id,
        "source": "dashboard_inline_reply",
        "graphLookupMessageId": graph_lookup_message_id,
        "normalizedInternetMessageId": normalized_internet_message_id,
        "conversationId": conversation_id,
        "authenticatedMailboxAddress": authenticated_mailbox,
        "fromAddress": from_addresses[0],
        "senderAddress": sender_addresses[0],
        "sourceAudience": {
            "to": list(to_addresses),
            "cc": [],
            "bcc": [],
            "replyTo": [],
        },
        "audience": {
            "to": [notification_recipient],
            "cc": [],
            "bcc": [],
        },
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if immutable_graph_message_id is not None:
        authority_doc["immutableGraphMessageId"] = immutable_graph_message_id
    return authority_doc


def _matches_created_document(
    actual: object,
    expected: Dict[str, Any],
    *,
    timestamp_fields: Tuple[str, ...],
) -> bool:
    if type(actual) is not dict or set(actual) != set(expected):
        return False
    for field, expected_value in expected.items():
        if field in timestamp_fields:
            timestamp = actual.get(field)
            try:
                timezone_aware = (
                    isinstance(timestamp, datetime)
                    and timestamp.tzinfo is not None
                    and timestamp.utcoffset() is not None
                )
            except (OverflowError, ValueError):
                timezone_aware = False
            if not timezone_aware:
                return False
        elif actual.get(field) != expected_value:
            return False
    return True


def _decrement_notification_rollups(client_data: Dict[str, Any], kind: Optional[str]) -> Dict[str, Any]:
    """Return client notification rollups after one notification of kind is resolved."""
    current_data = client_data or {}
    unread_count = max(0, int(current_data.get("notificationsUnread") or 0) - 1)
    new_update_count = int(current_data.get("newUpdateCount") or 0)
    if kind == "sheet_update":
        new_update_count = max(0, new_update_count - 1)

    notif_counts = dict(current_data.get("notifCounts") or {})
    if kind and kind in notif_counts:
        next_count = max(0, int(notif_counts.get(kind) or 0) - 1)
        if next_count:
            notif_counts[kind] = next_count
        else:
            notif_counts.pop(kind, None)

    return {
        "notificationsUnread": unread_count,
        "newUpdateCount": new_update_count,
        "notifCounts": notif_counts,
    }


def delete_notification_and_decrement_counters(uid: str, client_id: str, notification_id: str) -> bool:
    """Delete a notification and keep the parent client rollup counters in sync."""
    client_ref = _fs.collection("users").document(uid).collection("clients").document(client_id)
    notif_ref = client_ref.collection("notifications").document(notification_id)

    @firestore.transactional
    def delete_with_counters(transaction):
        notif_snapshot = notif_ref.get(transaction=transaction)
        if not notif_snapshot.exists:
            return False

        client_snapshot = client_ref.get(transaction=transaction)
        notif_data = notif_snapshot.to_dict() or {}
        client_data = client_snapshot.to_dict() if client_snapshot.exists else {}
        kind = notif_data.get("kind") or notif_data.get("type")

        transaction.delete(notif_ref)
        transaction.set(client_ref, _decrement_notification_rollups(client_data, kind), merge=True)
        return True

    transaction = _fs.transaction()
    return bool(delete_with_counters(transaction))


def extract_row_number_from_update(update: Dict[str, Any]) -> Optional[int]:
    """Extract a Sheet row number from explicit metadata or an A1 notation range."""
    row_number = update.get("rowNumber")
    if row_number:
        try:
            return int(row_number)
        except (TypeError, ValueError):
            pass

    range_value = str(update.get("range") or "")
    if not range_value:
        return None

    range_part = range_value.split("!")[-1]
    match = re.search(r"\$?[A-Z]+\$?(\d+)", range_part)
    if not match:
        return None

    try:
        return int(match.group(1))
    except ValueError:
        return None


def write_notification(
    uid: str,
    client_id: str,
    *,
    kind: str,
    priority: str,
    email: str,
    thread_id: str,
    row_number: int = None,
    row_anchor: str = None,
    meta: dict = None,
    dedupe_key: str = None,
    manual_reply_source: Optional[ManualReplySource] = None,
) -> str:
    """
    Write notification and bump counters atomically.
    Returns the notification document ID.
    """
    if (
        manual_reply_source is not None
        and type(manual_reply_source) is not ManualReplySource
    ):
        raise TypeError("manual_reply_source must be a ManualReplySource")

    try:
        # Use dedupe_key as doc ID if provided
        if dedupe_key:
            doc_id = hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()
        else:
            doc_id = None  # Let Firestore auto-generate
        logger.debug(
            "notification.dedupe_key",
            extra={
                "uid": uid,
                "client_id": client_id,
                "kind": kind,
                "priority": priority,
                "email": email,
                "thread_id": thread_id,
                "row_number": row_number,
                "dedupe_key": dedupe_key,
                "doc_id": doc_id,
            },
        )
        
        client_ref = _fs.collection("users").document(uid).collection("clients").document(client_id)
        # If doc_id is fixed (dedupe), we can safely create a stable ref now
        notif_ref = (client_ref.collection("notifications").document(doc_id)
                     if doc_id else client_ref.collection("notifications").document())

        notification_doc = {
            "kind": kind,
            "priority": priority,
            "email": email,
            "threadId": thread_id,
            "rowNumber": row_number,
            "rowAnchor": row_anchor,
            "createdAt": SERVER_TIMESTAMP,
            "meta": meta or {},
            "dedupeKey": dedupe_key
        }

        authority_doc = _manual_reply_authority_document(
            uid=uid,
            client_id=client_id,
            thread_id=thread_id,
            notification_id=notif_ref.id,
            kind=kind,
            email=email,
            meta=meta,
            source=manual_reply_source,
        )
        if authority_doc is not None and (
            type(dedupe_key) is not str or not dedupe_key
        ):
            raise ValueError("manual_reply_source requires dedupe_key")
        authority_ref = None
        if authority_doc is not None:
            authority_id = manual_reply_authority_key(
                uid=uid,
                client_id=client_id,
                notification_id=notif_ref.id,
            )
            notification_doc["manualReplyAuthorityKey"] = authority_id
            authority_ref = (
                _fs.collection("users")
                .document(uid)
                .collection("manualReplyAuthorities")
                .document(authority_id)
            )

        @firestore.transactional
        def update_with_counters(transaction):
            # READS FIRST
            client_snapshot = client_ref.get(transaction=transaction)

            # A qualifying notification and its server authority are one
            # idempotent pair. Any partial or drifting pair fails closed.
            if authority_ref is not None:
                notif_snapshot = notif_ref.get(transaction=transaction)
                authority_snapshot = authority_ref.get(transaction=transaction)
                if notif_snapshot.exists or authority_snapshot.exists:
                    notification_data = (
                        notif_snapshot.to_dict() or {}
                        if notif_snapshot.exists
                        else {}
                    )
                    authority_data = (
                        authority_snapshot.to_dict() or {}
                        if authority_snapshot.exists
                        else {}
                    )
                    notification_matches = (
                        notif_snapshot.exists
                        and _matches_created_document(
                            notification_data,
                            notification_doc,
                            timestamp_fields=("createdAt",),
                        )
                    )
                    authority_matches = (
                        authority_snapshot.exists
                        and _matches_created_document(
                            authority_data,
                            authority_doc,
                            timestamp_fields=("createdAt", "updatedAt"),
                        )
                    )
                    one_commit = (
                        notification_data.get("createdAt")
                        == authority_data.get("createdAt")
                        == authority_data.get("updatedAt")
                    )
                    if (
                        not notification_matches
                        or not authority_matches
                        or not one_commit
                    ):
                        raise ValueError("manual reply authority conflict")
                    print(f"📋 Skipped duplicate notification: {dedupe_key}")
                    return notif_ref.id  # No-op
            elif dedupe_key:
                # Preserve the legacy notification-only dedupe behavior.
                notif_snapshot = notif_ref.get(transaction=transaction)
                if notif_snapshot.exists:
                    print(f"📋 Skipped duplicate notification: {dedupe_key}")
                    return notif_ref.id  # No-op

            current_data = client_snapshot.to_dict() if client_snapshot.exists else {}
            unread_count = (current_data.get("notificationsUnread") or 0) + 1
            new_update_count = (current_data.get("newUpdateCount") or 0)
            notif_counts = dict(current_data.get("notifCounts") or {})

            if kind == "sheet_update":
                new_update_count += 1
            notif_counts[kind] = notif_counts.get(kind, 0) + 1

            # WRITES AFTER ALL READS
            transaction.set(notif_ref, notification_doc)
            if authority_ref is not None:
                transaction.set(authority_ref, authority_doc)
            transaction.set(
                client_ref,
                {
                    "notificationsUnread": unread_count,
                    "newUpdateCount": new_update_count,
                    "notifCounts": notif_counts
                },
                merge=True
            )
            return notif_ref.id

        transaction = _fs.transaction()
        created_id = update_with_counters(transaction)
        print(f"📋 Created {kind} notification for {client_id}: {created_id}")
        return created_id

    except Exception as e:
        print(f"❌ Failed to write notification: {e}")
        raise

def add_client_notifications(
    uid: str,
    client_id: str,
    email: str,
    thread_id: str,
    applied_updates: List[dict],
    notes: Optional[str] = None,
    address: Optional[str] = None,
):
    """
    UPDATED: Writes one notification doc per applied field change.
    Also updates summary on the client doc for quick dashboards.
    """
    try:
        # Write one notification per applied update
        for update in applied_updates:
            row_number = extract_row_number_from_update(update)
            dedupe_key = f"{thread_id}:{update.get('range', '')}:{update.get('column', '')}:{update.get('newValue', '')}"
            logger.debug(
                "notification.dedupe_key",
                extra={
                    "uid": uid,
                    "client_id": client_id,
                    "kind": "sheet_update",
                    "email": email,
                    "thread_id": thread_id,
                    "range": update.get("range", ""),
                    "column": update.get("column", ""),
                    "new_value": update.get("newValue", ""),
                    "dedupe_key": dedupe_key,
                },
            )

            write_notification(
                uid, client_id,
                kind="sheet_update",
                priority="normal",
                email=email,
                thread_id=thread_id,
                row_number=row_number,
                row_anchor=address,
                meta={
                    "column": update.get("column", ""),
                    "oldValue": update.get("oldValue", ""),
                    "newValue": update.get("newValue", ""),
                    "reason": update.get("reason", ""),
                    "confidence": update.get("confidence", 0.0),
                    "address": address or "",
                    "rowNumber": row_number,
                },
                dedupe_key=dedupe_key
            )

        # Legacy summary on client doc
        if applied_updates:
            base_ref = _fs.collection("users").document(uid)
            client_ref = base_ref.collection("clients").document(client_id)
            
            summary_items = [f"{u['column']}='{u['newValue']}'" for u in applied_updates]
            summary = f"Updated {', '.join(summary_items)} for {email}"

            client_ref.set({
                "lastNotificationSummary": summary,
                "lastNotificationAt": SERVER_TIMESTAMP,
            }, merge=True)

            print(f"📢 Created {len(applied_updates)} sheet_update notifications for client {client_id}")

    except Exception as e:
        print(f"❌ Failed to write client notifications: {e}")
