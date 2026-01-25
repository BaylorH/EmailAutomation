#!/usr/bin/env python3
"""
Production Reset Script
=======================
Wipes all operational data from Firebase while preserving:
- User authentication (managed by Firebase Auth)
- User profile (display name, signature, profile pic)
- MSAL OAuth tokens (in Firebase Storage)

This gives users a "fresh dashboard" without needing to re-authenticate.

Usage:
    # Dry run (shows what would be deleted)
    python scripts/production_reset.py --dry-run

    # Wipe specific user
    python scripts/production_reset.py --user-id abc123

    # Wipe all users (DANGEROUS)
    python scripts/production_reset.py --all-users --confirm

    # List all users first
    python scripts/production_reset.py --list-users
"""

import os
import sys
import argparse
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation.clients import get_firestore_client


# Collections to wipe (relative to users/{uid}/)
COLLECTIONS_TO_WIPE = [
    "clients",           # Includes nested notifications
    "outbox",
    "threads",           # Includes nested messages
    "msgIndex",
    "convIndex",
    "processedMessages",
    "optedOutContacts",
    "sheetChangeLog",
    "sync",
    "archivedClients",
    "archivedThreads",   # Includes nested messages
    "archivedMsgIndex",
    "archivedConvIndex",
]

# Nested collections (parent -> children)
NESTED_COLLECTIONS = {
    "clients": ["notifications"],
    "threads": ["messages"],
    "archivedThreads": ["messages"],
}

# User document fields to preserve
USER_FIELDS_TO_KEEP = [
    "displayName",
    "preferredDisplayName",
    "profilePic",
    "profilePicShape",
    "emailSignature",
    "signatureMode",
    "organizationName",
    "createdAt",  # Keep original signup date
]


def delete_collection(db, collection_ref, batch_size=100, dry_run=True):
    """Delete all documents in a collection."""
    deleted = 0
    docs = collection_ref.limit(batch_size).stream()

    for doc in docs:
        if dry_run:
            print(f"    [DRY RUN] Would delete: {doc.reference.path}")
        else:
            doc.reference.delete()
        deleted += 1

    # Recurse if there might be more
    if deleted >= batch_size:
        deleted += delete_collection(db, collection_ref, batch_size, dry_run)

    return deleted


def delete_nested_collection(db, parent_ref, nested_name, batch_size=100, dry_run=True):
    """Delete a nested collection from a parent document."""
    nested_ref = parent_ref.collection(nested_name)
    return delete_collection(db, nested_ref, batch_size, dry_run)


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

        # Check if collection has nested collections
        nested = NESTED_COLLECTIONS.get(collection_name, [])

        if nested:
            # First delete nested collections for each document
            print(f"\n  Processing {collection_name} (with nested: {nested})...")
            docs = collection_ref.stream()
            for doc in docs:
                for nested_name in nested:
                    nested_deleted = delete_nested_collection(
                        db, doc.reference, nested_name, dry_run=dry_run
                    )
                    stats["nested_deleted"] += nested_deleted

        # Then delete the parent collection
        print(f"  Deleting {collection_name}...")
        deleted = delete_collection(db, collection_ref, dry_run=dry_run)
        if deleted > 0:
            stats["collections_wiped"] += 1
            stats["documents_deleted"] += deleted
            print(f"    {'Would delete' if dry_run else 'Deleted'}: {deleted} documents")

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
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without actually deleting")
    parser.add_argument("--confirm", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    # Initialize Firebase
    db = get_firestore_client()

    print("\n" + "="*60)
    print("PRODUCTION RESET SCRIPT")
    print("="*60)
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE DELETE'}")
    print(f"Time: {datetime.now().isoformat()}")

    if args.list_users:
        list_users(db)
        return

    # Determine which users to process
    user_ids = []

    if args.user_id:
        user_ids = [args.user_id]
    elif args.all_users:
        user_ids = list_users(db)
        if not args.confirm:
            if not confirm_action(f"This will wipe data for {len(user_ids)} users. Are you sure?"):
                print("Aborted.")
                return
    else:
        print("\nError: Specify --user-id, --all-users, or --list-users")
        parser.print_help()
        return

    # Process each user
    total_stats = {
        "users_processed": 0,
        "collections_wiped": 0,
        "documents_deleted": 0,
        "nested_deleted": 0,
    }

    for user_id in user_ids:
        if not args.dry_run and not args.confirm:
            if not confirm_action(f"Wipe data for user {user_id}?"):
                print(f"  Skipped {user_id}")
                continue

        stats = wipe_user_data(db, user_id, dry_run=args.dry_run)
        total_stats["users_processed"] += 1
        total_stats["collections_wiped"] += stats["collections_wiped"]
        total_stats["documents_deleted"] += stats["documents_deleted"]
        total_stats["nested_deleted"] += stats["nested_deleted"]

    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    print(f"Users processed: {total_stats['users_processed']}")
    print(f"Collections wiped: {total_stats['collections_wiped']}")
    print(f"Documents deleted: {total_stats['documents_deleted']}")
    print(f"Nested documents deleted: {total_stats['nested_deleted']}")

    if args.dry_run:
        print("\n[DRY RUN] No data was actually deleted.")
        print("Run without --dry-run to perform actual deletion.")
    else:
        print("\nData has been deleted.")

    print("\nPreserved for each user:")
    print("  - Firebase Auth (sign-in credentials)")
    print("  - User profile (display name, signature, profile pic)")
    print("  - MSAL OAuth tokens (no Microsoft re-auth needed)")


if __name__ == "__main__":
    main()
