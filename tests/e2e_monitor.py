#!/usr/bin/env python3
"""
E2E Campaign Monitoring Tools

Usage:
    python tests/e2e_monitor.py snapshot before    # Take snapshot before campaign
    python tests/e2e_monitor.py snapshot after     # Take snapshot after phase
    python tests/e2e_monitor.py diff               # Compare before/after
    python tests/e2e_monitor.py outlook            # Show Outlook conversations
    python tests/e2e_monitor.py firebase           # Show Firebase state
    python tests/e2e_monitor.py sheet              # Show sheet state with highlights
    python tests/e2e_monitor.py watch              # Watch for changes in real-time
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(Path(__file__).parent.parent / "service-account.json")

from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import requests

# Configuration
UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
SNAPSHOT_DIR = Path("/tmp/e2e_snapshots")
SNAPSHOT_DIR.mkdir(exist_ok=True)

def get_db():
    return firestore.Client()

def get_sheets_client():
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    creds.refresh(Request())
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def get_outlook_token():
    from tests.outlook_helper import get_access_token
    return get_access_token(UID)

def get_active_client_id():
    """Get the first active client ID for this user."""
    db = get_db()
    clients = list(db.collection(f"users/{UID}/clients").limit(1).stream())
    if clients:
        return clients[0].id
    return None

def get_client_sheet_id(client_id):
    """Get the sheet ID for a client."""
    db = get_db()
    doc = db.collection(f"users/{UID}/clients").document(client_id).get()
    if doc.exists:
        return doc.to_dict().get("sheetId")
    return None

# ============================================================================
# SNAPSHOT FUNCTIONS
# ============================================================================

def take_snapshot(phase: str):
    """Take a snapshot of all Firebase state."""
    db = get_db()
    client_id = get_active_client_id()

    snapshot = {
        "timestamp": datetime.utcnow().isoformat(),
        "phase": phase,
        "client_id": client_id,
        "threads": [],
        "notifications": [],
        "outbox": [],
        "msgIndex_count": 0,
        "convIndex_count": 0,
    }

    # Get threads
    for t in db.collection(f"users/{UID}/threads").stream():
        data = t.to_dict()
        if data.get("clientId") == client_id:
            # Get message count
            messages = list(db.collection(f"users/{UID}/threads/{t.id}/messages").stream())
            snapshot["threads"].append({
                "id": t.id[:30],
                "subject": data.get("subject", "?")[:40],
                "status": data.get("status"),
                "followUpStatus": data.get("followUpStatus"),
                "rowNumber": data.get("rowNumber"),
                "messageCount": len(messages),
                "createdAt": str(data.get("createdAt", "")),
                "updatedAt": str(data.get("updatedAt", "")),
            })

    # Get notifications
    if client_id:
        for n in db.collection(f"users/{UID}/clients/{client_id}/notifications").stream():
            data = n.to_dict()
            snapshot["notifications"].append({
                "id": n.id[:20],
                "kind": data.get("kind"),
                "rowAnchor": data.get("rowAnchor", "")[:40],
                "reason": data.get("meta", {}).get("reason", ""),
                "createdAt": str(data.get("createdAt", "")),
            })

    # Get outbox
    for o in db.collection(f"users/{UID}/outbox").stream():
        data = o.to_dict()
        snapshot["outbox"].append({
            "id": o.id[:20],
            "to": data.get("to"),
            "subject": data.get("subject", "")[:40],
        })

    # Count indexes
    snapshot["msgIndex_count"] = len(list(db.collection(f"users/{UID}/msgIndex").stream()))
    snapshot["convIndex_count"] = len(list(db.collection(f"users/{UID}/convIndex").stream()))

    # Save snapshot
    filename = SNAPSHOT_DIR / f"snapshot_{phase}_{datetime.now().strftime('%H%M%S')}.json"
    with open(filename, "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"\n{'='*70}")
    print(f"SNAPSHOT: {phase.upper()}")
    print(f"{'='*70}")
    print(f"Saved to: {filename}")
    print(f"  Threads: {len(snapshot['threads'])}")
    print(f"  Notifications: {len(snapshot['notifications'])}")
    print(f"  Outbox: {len(snapshot['outbox'])}")
    print(f"  MsgIndex: {snapshot['msgIndex_count']}")
    print(f"  ConvIndex: {snapshot['convIndex_count']}")

    return filename

def compare_snapshots():
    """Compare before/after snapshots."""
    snapshots = sorted(SNAPSHOT_DIR.glob("snapshot_*.json"))
    if len(snapshots) < 2:
        print("Need at least 2 snapshots to compare. Run 'snapshot before' and 'snapshot after' first.")
        return

    before = json.load(open(snapshots[0]))
    after = json.load(open(snapshots[-1]))

    print(f"\n{'='*70}")
    print(f"COMPARING: {snapshots[0].name} vs {snapshots[-1].name}")
    print(f"{'='*70}")

    # Compare threads
    before_threads = {t["id"]: t for t in before["threads"]}
    after_threads = {t["id"]: t for t in after["threads"]}

    new_threads = set(after_threads.keys()) - set(before_threads.keys())
    removed_threads = set(before_threads.keys()) - set(after_threads.keys())

    print(f"\nTHREADS:")
    print(f"  Before: {len(before_threads)}, After: {len(after_threads)}")
    if new_threads:
        print(f"  NEW ({len(new_threads)}):")
        for tid in new_threads:
            t = after_threads[tid]
            print(f"    + {t['subject']} (status: {t['status']})")
    if removed_threads:
        print(f"  REMOVED ({len(removed_threads)}):")
        for tid in removed_threads:
            t = before_threads[tid]
            print(f"    - {t['subject']}")

    # Check for status changes
    print(f"\n  STATUS CHANGES:")
    for tid in set(before_threads.keys()) & set(after_threads.keys()):
        b, a = before_threads[tid], after_threads[tid]
        if b["status"] != a["status"]:
            print(f"    {a['subject'][:30]}: {b['status']} -> {a['status']}")

    # Compare notifications
    before_notifs = {n["id"]: n for n in before["notifications"]}
    after_notifs = {n["id"]: n for n in after["notifications"]}

    new_notifs = set(after_notifs.keys()) - set(before_notifs.keys())

    print(f"\nNOTIFICATIONS:")
    print(f"  Before: {len(before_notifs)}, After: {len(after_notifs)}")
    if new_notifs:
        print(f"  NEW ({len(new_notifs)}):")
        for nid in new_notifs:
            n = after_notifs[nid]
            print(f"    + {n['kind']}: {n['rowAnchor']}")

    # Compare outbox
    print(f"\nOUTBOX:")
    print(f"  Before: {len(before['outbox'])}, After: {len(after['outbox'])}")

# ============================================================================
# OUTLOOK FUNCTIONS
# ============================================================================

def show_outlook():
    """Show Outlook sent items and inbox for E2E test."""
    token = get_outlook_token()
    headers = {"Authorization": f"Bearer {token}"}

    print(f"\n{'='*70}")
    print("OUTLOOK CONVERSATIONS")
    print(f"{'='*70}")

    # E2E test properties
    e2e_props = ["699 Industrial", "135 Trade Center", "2017 St. Josephs",
                 "9300 Lottsford", "1 Randolph", "1800 Broad", "2525 Center West"]

    # Sent items
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages",
        headers=headers,
        params={"$top": "50", "$select": "subject,sentDateTime,toRecipients", "$orderby": "sentDateTime desc"}
    )
    sent = resp.json().get("value", [])

    print(f"\nSENT ITEMS (E2E related):")
    for msg in sent:
        subj = msg.get("subject", "")
        if any(p in subj for p in e2e_props):
            to = msg.get("toRecipients", [{}])[0].get("emailAddress", {}).get("address", "?")
            sent_time = msg.get("sentDateTime", "")[:19]
            print(f"  {sent_time} | To: {to:<35} | {subj[:40]}")

    # Inbox
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
        headers=headers,
        params={"$top": "30", "$select": "subject,receivedDateTime,from", "$orderby": "receivedDateTime desc"}
    )
    inbox = resp.json().get("value", [])

    print(f"\nINBOX (E2E related):")
    for msg in inbox:
        subj = msg.get("subject", "")
        if any(p in subj for p in e2e_props):
            from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "?")
            recv_time = msg.get("receivedDateTime", "")[:19]
            print(f"  {recv_time} | From: {from_addr:<35} | {subj[:40]}")

# ============================================================================
# FIREBASE FUNCTIONS
# ============================================================================

def show_firebase():
    """Show current Firebase state."""
    db = get_db()
    client_id = get_active_client_id()

    print(f"\n{'='*70}")
    print(f"FIREBASE STATE (Client: {client_id})")
    print(f"{'='*70}")

    # Threads
    print(f"\nTHREADS:")
    threads = list(db.collection(f"users/{UID}/threads").stream())
    for t in threads:
        data = t.to_dict()
        if data.get("clientId") == client_id:
            messages = list(db.collection(f"users/{UID}/threads/{t.id}/messages").stream())
            print(f"  Row {data.get('rowNumber', '?'):<3} | {data.get('status', '?'):<12} | "
                  f"{len(messages)} msgs | {data.get('subject', '?')[:35]}")

    # Notifications by kind
    print(f"\nNOTIFICATIONS:")
    if client_id:
        notifs = list(db.collection(f"users/{UID}/clients/{client_id}/notifications").stream())
        by_kind = {}
        for n in notifs:
            kind = n.to_dict().get("kind", "?")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for kind, count in sorted(by_kind.items()):
            print(f"  {kind}: {count}")

    # Outbox
    outbox = list(db.collection(f"users/{UID}/outbox").stream())
    print(f"\nOUTBOX: {len(outbox)} items")
    for o in outbox:
        data = o.to_dict()
        print(f"  To: {data.get('to', '?')} | {data.get('subject', '?')[:40]}")

# ============================================================================
# SHEET FUNCTIONS
# ============================================================================

def show_sheet():
    """Show sheet state with highlighting."""
    client_id = get_active_client_id()
    sheet_id = get_client_sheet_id(client_id)

    if not sheet_id:
        print("No active client/sheet found")
        return

    sheets = get_sheets_client()

    # Get sheet with formatting
    metadata = sheets.spreadsheets().get(
        spreadsheetId=sheet_id,
        includeGridData=True,
        ranges=["A1:R20"]
    ).execute()

    sheet_data = metadata.get("sheets", [{}])[0]
    grid_data = sheet_data.get("data", [{}])[0]
    row_data = grid_data.get("rowData", [])

    # Get values
    result = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="A1:R20"
    ).execute()
    values = result.get("values", [])

    # Find Flyer and Floorplan columns
    headers = values[1] if len(values) > 1 else []
    flyer_idx = None
    floorplan_idx = None
    for i, h in enumerate(headers):
        if "flyer" in h.lower():
            flyer_idx = i
        if "floorplan" in h.lower():
            floorplan_idx = i

    print(f"\n{'='*70}")
    print(f"SHEET STATE (ID: {sheet_id[:20]}...)")
    print(f"{'='*70}")

    print(f"\n{'Row':<4} {'Highlight':<10} {'Property':<30} {'Flyer':<8} {'Floorplan':<8}")
    print("-" * 70)

    for i in range(2, min(len(row_data), 12)):
        cells = row_data[i].get("values", [])

        # Check highlight color
        highlight = "none"
        if cells:
            bg = cells[0].get("effectiveFormat", {}).get("backgroundColor", {})
            r, g, b = bg.get("red", 1), bg.get("green", 1), bg.get("blue", 1)
            if r > 0.9 and g > 0.9 and b < 0.3:
                highlight = "YELLOW"
            elif r < 0.8 and g > 0.8 and b > 0.9:
                highlight = "BLUE"

        addr = values[i][0] if i < len(values) and values[i] else "?"
        flyer = "YES" if flyer_idx and i < len(values) and len(values[i]) > flyer_idx and values[i][flyer_idx] else ""
        floorplan = "YES" if floorplan_idx and i < len(values) and len(values[i]) > floorplan_idx and values[i][floorplan_idx] else ""

        print(f"{i+1:<4} {highlight:<10} {addr[:30]:<30} {flyer:<8} {floorplan:<8}")

# ============================================================================
# WATCH FUNCTION
# ============================================================================

def watch_changes():
    """Watch for Firebase changes in real-time."""
    db = get_db()
    client_id = get_active_client_id()

    print(f"\n{'='*70}")
    print("WATCHING FOR CHANGES (Ctrl+C to stop)")
    print(f"{'='*70}\n")

    last_threads = set()
    last_notifs = set()
    last_outbox = set()

    while True:
        try:
            # Check threads
            threads = list(db.collection(f"users/{UID}/threads").stream())
            current_threads = set()
            for t in threads:
                data = t.to_dict()
                if data.get("clientId") == client_id:
                    current_threads.add(t.id)

            new_threads = current_threads - last_threads
            for tid in new_threads:
                t = db.collection(f"users/{UID}/threads").document(tid).get()
                data = t.to_dict()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] + THREAD: {data.get('subject', '?')[:40]}")

            last_threads = current_threads

            # Check notifications
            if client_id:
                notifs = list(db.collection(f"users/{UID}/clients/{client_id}/notifications").stream())
                current_notifs = set(n.id for n in notifs)

                new_notifs = current_notifs - last_notifs
                for nid in new_notifs:
                    n = db.collection(f"users/{UID}/clients/{client_id}/notifications").document(nid).get()
                    data = n.to_dict()
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] + NOTIF: {data.get('kind')} - {data.get('rowAnchor', '?')[:30]}")

                removed_notifs = last_notifs - current_notifs
                for nid in removed_notifs:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] - NOTIF: {nid[:20]} removed")

                last_notifs = current_notifs

            # Check outbox
            outbox = list(db.collection(f"users/{UID}/outbox").stream())
            current_outbox = set(o.id for o in outbox)

            new_outbox = current_outbox - last_outbox
            for oid in new_outbox:
                o = db.collection(f"users/{UID}/outbox").document(oid).get()
                data = o.to_dict()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] + OUTBOX: {data.get('subject', '?')[:40]}")

            removed_outbox = last_outbox - current_outbox
            for oid in removed_outbox:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] - OUTBOX: {oid[:20]} sent/deleted")

            last_outbox = current_outbox

            time.sleep(2)

        except KeyboardInterrupt:
            print("\nStopped watching.")
            break

# ============================================================================
# MAIN
# ============================================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    if cmd == "snapshot":
        phase = sys.argv[2] if len(sys.argv) > 2 else "unnamed"
        take_snapshot(phase)
    elif cmd == "diff":
        compare_snapshots()
    elif cmd == "outlook":
        show_outlook()
    elif cmd == "firebase":
        show_firebase()
    elif cmd == "sheet":
        show_sheet()
    elif cmd == "watch":
        watch_changes()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

if __name__ == "__main__":
    main()
