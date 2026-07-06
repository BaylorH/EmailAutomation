"""SINGLE chained full-campaign end-to-end test (fullCampaignLanes rubric).

Drives all nine CRE lifecycle stages IN SEQUENCE against ONE in-memory Firestore
double, a faked Microsoft Graph (send + message metadata), and a faked Google
Sheet.  The output of every stage is the input to the next: the launch outbox is
sent by the REAL send_outboxes drain; the internetMessageId it indexes is what a
broker reply matches against; the matched thread is what the classifier extracts
from; the extracted proposal is what the sheet stage writes; the written/terminal
row is what the follow-up gate and completion checks read.

SAFETY (asserted across the whole run):
  * ZERO live sends / sheet writes / Firestore — every boundary is faked.
  * No placeholder or wrong-recipient body ever reaches the fake Graph send.
  * Recipients stay inside the BP21 test set (bp21harrison+rowN@gmail.com).
  * No duplicate send of the same outreach.
  * The sheet row that is written is the correct property anchor.
  * The campaign reaches a terminal/complete state with NO stuck/hidden-failed item.
  * Health reflects the real datastore state (healthy, empty queues).

Every stage calls the REAL production handler; only datastore/network boundaries
are faked.  If a stage regresses, the corresponding assertion fails.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import contextlib
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import google.cloud.firestore as _gcf

# clients.py runs `_fs = firestore.Client()` at import time; stub it so the
# package imports offline.  The real datastore boundary is faked per-run below.
_gcf.Client = lambda *a, **k: mock.MagicMock()

from email_automation import (
    ai_processing,
    campaign_safety,
    clients,
    column_config,
    dead_letter_recovery,
    email as email_mod,
    email_operations,
    followup as followup_mod,
    messaging,
    notifications,
    pending_responses,
    processing,
    scheduler_lease,
    sheet_operations,
    system_health,
)
from email_automation.utils import normalize_message_id

# Every module that did `from .clients import _fs` holds its own module-level
# reference; the shared fake must be installed on all of them.
_FS_MODULES = [
    clients,
    ai_processing,
    campaign_safety,
    dead_letter_recovery,
    email_mod,
    email_operations,
    followup_mod,
    messaging,
    notifications,
    pending_responses,
    processing,
    scheduler_lease,
    sheet_operations,
    system_health,
]

BP21_TEST_RECIPIENT = "bp21harrison+row3@gmail.com"


# ─────────────────────────────────────────────────────────────────────────────
# In-memory Firestore double (generic, path-based)
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_sentinel(value):
    # google.cloud.firestore.SERVER_TIMESTAMP is a module-level sentinel; give it
    # a concrete monotonic-ish value so order_by / timestamp math still works.
    if value is _gcf.SERVER_TIMESTAMP:
        return datetime.now(timezone.utc)
    return value


class _Snapshot:
    def __init__(self, doc_id, data, exists, reference):
        self.id = doc_id
        self._data = data
        self.exists = exists
        self.reference = reference

    def to_dict(self):
        return dict(self._data) if self._data is not None else {}

    def get(self, field):
        return (self._data or {}).get(field)


class _DocRef:
    def __init__(self, node, doc_id):
        self._node = node          # {"data":{}, "_exists":bool, "collections":{}}
        self.id = doc_id

    @property
    def reference(self):
        return self

    def collection(self, name):
        sub = self._node["collections"].setdefault(name, {"docs": {}})
        return _Collection(sub, name)

    def get(self, transaction=None):
        return _Snapshot(self.id, self._node["data"], self._node["_exists"], self)

    def set(self, data, merge=False):
        data = {k: _resolve_sentinel(v) for k, v in data.items()}
        if merge and self._node["_exists"]:
            self._node["data"].update(data)
        else:
            self._node["data"] = dict(data)
        self._node["_exists"] = True

    def update(self, data):
        if not self._node["_exists"]:
            self._node["data"] = {}
            self._node["_exists"] = True
        for key, value in data.items():
            value = _resolve_sentinel(value)
            if "." in key:
                parts = key.split(".")
                cursor = self._node["data"]
                for part in parts[:-1]:
                    cursor = cursor.setdefault(part, {})
                cursor[parts[-1]] = value
            else:
                self._node["data"][key] = value

    def delete(self):
        self._node["data"] = {}
        self._node["_exists"] = False


def _matches(op, cell, value):
    if op == "==":
        return cell == value
    if op == "array_contains":
        return isinstance(cell, (list, tuple)) and value in cell
    if op == "in":
        return cell in value
    if op == "!=":
        return cell != value
    raise NotImplementedError(f"fake firestore where op {op!r} not supported")


class _Query:
    def __init__(self, collection_node, filters=None, order=None, limit=None):
        self._node = collection_node
        self._filters = filters or []
        self._order = order
        self._limit = limit

    def where(self, field, op, value):
        return _Query(self._node, self._filters + [(field, op, value)], self._order, self._limit)

    def order_by(self, field, **kwargs):
        return _Query(self._node, self._filters, field, self._limit)

    def limit(self, n):
        return _Query(self._node, self._filters, self._order, n)

    def stream(self):
        snaps = []
        for doc_id, node in self._node["docs"].items():
            if not node["_exists"]:
                continue
            data = node["data"]
            if all(_matches(op, data.get(f), v) for f, op, v in self._filters):
                snaps.append(_Snapshot(doc_id, data, True, _DocRef(node, doc_id)))
        if self._order:
            snaps.sort(key=lambda s: (s._data.get(self._order) is None, s._data.get(self._order)))
        if self._limit is not None:
            snaps = snaps[: self._limit]
        return snaps


class _Collection:
    def __init__(self, node, name):
        self._node = node
        self._name = name
        self._auto = 0

    def document(self, doc_id=None):
        if doc_id is None:
            self._auto += 1
            doc_id = f"auto-{self._name}-{self._auto}"
        node = self._node["docs"].setdefault(doc_id, {"data": {}, "_exists": False, "collections": {}})
        return _DocRef(node, str(doc_id))

    def add(self, data):
        ref = self.document()
        ref.set(data)
        return (None, ref)

    def where(self, field, op, value):
        return _Query(self._node).where(field, op, value)

    def order_by(self, field, **kwargs):
        return _Query(self._node).order_by(field)

    def limit(self, n):
        return _Query(self._node).limit(n)

    def stream(self):
        return _Query(self._node).stream()


class FakeFirestore:
    """Path-based in-memory Firestore double supporting the full send/reply/
    follow-up chain: nested collection/document, set(merge)/update(dotted)/get/
    delete, where(==, array_contains)/order_by/limit/stream, add, and passthrough
    transactions.  Reused as the single shared datastore across all nine stages."""

    def __init__(self):
        self._root = {"collections": {}}

    def collection(self, name):
        node = self._root["collections"].setdefault(name, {"docs": {}})
        return _Collection(node, name)

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
    """Passthrough transaction: writes land directly on the referenced doc's
    node, faithfully modeling a committed Firestore transaction."""

    def update(self, doc_ref, fields):
        doc_ref.update(fields)

    def delete(self, doc_ref):
        doc_ref.delete()

    def set(self, doc_ref, data, merge=False):
        doc_ref.set(data, merge=merge)


def _passthrough_transactional(fn):
    def wrapper(transaction, *args, **kwargs):
        return fn(transaction, *args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# Fake Microsoft Graph (send + message metadata) — zero live sends
# ─────────────────────────────────────────────────────────────────────────────
OUTREACH_INTERNET_ID = "<outreach-0001@example.com>"
OUTREACH_CONVERSATION_ID = "conv-0001"
BROKER_REPLY_INTERNET_ID = "<broker-reply-0001@example.com>"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeGraph:
    """Stand-in for email_automation.email.requests. Records every message body
    that would be sent so the test can assert the safety invariants on the wire."""

    def __init__(self):
        self.created_drafts = []      # each draft's message payload
        self.sent_draft_ids = []      # draft ids that reached /send
        self._draft_seq = 0

    # --- helpers -----------------------------------------------------------
    def sent_bodies(self):
        return [d["body"]["content"] for d in self.created_drafts if d["_sent"]]

    def sent_recipients(self):
        out = []
        for d in self.created_drafts:
            if d["_sent"]:
                for r in d.get("toRecipients", []):
                    out.append(r["emailAddress"]["address"])
        return out

    # --- requests interface ------------------------------------------------
    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        if url.endswith("/me/messages"):
            self._draft_seq += 1
            draft_id = f"draft-{self._draft_seq}"
            record = dict(json or {})
            record["_id"] = draft_id
            record["_sent"] = False
            self.created_drafts.append(record)
            return _FakeResponse({"id": draft_id})
        m = re.search(r"/me/messages/([^/]+)/send$", url)
        if m:
            draft_id = m.group(1)
            for d in self.created_drafts:
                if d["_id"] == draft_id:
                    d["_sent"] = True
            self.sent_draft_ids.append(draft_id)
            return _FakeResponse({}, status_code=202)
        if "/attachments" in url:
            return _FakeResponse({"id": "attach-1"}, status_code=201)
        raise AssertionError(f"unexpected Graph POST to {url}")

    def get(self, url, headers=None, params=None, timeout=None, **kwargs):
        if re.search(r"/me/messages/draft-\d+$", url):
            return _FakeResponse({
                "internetMessageId": OUTREACH_INTERNET_ID,
                "conversationId": OUTREACH_CONVERSATION_ID,
                "subject": "120 Logistics Pkwy",
                "toRecipients": [{"emailAddress": {"address": BP21_TEST_RECIPIENT}}],
            })
        raise AssertionError(f"unexpected Graph GET to {url}")


# ─────────────────────────────────────────────────────────────────────────────
# Fake Google Sheet (roster grid + AI_META) — zero live sheet writes
# ─────────────────────────────────────────────────────────────────────────────
def _col_index_from_letters(letters):
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch.upper()) - ord("A") + 1)
    return idx  # 1-based


class _SheetRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeValues:
    def __init__(self, grid, ai_meta):
        self._grid = grid          # {row_number(1-based): [cells]}, header at row 2
        self._ai_meta = ai_meta    # list of AI_META data rows (no header)
        self.batch_update_calls = []
        self.append_calls = []

    def get(self, spreadsheetId=None, range=None, **kwargs):
        tab, _, a1 = range.partition("!")
        if tab == "AI_META":
            header = ["rowNumber", "columnName", "last_ai_value", "last_ai_write_iso",
                      "human_override", "rowAnchor"]
            return _SheetRequest({"values": [header, *[list(r) for r in self._ai_meta]]})
        # Main tab range: row-only ("2:2"), open A2:ZZZ, or single cell.
        if ":" in a1:
            left, right = a1.split(":")
            r1 = int(re.sub(r"[^0-9]", "", left) or "1")
            right_digits = re.sub(r"[^0-9]", "", right)
            r2 = int(right_digits) if right_digits else max([1, *self._grid.keys()])
            rows = [list(self._grid.get(r, [])) for r in range_inclusive(r1, r2)]
            # trim trailing all-empty rows so callers see real extent
            while rows and not any(c.strip() for c in rows[-1] if isinstance(c, str)):
                rows.pop()
            return _SheetRequest({"values": rows})
        # single cell
        m = re.match(r"([A-Z]+)(\d+)", a1)
        if m:
            col = _col_index_from_letters(m.group(1))
            row = int(m.group(2))
            cells = self._grid.get(row, [])
            val = cells[col - 1] if col - 1 < len(cells) else ""
            return _SheetRequest({"values": [[val]]})
        return _SheetRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        for item in (body or {}).get("data", []):
            tab, _, a1 = item["range"].partition("!")
            m = re.match(r"([A-Z]+)(\d+)", a1)
            if not m:
                continue
            col = _col_index_from_letters(m.group(1))
            row = int(m.group(2))
            cells = self._grid.setdefault(row, [])
            while len(cells) < col:
                cells.append("")
            cells[col - 1] = item["values"][0][0]
        return _SheetRequest({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        self.append_calls.append({"range": range, "body": body})
        for row in (body or {}).get("values", []):
            self._ai_meta.append(list(row))
        return _SheetRequest({})


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values
        self.batch_update_calls = []

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        return _SheetRequest({"sheets": [
            {"properties": {"title": "Sheet1", "sheetId": 0}},
            {"properties": {"title": "AI_META", "sheetId": 1}},
        ]})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return _SheetRequest({})


class FakeSheets:
    def __init__(self, grid, ai_meta):
        self.values_api = _FakeValues(grid, ai_meta)
        self.spreadsheets_api = _FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


def range_inclusive(a, b):
    return range(a, b + 1)


# ─────────────────────────────────────────────────────────────────────────────
# Fake OpenAI response for the classifier boundary
# ─────────────────────────────────────────────────────────────────────────────
class _FakeOpenAIResponse:
    def __init__(self, text):
        self.output_text = text
        self.usage = None
        self.id = "resp-fake-1"


PROPOSAL_JSON = """
{
  "updates": [
    {"column": "Total SF", "value": "18,500", "confidence": 0.96, "reason": "Broker stated 18,500 SF available."},
    {"column": "Rent/SF /Yr", "value": "14.50", "confidence": 0.95, "reason": "Broker quoted $14.50/SF NNN."},
    {"column": "Ops Ex /SF", "value": "3.25", "confidence": 0.93, "reason": "Broker quoted NNN of $3.25/SF."},
    {"column": "Docks", "value": "6", "confidence": 0.94, "reason": "Broker: six dock doors."},
    {"column": "Drive Ins", "value": "2", "confidence": 0.92, "reason": "Broker: two drive-in doors."},
    {"column": "Ceiling Ht", "value": "32", "confidence": 0.95, "reason": "Broker: 32' clear."},
    {"column": "Power", "value": "1200A 3-phase", "confidence": 0.9, "reason": "Broker: 1200A 3-phase service."}
  ],
  "events": [{"type": "broker_available_partial_specs"}],
  "response_email": null
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# World / fixture builder
# ─────────────────────────────────────────────────────────────────────────────
HEADER = ["Property Address", "City", "Leasing Contact", "Email", "Total SF",
          "Rent/SF /Yr", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht",
          "Power", "Flyer / Link", "Gross Rent"]

USER_ID = "e2e-user-1"
CLIENT_ID = "e2e-client-1"
SHEET_ID = "sheet-e2e-1"
PROPERTY_ADDRESS = "120 Logistics Pkwy"
CITY = "Fort Worth"
CONTACT_NAME = "Dana"

BROKER_REPLY_TEXT = (
    "Hi, thanks for reaching out about 120 Logistics Pkwy. It is still available. "
    "The suite is 18,500 SF, asking $14.50/SF NNN with operating expenses of $3.25/SF. "
    "It has 6 dock doors and 2 drive-in doors, 32' clear height, and 1200A 3-phase power. "
    "Happy to send a flyer if helpful. -Dana"
)


class World:
    def __init__(self):
        self.fs = FakeFirestore()
        self.graph = FakeGraph()
        # roster grid: row 1 title, row 2 header, row 3 the one campaign property
        self.grid = {
            1: ["Campaign Roster"],
            2: list(HEADER),
            3: [PROPERTY_ADDRESS, CITY, "Dana Broker", BP21_TEST_RECIPIENT,
                "", "", "", "", "", "", "", "", ""],
        }
        self.ai_meta = []
        self.sheets = FakeSheets(self.grid, self.ai_meta)
        self.row3 = list(self.grid[3])
        self.thread_root = normalize_message_id(OUTREACH_INTERNET_ID)
        self.proposal = None


@contextlib.contextmanager
def patched(world):
    """Install every faked boundary the chain touches for the duration of a stage."""
    stack = contextlib.ExitStack()
    # clients._fs is the source; function-local `from .clients import _fs` reads it
    # at call time. Modules that bound `_fs` at import get patched directly too.
    stack.enter_context(mock.patch.object(clients, "_fs", world.fs))
    for module in _FS_MODULES:
        if module is not clients and hasattr(module, "_fs"):
            stack.enter_context(mock.patch.object(module, "_fs", world.fs))
    stack.enter_context(mock.patch.object(email_mod, "requests", world.graph))
    stack.enter_context(mock.patch("google.cloud.firestore.transactional", _passthrough_transactional))
    # Patch _sheets_client on every module that references it → no module can
    # construct a live Google Sheets client (defense-in-depth: zero live sheet I/O).
    from email_automation import sheets as sheets_mod, logging as ea_logging
    for module in (email_mod, ai_processing, sheets_mod, sheet_operations,
                   processing, followup_mod, ea_logging):
        if hasattr(module, "_sheets_client"):
            stack.enter_context(mock.patch.object(module, "_sheets_client", lambda: world.sheets))
    stack.enter_context(mock.patch.object(ai_processing, "client", _FakeAIClient(world)))
    stack.enter_context(mock.patch.object(email_mod, "time", _NoSleep()))
    stack.enter_context(mock.patch.object(followup_mod, "time", _NoSleep()))
    stack.enter_context(mock.patch.object(
        sheet_operations, "_apply_gross_rent_formula_for_row", lambda *a, **k: False))
    try:
        yield
    finally:
        stack.close()


class _NoSleep:
    def sleep(self, *_a, **_k):
        return None


class _FakeAIClient:
    def __init__(self, world):
        self._world = world
        self.responses = self

    def create(self, *args, **kwargs):
        return _FakeOpenAIResponse(PROPOSAL_JSON)


# ─────────────────────────────────────────────────────────────────────────────
# Stage drivers (each calls the REAL production handler)
# ─────────────────────────────────────────────────────────────────────────────
def stage1_upload(world):
    """UPLOAD: roster upload persists user + client/campaign rows into Firestore.
    Verified by the REAL clients._get_sheet_id_or_fail reading the campaign back."""
    world.fs.collection("users").document(USER_ID).set({"email": "owner@example.com"})
    world.fs.collection("users").document(USER_ID).collection("clients").document(CLIENT_ID).set({
        "sheetId": SHEET_ID,
        "name": "Tenant Rep Campaign",
        "status": "live",
    })
    # REAL production read proves the uploaded campaign row is persisted & readable.
    resolved_sheet = clients._get_sheet_id_or_fail(USER_ID, CLIENT_ID)
    assert resolved_sheet == SHEET_ID, resolved_sheet
    return resolved_sheet


def stage2_map(world):
    """MAP: resolve the roster's column headers to canonical fields via the REAL
    detect_column_mapping, then persist columnConfig on the client."""
    mapping = column_config.detect_column_mapping(HEADER, use_ai=False)
    world.fs.collection("users").document(USER_ID).collection("clients").document(CLIENT_ID).set(
        {"columnMapping": mapping["mappings"], "extractionFields": mapping["extractionFields"]},
        merge=True,
    )
    return mapping


def stage3_launch(world):
    """LAUNCH: enqueue the initial outreach outbox item (BP21 test recipient) and
    prove the REAL launch gate classifies it and does NOT drop it for a live client."""
    outbox_data = {
        "clientId": CLIENT_ID,
        "source": "dashboard_new_campaign",
        "assignedEmails": [BP21_TEST_RECIPIENT],
        "subject": PROPERTY_ADDRESS,
        "script": f"Hi {CONTACT_NAME}, I represent a tenant interested in {PROPERTY_ADDRESS}. "
                  "Is it still available, and could you share the specs?",
        "rowNumber": 3,
        "contactName": CONTACT_NAME,
        "createdAt": datetime.now(timezone.utc),
    }
    assert email_mod._is_campaign_launch_outbox(outbox_data)
    ref = world.fs.collection("users").document(USER_ID).collection("outbox").document("outbox-1")
    ref.set(outbox_data)
    # REAL launch terminal-gate: live client → not dropped, stays queued for send.
    dropped = email_mod._pause_client_outbox_item_if_needed(USER_ID, ref, dict(outbox_data))
    assert dropped is False
    return ref


def stage4_send(world):
    """SEND: the REAL send_outboxes drain sends the queued outreach through the
    fake Graph, runs the recipient/row safety guard against the fake Sheet, and
    indexes the thread + message id in Firestore."""
    email_mod.send_outboxes(USER_ID, {"Authorization": "Bearer fake"})
    return world.graph


def stage5_reply(world):
    """REPLY: a realistic broker reply arrives in the fake inbox referencing the
    outreach we sent, and the REAL inbox-matching primitive resolves it to the
    originating thread by the outbound internetMessageId (In-Reply-To)."""
    inbound = {
        "internetMessageId": BROKER_REPLY_INTERNET_ID,
        "inReplyTo": OUTREACH_INTERNET_ID,
        "from": {"emailAddress": {"address": BP21_TEST_RECIPIENT, "name": "Dana Broker"}},
        "subject": "Re: 120 Logistics Pkwy",
        "body": {"contentType": "Text", "content": BROKER_REPLY_TEXT},
        "receivedDateTime": "2026-07-01T15:00:00Z",
    }
    matched = messaging.lookup_thread_by_message_id(USER_ID, inbound["inReplyTo"])
    assert matched == world.thread_root, (matched, world.thread_root)
    # record the reply on the thread the way an inbox scan would
    world.fs.collection("users").document(USER_ID).collection("threads").document(
        world.thread_root
    ).set({"hasInboundReply": True, "status": "active"}, merge=True)
    messaging.save_message(USER_ID, world.thread_root, normalize_message_id(BROKER_REPLY_INTERNET_ID), {
        "direction": "inbound",
        "from": BP21_TEST_RECIPIENT,
        "to": ["me"],
        "subject": inbound["subject"],
        "receivedDateTime": inbound["receivedDateTime"],
        "body": {"contentType": "Text", "content": BROKER_REPLY_TEXT, "preview": BROKER_REPLY_TEXT[:200]},
    })
    return inbound, matched


def stage6_classify(world, inbound):
    """CLASSIFY: the REAL propose_sheet_updates runs over the outbound+reply
    conversation (OpenAI boundary faked with a realistic proposal), producing
    events + sheet updates via the real parse/augment/sanitize pipeline."""
    conversation = [
        {"direction": "outbound", "from": "me", "to": [BP21_TEST_RECIPIENT],
         "subject": PROPERTY_ADDRESS, "timestamp": "2026-07-01T09:00:00Z",
         "content": f"Hi {CONTACT_NAME}, is {PROPERTY_ADDRESS} still available and can you share specs?"},
        {"direction": "inbound", "from": BP21_TEST_RECIPIENT, "to": ["me"],
         "subject": inbound["subject"], "timestamp": inbound["receivedDateTime"],
         "content": BROKER_REPLY_TEXT},
    ]
    proposal = ai_processing.propose_sheet_updates(
        USER_ID, CLIENT_ID, BP21_TEST_RECIPIENT, SHEET_ID, HEADER, 3, world.row3,
        world.thread_root, conversation=conversation, dry_run=True,
    )
    assert proposal is not None
    world.proposal = proposal
    return proposal


def stage7_sheet(world, proposal):
    """SHEET: apply_proposal_to_sheet writes the extracted specs to the correct
    anchored row (row 3) of the fake Sheet via the REAL write-guard pipeline."""
    result = ai_processing.apply_proposal_to_sheet(
        USER_ID, CLIENT_ID, SHEET_ID, HEADER, 3, world.row3, proposal,
    )
    return result


def stage8_followup(world):
    """FOLLOWUP: the REAL check_and_send_followups gate must NOT send a follow-up
    on a thread the broker already replied to.  Positive/negative controls on the
    REAL _followup_terminal_block_reason prove the gate discriminates."""
    thread_ref = world.fs.collection("users").document(USER_ID).collection("threads").document(world.thread_root)
    thread_ref.set({
        "clientId": CLIENT_ID,
        "followUpStatus": "waiting",
        "hasInboundReply": True,
        "followUpConfig": {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"waitTime": 3, "waitUnit": "days"}],
            "nextFollowUpAt": datetime.now(timezone.utc) - timedelta(days=1),
        },
    }, merge=True)
    # Neutralize only the business-hours scheduler (a calendar helper, NOT a safety
    # gate) so the run deterministically reaches the REAL broker-reply pause branch
    # instead of a weekend deferral. The reply-withhold logic itself stays real.
    with mock.patch.object(followup_mod, "_next_business_followup_time", lambda now, cfg: now):
        sent = followup_mod.check_and_send_followups(USER_ID, {"Authorization": "Bearer fake"})
    paused = thread_ref.get().to_dict().get("followUpStatus")
    return sent, paused


def stage9_completion(world):
    """COMPLETION: the row reaches a terminal 'completed' state; re-draining the
    outbox and re-running the follow-up gate produce ZERO further sends; health
    reflects the real (healthy, empty-queue) datastore state."""
    world.fs.collection("users").document(USER_ID).collection("threads").document(
        world.thread_root
    ).set({"status": "completed", "followUpStatus": "complete"}, merge=True)
    sends_before = len(world.graph.sent_draft_ids)
    email_mod.send_outboxes(USER_ID, {"Authorization": "Bearer fake"})
    followup_mod.check_and_send_followups(USER_ID, {"Authorization": "Bearer fake"})
    sends_after = len(world.graph.sent_draft_ids)
    status = messaging.get_thread_status(USER_ID, world.thread_root)
    health = system_health.collect_user_health(
        USER_ID, fs_client=world.fs,
        token_state={"status": "ok"}, graph_state={"status": "ok"},
    )
    return sends_before, sends_after, status, health


def _drive_through(world, last_stage):
    """Run stages 1..last_stage in order on a shared world, returning per-stage outputs."""
    out = {}
    stage1_upload(world)
    if last_stage >= 2:
        out["map"] = stage2_map(world)
    if last_stage >= 3:
        out["launch"] = stage3_launch(world)
    if last_stage >= 4:
        out["send"] = stage4_send(world)
    if last_stage >= 5:
        out["reply"] = stage5_reply(world)
    if last_stage >= 6:
        out["classify"] = stage6_classify(world, out["reply"][0])
    if last_stage >= 7:
        out["sheet"] = stage7_sheet(world, out["classify"])
    if last_stage >= 8:
        out["followup"] = stage8_followup(world)
    if last_stage >= 9:
        out["completion"] = stage9_completion(world)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tests: one method per stage transition + one whole-chain method
# ─────────────────────────────────────────────────────────────────────────────
class FullCampaignE2ETests(unittest.TestCase):

    def test_stage1_upload_persists_campaign_rows(self):
        world = World()
        with patched(world):
            sheet_id = stage1_upload(world)
        self.assertEqual(SHEET_ID, sheet_id)

    def test_stage2_map_resolves_columns(self):
        world = World()
        with patched(world):
            out = _drive_through(world, 2)
        mapping = out["map"]["mappings"]
        self.assertEqual("Total SF", mapping["total_sf"])
        self.assertEqual("Power", mapping["power"])
        self.assertIn("total_sf", out["map"]["extractionFields"])

    def test_stage3_launch_enqueues_and_survives_gate(self):
        world = World()
        with patched(world):
            ref = _drive_through(world, 3)["launch"]
            snap = ref.get()
        self.assertTrue(snap.exists)
        self.assertEqual([BP21_TEST_RECIPIENT], snap.to_dict()["assignedEmails"])

    def test_stage4_send_drains_outbox_through_fake_graph(self):
        world = World()
        with patched(world):
            _drive_through(world, 4)
            outbox_left = list(
                world.fs.collection("users").document(USER_ID).collection("outbox").stream()
            )
            thread = world.fs.collection("users").document(USER_ID).collection("threads").document(
                world.thread_root).get()
        # exactly one send, to the BP21 recipient, no leftover outbox item
        self.assertEqual([BP21_TEST_RECIPIENT], world.graph.sent_recipients())
        self.assertEqual(1, len(world.graph.sent_draft_ids))
        self.assertEqual([], outbox_left)
        self.assertTrue(thread.exists)

    def test_stage5_reply_matches_thread(self):
        world = World()
        with patched(world):
            _inbound, matched = _drive_through(world, 5)["reply"]
        self.assertEqual(world.thread_root, matched)

    def test_stage6_classify_produces_updates(self):
        world = World()
        with patched(world):
            proposal = _drive_through(world, 6)["classify"]
        cols = {u["column"] for u in proposal.get("updates", [])}
        self.assertIn("Total SF", cols)
        self.assertIn("Power", cols)

    def test_stage7_sheet_writes_correct_anchor_row(self):
        world = World()
        with patched(world):
            result = _drive_through(world, 7)["sheet"]
        self.assertEqual([], result["skipped"])
        self.assertEqual(f"{PROPERTY_ADDRESS}, {CITY}", result["targetAnchor"])
        applied = {a["column"]: a for a in result["applied"]}
        self.assertEqual("Sheet1!E3", applied["Total SF"]["range"])  # E=col5, row 3
        # The deterministic Total SF extractor normalizes to a plain integer
        # (comma stripped) — the canonical sheet-numeric form used across the
        # codebase (battery tests write "12000"/"2000"/"9000") — and overwrites
        # the LLM's comma-formatted "18,500". Same value, canonical form.
        self.assertEqual("18500", applied["Total SF"]["newValue"])
        # the write really landed on row 3 of the fake grid
        self.assertEqual("18500", world.grid[3][4])

    def test_stage8_followup_withholds_after_broker_reply(self):
        world = World()
        with patched(world):
            sent, paused = _drive_through(world, 8)["followup"]
        # #20: check_and_send_followups now returns a Graph op-state list; a
        # withheld follow-up sends nothing, so the list is empty (no error state).
        self.assertEqual([], sent)
        # proves the WITHHOLD happened via the real broker-reply pause branch
        self.assertEqual("paused", paused)
        # gate discriminates: terminal blocks, active-due does not
        active_due = {"enabled": True, "currentFollowUpIndex": 0,
                      "followUps": [{"waitTime": 3, "waitUnit": "days"}]}
        self.assertIsNotNone(
            followup_mod._followup_terminal_block_reason({"status": "completed"}, active_due, 0))
        self.assertIsNone(
            followup_mod._followup_terminal_block_reason({"status": "active"}, active_due, 0))

    def test_stage9_completion_no_further_sends_and_health_ok(self):
        world = World()
        with patched(world):
            sends_before, sends_after, status, health = _drive_through(world, 9)["completion"]
        self.assertEqual(sends_before, sends_after)
        self.assertEqual("completed", status)
        self.assertEqual("healthy", health["status"])
        self.assertEqual(0, health["queues"].get("deadLetterQueue"))

    def test_send_stage_blocks_wrong_recipient_before_graph(self):
        """NEGATIVE CONTROL / safety pin: if the queued recipient does NOT match
        the campaign sheet row, the REAL row-recipient guard must dead-letter the
        item with ZERO Graph sends — proving no wrong-recipient can reach send and
        that the send stage genuinely exercises live safety code (fails on bypass)."""
        world = World()
        world.grid[3][3] = "someone-else@example.com"  # roster row != queued recipient
        with patched(world):
            _drive_through(world, 4)
            dead = list(
                world.fs.collection("users").document(USER_ID)
                .collection("deadLetterQueue").stream()
            )
        self.assertEqual(0, len(world.graph.sent_draft_ids))
        self.assertEqual(1, len(dead))
        self.assertIn("does not match sheet row", dead[0].to_dict()["failureReason"])

    def test_full_chain_end_to_end_with_safety_invariants(self):
        world = World()
        with patched(world):
            out = _drive_through(world, 9)

            # --- SAFETY INVARIANTS across the whole run -------------------------
            sent_recipients = world.graph.sent_recipients()
            # exactly one outreach send, no duplicate
            self.assertEqual(1, len(world.graph.sent_draft_ids))
            self.assertEqual([BP21_TEST_RECIPIENT], sent_recipients)
            # recipients stay inside the BP21 test set — never a real broker
            for r in sent_recipients:
                self.assertIn("bp21harrison", r)
            # no unresolved placeholder / raw merge field on the wire
            for body in world.graph.sent_bodies():
                self.assertNotRegex(body, r"\[[A-Za-z ]+\]")
                self.assertNotIn("{{", body)
            # the sheet row written is the correct anchor (row 3, the campaign property)
            sheet_result = out["sheet"]
            self.assertEqual(f"{PROPERTY_ADDRESS}, {CITY}", sheet_result["targetAnchor"])
            self.assertTrue(all(a["range"].endswith("3") for a in sheet_result["applied"]))
            # follow-up gate withheld after reply (#20 op-state list empty -> no
            # send, thread paused by the real broker-reply gate)
            self.assertEqual(([], "paused"), out["followup"])
            # completion: terminal, no further sends, no stuck/hidden-failed item
            sends_before, sends_after, status, health = out["completion"]
            self.assertEqual(sends_before, sends_after)
            self.assertEqual("completed", status)
            self.assertEqual("healthy", health["status"])
            # ZERO items landed in the dead-letter queue anywhere in the chain
            dead = list(
                world.fs.collection("users").document(USER_ID)
                .collection("deadLetterQueue").stream()
            )
            self.assertEqual([], dead, "no stage may hidden-fail into the dead-letter queue")


if __name__ == "__main__":
    unittest.main()
