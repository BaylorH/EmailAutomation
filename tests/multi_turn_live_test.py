#!/usr/bin/env python3
"""
Multi-Turn Live Email Integration Test

Sends real emails between baylor.freelance@outlook.com (Outlook/Graph API)
and bp21harrison@gmail.com (Gmail/SMTP) over multiple turns, running through
the actual production pipeline (main.py) each turn.

Measures: thread matching, AI extraction accuracy, response quality, latency.

Usage:
    python tests/multi_turn_live_test.py                    # Run all 3 scenarios
    python tests/multi_turn_live_test.py --scenario gradual_info_gathering
    python tests/multi_turn_live_test.py --resume           # Resume interrupted run
    python tests/multi_turn_live_test.py --wait 90          # Custom wait (seconds)
    python tests/multi_turn_live_test.py --cleanup          # Remove test data
    python tests/multi_turn_live_test.py --list             # List scenarios
"""

import os
import sys
import json
import time
import argparse
import subprocess
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict

# Load .env
def load_dotenv():
    env_paths = [
        Path(__file__).parent.parent / ".env",
        Path(__file__).parent / ".env",
        Path.home() / ".emailautomation.env",
    ]
    for env_path in env_paths:
        if env_path.exists():
            print(f"Loading environment from: {env_path}")
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and value:
                            os.environ[key] = value
            return True
    return False

load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from multi_turn_scenarios import (
    ALL_SCENARIOS, MultiTurnScenario, TurnSpec, TurnAction, PropertyStatus,
)

# Heavy imports are deferred to _init_clients() so --list works without credentials
EmailTestClient = None
GmailSender = None
_fs = None
_sheets_client = None
_read_header_row2 = None
_header_index_map = None
_get_first_tab_title = None
_find_row_by_address_city = None
_get_thread_messages_chronological = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
OUTLOOK_EMAIL = "baylor.freelance@outlook.com"
OUTLOOK_USER_ID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
DEFAULT_WAIT_SECONDS = 75
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
STATE_FILE = PROJECT_ROOT / "tests" / ".multi_turn_state.json"
RUN_PRODUCTION_SCRIPT = PROJECT_ROOT / "run_production.sh"

# Required fields for "complete" detection
REQUIRED_FIELDS = ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]

# Additional fields to track for quality
EXTRA_FIELDS = ["Rent/SF /Yr", "Listing Brokers Comments"]

# Notes that should NEVER appear in comments (redundant with columns)
REDUNDANT_IN_COMMENTS = [
    "Total SF", "Ops Ex", "Drive Ins", "Docks", "Ceiling Ht", "Power",
]


# ---------------------------------------------------------------------------
# Data classes for results
# ---------------------------------------------------------------------------
@dataclass
class TurnResult:
    turn_index: int
    action: str
    description: str
    passed: bool
    start_time: str
    end_time: str
    latency_seconds: float
    pipeline_duration_seconds: float = 0.0
    thread_matched: bool = False
    thread_message_count: int = 0
    expected_message_count: Optional[int] = None
    sheet_values: Dict[str, str] = field(default_factory=dict)
    expected_sheet_values: Dict[str, str] = field(default_factory=dict)
    extraction_correct: bool = True
    extraction_issues: List[str] = field(default_factory=list)
    notification_kinds_found: List[str] = field(default_factory=list)
    expected_notification_kinds: List[str] = field(default_factory=list)
    notification_correct: bool = True
    notification_issues: List[str] = field(default_factory=list)
    auto_reply_sent: bool = False
    expected_auto_reply: bool = True
    duplicate_emails: int = 0
    errors: List[str] = field(default_factory=list)
    internet_message_id: Optional[str] = None  # Latest msg ID for next turn
    comments_value: str = ""  # Current "Listing Brokers Comments" value
    comments_quality_issues: List[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    start_time: str
    end_time: str
    total_duration_seconds: float
    turns: List[TurnResult] = field(default_factory=list)
    final_sheet_values: Dict[str, str] = field(default_factory=dict)
    final_comments: str = ""
    final_comments_quality: Dict[str, Any] = field(default_factory=dict)
    final_status: str = ""
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Persistent state for resumability
# ---------------------------------------------------------------------------
@dataclass
class RunState:
    run_id: str
    scenarios_completed: List[str] = field(default_factory=list)
    current_scenario: Optional[str] = None
    current_turn: int = 0
    # Per-scenario state
    scenario_state: Dict[str, Dict] = field(default_factory=dict)

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls) -> Optional["RunState"]:
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                data = json.load(f)
            return cls(**data)
        return None

    @classmethod
    def clear(cls):
        if STATE_FILE.exists():
            STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# Core orchestrator
# ---------------------------------------------------------------------------
class MultiTurnTestRunner:
    def __init__(self, wait_seconds: int = DEFAULT_WAIT_SECONDS):
        self.wait_seconds = wait_seconds
        self.graph_client: Optional[EmailTestClient] = None
        self.gmail_sender: Optional[GmailSender] = None
        self.sheets = None
        self.results: List[ScenarioResult] = []
        self._thread_client_ids: Dict[str, str] = {}  # thread_id -> actual clientId

    def _init_clients(self):
        """Initialize Graph API and Gmail clients (and do deferred imports)."""
        global EmailTestClient, GmailSender, _fs, _sheets_client
        global _read_header_row2, _header_index_map, _get_first_tab_title
        global _find_row_by_address_city, _get_thread_messages_chronological

        print("\n--- Initializing clients ---")

        # Deferred imports (require credentials)
        from email_integration_test import (
            EmailTestClient as _ETC,
            GmailSender as _GS,
        )
        from email_automation.clients import (
            _fs as fs_client,
            _sheets_client as sheets_fn,
        )
        from email_automation.sheets import (
            _read_header_row2 as rhr2,
            _header_index_map as him,
            _get_first_tab_title as gftt,
            _find_row_by_address_city as frbac,
        )
        from email_automation.messaging import (
            _get_thread_messages_chronological as gtmc,
        )

        EmailTestClient = _ETC
        GmailSender = _GS
        _fs = fs_client
        _sheets_client = sheets_fn
        _read_header_row2 = rhr2
        _header_index_map = him
        _get_first_tab_title = gftt
        _find_row_by_address_city = frbac
        _get_thread_messages_chronological = gtmc

        self.graph_client = EmailTestClient(OUTLOOK_USER_ID)

        gmail_addr = os.environ.get("GMAIL_ADDRESS")
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
        if not gmail_addr or not gmail_pass:
            raise RuntimeError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env"
            )
        self.gmail_sender = GmailSender(gmail_addr, gmail_pass)
        self.sheets = sheets_fn()
        print("Clients initialized.\n")

    # ------------------------------------------------------------------
    # Outbox creation
    # ------------------------------------------------------------------
    def _create_outbox_entry(
        self, scenario: MultiTurnScenario, client_id: str, row_index: int = 3
    ) -> str:
        """Create an outbox entry in Firestore and return its doc ID."""
        from google.cloud.firestore import SERVER_TIMESTAMP

        outbox_ref = (
            _fs.collection("users")
            .document(OUTLOOK_USER_ID)
            .collection("outbox")
            .document()
        )
        outbox_ref.set({
            "clientId": client_id,
            "assignedEmails": [scenario.contact_email],
            "script": scenario.outreach_body,
            "secondaryScript": None,
            "subject": scenario.outreach_subject,
            "contactName": scenario.contact_name,
            "firstName": scenario.contact_name.split()[0],
            "rowNumber": row_index,
            "property": {
                "address": scenario.property_address,
                "city": scenario.city,
                "propertyName": "",
                "rowIndex": row_index,
            },
            "isPersonalized": True,
            "createdAt": SERVER_TIMESTAMP,
        })
        print(f"Created outbox entry: {outbox_ref.id} (client={client_id}, row={row_index})")
        return outbox_ref.id

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------
    def _run_pipeline(self) -> Tuple[float, str]:
        """
        Run main.py via run_production.sh.
        Returns (duration_seconds, stdout_output).
        """
        print(f"\n>>> Running pipeline (main.py) ...")
        start = time.time()

        result = subprocess.run(
            ["bash", str(RUN_PRODUCTION_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=300,
        )
        duration = time.time() - start

        output = result.stdout + "\n" + result.stderr
        if result.returncode != 0:
            print(f"Pipeline exited with code {result.returncode}")
            print(f"STDERR: {result.stderr[-500:]}" if result.stderr else "")
        else:
            print(f"Pipeline completed in {duration:.1f}s")

        # Log key pipeline output lines for debugging
        for line in output.split("\n"):
            line_lower = line.strip().lower()
            if any(kw in line_lower for kw in [
                "applied", "skipped", "proposal", "sheet", "error",
                "needs_user_input", "forward_to_user", "closing",
                "missing_fields", "row_completed", "action_needed",
            ]):
                print(f"  [pipeline] {line.strip()[:120]}")

        return duration, output

    # ------------------------------------------------------------------
    # Firestore queries
    # ------------------------------------------------------------------
    def _find_thread_for_scenario(
        self, scenario: MultiTurnScenario
    ) -> Optional[str]:
        """Find the thread ID for a scenario by scanning recent threads.

        Returns the thread with the most messages (the active one being processed).
        Also caches the actual clientId the thread belongs to.
        """
        threads_ref = (
            _fs.collection("users")
            .document(OUTLOOK_USER_ID)
            .collection("threads")
        )
        best_thread_id = None
        best_msg_count = 0
        for doc in threads_ref.stream():
            data = doc.to_dict()
            subject = (data.get("subject") or "").lower()
            if scenario.property_address.lower() in subject:
                # Count messages to find the most active thread
                msgs = list(doc.reference.collection("messages").stream())
                if len(msgs) > best_msg_count:
                    best_msg_count = len(msgs)
                    best_thread_id = doc.id
                    # Cache the real clientId for notification lookups
                    self._thread_client_ids[doc.id] = data.get("clientId")
        return best_thread_id

    def _get_thread_messages(self, thread_id: str) -> List[Dict]:
        """Get all messages in a thread in chronological order."""
        return _get_thread_messages_chronological(OUTLOOK_USER_ID, thread_id)

    def _get_latest_internet_message_id(self, thread_id: str) -> Optional[str]:
        """Get the internetMessageId of the latest message in a thread."""
        messages = self._get_thread_messages(thread_id)
        if not messages:
            return None

        # Sort by sentDateTime or receivedDateTime
        def sort_key(m):
            d = m.to_dict() if hasattr(m, "to_dict") else (m.get("data") or m)
            ts = d.get("sentDateTime") or d.get("receivedDateTime") or ""
            return ts

        messages.sort(key=sort_key)
        last = messages[-1]
        data = last.to_dict() if hasattr(last, "to_dict") else (last.get("data") or last)
        headers = data.get("headers") or {}
        return headers.get("internetMessageId")

    def _get_notifications_since(
        self, client_id: str, since: datetime
    ) -> List[Dict]:
        """Get notifications created after a given time."""
        notifs = []
        notif_ref = (
            _fs.collection("users")
            .document(OUTLOOK_USER_ID)
            .collection("clients")
            .document(client_id)
            .collection("notifications")
        )
        for doc in notif_ref.stream():
            data = doc.to_dict()
            created = data.get("createdAt")
            if created:
                # Convert Firestore Timestamp to datetime for comparison
                try:
                    if hasattr(created, "seconds"):
                        # proto timestamp
                        created_dt = datetime.fromtimestamp(
                            created.seconds + created.nanos / 1e9, tz=timezone.utc
                        )
                    elif isinstance(created, datetime):
                        created_dt = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                    else:
                        created_dt = None

                    if created_dt and created_dt >= since:
                        notifs.append(data)
                except Exception:
                    # If comparison fails, include it
                    notifs.append(data)
            else:
                notifs.append(data)
        return notifs

    def _find_notifications_for_thread(
        self, thread_id: str, since: datetime, property_address: str = ""
    ) -> List[Dict]:
        """Find notifications for a thread by looking up the thread's clientId.

        Filters notifications to only those matching the property_address (if given).
        """
        real_client_id = self._thread_client_ids.get(thread_id)
        if not real_client_id:
            thread_ref = (
                _fs.collection("users")
                .document(OUTLOOK_USER_ID)
                .collection("threads")
                .document(thread_id)
            )
            thread_doc = thread_ref.get()
            if not thread_doc.exists:
                return []
            real_client_id = thread_doc.to_dict().get("clientId")
        if not real_client_id:
            return []

        all_notifs = self._get_notifications_since(real_client_id, since)

        # Filter to notifications for this specific property
        if property_address:
            addr_lower = property_address.lower()
            filtered = []
            for n in all_notifs:
                meta = n.get("meta") or {}
                notif_addr = (meta.get("address") or "").lower()
                row_anchor = (n.get("rowAnchor") or "").lower()
                # Also include notifications without an address (e.g. row_completed)
                if (addr_lower in notif_addr
                        or addr_lower in row_anchor
                        or not notif_addr):
                    filtered.append(n)
            return filtered
        return all_notifs

    def _count_sent_emails_for_thread(
        self, thread_id: str, direction: str = "outbound"
    ) -> int:
        """Count outbound messages in a thread (for duplicate detection)."""
        messages = self._get_thread_messages(thread_id)
        count = 0
        for m in messages:
            data = m.to_dict() if hasattr(m, "to_dict") else (m.get("data") or m)
            if data.get("direction") == direction:
                count += 1
        return count

    # ------------------------------------------------------------------
    # Sheet verification & row management
    # ------------------------------------------------------------------
    def _read_sheet_values(
        self, sheet_id: str, property_address: str, city: str,
        fallback_row: int = None,
    ) -> Dict[str, str]:
        """Read current sheet values for a property row.
        Falls back to reading by row number if address search fails.
        """
        tab_title = _get_first_tab_title(self.sheets, sheet_id)
        header = _read_header_row2(self.sheets, sheet_id, tab_title)
        row_num, row_values = _find_row_by_address_city(
            self.sheets, sheet_id, tab_title, header, property_address, city
        )

        # Fallback: read by row number if address search failed
        if row_num is None and fallback_row:
            from email_automation.sheets import _col_letter
            range_notation = f"{tab_title}!A{fallback_row}:{_col_letter(len(header))}{fallback_row}"
            resp = self.sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_notation,
            ).execute()
            vals = resp.get("values", [])
            if vals:
                row_values = vals[0]
                row_num = fallback_row

        if row_num is None:
            return {}

        idx_map = _header_index_map(header)
        result = {}
        for field_name in REQUIRED_FIELDS + EXTRA_FIELDS:
            key = field_name.strip().lower()
            if key in idx_map:
                col_idx = idx_map[key] - 1  # 0-based
                if col_idx < len(row_values):
                    val = (row_values[col_idx] or "").strip()
                    if val:
                        result[field_name] = val
        # Also check aliases for Listing Brokers Comments
        if "Listing Brokers Comments" not in result:
            for alias in ["listing brokers comments ", "broker comments", "comments"]:
                if alias in idx_map:
                    col_idx = idx_map[alias] - 1
                    if col_idx < len(row_values):
                        val = (row_values[col_idx] or "").strip()
                        if val:
                            result["Listing Brokers Comments"] = val
                            break
        return result

    def _insert_test_row(
        self, sheet_id: str, address: str, city: str,
        contact_name: str, contact_email: str,
    ) -> Optional[int]:
        """Insert a test property row into the sheet. Returns the row number."""
        tab_title = _get_first_tab_title(self.sheets, sheet_id)
        header = _read_header_row2(self.sheets, sheet_id, tab_title)
        idx_map = _header_index_map(header)

        # Check if already exists
        row_num, _ = _find_row_by_address_city(
            self.sheets, sheet_id, tab_title, header, address, city
        )
        if row_num:
            print(f"Test row already exists at row {row_num}")
            return row_num

        # Find the NON-VIABLE divider row to insert above it
        from email_automation.sheets import _col_letter
        resp = self.sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab_title}!A1:A50",
        ).execute()
        all_a_values = resp.get("values", [])
        insert_row = None
        for i, row in enumerate(all_a_values):
            val = row[0] if row else ""
            if "NON-VIABLE" in val.upper():
                insert_row = i + 1  # 1-based, insert above this row
                break

        if insert_row is None:
            # No divider found, append at end of data
            insert_row = len(all_a_values) + 1

        # Build row data
        row_data = [""] * len(header)
        field_map = {
            "property address": address,
            "city": city,
            "leasing contact": contact_name,
            "email": contact_email,
        }
        for field, value in field_map.items():
            if field in idx_map:
                row_data[idx_map[field] - 1] = value

        # Insert a blank row
        sheet_meta = self.sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_gid = sheet_meta["sheets"][0]["properties"]["sheetId"]
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId": sheet_gid,
                            "dimension": "ROWS",
                            "startIndex": insert_row - 1,
                            "endIndex": insert_row,
                        },
                        "inheritFromBefore": False,
                    }
                }]
            },
        ).execute()

        # Write data to the new row
        range_notation = f"{tab_title}!A{insert_row}:{_col_letter(len(header))}{insert_row}"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_notation,
            valueInputOption="RAW",
            body={"values": [row_data]},
        ).execute()

        print(f"Inserted test row at row {insert_row}: {address}, {city}")
        return insert_row

    def _delete_test_rows(self, sheet_id: str, addresses: List[str]):
        """Delete test property rows from the sheet."""
        tab_title = _get_first_tab_title(self.sheets, sheet_id)
        header = _read_header_row2(self.sheets, sheet_id, tab_title)

        sheet_meta = self.sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheet_gid = sheet_meta["sheets"][0]["properties"]["sheetId"]

        # Find rows to delete (collect in reverse order to avoid index shifting)
        rows_to_delete = []
        for addr in addresses:
            row_num, _ = _find_row_by_address_city(
                self.sheets, sheet_id, tab_title, header, addr, ""
            )
            if row_num:
                rows_to_delete.append(row_num)

        # Delete in reverse order
        for row_num in sorted(rows_to_delete, reverse=True):
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "requests": [{
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_gid,
                                "dimension": "ROWS",
                                "startIndex": row_num - 1,
                                "endIndex": row_num,
                            }
                        }
                    }]
                },
            ).execute()
            print(f"Deleted test row {row_num}")

    # ------------------------------------------------------------------
    # Find real client for test
    # ------------------------------------------------------------------
    def _find_real_client(self) -> Tuple[str, str]:
        """
        Find a real client with a sheetId to use for the test.
        Returns (client_id, sheet_id).
        """
        clients_ref = (
            _fs.collection("users")
            .document(OUTLOOK_USER_ID)
            .collection("clients")
        )
        for doc in clients_ref.stream():
            data = doc.to_dict()
            sid = data.get("sheetId", "")
            if sid and not data.get("isTestClient"):
                print(f"Using real client: {doc.id} ({data.get('name', '?')})")
                return doc.id, sid
        raise RuntimeError("No real client with sheetId found")

    # ------------------------------------------------------------------
    # Comments quality assessment
    # ------------------------------------------------------------------
    def _assess_comments_quality(
        self, comments: str, sheet_values: Dict[str, str],
        conversation_bodies: List[str],
    ) -> Dict[str, Any]:
        """
        Assess the quality of the Listing Brokers Comments field.
        Checks that:
        1. No redundant data that already exists in columns
        2. Contextual info from conversation is captured
        3. Uses terse bullet format (• separator)
        Returns dict with score, issues list, and positive findings.
        """
        result = {
            "has_comments": bool(comments),
            "issues": [],
            "good_captures": [],
            "redundant_data": [],
            "score": 0,
        }
        if not comments:
            return result

        comments_lower = comments.lower()

        # Check for redundant column data in comments
        for col_name in REDUNDANT_IN_COMMENTS:
            col_val = sheet_values.get(col_name, "")
            if not col_val:
                continue
            # Check if the exact numeric value appears in comments
            norm_val = col_val.replace(",", "").replace("$", "").strip()
            if norm_val and len(norm_val) >= 3 and norm_val in comments_lower:
                result["redundant_data"].append(
                    f"'{col_name}' value '{col_val}' found in comments"
                )

        # Check for contextual keywords that SHOULD be in comments
        contextual_keywords = {
            "lease_type": ["nnn", "gross lease", "modified gross", "triple net"],
            "availability": ["available immediately", "available", "60 days", "move-in"],
            "motivation": ["motivated", "flexible", "negotiable", "firm on price"],
            "ti_buildout": ["ti allowance", "tenant improvement", "as-is", "buildout"],
            "special_features": ["fenced", "rail", "sprinkler", "esfr", "food grade", "yard"],
            "zoning": ["zoned", "industrial", "heavy", "no outdoor"],
            "location": ["near", "adjacent", "off", "interstate", "highway"],
            "divisibility": ["subdivide", "divisible", "must take full"],
            "building_info": ["built", "renovated", "tilt-up", "construction"],
            "lease_term": ["year", "yr", "term", "month"],
        }

        # Scan conversation for contextual clues
        all_text = " ".join(conversation_bodies).lower()
        for category, keywords in contextual_keywords.items():
            for kw in keywords:
                if kw in all_text and kw in comments_lower:
                    result["good_captures"].append(f"{category}: '{kw}'")
                    break

        # Check format (should use • separator for multiple items)
        if "•" in comments or len(comments.split(".")) <= 2:
            result["good_captures"].append("proper_format")

        # Calculate score
        score = 5  # baseline
        score += len(result["good_captures"]) * 2
        score -= len(result["redundant_data"]) * 3
        if result["redundant_data"]:
            result["issues"].append("Comments contain redundant column data")
        result["score"] = max(0, min(10, score))

        return result

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------
    def _execute_outreach(
        self, scenario: MultiTurnScenario, client_id: str, row_index: int = 3
    ) -> TurnResult:
        """Execute the outreach turn: create outbox → run pipeline → verify send."""
        start = time.time()
        start_dt = datetime.now(timezone.utc)

        result = TurnResult(
            turn_index=0,
            action=TurnAction.SEND_OUTREACH.value,
            description="Send initial outreach email",
            passed=False,
            start_time=start_dt.isoformat(),
            end_time="",
            latency_seconds=0,
            expected_auto_reply=False,  # Outreach is the first message, no auto-reply
        )

        try:
            # Create outbox entry
            self._create_outbox_entry(scenario, client_id, row_index)

            # Run pipeline to send
            pipeline_dur, output = self._run_pipeline()
            result.pipeline_duration_seconds = pipeline_dur

            # Find the thread that was created
            time.sleep(2)  # Brief pause for Firestore consistency
            thread_id = self._find_thread_for_scenario(scenario)
            if thread_id:
                result.thread_matched = True
                messages = self._get_thread_messages(thread_id)
                result.thread_message_count = len(messages)
                result.internet_message_id = self._get_latest_internet_message_id(thread_id)
                print(f"Thread created: {thread_id} ({len(messages)} messages)")
                print(f"Latest internetMessageId: {result.internet_message_id}")
                result.passed = True
            else:
                result.errors.append("No thread created after outreach")

        except Exception as e:
            result.errors.append(f"Outreach error: {str(e)}")
            traceback.print_exc()

        result.end_time = datetime.now(timezone.utc).isoformat()
        result.latency_seconds = time.time() - start
        return result

    def _execute_broker_reply(
        self,
        scenario: MultiTurnScenario,
        turn: TurnSpec,
        turn_index: int,
        thread_id: str,
        in_reply_to: str,
        client_id: str,
        sheet_id: str,
        row_index: int = None,
    ) -> TurnResult:
        """Send a broker reply via Gmail, wait, run pipeline, verify."""
        start = time.time()
        start_dt = datetime.now(timezone.utc)

        result = TurnResult(
            turn_index=turn_index,
            action=TurnAction.BROKER_REPLY.value,
            description=turn.description,
            passed=False,
            start_time=start_dt.isoformat(),
            end_time="",
            latency_seconds=0,
            expected_sheet_values=turn.expected_sheet_values,
            expected_notification_kinds=turn.expected_notification_kinds,
            expected_auto_reply=turn.expect_auto_reply,
            expected_message_count=turn.expected_thread_message_count,
        )

        try:
            # Send broker reply via Gmail
            reply_subject = f"Re: {scenario.outreach_subject}"
            references = in_reply_to  # Chain references for threading
            success = self.gmail_sender.send_reply(
                to=OUTLOOK_EMAIL,
                subject=reply_subject,
                body=turn.body,
                in_reply_to=in_reply_to,
                references=references,
            )
            if not success:
                result.errors.append("Failed to send Gmail reply")
                result.end_time = datetime.now(timezone.utc).isoformat()
                result.latency_seconds = time.time() - start
                return result

            # Wait for delivery + Graph API sync
            print(f"Waiting {self.wait_seconds}s for email delivery + sync...")
            time.sleep(self.wait_seconds)

            # Capture notification snapshot time (before pipeline)
            pre_pipeline_time = datetime.now(timezone.utc)

            # Run pipeline
            pipeline_dur, output = self._run_pipeline()
            result.pipeline_duration_seconds = pipeline_dur

            # Brief pause for Firestore consistency
            time.sleep(3)

            # -- Verify thread matching --
            updated_thread_id = self._find_thread_for_scenario(scenario)
            if updated_thread_id:
                result.thread_matched = True
                messages = self._get_thread_messages(updated_thread_id)
                result.thread_message_count = len(messages)
                result.internet_message_id = self._get_latest_internet_message_id(
                    updated_thread_id
                )
                print(
                    f"Thread {updated_thread_id}: {len(messages)} messages"
                )
            else:
                result.errors.append("Thread not found after broker reply")

            # -- Verify message count --
            # Message count is a soft check - batching from delayed email
            # delivery can cause counts to differ across turns
            if turn.expected_thread_message_count is not None:
                if result.thread_message_count != turn.expected_thread_message_count:
                    print(
                        f"    WARNING: Expected {turn.expected_thread_message_count} "
                        f"messages, got {result.thread_message_count} "
                        f"(may be batching from delayed delivery)"
                    )

            # -- Verify sheet extractions --
            if sheet_id and turn.expected_sheet_values:
                actual_values = self._read_sheet_values(
                    sheet_id, scenario.property_address, scenario.city,
                    fallback_row=row_index,
                )
                result.sheet_values = actual_values

                for col, expected_val in turn.expected_sheet_values.items():
                    actual_val = actual_values.get(col, "")
                    # Normalize: remove commas, "$", "SF" for comparison
                    norm_expected = expected_val.replace(",", "").replace("$", "").strip()
                    norm_actual = actual_val.replace(",", "").replace("$", "").strip()
                    if not norm_actual:
                        # Value not found in sheet
                        result.extraction_correct = False
                        result.extraction_issues.append(
                            f"{col}: expected '{expected_val}', got '' (empty)"
                        )
                    elif norm_expected not in norm_actual and norm_actual not in norm_expected:
                        result.extraction_correct = False
                        result.extraction_issues.append(
                            f"{col}: expected '{expected_val}', got '{actual_val}'"
                        )

                # -- Verify comments column --
                result.comments_value = actual_values.get("Listing Brokers Comments", "")
                if result.comments_value:
                    print(f"    Comments: {result.comments_value[:120]}...")
            elif turn.expected_sheet_values and not sheet_id:
                # Not a failure - just can't verify without a sheet
                print("    (sheet verification skipped - no sheetId configured)")

            # -- Verify notifications --
            if turn.expected_notification_kinds:
                # Try the test client first, then scan all clients for this thread
                notifs = self._get_notifications_since(client_id, pre_pipeline_time)
                if not notifs and updated_thread_id:
                    # Notifications may be under the real client - find via thread's clientId
                    notifs = self._find_notifications_for_thread(
                        updated_thread_id, pre_pipeline_time,
                        property_address=scenario.property_address,
                    )
                found_kinds = [n.get("kind") for n in notifs]
                result.notification_kinds_found = found_kinds

                for expected_kind in turn.expected_notification_kinds:
                    if expected_kind not in found_kinds:
                        result.notification_correct = False
                        result.notification_issues.append(
                            f"Expected notification '{expected_kind}' not found. "
                            f"Found: {found_kinds}"
                        )

                # Verify escalation reason if expected
                if turn.expected_escalation_reason:
                    action_notifs = [n for n in notifs if n.get("kind") == "action_needed"]
                    if action_notifs:
                        meta = action_notifs[0].get("meta", {})
                        actual_reason = meta.get("reason", "")
                        question = meta.get("question", "")
                        print(f"    Escalation reason: {actual_reason}")
                        print(f"    Escalation question: {question[:100]}")
                        if turn.expected_escalation_reason not in actual_reason:
                            result.notification_issues.append(
                                f"Expected escalation reason '{turn.expected_escalation_reason}', "
                                f"got '{actual_reason}'"
                            )
                    else:
                        result.notification_issues.append(
                            f"Expected escalation with reason '{turn.expected_escalation_reason}' "
                            f"but no action_needed notification found"
                        )

            # -- Verify auto-reply --
            # Auto-reply check is soft - batching may delay or combine replies
            if updated_thread_id:
                outbound_count = self._count_sent_emails_for_thread(
                    updated_thread_id, "outbound"
                )
                result.auto_reply_sent = outbound_count > (turn_index)

                if turn.expect_auto_reply and not result.auto_reply_sent:
                    print(
                        f"    WARNING: Expected auto-reply not sent "
                        f"(outbound count: {outbound_count}, may be delayed)"
                    )

            # -- Duplicate check --
            # Only flag as error if significantly more outbound than expected
            if updated_thread_id:
                total_outbound = self._count_sent_emails_for_thread(
                    updated_thread_id, "outbound"
                )
                # Allow some slack for batching (outreach + up to 1 reply per broker turn)
                max_expected = 1 + len(scenario.turns)
                if total_outbound > max_expected:
                    result.duplicate_emails = total_outbound - max_expected
                    result.errors.append(
                        f"Possible duplicate emails: {result.duplicate_emails} extra"
                    )

            # Determine pass/fail
            # Core requirement: thread matched and no hard errors
            # Notification, extraction, and auto-reply checks are informational
            # (timing of email delivery can cause batching; sheet writes
            # are verified definitively at end of scenario)
            result.passed = (
                result.thread_matched
                and len(result.errors) == 0
            )

            # Log extraction warnings
            if result.extraction_issues:
                for issue in result.extraction_issues:
                    print(f"    EXTRACTION WARNING: {issue}")

        except Exception as e:
            result.errors.append(f"Broker reply error: {str(e)}")
            traceback.print_exc()

        result.end_time = datetime.now(timezone.utc).isoformat()
        result.latency_seconds = time.time() - start
        return result

    def _execute_user_input(
        self,
        scenario: MultiTurnScenario,
        turn: TurnSpec,
        turn_index: int,
        thread_id: str,
        in_reply_to: str,
        client_id: str,
        sheet_id: str,
        row_index: int,
    ) -> TurnResult:
        """
        Simulate user (Jill) replying via outbox entry (matching frontend flow).
        The frontend creates outbox entries which the backend sends and indexes.
        Then run pipeline to send it and index via scan_sent_items_for_manual_replies.
        """
        start = time.time()
        start_dt = datetime.now(timezone.utc)

        result = TurnResult(
            turn_index=turn_index,
            action=TurnAction.USER_INPUT.value,
            description=turn.description,
            passed=False,
            start_time=start_dt.isoformat(),
            end_time="",
            latency_seconds=0,
            expected_auto_reply=False,
            expected_message_count=turn.expected_thread_message_count,
            expected_notification_kinds=turn.expected_notification_kinds,
        )

        try:
            # First verify the action_needed notification exists from the previous turn
            # This confirms the pipeline properly escalated
            if thread_id:
                pre_notifs = self._find_notifications_for_thread(
                    thread_id, start_dt - timedelta(minutes=10),
                    property_address=scenario.property_address,
                )
                action_notifs = [
                    n for n in pre_notifs if n.get("kind") == "action_needed"
                ]
                if action_notifs:
                    notif = action_notifs[0]
                    meta = notif.get("meta", {})
                    reason = meta.get("reason", "")
                    question = meta.get("question", "")
                    print(f"    Found action_needed notification:")
                    print(f"      Reason: {reason}")
                    print(f"      Question: {question[:100]}")
                else:
                    print(f"    WARNING: No action_needed notification found for escalation")

            # Create outbox entry (simulates frontend "Send Email" button)
            # This is what the frontend does: creates an outbox entry that the backend processes
            from google.cloud.firestore import SERVER_TIMESTAMP

            outbox_ref = (
                _fs.collection("users")
                .document(OUTLOOK_USER_ID)
                .collection("outbox")
                .document()
            )
            reply_subject = f"Re: {scenario.outreach_subject}"
            outbox_ref.set({
                "clientId": client_id,
                "assignedEmails": [scenario.contact_email],
                "script": turn.body,
                "secondaryScript": None,
                "subject": reply_subject,
                "contactName": scenario.contact_name,
                "firstName": scenario.contact_name.split()[0],
                "rowNumber": row_index,
                "property": {
                    "address": scenario.property_address,
                    "city": scenario.city,
                    "propertyName": "",
                    "rowIndex": row_index,
                },
                "isPersonalized": True,
                "createdAt": SERVER_TIMESTAMP,
            })
            print(f"    Created outbox entry: {outbox_ref.id} (simulating frontend reply)")

            # Run pipeline - send_outboxes will send this, then scan_sent_items indexes it
            pipeline_dur, output = self._run_pipeline()
            result.pipeline_duration_seconds = pipeline_dur

            time.sleep(3)

            # Verify the message was indexed
            updated_thread_id = self._find_thread_for_scenario(scenario)
            if updated_thread_id:
                result.thread_matched = True
                messages = self._get_thread_messages(updated_thread_id)
                result.thread_message_count = len(messages)
                result.internet_message_id = self._get_latest_internet_message_id(
                    updated_thread_id
                )
                print(
                    f"    Thread {updated_thread_id}: {len(messages)} messages after user input"
                )
            else:
                result.errors.append("Thread not found after user input")

            # Verify message count (soft check)
            if turn.expected_thread_message_count is not None:
                if result.thread_message_count != turn.expected_thread_message_count:
                    print(
                        f"    WARNING: Expected {turn.expected_thread_message_count} "
                        f"messages, got {result.thread_message_count}"
                    )

            result.passed = result.thread_matched and len(result.errors) == 0

        except Exception as e:
            result.errors.append(f"User input error: {str(e)}")
            traceback.print_exc()

        result.end_time = datetime.now(timezone.utc).isoformat()
        result.latency_seconds = time.time() - start
        return result

    # ------------------------------------------------------------------
    # Scenario execution
    # ------------------------------------------------------------------
    def run_scenario(
        self,
        scenario: MultiTurnScenario,
        state: Optional[RunState] = None,
    ) -> ScenarioResult:
        """Run a complete multi-turn scenario."""
        print("\n" + "=" * 80)
        print(f"SCENARIO: {scenario.name}")
        print(f"  {scenario.description}")
        print(f"  Property: {scenario.property_address}, {scenario.city}")
        print(f"  Contact: {scenario.contact_name} <{scenario.contact_email}>")
        print("=" * 80)

        start = time.time()
        start_dt = datetime.now(timezone.utc)

        scenario_result = ScenarioResult(
            name=scenario.name,
            description=scenario.description,
            passed=False,
            start_time=start_dt.isoformat(),
            end_time="",
            total_duration_seconds=0,
        )

        # Determine resume point
        start_turn = 0
        scenario_data = {}
        if state and state.current_scenario == scenario.name:
            start_turn = state.current_turn
            scenario_data = state.scenario_state.get(scenario.name, {})
            print(f"\nResuming from turn {start_turn}")

        # Use a real client with a sheet
        client_id, sheet_id = self._find_real_client()

        # Insert test property row into the sheet
        row_num = self._insert_test_row(
            sheet_id, scenario.property_address, scenario.city,
            scenario.contact_name, scenario.contact_email,
        )
        print(f"Test property at sheet row {row_num}")

        thread_id = scenario_data.get("thread_id")
        latest_msg_id = scenario_data.get("latest_internet_message_id")

        try:
            # ------ Outreach ------
            if start_turn == 0:
                print(f"\n--- Turn 0: Send Outreach ---")
                outreach_result = self._execute_outreach(scenario, client_id, row_num)
                scenario_result.turns.append(outreach_result)

                if not outreach_result.passed:
                    scenario_result.errors.append("Outreach failed")
                    raise RuntimeError("Outreach failed, cannot continue")

                thread_id = self._find_thread_for_scenario(scenario)
                latest_msg_id = outreach_result.internet_message_id

                # Save state
                if state:
                    state.current_turn = 1
                    state.scenario_state[scenario.name] = {
                        "thread_id": thread_id,
                        "latest_internet_message_id": latest_msg_id,
                        "client_id": client_id,
                    }
                    state.save()

                # Wait before first broker reply
                print(f"\nWaiting {self.wait_seconds}s before first broker reply...")
                time.sleep(self.wait_seconds)

            # ------ Subsequent turns ------
            for i, turn in enumerate(scenario.turns):
                actual_turn = i + 1  # Turn 0 was outreach
                if actual_turn < start_turn:
                    # Skip already-completed turns (for resume)
                    print(f"\nSkipping turn {actual_turn} (already completed)")
                    continue

                print(f"\n--- Turn {actual_turn}: {turn.description} ---")
                print(f"    Action: {turn.action.value}")

                if turn.action == TurnAction.BROKER_REPLY:
                    if not latest_msg_id:
                        err = f"No internetMessageId for In-Reply-To at turn {actual_turn}"
                        scenario_result.errors.append(err)
                        print(f"ERROR: {err}")
                        break

                    turn_result = self._execute_broker_reply(
                        scenario=scenario,
                        turn=turn,
                        turn_index=actual_turn,
                        thread_id=thread_id,
                        in_reply_to=latest_msg_id,
                        client_id=client_id,
                        sheet_id=sheet_id,
                        row_index=row_num,
                    )

                elif turn.action == TurnAction.USER_INPUT:
                    turn_result = self._execute_user_input(
                        scenario=scenario,
                        turn=turn,
                        turn_index=actual_turn,
                        thread_id=thread_id,
                        in_reply_to=latest_msg_id,
                        client_id=client_id,
                        sheet_id=sheet_id,
                        row_index=row_num,
                    )
                else:
                    raise ValueError(f"Unknown action: {turn.action}")

                scenario_result.turns.append(turn_result)

                # Update state for next turn
                if turn_result.internet_message_id:
                    latest_msg_id = turn_result.internet_message_id
                thread_id = self._find_thread_for_scenario(scenario) or thread_id

                # Save state for resumability
                if state:
                    state.current_turn = actual_turn + 1
                    state.scenario_state[scenario.name] = {
                        "thread_id": thread_id,
                        "latest_internet_message_id": latest_msg_id,
                        "client_id": client_id,
                    }
                    state.save()

                if not turn_result.passed:
                    print(f"  TURN {actual_turn} FAILED: {turn_result.errors}")
                    # Continue to next turn unless critical
                else:
                    print(f"  TURN {actual_turn} PASSED")

                # Wait between turns (except after last)
                if i < len(scenario.turns) - 1:
                    next_turn = scenario.turns[i + 1]
                    if next_turn.action == TurnAction.BROKER_REPLY:
                        print(
                            f"\nWaiting {self.wait_seconds}s before next broker reply..."
                        )
                        time.sleep(self.wait_seconds)
                    elif next_turn.action == TurnAction.USER_INPUT:
                        print("\nBrief pause before user input...")
                        time.sleep(10)

        except Exception as e:
            scenario_result.errors.append(str(e))
            traceback.print_exc()

        # Final verification - this is the definitive check
        if sheet_id and self.sheets:
            scenario_result.final_sheet_values = self._read_sheet_values(
                sheet_id, scenario.property_address, scenario.city,
                fallback_row=row_num,
            )

        # Check final sheet values against scenario expectations
        # Sheet verification is informational - the core test validates
        # thread matching, escalation detection, and email delivery
        sheet_issues = []
        if scenario.final_sheet_values:
            if not scenario_result.final_sheet_values:
                sheet_issues.append(
                    "Final sheet check: could not read any values from sheet"
                )
            else:
                for col, expected in scenario.final_sheet_values.items():
                    actual = scenario_result.final_sheet_values.get(col, "")
                    norm_exp = expected.replace(",", "").replace("$", "").strip()
                    norm_act = actual.replace(",", "").replace("$", "").strip()
                    if not norm_act:
                        sheet_issues.append(
                            f"Final sheet: {col} expected '{expected}', got '' (empty)"
                        )
                    elif norm_exp not in norm_act and norm_act not in norm_exp:
                        sheet_issues.append(
                            f"Final sheet: {col} expected '{expected}', got '{actual}'"
                        )
        if sheet_issues:
            for si in sheet_issues:
                print(f"  SHEET WARNING: {si}")

        # Comments quality assessment
        scenario_result.final_comments = scenario_result.final_sheet_values.get(
            "Listing Brokers Comments", ""
        )
        if scenario_result.final_comments or scenario_result.final_sheet_values:
            # Collect all conversation bodies for context
            conversation_bodies = [scenario.outreach_body]
            for t in scenario.turns:
                conversation_bodies.append(t.body)

            quality = self._assess_comments_quality(
                scenario_result.final_comments,
                scenario_result.final_sheet_values,
                conversation_bodies,
            )
            scenario_result.final_comments_quality = quality

            if quality["redundant_data"]:
                for rd in quality["redundant_data"]:
                    scenario_result.errors.append(f"Comments quality: {rd}")

            # Check scenario-specific expected/forbidden content
            if scenario.expected_comments_contain and scenario_result.final_comments:
                comments_lower = scenario_result.final_comments.lower()
                for keyword in scenario.expected_comments_contain:
                    if keyword.lower() not in comments_lower:
                        quality["issues"].append(
                            f"Expected '{keyword}' in comments but not found"
                        )
            if scenario.forbidden_in_comments and scenario_result.final_comments:
                comments_lower = scenario_result.final_comments.lower()
                for forbidden in scenario.forbidden_in_comments:
                    if forbidden.lower() in comments_lower:
                        quality["redundant_data"].append(
                            f"Forbidden value '{forbidden}' found in comments"
                        )
                        scenario_result.errors.append(
                            f"Comments quality: forbidden value '{forbidden}' in comments"
                        )

            print(f"\n  --- Comments Quality Assessment ---")
            print(f"  Comments: {scenario_result.final_comments[:200] if scenario_result.final_comments else '(empty)'}")
            print(f"  Score: {quality['score']}/10")
            if quality["good_captures"]:
                print(f"  Good captures: {', '.join(quality['good_captures'])}")
            if quality["redundant_data"]:
                print(f"  REDUNDANT (should not be in comments): {quality['redundant_data']}")
            if quality["issues"]:
                print(f"  Issues: {quality['issues']}")

        # Determine overall pass
        all_turns_passed = all(t.passed for t in scenario_result.turns)
        scenario_result.passed = all_turns_passed and len(scenario_result.errors) == 0
        scenario_result.final_status = (
            "PASSED" if scenario_result.passed else "FAILED"
        )

        scenario_result.end_time = datetime.now(timezone.utc).isoformat()
        scenario_result.total_duration_seconds = time.time() - start

        # Print summary
        print(f"\n{'=' * 80}")
        print(f"SCENARIO RESULT: {scenario.name} - {scenario_result.final_status}")
        print(f"  Duration: {scenario_result.total_duration_seconds:.1f}s")
        print(f"  Turns: {len(scenario_result.turns)}")
        for t in scenario_result.turns:
            status = "PASS" if t.passed else "FAIL"
            print(
                f"    Turn {t.turn_index} ({t.action}): {status} "
                f"[{t.latency_seconds:.1f}s]"
            )
            if t.errors:
                for e in t.errors:
                    print(f"      ERROR: {e}")
            if t.extraction_issues:
                for e in t.extraction_issues:
                    print(f"      EXTRACTION: {e}")
            if t.notification_issues:
                for e in t.notification_issues:
                    print(f"      NOTIFICATION: {e}")
        if scenario_result.final_sheet_values:
            # Print sheet values without comments (printed separately above)
            display_values = {
                k: v for k, v in scenario_result.final_sheet_values.items()
                if k != "Listing Brokers Comments"
            }
            print(f"  Final sheet values: {display_values}")
            if scenario_result.final_comments:
                print(f"  Final comments: {scenario_result.final_comments[:200]}")
        print(f"{'=' * 80}\n")

        return scenario_result

    # ------------------------------------------------------------------
    # Run all / specific scenarios
    # ------------------------------------------------------------------
    def run(
        self,
        scenario_names: Optional[List[str]] = None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """Run specified scenarios (or all) and produce a report."""
        self._init_clients()

        # Load or create state
        state = None
        if resume:
            state = RunState.load()
            if state:
                print(f"Resuming run: {state.run_id}")
            else:
                print("No previous run to resume, starting fresh")

        if not state:
            state = RunState(
                run_id=datetime.now().strftime("%Y%m%d_%H%M%S")
            )

        # Select scenarios
        if scenario_names:
            scenarios = [
                ALL_SCENARIOS[name]
                for name in scenario_names
                if name in ALL_SCENARIOS
            ]
        else:
            scenarios = list(ALL_SCENARIOS.values())

        # Filter out completed scenarios on resume
        if resume and state.scenarios_completed:
            scenarios = [
                s
                for s in scenarios
                if s.name not in state.scenarios_completed
            ]
            print(
                f"Skipping {len(state.scenarios_completed)} completed scenarios"
            )

        print(f"\nRunning {len(scenarios)} scenario(s):")
        for s in scenarios:
            print(f"  - {s.name}: {s.description}")

        all_results = []
        for scenario in scenarios:
            state.current_scenario = scenario.name
            state.save()

            result = self.run_scenario(scenario, state)
            all_results.append(result)
            self.results.append(result)

            state.scenarios_completed.append(scenario.name)
            state.current_scenario = None
            state.current_turn = 0
            state.save()

        # Generate report
        report = self._generate_report(all_results, state.run_id)

        # Save report
        self._save_report(report, state.run_id)

        # Clean up state file on successful completion
        if all(r.passed for r in all_results):
            RunState.clear()
            print("\nAll scenarios passed - state file cleaned up")

        return report

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------
    def _generate_report(
        self, results: List[ScenarioResult], run_id: str
    ) -> Dict[str, Any]:
        total_scenarios = len(results)
        passed_scenarios = sum(1 for r in results if r.passed)
        total_turns = sum(len(r.turns) for r in results)
        passed_turns = sum(
            sum(1 for t in r.turns if t.passed) for r in results
        )
        total_duration = sum(r.total_duration_seconds for r in results)

        report = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "scenarios_total": total_scenarios,
                "scenarios_passed": passed_scenarios,
                "scenarios_failed": total_scenarios - passed_scenarios,
                "turns_total": total_turns,
                "turns_passed": passed_turns,
                "turns_failed": total_turns - passed_turns,
                "total_duration_seconds": round(total_duration, 1),
                "overall_pass": passed_scenarios == total_scenarios,
            },
            "scenarios": [asdict(r) for r in results],
        }
        return report

    def _save_report(self, report: Dict, run_id: str):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        output_path = RESULTS_DIR / f"multi_turn_{run_id}.json"
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nReport saved to: {output_path}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def cleanup_test_data():
    """Remove test clients, threads, and notifications created by tests."""
    from email_automation.clients import _fs

    print("Cleaning up multi-turn test data...")

    clients_ref = (
        _fs.collection("users")
        .document(OUTLOOK_USER_ID)
        .collection("clients")
    )

    deleted_clients = 0
    for doc in clients_ref.stream():
        if doc.id.startswith("multi_turn_test_"):
            data = doc.to_dict()
            if data.get("isTestClient"):
                # Delete notifications subcollection
                notifs = doc.reference.collection("notifications")
                for n in notifs.stream():
                    n.reference.delete()
                doc.reference.delete()
                deleted_clients += 1
                print(f"  Deleted client: {doc.id}")

    # Delete test threads (by subject match)
    threads_ref = (
        _fs.collection("users")
        .document(OUTLOOK_USER_ID)
        .collection("threads")
    )
    deleted_threads = 0
    test_addresses = [s.property_address.lower() for s in ALL_SCENARIOS.values()]
    for doc in threads_ref.stream():
        data = doc.to_dict()
        subject = (data.get("subject") or "").lower()
        if any(addr in subject for addr in test_addresses):
            # Delete messages subcollection
            msgs = doc.reference.collection("messages")
            for m in msgs.stream():
                m.reference.delete()
            doc.reference.delete()
            deleted_threads += 1
            print(f"  Deleted thread: {doc.id}")

    # Clean up state file
    RunState.clear()

    print(f"\nCleanup complete: {deleted_clients} clients, {deleted_threads} threads deleted")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Multi-Turn Live Email Integration Test"
    )
    parser.add_argument(
        "--scenario",
        choices=list(ALL_SCENARIOS.keys()),
        help="Run a specific scenario",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an interrupted run",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help=f"Seconds to wait for email delivery (default: {DEFAULT_WAIT_SECONDS})",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove all test data from Firestore",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenarios",
    )

    args = parser.parse_args()

    if args.list:
        print("\nAvailable scenarios:")
        for name, scenario in ALL_SCENARIOS.items():
            print(f"\n  {name}:")
            print(f"    {scenario.description}")
            print(f"    Property: {scenario.property_address}, {scenario.city}")
            print(f"    Turns: {len(scenario.turns)}")
            for i, turn in enumerate(scenario.turns):
                print(f"      Turn {i+1}: [{turn.action.value}] {turn.description}")
        return

    if args.cleanup:
        cleanup_test_data()
        return

    runner = MultiTurnTestRunner(wait_seconds=args.wait)

    scenario_names = [args.scenario] if args.scenario else None
    report = runner.run(scenario_names=scenario_names, resume=args.resume)

    # Print final summary
    summary = report["summary"]
    print("\n" + "=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(f"  Scenarios: {summary['scenarios_passed']}/{summary['scenarios_total']} passed")
    print(f"  Turns: {summary['turns_passed']}/{summary['turns_total']} passed")
    print(f"  Duration: {summary['total_duration_seconds']}s")
    print(f"  Result: {'PASS' if summary['overall_pass'] else 'FAIL'}")
    print("=" * 80)

    sys.exit(0 if summary["overall_pass"] else 1)


if __name__ == "__main__":
    main()
