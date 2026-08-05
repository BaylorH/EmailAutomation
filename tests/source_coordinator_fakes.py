"""Deterministic Firestore fakes for source-coordinator state-machine tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from threading import RLock

from google.api_core.exceptions import Aborted


class FakeTransactionAborted(Aborted, RuntimeError):
    """Firestore-shaped conflict raised for a stale transaction snapshot."""


class MutableClock:
    """Small aware-datetime clock for lease and takeover tests."""

    def __init__(self, current):
        self.current = current

    def __call__(self):
        return self.current

    def advance(self, *, seconds):
        self.current += timedelta(seconds=seconds)
        return self.current


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

    def where(self, field_path, operator, value):
        return FakeQuery(self).where(field_path, operator, value)

    def order_by(self, field_path):
        return FakeQuery(self).order_by(field_path)


class FakeQuery:
    def __init__(
        self,
        collection,
        *,
        filters=(),
        ordering=(),
        limit_count=None,
        start_after_path=None,
    ):
        self._collection = collection
        self._store = collection._store
        self._filters = tuple(filters)
        self._ordering = tuple(ordering)
        self._limit_count = limit_count
        self._start_after_path = start_after_path

    def where(self, field_path, operator, value):
        if type(field_path) is not str or not field_path:
            raise ValueError("fake query field path must be non-empty")
        if operator != "==":
            raise ValueError("fake query supports equality filters only")
        return FakeQuery(
            self._collection,
            filters=(*self._filters, (field_path, operator, deepcopy(value))),
            ordering=self._ordering,
            limit_count=self._limit_count,
            start_after_path=self._start_after_path,
        )

    def order_by(self, field_path):
        if type(field_path) is not str or not field_path:
            raise ValueError("fake query order field must be non-empty")
        return FakeQuery(
            self._collection,
            filters=self._filters,
            ordering=(*self._ordering, field_path),
            limit_count=self._limit_count,
            start_after_path=self._start_after_path,
        )

    def limit(self, count):
        if isinstance(count, bool) or type(count) is not int or count < 1:
            raise ValueError("fake query limit must be a positive integer")
        return FakeQuery(
            self._collection,
            filters=self._filters,
            ordering=self._ordering,
            limit_count=count,
            start_after_path=self._start_after_path,
        )

    def start_after(self, snapshot):
        if not isinstance(snapshot, FakeDocumentSnapshot):
            raise TypeError("fake query cursor requires a document snapshot")
        return FakeQuery(
            self._collection,
            filters=self._filters,
            ordering=self._ordering,
            limit_count=self._limit_count,
            start_after_path=snapshot.reference.path,
        )

    def stream(self):
        """Match Firestore Query.stream() for nontransactional readback."""
        transaction = self._store.transaction()
        transaction._begin(retry_id="query-stream")
        try:
            snapshots = transaction.get_query(self)
        finally:
            if transaction.in_progress:
                transaction._rollback()
        return iter(snapshots)


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
        self._query_read_set = {}

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
        self._query_read_set = {}

    def _begin(self, retry_id=None):
        if self.in_progress:
            raise ValueError("fake transaction is already in progress")
        with self._store._lock:
            self._id = b"fake-transaction"
            self._snapshot_data = deepcopy(self._store.data)
            self._snapshot_versions = dict(self._store._versions)
            self._read_set = {}
            self._query_read_set = {}
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
        if isinstance(document_ref, FakeQuery):
            return iter(self.get_query(document_ref))
        return iter((self.get_document(document_ref),))

    def get_query(self, query):
        self._ensure_open()
        if not self.in_progress:
            raise ValueError("fake transaction is not in progress")
        if not isinstance(query, FakeQuery) or query._store is not self._store:
            raise TypeError("fake transaction query belongs to another store")
        if self._operations:
            raise RuntimeError("fake Firestore forbids reads after writes")
        collection_parts = query._collection._parts
        collection_path = query._collection.path
        collection_state = {}
        matching = []
        for path, payload in self._snapshot_data.items():
            parts = tuple(path.split("/"))
            if (
                len(parts) != len(collection_parts) + 1
                or parts[: len(collection_parts)] != collection_parts
            ):
                continue
            collection_state[path] = (
                self._snapshot_versions.get(path, 0),
                deepcopy(payload),
            )
            if all(
                payload.get(field_path) == expected
                for field_path, _operator, expected in query._filters
            ):
                matching.append((path, deepcopy(payload)))
        self._query_read_set[collection_path] = collection_state
        self._store.events.append(
            ("query", collection_path, query._filters, query._ordering)
        )
        matching.sort(
            key=lambda item: tuple(item[1].get(field) for field in query._ordering)
            + (item[0],)
        )
        if query._start_after_path is not None:
            matching = [
                item for item in matching if item[0] > query._start_after_path
            ]
        if query._limit_count is not None:
            matching = matching[: query._limit_count]
        return [
            FakeDocumentSnapshot(
                FakeDocumentReference(self._store, tuple(path.split("/"))),
                payload,
            )
            for path, payload in matching
        ]

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
        barrier = self._store.before_commit_barrier
        if barrier is not None:
            barrier_index = barrier.wait(timeout=5)
            if barrier_index == 0 and self._store.before_commit_barrier is barrier:
                self._store.before_commit_barrier = None
        with self._store._lock:
            if self._store.fail_next_commit is not None:
                failure = self._store.fail_next_commit
                self._store.fail_next_commit = None
                self._store.events.append(("commit_failed_before_apply",))
                _raise_configured_failure(failure)

            if self._store.before_next_commit_hook is not None:
                hook = self._store.before_next_commit_hook
                self._store.before_next_commit_hook = None
                self._store.events.append(("before_commit_hook",))
                hook()

            if self._store.before_commit_hook is not None:
                self._store.events.append(("before_commit_inspection_hook",))
                self._store.before_commit_hook(self)

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
        for collection_path, snapshot_state in self._query_read_set.items():
            prefix = tuple(collection_path.split("/"))
            current_state = {}
            for path, payload in self._store.data.items():
                parts = tuple(path.split("/"))
                if len(parts) == len(prefix) + 1 and parts[: len(prefix)] == prefix:
                    current_state[path] = (
                        self._store._versions.get(path, 0),
                        deepcopy(payload),
                    )
            if current_state != snapshot_state:
                self._store.events.append(
                    ("commit_aborted_stale_query", collection_path)
                )
                raise FakeTransactionAborted(
                    f"fake transaction query snapshot is stale for {collection_path}"
                )


class FakeFirestore:
    def __init__(self):
        self.data = {}
        self.events = []
        self.fail_next_commit = None
        self.apply_then_raise_next_commit = None
        self.before_next_commit_hook = None
        self.before_commit_hook = None
        self.before_commit_barrier = None
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
