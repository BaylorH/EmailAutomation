#!/usr/bin/env python3
"""
Production Analysis Script
==========================
Analyzes the current state of Firebase production data.

Usage:
    # Set credentials and run
    source ~/Documents/GitHub/email-admin-ui/functions/.env.local
    echo "$GOOGLE_SERVICE_ACCOUNT_JSON" > /tmp/firebase_sa.json
    GOOGLE_APPLICATION_CREDENTIALS=/tmp/firebase_sa.json python3 scripts/analyze_production.py
"""

import os
import sys
import json
from datetime import datetime

# Suppress warnings
import warnings
warnings.filterwarnings("ignore")

from google.cloud import firestore


def init_firestore():
    """Initialize Firestore client."""
    # Check for credentials
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds_path:
        print("Error: GOOGLE_APPLICATION_CREDENTIALS not set")
        print("\nTo set up:")
        print("  source ~/Documents/GitHub/email-admin-ui/functions/.env.local")
        print('  echo "$GOOGLE_SERVICE_ACCOUNT_JSON" > /tmp/firebase_sa.json')
        print("  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/firebase_sa.json")
        sys.exit(1)

    return firestore.Client()


def count_collection(db, path):
    """Count documents in a collection (up to 1000)."""
    try:
        parts = path.split("/")
        ref = db
        for i, part in enumerate(parts):
            if i % 2 == 0:
                ref = ref.collection(part)
            else:
                ref = ref.document(part)
        docs = list(ref.limit(1000).stream())
        return len(docs)
    except Exception as e:
        return 0


def analyze_user(db, uid):
    """Analyze data for a single user."""
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()

    if not user_doc.exists:
        return None

    user_data = user_doc.to_dict() or {}

    analysis = {
        "uid": uid,
        "display_name": user_data.get("preferredDisplayName") or user_data.get("displayName") or "(no name)",
        "has_profile_pic": bool(user_data.get("profilePic")),
        "has_signature": bool(user_data.get("emailSignature")),
        "organization": user_data.get("organizationName", ""),
        "collections": {}
    }

    # Count each collection
    collections_to_check = [
        "clients",
        "outbox",
        "threads",
        "msgIndex",
        "convIndex",
        "processedMessages",
        "optedOutContacts",
        "sheetChangeLog",
        "archivedClients",
        "archivedThreads",
    ]

    total_docs = 0
    for coll_name in collections_to_check:
        try:
            docs = list(user_ref.collection(coll_name).limit(1000).stream())
            count = len(docs)
            if count > 0:
                analysis["collections"][coll_name] = count
                total_docs += count
        except:
            pass

    # Count nested collections (notifications, messages)
    nested_count = 0

    # Count notifications in each client
    try:
        clients = list(user_ref.collection("clients").stream())
        for client in clients:
            try:
                notifs = list(client.reference.collection("notifications").limit(500).stream())
                nested_count += len(notifs)
            except:
                pass
    except:
        pass

    # Count messages in each thread (sample first 50 threads)
    try:
        threads = list(user_ref.collection("threads").limit(50).stream())
        for thread in threads:
            try:
                msgs = list(thread.reference.collection("messages").limit(100).stream())
                nested_count += len(msgs)
            except:
                pass
    except:
        pass

    if nested_count > 0:
        analysis["collections"]["_nested (notif+msgs)"] = nested_count
        total_docs += nested_count

    analysis["total_documents"] = total_docs

    return analysis


def main():
    print("\n" + "="*70)
    print("PRODUCTION FIREBASE ANALYSIS")
    print("="*70)
    print(f"Time: {datetime.now().isoformat()}")

    db = init_firestore()
    print("Firestore connection: OK\n")

    # List all users
    users_ref = db.collection("users")
    users = list(users_ref.stream())

    print(f"Found {len(users)} users")
    print("-"*70)

    total_all_docs = 0
    user_analyses = []

    for user_doc in users:
        analysis = analyze_user(db, user_doc.id)
        if analysis:
            user_analyses.append(analysis)
            total_all_docs += analysis["total_documents"]

            # Print summary for this user
            print(f"\nUser: {analysis['display_name']}")
            print(f"  UID: {analysis['uid'][:20]}...")
            if analysis["organization"]:
                print(f"  Org: {analysis['organization']}")
            print(f"  Profile: pic={'yes' if analysis['has_profile_pic'] else 'no'}, sig={'yes' if analysis['has_signature'] else 'no'}")
            print(f"  Total documents: {analysis['total_documents']}")

            if analysis["collections"]:
                for coll, count in sorted(analysis["collections"].items()):
                    print(f"    - {coll}: {count}")
            else:
                print("    (no operational data)")

    # Overall summary
    print("\n" + "="*70)
    print("OVERALL SUMMARY")
    print("="*70)
    print(f"Total users: {len(users)}")
    print(f"Total documents (all users): {total_all_docs}")

    # What would be preserved
    print("\n" + "-"*70)
    print("IF WE RESET:")
    print("-"*70)
    print(f"Documents to DELETE: {total_all_docs}")
    print(f"Documents to KEEP:")
    print(f"  - {len(users)} user profile documents (name, signature, pic)")
    print(f"  - MSAL OAuth tokens (in Firebase Storage - users stay logged in)")

    if total_all_docs == 0:
        print("\n*** Production is already clean! No operational data to wipe. ***")
    else:
        print("\nTo perform the reset, run:")
        print("  python scripts/production_reset.py --all-users --dry-run")
        print("  python scripts/production_reset.py --all-users --confirm")


if __name__ == "__main__":
    main()
