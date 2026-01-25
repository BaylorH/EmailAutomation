#!/usr/bin/env python3
"""
Production Reset Script
=======================
Wipes all operational data from Firebase while preserving:
- User authentication (managed by Firebase Auth)
- User profile (display name, signature, profile pic)
- MSAL OAuth tokens (in Firebase Storage)

Usage:
    export GOOGLE_APPLICATION_CREDENTIALS=~/Downloads/firebase-keys/email-automation-cache-firebase-adminsdk-fbsvc-d27630c820.json
    python scripts/production_reset.py --all-users --confirm
"""

import os
import sys
import argparse
from datetime import datetime

import warnings
warnings.filterwarnings("ignore")

from google.cloud import firestore


def get_firestore_client():
    """Initialize Firestore client."""
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        print("Error: GOOGLE_APPLICATION_CREDENTIALS not set")
        sys.exit(1)
    return firestore.Client()


# Collections to wipe (relative to users/{uid}/)
COLLECTIONS_TO_WIPE = [
    "clients",
    "outbox",
    "threads",
    "msgIndex",
    "convIndex",
    "processedMessages",
    "optedOutContacts",
    "sheetChangeLog",
    "sync",
    "archivedClients",
    "archivedThreads",
    "archivedMsgIndex",
    "archivedConvIndex",
]

NESTED_COLLECTIONS = {
    "clients": ["notifications"],
    "threads": ["messages"],
    "archivedThreads": ["messages"],
}


def delete_collection_batched(db, collection_ref, batch_size=50, dry_run=True):
    """Delete all documents in a collection using batched deletes."""
    deleted = 0

    while True:
        # Get a batch of documents
        docs = list(collection_ref.limit(batch_size).stream())

        if not docs:
            break

        # Use a batch for efficient deletes
        batch = db.batch()

        for doc in docs:
            if dry_run:
                print(f"    [DRY RUN] Would delete: {doc.reference.path}")
            else:
                batch.delete(doc.reference)
            deleted += 1

        if not dry_run:
            batch.commit()
            print(f"    Deleted batch of {len(docs)} documents...")

        # If we got fewer than batch_size, we're done
        if len(docs) < batch_size:
            break

    return deleted


def wipe_user_data(db, user_id, dry_run=True):
    """Wipe all operational data for a specific user."""
    user_ref = db.collection("users").document(user_id)

    stats = {
        "collections_wiped": 0,
        "documents_deleted": 0,
        "nested_deleted": 0,
    }

    print(f"\n{'='*60}")
    print(f"Wiping user: {user_id}")
    print(f"{'='*60}")

    for collection_name in COLLECTIONS_TO_WIPE:
        collection_ref = user_ref.collection(collection_name)
        nested = NESTED_COLLECTIONS.get(collection_name, [])

        if nested:
            print(f"\n  Processing {collection_name} (with nested: {nested})...")
            # Get parent docs first, then delete nested
            parent_docs = list(collection_ref.limit(500).stream())

            for parent_doc in parent_docs:
                for nested_name in nested:
                    nested_ref = parent_doc.reference.collection(nested_name)
                    nested_deleted = delete_collection_batched(
                        db, nested_ref, batch_size=100, dry_run=dry_run
                    )
                    stats["nested_deleted"] += nested_deleted

        print(f"  Deleting {collection_name}...")
        deleted = delete_collection_batched(db, collection_ref, batch_size=100, dry_run=dry_run)

        if deleted > 0:
            stats["collections_wiped"] += 1
            stats["documents_deleted"] += deleted
            print(f"    Total deleted from {collection_name}: {deleted}")

    return stats


def list_users(db):
    """List all users in the database."""
    users_ref = db.collection("users")
    users = list(users_ref.stream())

    print(f"\nFound {len(users)} users:")
    print("-" * 60)

    for user in users:
        data = user.to_dict() or {}
        display_name = data.get("preferredDisplayName") or data.get("displayName") or "(no name)"
        print(f"  {user.id}: {display_name}")

    return [u.id for u in users]


def confirm_action(message):
    """Ask for user confirmation."""
    response = input(f"\n{message} (yes/no): ").strip().lower()
    return response == "yes"


def main():
    parser = argparse.ArgumentParser(description="Production reset - wipe operational data")
    parser.add_argument("--user-id", help="Specific user ID to wipe")
    parser.add_argument("--all-users", action="store_true", help="Wipe all users")
    parser.add_argument("--list-users", action="store_true", help="List all users")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    parser.add_argument("--confirm", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    db = get_firestore_client()

    print("\n" + "="*60)
    print("PRODUCTION RESET SCRIPT")
    print("="*60)
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE DELETE'}")
    print(f"Time: {datetime.now().isoformat()}")

    if args.list_users:
        list_users(db)
        return

    user_ids = []

    if args.user_id:
        user_ids = [args.user_id]
    elif args.all_users:
        user_ids = list_users(db)
        if not args.confirm and not args.dry_run:
            if not confirm_action(f"This will wipe data for {len(user_ids)} users. Are you sure?"):
                print("Aborted.")
                return
    else:
        print("\nError: Specify --user-id, --all-users, or --list-users")
        parser.print_help()
        return

    total_stats = {
        "users_processed": 0,
        "collections_wiped": 0,
        "documents_deleted": 0,
        "nested_deleted": 0,
    }

    for user_id in user_ids:
        stats = wipe_user_data(db, user_id, dry_run=args.dry_run)
        total_stats["users_processed"] += 1
        total_stats["collections_wiped"] += stats["collections_wiped"]
        total_stats["documents_deleted"] += stats["documents_deleted"]
        total_stats["nested_deleted"] += stats["nested_deleted"]

    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    print(f"Users processed: {total_stats['users_processed']}")
    print(f"Collections wiped: {total_stats['collections_wiped']}")
    print(f"Documents deleted: {total_stats['documents_deleted']}")
    print(f"Nested documents deleted: {total_stats['nested_deleted']}")

    if args.dry_run:
        print("\n[DRY RUN] No data was actually deleted.")
    else:
        print("\n✅ Data has been deleted.")
        print("\nPreserved for each user:")
        print("  - Firebase Auth (sign-in credentials)")
        print("  - User profile (display name, signature, profile pic)")
        print("  - MSAL OAuth tokens (no Microsoft re-auth needed)")


if __name__ == "__main__":
    main()
