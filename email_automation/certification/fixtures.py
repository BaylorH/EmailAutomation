"""In-image certification fixture identities, doubles, and seeds.

The scenario registry owns LOGICAL fixture keys only. Every concrete identity -
user, client, sheet, recipient, row - is resolved here, at execution time, from
values that ship inside the image. A caller may never name one; that is the
whole security model of the instrument (see ``models.CertificationRequest``).

The doubles below are deliberately the same ones the certification suite drives.
They lived only in ``tests/test_production_certification.py`` until the runner
needed them, which meant the product could be certified only from inside a test
process. A fixture the runner cannot build is a fixture no run can use.

Nothing here performs I/O. There is no client, no credential, and no base URL,
so preparing a fixture cannot reach anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Tuple

# -- bound fixture identities ------------------------------------------------
#
# Synthetic throughout. The recipient domain is ``.example.com`` (RFC 2606), so
# an identity that escaped into a real header would be inert rather than
# routable.

FIXTURE_UID = "cert-uid-0001"
FIXTURE_CLIENT = "cert-client-0001"
FIXTURE_SHEET = "cert-sheet-0001"
FIXTURE_PREFIX = f"users/{FIXTURE_UID}"
FIXTURE_RECIPIENT = "broker@fixture.example.com"
FIXTURE_SENDER = "sender@fixture.invalid"
FIXTURE_ROW = 7

# The one global document a run may read and may never write.
CAMPAIGN_AUTHORITY_PATH = "systemConfig/campaignAccess"


class FixtureError(RuntimeError):
    """No fixture is registered for the scenario's logical key."""


class AmbientClientReached(RuntimeError):
    """A call site reached a production client instead of the scoped one."""


class ExplodingClient:
    """Booby-trap for every ambient production client.

    A certification run that quietly falls back to the ambient client would look
    clean while writing to production, so the ambient handle must be one that
    cannot be used at all rather than one that happens not to be called.
    """

    def __init__(self, label: str, log: List[str]) -> None:
        self._label = label
        self._log = log

    def __getattr__(self, name: str) -> Any:
        self._log.append(f"{self._label}.{name}")
        raise AmbientClientReached(f"{self._label}.{name}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._log.append(f"{self._label}()")
        raise AmbientClientReached(f"{self._label}()")


# -- firestore double --------------------------------------------------------


class FixtureSnapshot:
    def __init__(self, store, path, data, exists=True):
        self._store = store
        self._path = path
        self.id = path.rsplit("/", 1)[-1]
        self._data = dict(data)
        self.exists = exists

    def to_dict(self):
        return dict(self._data)

    @property
    def reference(self):
        return FixtureDocument(self._store, self._path)


class FixtureDocument:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.rsplit("/", 1)[-1]

    def collection(self, name):
        return FixtureCollection(self._store, f"{self._path}/{name}")

    def get(self, transaction=None):
        exists = self._path in self._store.data
        self._store.reads.append(self._path)
        return FixtureSnapshot(
            self._store, self._path, self._store.data.get(self._path, {}), exists=exists
        )

    def set(self, data, merge=False):
        self._store.writes.append(("set", self._path, dict(data), merge))
        current = dict(self._store.data.get(self._path, {})) if merge else {}
        current.update(data)
        self._store.data[self._path] = current

    def update(self, data):
        self._store.writes.append(("update", self._path, dict(data), None))
        current = dict(self._store.data.get(self._path, {}))
        current.update(data)
        self._store.data[self._path] = current

    def create(self, data):
        self._store.writes.append(("create", self._path, dict(data), None))
        self._store.data[self._path] = dict(data)

    def delete(self):
        self._store.writes.append(("delete", self._path, None, None))
        self._store.data.pop(self._path, None)


class FixtureCollection:
    def __init__(self, store, path, filters=()):
        self._store = store
        self._path = path
        self._filters = tuple(filters)

    def document(self, name):
        return FixtureDocument(self._store, f"{self._path}/{name}")

    def where(self, field=None, op=None, value=None, **kwargs):
        return FixtureCollection(self._store, self._path, self._filters + ((field, op, value),))

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def add(self, data):
        index = self._store.generated
        self._store.generated += 1
        path = f"{self._path}/generated-{index}"
        self._store.writes.append(("add", path, dict(data), None))
        self._store.data[path] = dict(data)
        return FixtureDocument(self._store, path)

    def _matches(self, data):
        for field_name, op, value in self._filters:
            actual = data.get(field_name)
            if op == "array_contains":
                if not isinstance(actual, (list, tuple)) or value not in actual:
                    return False
            elif actual != value:
                return False
        return True

    def stream(self):
        self._store.reads.append(self._path)
        depth = self._path.count("/") + 1
        for path, data in sorted(self._store.data.items()):
            if path.startswith(self._path + "/") and path.count("/") == depth:
                if self._matches(data):
                    yield FixtureSnapshot(self._store, path, data)

    def get(self):
        return list(self.stream())


class FixtureTransaction:
    """Applies immediately. Atomicity is not what the bootstrap scenario proves."""

    def __init__(self, store):
        self._store = store
        self._max_attempts = 1
        self._read_only = False
        self._id = b"fixture"

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        return None

    def _commit(self):
        return []

    def _rollback(self):
        return None

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)

    def create(self, ref, data):
        ref.create(data)

    def delete(self, ref):
        ref.delete()


class FixtureBatch(FixtureTransaction):
    def commit(self):
        return []


class FixtureFirestore:
    def __init__(self):
        self.data: Dict[str, Any] = {}
        self.writes: List[Tuple[str, str, Any, Any]] = []
        self.reads: List[str] = []
        self.generated = 0

    def collection(self, name):
        return FixtureCollection(self, name)

    def transaction(self, **kwargs):
        return FixtureTransaction(self)

    def batch(self):
        return FixtureBatch(self)


# -- sheets double -----------------------------------------------------------


class FixtureSheetRequest:
    def __init__(self, provider, label, kwargs, payload):
        self._provider = provider
        self._label = label
        self._kwargs = kwargs
        self._payload = payload

    def execute(self):
        self._provider.calls.append((self._label, self._kwargs))
        return self._payload


class FixtureSheetValues:
    def __init__(self, provider):
        self._provider = provider

    def get(self, **kwargs):
        range_name = kwargs.get("range") or ""
        return FixtureSheetRequest(
            self._provider, "values.get", kwargs,
            {"values": [self._provider.row_for(range_name)]},
        )

    def update(self, **kwargs):
        return FixtureSheetRequest(self._provider, "values.update", kwargs, {})

    def batchUpdate(self, **kwargs):  # noqa: N802 - Google API name
        return FixtureSheetRequest(self._provider, "values.batchUpdate", kwargs, {})


class FixtureSpreadsheets:
    def __init__(self, provider):
        self._provider = provider

    def values(self):
        return FixtureSheetValues(self._provider)

    def get(self, **kwargs):
        return FixtureSheetRequest(
            self._provider, "spreadsheets.get", kwargs,
            {"sheets": [{"properties": {"title": "Sheet1", "sheetId": 0}}]},
        )

    def batchUpdate(self, **kwargs):  # noqa: N802 - Google API name
        return FixtureSheetRequest(self._provider, "spreadsheets.batchUpdate", kwargs, {})


class FixtureSheets:
    def __init__(self, header, row):
        self.calls: List[Tuple[str, Mapping[str, Any]]] = []
        self._header = header
        self._row = row

    def row_for(self, range_name):
        return self._row if range_name.endswith(f"{FIXTURE_ROW}:{FIXTURE_ROW}") else self._header

    def spreadsheets(self):
        return FixtureSpreadsheets(self)


# -- prepared fixture --------------------------------------------------------


@dataclass
class PreparedFixture:
    """Everything one run needs, and the observation surfaces it will be judged on."""

    logical_key: str
    firestore: FixtureFirestore
    sheets: FixtureSheets
    prefix: str
    sheet_ids: Tuple[str, ...]
    readable_paths: Tuple[str, ...]
    ambient_reaches: List[str] = field(default_factory=list)


SHEET_HEADER = ["Property Address", "Email", "Name"]
SHEET_ROW = ["100 Fixture Way", FIXTURE_RECIPIENT, "Pat Fixture"]


def _seed_campaign_one_property() -> FixtureFirestore:
    """One live client, one queued outreach, follow-ups enabled."""
    store = FixtureFirestore()
    store.data[FIXTURE_PREFIX] = {
        "email": FIXTURE_SENDER,
        "signatureMode": "none",
    }
    store.data[CAMPAIGN_AUTHORITY_PATH] = {
        "automationEnabled": True,
        "allowedUids": [FIXTURE_UID],
    }
    from email_automation.column_config import get_default_column_config

    store.data[f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}"] = {
        "sheetId": FIXTURE_SHEET,
        "status": "live",
        "columnConfig": get_default_column_config(),
    }
    store.data[
        f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}/notifications/notif-1"
    ] = {"kind": "sheet_update"}
    store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"] = {
        "assignedEmails": [FIXTURE_RECIPIENT],
        "script": "Hi Pat, could you share the asking rent for 100 Fixture Way?",
        "scriptSelectionMode": "exact",
        "clientId": FIXTURE_CLIENT,
        "subject": "100 Fixture Way",
        "rowNumber": FIXTURE_ROW,
        "source": "dashboard_new_campaign",
        "actionType": "campaign_launch",
        "contactName": "Pat",
        "actionAuditId": "audit-1",
        "notificationId": "notif-1",
        "notificationClientId": FIXTURE_CLIENT,
        "deleteNotificationOnSend": True,
        "followUpConfig": {
            "enabled": True,
            "followUps": [
                {"waitTime": 3, "waitUnit": "days",
                 "message": "Following up on 100 Fixture Way."}
            ],
        },
        "createdAt": "2026-08-17T00:00:00Z",
    }
    return store


# Logical fixture key -> seed builder. Membership here is the admission test:
# a scenario naming a key with no builder cannot run, and says so.
SEEDS = {
    "certification-integrity/campaign-one-property": _seed_campaign_one_property,
}


def prepare(logical_key: str) -> PreparedFixture:
    """Build the fixture a scenario's logical key names.

    Raises ``FixtureError`` rather than improvising a fixture: an unregistered
    key means the scenario is not runnable yet, which is a different verdict
    from a scenario that ran and failed.
    """
    builder = SEEDS.get(logical_key)
    if builder is None:
        raise FixtureError(f"no fixture is registered for logical key {logical_key}")
    return PreparedFixture(
        logical_key=logical_key,
        firestore=builder(),
        sheets=FixtureSheets(SHEET_HEADER, SHEET_ROW),
        prefix=FIXTURE_PREFIX,
        sheet_ids=(FIXTURE_SHEET,),
        # campaign authority is a genuinely global decision: readable, never writable
        readable_paths=(CAMPAIGN_AUTHORITY_PATH,),
    )
