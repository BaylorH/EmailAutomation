"""Deterministic Firestore fakes for source-coordinator state-machine tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock

from google.api_core.exceptions import Aborted


class FakeTransactionAborted(Aborted, RuntimeError):
    """Firestore-shaped conflict raised for a stale transaction snapshot."""


def _raise_configured_failure(value):
    if isinstance(value, BaseException):
        raise value
    if isinstance(value, type) and issubclass(value, BaseException):
        raise value()
    raise RuntimeError(str(value))


@dataclass(frozen=True)
class FakeDocumentSnapshot:
    reference: "FakeDocumentReference"
    _data: dict | None

    @property
    def exists(self):
        return self._data is not None

    @property
    def id(self):
        return self.reference.id

    def to_dict(self):
        return deepcopy(self._data) if self._data is not None else None

    def get(self, field_path):
        if self._data is None:
            raise KeyError(field_path)
        return deepcopy(self._data[field_path])


class FakeCollectionReference:
    def __init__(self, store, path):
        self._store = store
        self._parts = tuple(path)

    @property
    def id(self):
        return self._parts[-1]

    @property
    def path(self):
        return "/".join(self._parts)

    def document(self, document_id):
        if (
            type(document_id) is not str
            or not document_id
            or "/" in document_id
            or (
                len(document_id) >= 4
                and document_id.startswith("__")
                and document_id.endswith("__")
            )
        ):
            raise ValueError("fake document id must be a non-empty string")
        return FakeDocumentReference(self._store, (*self._parts, document_id))


class FakeDocumentReference:
    def __init__(self, store, path):
        self._store = store
        self._parts = tuple(path)

    @property
    def id(self):
        return self._parts[-1]

    @property
    def path(self):
        return "/".join(self._parts)

    def collection(self, name):
        if type(name) is not str or not name:
            raise ValueError("fake collection name must be a non-empty string")
        return FakeCollectionReference(self._store, (*self._parts, name))

    def get(self, *, transaction=None):
        if transaction is not None:
            return transaction.get_document(self)
        return self._store._snapshot(self)

    def create(self, data):
        transaction = self._store.transaction()
        transaction.create(self, data)
        transaction.commit()

    def set(self, data, *, merge=False):
        transaction = self._store.transaction()
        transaction.set(self, data, merge=merge)
        transaction.commit()

    def update(self, data):
        transaction = self._store.transaction()
        transaction.update(self, data)
        transaction.commit()

    def delete(self):
        transaction = self._store.transaction()
        transaction.delete(self)
        transaction.commit()

    def __eq__(self, other):
        return (
            isinstance(other, FakeDocumentReference)
            and self._store is other._store
            and self._parts == other._parts
        )

    def __hash__(self):
        return hash((id(self._store), self._parts))


class FakeTransaction:
    def __init__(self, store, *, max_attempts=5):
        if isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("fake transaction max_attempts must be positive")
        self._store = store
        self._operations = []
        self._committed = False
        self._max_attempts = max_attempts
        self._read_only = False
        self._id = None
        self._snapshot_data = None
        self._snapshot_versions = None
        self._read_set = {}

    @property
    def in_progress(self):
        return self._id is not None

    def _clean_up(self):
        self._operations = []
        self._committed = False
        self._id = None
        self._snapshot_data = None
        self._snapshot_versions = None
        self._read_set = {}

    def _begin(self, retry_id=None):
        if self.in_progress:
            raise ValueError("fake transaction is already in progress")
        with self._store._lock:
            self._id = b"fake-transaction"
            self._snapshot_data = deepcopy(self._store.data)
            self._snapshot_versions = dict(self._store._versions)
            self._read_set = {}
            self._store.events.append(("transaction_began", retry_id))

    def _commit(self):
        if not self.in_progress:
            raise ValueError("fake transaction is not in progress")
        result = self._apply_buffered()
        self._clean_up()
        return result

    def _rollback(self):
        if not self.in_progress:
            raise ValueError("fake transaction is not in progress")
        self._store.events.append(("transaction_rolled_back",))
        self._clean_up()

    def get(self, document_ref):
        """Match Firestore Transaction.get(): return a snapshot iterator."""
        return iter((self.get_document(document_ref),))

    def get_document(self, document_ref):
        self._ensure_open()
        if not self.in_progress:
            raise ValueError("fake transaction is not in progress")
        self._validate_ref(document_ref)
        if self._operations:
            raise RuntimeError("fake Firestore forbids reads after writes")
        return self._snapshot(document_ref)

    def _snapshot(self, document_ref):
        path = document_ref.path
        snapshot_data = deepcopy(self._snapshot_data.get(path))
        self._read_set[path] = (
            self._snapshot_versions.get(path, 0),
            path in self._snapshot_data,
            deepcopy(snapshot_data),
        )
        self._store.events.append(("get", path))
        return FakeDocumentSnapshot(document_ref, snapshot_data)

    def create(self, document_ref, data):
        self._buffer("create", document_ref, data, False)

    def set(self, document_ref, data, *, merge=False):
        self._buffer("set", document_ref, data, bool(merge))

    def update(self, document_ref, data):
        self._buffer("update", document_ref, data, False)

    def delete(self, document_ref):
        self._ensure_open()
        self._validate_ref(document_ref)
        self._operations.append(("delete", document_ref, None, False))

    def _buffer(self, operation, document_ref, data, merge):
        self._ensure_open()
        self._validate_ref(document_ref)
        if type(data) is not dict:
            raise TypeError("fake Firestore writes require a dict")
        self._operations.append(
            (operation, document_ref, deepcopy(data), merge)
        )

    def _ensure_open(self):
        if self._committed:
            raise RuntimeError("fake transaction is already committed")

    def _validate_ref(self, document_ref):
        if (
            not isinstance(document_ref, FakeDocumentReference)
            or document_ref._store is not self._store
        ):
            raise TypeError("fake transaction reference belongs to another store")

    def commit(self):
        self._ensure_open()
        self._committed = True
        return self._apply_buffered()

    def _apply_buffered(self):
        with self._store._lock:
            if self._store.fail_next_commit is not None:
                failure = self._store.fail_next_commit
                self._store.fail_next_commit = None
                self._store.events.append(("commit_failed_before_apply",))
                _raise_configured_failure(failure)

            self._validate_read_versions()
            staged = deepcopy(self._store.data)
            staged_events = []
            written_paths = []
            for operation, document_ref, payload, merge in self._operations:
                path = document_ref.path
                if operation == "create":
                    if path in staged:
                        raise RuntimeError(
                            f"fake create precondition failed for {path}"
                        )
                    staged[path] = deepcopy(payload)
                elif operation == "set":
                    if merge and path in staged:
                        merged = deepcopy(staged[path])
                        merged.update(deepcopy(payload))
                        staged[path] = merged
                    else:
                        staged[path] = deepcopy(payload)
                elif operation == "update":
                    if path not in staged:
                        raise RuntimeError(
                            f"fake update precondition failed for {path}"
                        )
                    updated = deepcopy(staged[path])
                    updated.update(deepcopy(payload))
                    staged[path] = updated
                elif operation == "delete":
                    staged.pop(path, None)
                else:  # pragma: no cover - guarded by the public fake methods
                    raise AssertionError(f"unknown fake operation {operation}")
                staged_events.append((operation, path, deepcopy(payload), merge))
                if path not in written_paths:
                    written_paths.append(path)

            self._store.data.clear()
            self._store.data.update(staged)
            for path in written_paths:
                self._store._version_clock += 1
                self._store._versions[path] = self._store._version_clock
            self._store.events.extend(staged_events)
            self._store.events.append(("commit_applied", len(staged_events)))

            if self._store.apply_then_raise_next_commit is not None:
                failure = self._store.apply_then_raise_next_commit
                self._store.apply_then_raise_next_commit = None
                self._store.events.append(("commit_raised_after_apply",))
                _raise_configured_failure(failure)

        return []

    def _validate_read_versions(self):
        for path, (snapshot_version, existed, snapshot_data) in self._read_set.items():
            current_version = self._store._versions.get(path, 0)
            current_existed = path in self._store.data
            current_data = self._store.data.get(path)
            if (
                current_version != snapshot_version
                or current_existed != existed
                or current_data != snapshot_data
            ):
                self._store.events.append(("commit_aborted_stale_read", path))
                raise FakeTransactionAborted(
                    f"fake transaction snapshot is stale for {path}"
                )


class FakeFirestore:
    def __init__(self):
        self.data = {}
        self.events = []
        self.fail_next_commit = None
        self.apply_then_raise_next_commit = None
        self._lock = RLock()
        self._versions = {}
        self._version_clock = 0

    def collection(self, name):
        if type(name) is not str or not name:
            raise ValueError("fake collection name must be a non-empty string")
        return FakeCollectionReference(self, (name,))

    def transaction(self, max_attempts=5):
        return FakeTransaction(self, max_attempts=max_attempts)

    def _snapshot(self, document_ref):
        if not isinstance(document_ref, FakeDocumentReference):
            raise TypeError("snapshot requires a fake document reference")
        with self._lock:
            self.events.append(("get", document_ref.path))
            return FakeDocumentSnapshot(
                document_ref,
                deepcopy(self.data.get(document_ref.path)),
            )
