import hashlib
from typing import Optional, List, Dict, Any
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from .clients import _fs

def write_notification(uid: str, client_id: str, *, kind: str, priority: str, email: str, 
                      thread_id: str, row_number: int = None, row_anchor: str = None, 
                      meta: dict = None, dedupe_key: str = None) -> str:
    """
    Write notification and bump counters atomically.
    Returns the notification document ID.
    """
    try:
        # Use dedupe_key as doc ID if provided
        if dedupe_key:
            doc_id = hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()
        else:
            doc_id = None  # Let Firestore auto-generate
        
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

        @_fs.transactional
        def update_with_counters(transaction):
            # READS FIRST
            client_snapshot = client_ref.get(transaction=transaction)

            # Dedupe check must also be a read before any WRITE
            if dedupe_key:
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
    applied_updates: list[dict],
    notes: str | None = None,
):
    """
    UPDATED: Writes one notification doc per applied field change.
    Also updates summary on the client doc for quick dashboards.
    """
    try:
        # Write one notification per applied update
        for update in applied_updates:
            dedupe_key = f"{thread_id}:{update.get('range', '')}:{update.get('column', '')}:{update.get('newValue', '')}"
            
            write_notification(
                uid, client_id,
                kind="sheet_update",
                priority="normal",
                email=email,
                thread_id=thread_id,
                row_number=None,  # Could extract from range if needed
                row_anchor=None,
                meta={
                    "column": update.get("column", ""),
                    "oldValue": update.get("oldValue", ""),
                    "newValue": update.get("newValue", ""),
                    "reason": update.get("reason", ""),
                    "confidence": update.get("confidence", 0.0)
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