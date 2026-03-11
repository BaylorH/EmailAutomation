"""
E2E Test Monitoring Tools
=========================
Quick tools to check Outlook, Firestore, and Google Sheets during E2E testing.
"""

import os
import json
import requests
from datetime import datetime, timezone
from pathlib import Path
from msal import ConfidentialClientApplication, SerializableTokenCache
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# Load environment
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
CLIENT_ID = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET")
USER_ID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"  # baylor.freelance@outlook.com

# Initialize Firestore using service account
sa_path = Path(__file__).parent.parent / "service-account.json"
if sa_path.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(sa_path)
_fs = firestore.Client()

def _get_outlook_token():
    """Get Outlook access token from Firebase cache."""
    object_path = f"msal_caches/{USER_ID}/msal_token_cache.bin"
    encoded_path = object_path.replace("/", "%2F")
    url = f"https://firebasestorage.googleapis.com/v0/b/email-automation-cache.firebasestorage.app/o/{encoded_path}?alt=media&key={FIREBASE_API_KEY}"
    r = requests.get(url)
    cache = SerializableTokenCache()
    cache.deserialize(r.text)
    app = ConfidentialClientApplication(CLIENT_ID, authority="https://login.microsoftonline.com/common", client_credential=CLIENT_SECRET, token_cache=cache)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(["https://graph.microsoft.com/Mail.ReadWrite"], account=accounts[0])
    return result["access_token"]

def _get_sheets_client():
    """Get Google Sheets client."""
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    creds.refresh(Request())
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

# =============================================================================
# OUTLOOK TOOLS
# =============================================================================

def check_outlook_sent(limit=20):
    """Check sent emails in Outlook."""
    token = _get_outlook_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$top={limit}&$orderby=sentDateTime desc"
    resp = requests.get(url, headers=headers)
    messages = resp.json().get("value", [])

    print(f"\n{'='*70}")
    print(f"OUTLOOK SENT ITEMS ({len(messages)} most recent)")
    print(f"{'='*70}\n")

    for msg in messages:
        sent = msg.get("sentDateTime", "")[:19].replace("T", " ")
        subj = msg.get("subject", "")[:50]
        to = msg.get("toRecipients", [{}])[0].get("emailAddress", {}).get("address", "")
        print(f"[{sent}] To: {to}")
        print(f"  Subject: {subj}")
        print()

    return messages

def check_outlook_inbox(limit=20):
    """Check inbox in Outlook."""
    token = _get_outlook_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top={limit}&$orderby=receivedDateTime desc"
    resp = requests.get(url, headers=headers)
    messages = resp.json().get("value", [])

    print(f"\n{'='*70}")
    print(f"OUTLOOK INBOX ({len(messages)} most recent)")
    print(f"{'='*70}\n")

    for msg in messages:
        recv = msg.get("receivedDateTime", "")[:19].replace("T", " ")
        subj = msg.get("subject", "")[:50]
        frm = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        print(f"[{recv}] From: {frm}")
        print(f"  Subject: {subj}")
        print()

    return messages

def get_email_body(message_id):
    """Get full email body by ID."""
    token = _get_outlook_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
    resp = requests.get(url, headers=headers)
    msg = resp.json()

    body = msg.get("body", {}).get("content", "")
    # Strip HTML if needed
    if msg.get("body", {}).get("contentType") == "html":
        import re
        body = re.sub(r'<[^>]+>', '', body)
        body = re.sub(r'\s+', ' ', body).strip()

    return body[:2000]  # Truncate for readability

# =============================================================================
# FIRESTORE TOOLS
# =============================================================================

def check_firestore_all(client_id=None):
    """Check all Firestore collections for the user."""
    print(f"\n{'='*70}")
    print(f"FIRESTORE STATE FOR USER: {USER_ID}")
    print(f"{'='*70}\n")

    collections = ["threads", "msgIndex", "convIndex", "notifications", "outbox", "processedMessages"]

    for coll_name in collections:
        if client_id and coll_name == "notifications":
            # Notifications are under clients/{clientId}/notifications
            docs = list(_fs.collection("users").document(USER_ID).collection("clients").document(client_id).collection("notifications").stream())
        else:
            docs = list(_fs.collection("users").document(USER_ID).collection(coll_name).stream())

        print(f"📁 {coll_name}: {len(docs)} documents")

        if docs and coll_name in ["threads", "notifications"]:
            for doc in docs[:10]:  # Show first 10
                data = doc.to_dict()
                if coll_name == "threads":
                    subj = data.get("subject", "")[:40]
                    status = data.get("followUpStatus", "?")
                    print(f"   └─ {doc.id[:20]}... | {status} | {subj}")
                elif coll_name == "notifications":
                    kind = data.get("kind", "?")
                    reason = data.get("reason", "")
                    prop = data.get("propertyAddress", "")[:30]
                    print(f"   └─ {kind}:{reason} | {prop}")
        print()

    return True

def check_threads_detail():
    """Get detailed thread information."""
    print(f"\n{'='*70}")
    print("THREAD DETAILS")
    print(f"{'='*70}\n")

    docs = list(_fs.collection("users").document(USER_ID).collection("threads").stream())

    for doc in docs:
        data = doc.to_dict()
        print(f"Thread: {doc.id[:30]}...")
        print(f"  Subject: {data.get('subject', '')[:50]}")
        print(f"  Status: {data.get('followUpStatus', 'N/A')}")
        print(f"  Client: {data.get('clientId', 'N/A')[:20]}")

        fu_config = data.get("followUpConfig", {})
        if fu_config:
            print(f"  Follow-up Index: {fu_config.get('currentFollowUpIndex', 'N/A')}")
            next_fu = fu_config.get("nextFollowUpAt")
            if next_fu and hasattr(next_fu, 'timestamp'):
                next_dt = datetime.fromtimestamp(next_fu.timestamp(), tz=timezone.utc)
                print(f"  Next Follow-up: {next_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        print()

    return docs

def check_notifications(client_id):
    """Get all notifications for a client."""
    print(f"\n{'='*70}")
    print(f"NOTIFICATIONS FOR CLIENT: {client_id[:20]}...")
    print(f"{'='*70}\n")

    docs = list(_fs.collection("users").document(USER_ID).collection("clients").document(client_id).collection("notifications").stream())

    for doc in docs:
        data = doc.to_dict()
        kind = data.get("kind", "?")
        reason = data.get("reason", "")
        prop = data.get("propertyAddress", "")
        created = data.get("createdAt")

        created_str = ""
        if created and hasattr(created, 'timestamp'):
            created_str = datetime.fromtimestamp(created.timestamp(), tz=timezone.utc).strftime('%H:%M:%S')

        print(f"[{created_str}] {kind}")
        if reason:
            print(f"  Reason: {reason}")
        print(f"  Property: {prop}")
        print()

    return docs

def get_client_id():
    """Get the active client ID."""
    docs = list(_fs.collection("users").document(USER_ID).collection("clients").stream())
    for doc in docs:
        print(f"Client: {doc.id} - {doc.to_dict().get('name', 'unnamed')}")
    if docs:
        return docs[0].id
    return None

# =============================================================================
# GOOGLE SHEETS TOOLS
# =============================================================================

def check_sheet(sheet_id, include_values=True):
    """Check Google Sheet state including formatting."""
    sheets = _get_sheets_client()

    # Get sheet metadata
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id, includeGridData=True).execute()

    print(f"\n{'='*70}")
    print(f"GOOGLE SHEET STATE")
    print(f"{'='*70}\n")

    for sheet in meta.get("sheets", []):
        title = sheet["properties"]["title"]
        if title == "AI_META":
            continue

        print(f"📊 Tab: {title}")

        grid_data = sheet.get("data", [{}])[0]
        row_data = grid_data.get("rowData", [])

        if len(row_data) < 2:
            print("  (empty)")
            continue

        # Header row
        header = []
        for cell in row_data[1].get("values", []):
            header.append(cell.get("formattedValue", ""))

        # Find key columns
        addr_idx = next((i for i, h in enumerate(header) if "address" in h.lower()), 0)

        print(f"  Header: {header[:8]}...")
        print()

        # Data rows
        for row_num, row in enumerate(row_data[2:], start=3):
            cells = row.get("values", [])
            if not cells:
                continue

            # Check for highlighting
            bg_color = cells[0].get("effectiveFormat", {}).get("backgroundColor", {})
            is_highlighted = bg_color.get("blue", 0) > 0.8 and bg_color.get("red", 0) < 0.5
            highlight_marker = "🔵" if is_highlighted else "  "

            # Get address
            addr = cells[addr_idx].get("formattedValue", "") if addr_idx < len(cells) else ""

            # Check for NON-VIABLE
            if addr and "NON-VIABLE" in addr.upper():
                print(f"  --- NON-VIABLE DIVIDER ---")
                continue

            if addr:
                # Get a few key values
                values = [c.get("formattedValue", "") for c in cells[:10]]
                print(f"  {highlight_marker} Row {row_num}: {addr[:35]}")
                if include_values:
                    # Show filled vs empty
                    filled = sum(1 for v in values if v.strip())
                    print(f"       Filled: {filled}/{len(values)} columns")

        print()

    return meta

def get_sheet_row_values(sheet_id, row_num):
    """Get all values from a specific row."""
    sheets = _get_sheets_client()
    result = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"A{row_num}:Z{row_num}"
    ).execute()
    values = result.get("values", [[]])[0]

    # Get header
    header_result = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="A2:Z2"
    ).execute()
    header = header_result.get("values", [[]])[0]

    print(f"\n{'='*70}")
    print(f"ROW {row_num} VALUES")
    print(f"{'='*70}\n")

    for i, (h, v) in enumerate(zip(header, values)):
        if v.strip():
            print(f"  {h}: {v}")

    return dict(zip(header, values))

# =============================================================================
# GITHUB ACTIONS TOOLS
# =============================================================================

import subprocess
from datetime import datetime as dt

REPO = "BaylorH/EmailAutomation"

def trigger_scheduler():
    """Manually trigger the GitHub Actions scheduler workflow."""
    print(f"\n{'='*70}")
    print("TRIGGERING GITHUB ACTIONS SCHEDULER")
    print(f"{'='*70}\n")

    result = subprocess.run(
        ["gh", "workflow", "run", "email.yml", "-R", REPO],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        print("✅ Workflow triggered successfully!")
        print("   Run `python3 scripts/e2e_tools.py runs` to check status")
    else:
        print(f"❌ Failed to trigger: {result.stderr}")

    return result.returncode == 0

def get_workflow_runs(limit=10):
    """List recent GitHub Actions workflow runs."""
    print(f"\n{'='*70}")
    print(f"RECENT WORKFLOW RUNS (last {limit})")
    print(f"{'='*70}\n")

    result = subprocess.run(
        ["gh", "run", "list", "-R", REPO, "-L", str(limit), "--json",
         "databaseId,status,conclusion,createdAt,updatedAt,event"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"❌ Failed: {result.stderr}")
        return []

    runs = json.loads(result.stdout)

    for run in runs:
        run_id = run["databaseId"]
        status = run["status"]
        conclusion = run.get("conclusion", "-")
        created = run["createdAt"][:19].replace("T", " ")
        event = run["event"]

        # Status emoji
        if status == "completed":
            emoji = "✅" if conclusion == "success" else "❌"
        elif status == "in_progress":
            emoji = "🔄"
        else:
            emoji = "⏳"

        print(f"{emoji} Run {run_id} | {status}/{conclusion} | {created} | {event}")

    return runs

def get_workflow_logs(run_id=None):
    """Fetch logs from a specific workflow run (or latest)."""
    print(f"\n{'='*70}")
    print(f"WORKFLOW LOGS" + (f" (Run {run_id})" if run_id else " (Latest)"))
    print(f"{'='*70}\n")

    # Get run ID if not specified
    if not run_id:
        result = subprocess.run(
            ["gh", "run", "list", "-R", REPO, "-L", "1", "--json", "databaseId"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"❌ Failed to get latest run: {result.stderr}")
            return None
        runs = json.loads(result.stdout)
        if not runs:
            print("❌ No workflow runs found")
            return None
        run_id = runs[0]["databaseId"]
        print(f"📋 Using latest run: {run_id}\n")

    # Fetch logs
    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "-R", REPO, "--log"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"❌ Failed to fetch logs: {result.stderr}")
        return None

    logs = result.stdout

    # Extract just the main.py output (skip setup steps)
    in_run_section = False
    relevant_lines = []
    for line in logs.split("\n"):
        if "Run email script" in line or "Run python main.py" in line:
            in_run_section = True
        if in_run_section:
            relevant_lines.append(line)

    if relevant_lines:
        print("\n".join(relevant_lines[:200]))  # First 200 lines
        if len(relevant_lines) > 200:
            print(f"\n... ({len(relevant_lines) - 200} more lines)")
    else:
        # Show last 100 lines if no specific section found
        lines = logs.split("\n")
        print("\n".join(lines[-100:]))

    return logs

def save_workflow_logs(run_id=None, filename=None):
    """Save full workflow logs to a file for later review."""
    if not run_id:
        result = subprocess.run(
            ["gh", "run", "list", "-R", REPO, "-L", "1", "--json", "databaseId"],
            capture_output=True, text=True
        )
        runs = json.loads(result.stdout)
        run_id = runs[0]["databaseId"] if runs else None

    if not run_id:
        print("❌ No run ID available")
        return None

    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "-R", REPO, "--log"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"❌ Failed: {result.stderr}")
        return None

    if not filename:
        filename = f"logs/workflow_{run_id}_{dt.now().strftime('%Y%m%d_%H%M%S')}.log"

    # Ensure logs directory exists
    Path("logs").mkdir(exist_ok=True)

    with open(filename, "w") as f:
        f.write(result.stdout)

    print(f"✅ Saved logs to {filename}")
    return filename

# =============================================================================
# LOCAL RUNNER TOOLS
# =============================================================================

def run_local(save_log=True):
    """Run main.py locally and capture output."""
    print(f"\n{'='*70}")
    print("RUNNING MAIN.PY LOCALLY")
    print(f"{'='*70}\n")

    timestamp = dt.now().strftime('%Y%m%d_%H%M%S')
    log_file = f"logs/local_run_{timestamp}.log" if save_log else None

    if save_log:
        Path("logs").mkdir(exist_ok=True)
        print(f"📝 Logging to: {log_file}\n")

    # Run main.py from parent directory
    script_dir = Path(__file__).parent.parent

    result = subprocess.run(
        ["python3", "main.py"],
        cwd=script_dir,
        capture_output=True,
        text=True,
        timeout=300  # 5 min timeout
    )

    output = result.stdout + "\n" + result.stderr

    # Print output
    print(output)

    # Save to log file
    if save_log and log_file:
        with open(script_dir / log_file, "w") as f:
            f.write(f"=== Local Run {timestamp} ===\n")
            f.write(f"Exit code: {result.returncode}\n\n")
            f.write("=== STDOUT ===\n")
            f.write(result.stdout)
            f.write("\n=== STDERR ===\n")
            f.write(result.stderr)
        print(f"\n✅ Log saved to {log_file}")

    return result.returncode == 0

def list_logs():
    """List all saved log files."""
    print(f"\n{'='*70}")
    print("SAVED LOG FILES")
    print(f"{'='*70}\n")

    logs_dir = Path(__file__).parent.parent / "logs"
    if not logs_dir.exists():
        print("No logs directory found")
        return []

    log_files = sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)

    for f in log_files[:20]:
        size = f.stat().st_size
        mtime = dt.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {f.name} ({size:,} bytes) - {mtime}")

    return log_files

def review_logs():
    """Review all logs for issues, errors, or cleanup needed."""
    print(f"\n{'='*70}")
    print("LOG REVIEW SUMMARY")
    print(f"{'='*70}\n")

    logs_dir = Path(__file__).parent.parent / "logs"
    if not logs_dir.exists():
        print("No logs directory found")
        return

    log_files = sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime)

    issues = []
    warnings = []
    errors = []

    error_patterns = ["error", "exception", "traceback", "failed", "❌"]
    warning_patterns = ["warning", "⚠️", "skipping", "retry"]

    for log_file in log_files:
        with open(log_file) as f:
            content = f.read().lower()
            lines = f.read().split("\n")

        with open(log_file) as f:
            for i, line in enumerate(f):
                line_lower = line.lower()

                for pattern in error_patterns:
                    if pattern in line_lower:
                        errors.append((log_file.name, i+1, line.strip()[:100]))
                        break

                for pattern in warning_patterns:
                    if pattern in line_lower and not any(p in line_lower for p in error_patterns):
                        warnings.append((log_file.name, i+1, line.strip()[:100]))
                        break

    print(f"📊 Reviewed {len(log_files)} log files\n")

    if errors:
        print(f"❌ ERRORS FOUND ({len(errors)}):")
        print("-" * 50)
        for fname, line_num, text in errors[:20]:
            print(f"  {fname}:{line_num} - {text}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
        print()

    if warnings:
        print(f"⚠️  WARNINGS FOUND ({len(warnings)}):")
        print("-" * 50)
        for fname, line_num, text in warnings[:10]:
            print(f"  {fname}:{line_num} - {text}")
        if len(warnings) > 10:
            print(f"  ... and {len(warnings) - 10} more")
        print()

    if not errors and not warnings:
        print("✅ No errors or warnings found in logs!")

    return {"errors": errors, "warnings": warnings}

# =============================================================================
# QUICK COMMANDS
# =============================================================================

def snapshot_all(client_id=None, sheet_id=None):
    """Take a full snapshot of everything."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'#'*70}")
    print(f"# FULL SNAPSHOT @ {timestamp}")
    print(f"{'#'*70}")

    check_outlook_sent(10)
    check_outlook_inbox(10)
    check_firestore_all(client_id)

    if sheet_id:
        check_sheet(sheet_id)

    print(f"\n{'#'*70}")
    print(f"# END SNAPSHOT")
    print(f"{'#'*70}\n")

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("")
        print("  OUTLOOK:")
        print("    python e2e_tools.py sent          - Check Outlook sent")
        print("    python e2e_tools.py inbox         - Check Outlook inbox")
        print("    python e2e_tools.py body MSG_ID   - Get email body")
        print("")
        print("  FIRESTORE:")
        print("    python e2e_tools.py firestore     - Check all Firestore collections")
        print("    python e2e_tools.py threads       - Check thread details")
        print("    python e2e_tools.py notifications - Check notifications")
        print("")
        print("  SHEETS:")
        print("    python e2e_tools.py sheet SHEET_ID - Check sheet state")
        print("    python e2e_tools.py row SHEET_ID N - Get row N values")
        print("")
        print("  GITHUB ACTIONS:")
        print("    python e2e_tools.py trigger       - Trigger scheduler workflow")
        print("    python e2e_tools.py runs          - List recent workflow runs")
        print("    python e2e_tools.py logs [RUN_ID] - View workflow logs")
        print("    python e2e_tools.py save-logs [RUN_ID] - Save logs to file")
        print("")
        print("  LOCAL RUNNER:")
        print("    python e2e_tools.py run-local     - Run main.py locally")
        print("    python e2e_tools.py list-logs     - List saved log files")
        print("    python e2e_tools.py review-logs   - Review logs for errors")
        print("")
        print("  COMBINED:")
        print("    python e2e_tools.py snapshot [SHEET_ID] - Full snapshot of everything")
        sys.exit(0)

    cmd = sys.argv[1]

    # OUTLOOK
    if cmd == "sent":
        check_outlook_sent()
    elif cmd == "inbox":
        check_outlook_inbox()
    elif cmd == "body" and len(sys.argv) > 2:
        print(get_email_body(sys.argv[2]))

    # FIRESTORE
    elif cmd == "firestore":
        client_id = get_client_id()
        check_firestore_all(client_id)
    elif cmd == "threads":
        check_threads_detail()
    elif cmd == "notifications":
        client_id = get_client_id()
        if client_id:
            check_notifications(client_id)

    # SHEETS
    elif cmd == "sheet" and len(sys.argv) > 2:
        check_sheet(sys.argv[2])
    elif cmd == "row" and len(sys.argv) > 3:
        get_sheet_row_values(sys.argv[2], int(sys.argv[3]))

    # GITHUB ACTIONS
    elif cmd == "trigger":
        trigger_scheduler()
    elif cmd == "runs":
        get_workflow_runs()
    elif cmd == "logs":
        run_id = sys.argv[2] if len(sys.argv) > 2 else None
        get_workflow_logs(run_id)
    elif cmd == "save-logs":
        run_id = sys.argv[2] if len(sys.argv) > 2 else None
        save_workflow_logs(run_id)

    # LOCAL RUNNER
    elif cmd == "run-local":
        run_local()
    elif cmd == "list-logs":
        list_logs()
    elif cmd == "review-logs":
        review_logs()

    # COMBINED
    elif cmd == "snapshot":
        client_id = get_client_id()
        sheet_id = sys.argv[2] if len(sys.argv) > 2 else None
        snapshot_all(client_id, sheet_id)

    else:
        print(f"Unknown command: {cmd}")
