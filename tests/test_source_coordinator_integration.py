import os
import io
import logging
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.source_coordinator_fakes import FakeFirestore, MutableClock


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("AZURE_API_APP_ID", "test-client-id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-api-key")
_IMPORT_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
_SCOPED_IMPORT_OPENAI_API_KEY = (
    _IMPORT_OPENAI_API_KEY or "test-openai-api-key"
)
with mock.patch.dict(
    os.environ,
    {"OPENAI_API_KEY": _SCOPED_IMPORT_OPENAI_API_KEY},
    clear=False,
), mock.patch("google.cloud.firestore.Client", return_value=mock.Mock()):
    import main
    import scheduler_runner
    from email_automation import (
        messaging,
        processing,
        source_coordinator,
        system_health,
    )


MODE_ENV = source_coordinator.SOURCE_COORDINATOR_MODE_ENV
FROZEN_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
B1_HEALTH_KEYS = frozenset(
    {
        "b1ActiveClassifications",
        "b1AmbiguousClassifications",
        "b1BlockedSources",
        "b1NonsettledPendingAdmissions",
        "b1UnsettledWorkLedgers",
        "b1AliasConflicts",
        "b1MarkerOrSettlementAmbiguities",
        "b1LegacyTerminalQuarantined",
        "b1LegacyMarkerOnlyAmbiguous",
        "b1LegacyReplayClaimQuarantined",
    }
)


class SequentialIds:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return f"integration-{self.calls:04d}"


class FakeSnapshot:
    def __init__(self, data):
        self._data = deepcopy(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return deepcopy(self._data)


class RecordingDocument:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def collection(self, name):
        self.store.events.append(("collection", self.path, name))
        return RecordingCollection(self.store, f"{self.path}/{name}")

    def get(self):
        self.store.events.append(("get", self.path))
        if self.store.fail_get:
            raise RuntimeError("configured marker read failure")
        return FakeSnapshot(self.store.data.get(self.path))

    def set(self, payload, *, merge=False):
        copied = deepcopy(payload)
        self.store.events.append(("set", self.path, copied, merge))
        if self.store.fail_set:
            raise RuntimeError("configured marker write failure")
        if merge and self.path in self.store.data:
            self.store.data[self.path].update(copied)
        else:
            self.store.data[self.path] = copied


class RecordingCollection:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def document(self, name):
        self.store.events.append(("document", self.path, name))
        return RecordingDocument(self.store, f"{self.path}/{name}")


class RecordingFirestore:
    def __init__(self, *, fail_get=False, fail_set=False):
        self.data = {}
        self.events = []
        self.fail_get = fail_get
        self.fail_set = fail_set

    def collection(self, name):
        self.events.append(("collection", "", name))
        return RecordingCollection(self, name)


class ExplodingFirestore:
    def __init__(self):
        self.calls = []

    def collection(self, name):
        self.calls.append(name)
        raise AssertionError(f"source containment touched Firestore {name}")


class FakeSettlementCoordinator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def settle_source_markers_if_ready(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.result


class ReadOnlyCoordinatorHealthSnapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.id = reference.id
        self._data = deepcopy(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return deepcopy(self._data)


class ReadOnlyCoordinatorHealthQuery:
    def __init__(self, view, path, *, filters=(), limit_count=None):
        self.view = view
        self.path = path.strip("/")
        self.filters = tuple(filters)
        self.limit_count = limit_count

    def where(
        self,
        field_path=None,
        operator=None,
        value=None,
        *,
        filter=None,
    ):
        if filter is not None:
            field_path = filter.field_path
            operator = getattr(filter, "op_string", None)
            value = filter.value
        return ReadOnlyCoordinatorHealthQuery(
            self.view,
            self.path,
            filters=(*self.filters, (field_path, operator, deepcopy(value))),
            limit_count=self.limit_count,
        )

    def order_by(self, _field_path):
        return self

    def limit(self, count):
        return ReadOnlyCoordinatorHealthQuery(
            self.view,
            self.path,
            filters=self.filters,
            limit_count=count,
        )

    @staticmethod
    def _matches(actual, operator, expected):
        if operator == "==":
            return actual == expected
        if operator == "!=":
            return actual != expected
        if operator == "in":
            return actual in expected
        if operator == "not-in":
            return actual not in expected
        raise AssertionError(
            f"unsupported read-only health query operator: {operator}"
        )

    def stream(self):
        prefix = f"{self.path}/"
        expected_depth = len(self.path.split("/")) + 1
        snapshots = []
        for document_path, data in sorted(self.view.backing.data.items()):
            if (
                not document_path.startswith(prefix)
                or len(document_path.split("/")) != expected_depth
            ):
                continue
            if not all(
                self._matches(data.get(field), operator, expected)
                for field, operator, expected in self.filters
            ):
                continue
            reference = ReadOnlyCoordinatorHealthDocument(
                self.view,
                document_path,
            )
            snapshots.append(
                ReadOnlyCoordinatorHealthSnapshot(reference, data)
            )
        if self.limit_count is not None:
            snapshots = snapshots[: self.limit_count]
        self.view.reads.append(
            (self.path, deepcopy(self.filters), self.limit_count)
        )
        return iter(snapshots)


class ReadOnlyCoordinatorHealthCollection(ReadOnlyCoordinatorHealthQuery):
    def document(self, document_id=None):
        if document_id is None:
            self.view._reject_write(
                "collection.document(auto-id)",
                self.path,
            )
        return ReadOnlyCoordinatorHealthDocument(
            self.view,
            f"{self.path}/{document_id}",
        )

    def add(self, _data, document_id=None, **_options):
        del document_id, _options
        self.view._reject_write("collection.add", self.path)


class ReadOnlyCoordinatorHealthDocument:
    def __init__(self, view, path):
        self.view = view
        self.path = path.strip("/")
        self.id = self.path.rsplit("/", 1)[-1]

    def collection(self, name):
        return ReadOnlyCoordinatorHealthCollection(
            self.view,
            f"{self.path}/{name}",
        )

    def get(self):
        self.view.reads.append((self.path, (), None))
        return ReadOnlyCoordinatorHealthSnapshot(
            self,
            self.view.backing.data.get(self.path),
        )

    def _reject_write(self, operation):
        self.view._reject_write(f"document.{operation}", self.path)

    def create(self, _data, **_options):
        del _options
        self._reject_write("create")

    def set(self, _data, *, merge=False, **_options):
        del merge, _options
        self._reject_write("set")

    def update(self, _data, **_options):
        del _options
        self._reject_write("update")

    def delete(self, **_options):
        del _options
        self._reject_write("delete")


class ReadOnlyCoordinatorHealthWriteProxy:
    def __init__(self, view, kind):
        self.view = view
        self.kind = kind

    @staticmethod
    def _reference_path(reference):
        path = getattr(reference, "path", None)
        return path if isinstance(path, str) and path else "<unknown-reference>"

    def _reject_reference_write(self, operation, reference):
        self.view._reject_write(
            f"{self.kind}.{operation}",
            self._reference_path(reference),
        )

    def create(self, reference, _data, **_options):
        del _options
        self._reject_reference_write("create", reference)

    def set(self, reference, _data, *, merge=False, **_options):
        del merge, _options
        self._reject_reference_write("set", reference)

    def update(self, reference, _data, **_options):
        del _options
        self._reject_reference_write("update", reference)

    def delete(self, reference, **_options):
        del _options
        self._reject_reference_write("delete", reference)

    def commit(self, **_options):
        del _options
        self.view._reject_write(f"{self.kind}.commit", "<client>")

    def flush(self, **_options):
        del _options
        self.view._reject_write(f"{self.kind}.flush", "<client>")

    def close(self, **_options):
        del _options
        self.view._reject_write(f"{self.kind}.close", "<client>")


class ReadOnlyCoordinatorHealthView:
    """Expose real coordinator fake state without granting write methods."""

    def __init__(self, backing):
        self.backing = backing
        self.reads = []
        self.writes = []

    def collection(self, name):
        return ReadOnlyCoordinatorHealthCollection(self, name)

    def _record_write(self, operation, path):
        self.writes.append((operation, path))

    def _reject_write(self, operation, path):
        self._record_write(operation, path)
        raise AssertionError(
            "read-only coordinator health view attempted "
            f"{operation} at {path}"
        )

    def batch(self, **_options):
        del _options
        self._record_write("client.batch", "<client>")
        return ReadOnlyCoordinatorHealthWriteProxy(self, "batch")

    def transaction(self, **_options):
        del _options
        self._record_write("client.transaction", "<client>")
        return ReadOnlyCoordinatorHealthWriteProxy(self, "transaction")

    def bulk_writer(self, **_options):
        del _options
        self._record_write("client.bulk_writer", "<client>")
        return ReadOnlyCoordinatorHealthWriteProxy(self, "bulk_writer")

    def recursive_delete(self, reference, **_options):
        del _options
        path = getattr(reference, "path", "<unknown-reference>")
        self._reject_write("client.recursive_delete", path)


def build_ready_ordinary_source(
    *,
    graph_id="graph-enforced",
    complete_work=True,
    message_body="Please update the stage.",
    proposal_value="warm",
    recipient="recipient@example.test",
):
    fake = FakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        fake,
        uuid_factory=SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    identity = coordinator.admit_or_repair_source_identity(
        user_id="user-enforced",
        hydrated_message={"id": graph_id},
        evidence_kind="graph_hydration",
        thread_id="thread-enforced",
    )
    coordinator.classify_source_once(
        user_id="user-enforced",
        canonical_source_id=identity.canonical_source_id,
        lease_seconds=60,
        classification_input={
            "schemaVersion": 1,
            "message": {
                "from": "sender@example.test",
                "subject": "Ready source",
                "body": message_body,
                "recipient": recipient,
            },
        },
        classifier=lambda: (
            {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [
                    {
                        "type": "field_update",
                        "field": "stage",
                        "value": proposal_value,
                    }
                ],
            },
            {
                "schemaVersion": 1,
                "evidenceKind": "model_capture",
                "responseHash": "a" * 64,
            },
        ),
    )
    coordinator.elect_transition_owner_from_snapshot(
        user_id="user-enforced",
        canonical_source_id=identity.canonical_source_id,
    )
    ledger = coordinator.create_or_verify_source_work_ledger(
        user_id="user-enforced",
        canonical_source_id=identity.canonical_source_id,
    )
    with mock.patch.object(processing, "_fs", fake):
        saved_history_binding, index_binding = (
            processing._persist_strict_source_history_and_index(
                user_id="user-enforced",
                thread_id="thread-enforced",
                canonical_source_id=identity.canonical_source_id,
                message_record={
                    "direction": "inbound",
                    "body": {
                        "contentType": "Text",
                        "content": message_body,
                    },
                    "to": [recipient],
                    "headers": {"internetMessageId": None},
                    "sourceMessage": {
                        "graphMessageId": graph_id,
                        "internetMessageId": None,
                    },
                },
            )
        )
    coordinator.admit_pending_inbound(
        user_id="user-enforced",
        canonical_source_id=identity.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding=saved_history_binding,
        index_binding=index_binding,
    )
    entry = ledger["entries"][0]
    work_arguments = {
        "user_id": "user-enforced",
        "canonical_source_id": identity.canonical_source_id,
        "ledger_hash": ledger["ledgerHash"],
        "work_key": entry["workKey"],
        "payload_hash": entry["payloadHash"],
    }
    if complete_work:
        coordinator.record_source_work_applying(**work_arguments)
        coordinator.complete_source_work_entry(
            **work_arguments,
            completion_record={
                "schemaVersion": 1,
                "evidenceKind": "work_completion",
                "workKind": "field_update",
                "resultHash": "b" * 64,
            },
        )
    return fake, coordinator, identity, ledger


class SourceCoordinatorHealthIntegrationTests(unittest.TestCase):
    PENDING_ADMISSION_KEY = "b1NonsettledPendingAdmissions"
    UNSETTLED_LEDGER_KEY = "b1UnsettledWorkLedgers"
    ACTIVE_CLASSIFICATION_KEY = "b1ActiveClassifications"
    AMBIGUOUS_CLASSIFICATION_KEY = "b1AmbiguousClassifications"
    LEGACY_TERMINAL_QUARANTINE_KEY = "b1LegacyTerminalQuarantined"

    @staticmethod
    def _collect_health(backing):
        view = ReadOnlyCoordinatorHealthView(backing)
        stdout_output = io.StringIO()
        stderr_output = io.StringIO()
        logging_output = io.StringIO()
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        capture_handler = logging.StreamHandler(logging_output)
        capture_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(capture_handler)
        root_logger.setLevel(logging.DEBUG)
        try:
            with redirect_stdout(stdout_output), redirect_stderr(stderr_output):
                payload = system_health.collect_user_health(
                    "user-enforced",
                    fs_client=view,
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                    now=FROZEN_NOW,
                )
        finally:
            root_logger.removeHandler(capture_handler)
            root_logger.setLevel(previous_level)
            capture_handler.close()
        rendered_channels = "".join(
            (
                stdout_output.getvalue(),
                stderr_output.getvalue(),
                logging_output.getvalue(),
            )
        )
        return payload, rendered_channels, view

    def assertAggregateHealthContract(self, payload, rendered_channels, *, raw_values):
        self.assertEqual(
            {
                "status",
                "token",
                "graph",
                "queues",
                "countErrors",
                "lastCheckedAt",
                "updatedAt",
            },
            set(payload),
        )
        queues = payload["queues"]
        self.assertEqual(
            {
                *system_health.QUEUE_COLLECTIONS,
                system_health.TERMINAL_PROTOCOL_HEALTH_KEY,
                *B1_HEALTH_KEYS,
            },
            set(queues),
        )
        self.assertEqual(
            B1_HEALTH_KEYS,
            {key for key in queues if key.startswith("b1")},
        )
        for key in B1_HEALTH_KEYS:
            self.assertIs(type(queues[key]), int, key)
        rendered = f"{payload!r}\n{rendered_channels}"
        for raw_value in raw_values:
            self.assertNotIn(raw_value, rendered)


    @staticmethod
    def _settle_ordinary_source(coordinator, identity, ledger):
        entry = ledger["entries"][0]
        work_arguments = {
            "user_id": "user-enforced",
            "canonical_source_id": identity.canonical_source_id,
            "ledger_hash": ledger["ledgerHash"],
            "work_key": entry["workKey"],
            "payload_hash": entry["payloadHash"],
        }
        coordinator.record_source_work_applying(**work_arguments)
        coordinator.complete_source_work_entry(
            **work_arguments,
            completion_record={
                "schemaVersion": 1,
                "evidenceKind": "work_completion",
                "workKind": "field_update",
                "resultHash": "b" * 64,
            },
        )
        coordinator.settle_source_markers_if_ready(
            user_id="user-enforced",
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
        )

    @staticmethod
    def _add_legacy_terminal_quarantine(backing):
        graph_message_id = "raw-legacy-graph-health-canary"
        internet_message_id = "raw-legacy-address-health@example.test"
        thread_id = "raw-legacy-thread-health-canary"
        client_id = "raw-legacy-customer-health-canary"
        backing.data[
            f"users/user-enforced/threads/{thread_id}"
        ] = {"clientId": client_id}

        def retained_terminal_loader(*_args, **_kwargs):
            return {
                "kind": "active",
                "saga": {
                    "clientId": client_id,
                    "sourceMessageKey": internet_message_id,
                    "sourceGraphMessageId": graph_message_id,
                    "sourceInternetMessageId": internet_message_id,
                    "sagaKey": "raw-legacy-saga-health-canary",
                    "immutableHash": "c" * 64,
                    "phase": "classified",
                },
                "settlement": None,
                "exactSourceConfirmed": True,
            }

        coordinator = source_coordinator.SourceCoordinator(
            backing,
            uuid_factory=lambda: "raw-legacy-canonical-health-canary",
            now_factory=MutableClock(FROZEN_NOW),
            retained_terminal_authority_loader=(
                retained_terminal_loader
            ),
        )
        identity = coordinator.admit_or_repair_source_identity(
            user_id="user-enforced",
            hydrated_message={
                "id": graph_message_id,
                "internetMessageId": internet_message_id,
                "body": "raw customer message body health canary",
                "recipient": "raw-recipient-health@example.test",
            },
            evidence_kind="operator_replay",
            thread_id=thread_id,
        )
        coordinator.quarantine_retained_terminal_authority(
            user_id="user-enforced",
            canonical_source_id=identity.canonical_source_id,
            thread_id=thread_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
        )
        return identity

    def test_empty_openai_key_is_scoped_only_during_offline_imports(self):
        self.assertTrue(_SCOPED_IMPORT_OPENAI_API_KEY)
        if _IMPORT_OPENAI_API_KEY == "":
            self.assertEqual("", os.environ.get("OPENAI_API_KEY"))
        self.assertIsNotNone(main)
        self.assertIsNotNone(system_health)

    def test_health_capture_includes_stderr_and_python_logging_channels(self):
        backing = FakeFirestore()
        stderr_canary = "raw-health-stderr-leak-canary"
        logging_canary = "raw-health-python-log-leak-canary"
        collect = system_health.collect_user_health

        def leaking_collect(*args, **kwargs):
            print(stderr_canary, file=sys.stderr)
            logging.getLogger("tests.health-leak-probe").warning(logging_canary)
            return collect(*args, **kwargs)

        with mock.patch.object(
            system_health,
            "collect_user_health",
            side_effect=leaking_collect,
        ):
            _payload, rendered_channels, _view = self._collect_health(backing)

        self.assertIn(stderr_canary, rendered_channels)
        self.assertIn(logging_canary, rendered_channels)

    def test_aggregate_contract_rejects_debug_topology_and_channel_leaks(self):
        aggregate_payload, rendered_channels, _view = self._collect_health(
            FakeFirestore()
        )
        debug_payload = deepcopy(aggregate_payload)
        debug_payload["queues"]["b1DebugDetails"] = 0

        with self.assertRaises(AssertionError):
            self.assertAggregateHealthContract(
                debug_payload,
                rendered_channels,
                raw_values=(),
            )
        with self.assertRaises(AssertionError):
            self.assertAggregateHealthContract(
                aggregate_payload,
                "raw-health-channel-leak-canary",
                raw_values=("raw-health-channel-leak-canary",),
            )

        stderr_canary = "raw-health-stderr-contract-canary"
        logging_canary = "raw-health-logging-contract-canary"
        collect = system_health.collect_user_health

        def leaking_collect(*args, **kwargs):
            print(stderr_canary, file=sys.stderr)
            logging.getLogger("tests.health-contract-probe").error(
                logging_canary
            )
            return collect(*args, **kwargs)

        with mock.patch.object(
            system_health,
            "collect_user_health",
            side_effect=leaking_collect,
        ):
            leaked_payload, leaked_channels, _leaked_view = (
                self._collect_health(FakeFirestore())
            )
        with self.assertRaises(AssertionError):
            self.assertAggregateHealthContract(
                leaked_payload,
                leaked_channels,
                raw_values=(stderr_canary, logging_canary),
            )

    def test_read_only_health_view_observes_every_supported_write_surface(self):
        def explicit_document(view):
            return view.collection("users").document("user-enforced")

        def arbitrary_document(view):
            return explicit_document(view).collection("healthProbe").document(
                "probe"
            )

        cases = {
            "collection add": (
                lambda view: explicit_document(view)
                .collection("healthProbe")
                .add({"unexpected": True}),
                "collection.add",
            ),
            "collection add options": (
                lambda view: explicit_document(view)
                .collection("healthProbe")
                .add(
                    {"unexpected": True},
                    document_id="probe",
                    retry="retry-policy",
                    timeout=1,
                ),
                "collection.add",
            ),
            "collection auto id": (
                lambda view: explicit_document(view)
                .collection("healthProbe")
                .document(),
                "collection.document(auto-id)",
            ),
            "document create": (
                lambda view: arbitrary_document(view).create(
                    {"unexpected": True}
                ),
                "document.create",
            ),
            "document set": (
                lambda view: arbitrary_document(view).set(
                    {"unexpected": True}
                ),
                "document.set",
            ),
            "document set options": (
                lambda view: arbitrary_document(view).set(
                    {"unexpected": True},
                    merge=True,
                    retry="retry-policy",
                    timeout=1,
                ),
                "document.set",
            ),
            "document update": (
                lambda view: arbitrary_document(view).update(
                    {"unexpected": True}
                ),
                "document.update",
            ),
            "document delete": (
                lambda view: arbitrary_document(view).delete(),
                "document.delete",
            ),
            "batch factory": (lambda view: view.batch(), "client.batch"),
            "batch create": (
                lambda view: view.batch().create(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "batch.create",
            ),
            "batch set": (
                lambda view: view.batch().set(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "batch.set",
            ),
            "batch update": (
                lambda view: view.batch().update(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "batch.update",
            ),
            "batch update option": (
                lambda view: view.batch().update(
                    arbitrary_document(view),
                    {"unexpected": True},
                    option="write-option",
                ),
                "batch.update",
            ),
            "batch delete": (
                lambda view: view.batch().delete(arbitrary_document(view)),
                "batch.delete",
            ),
            "batch commit": (
                lambda view: view.batch().commit(
                    retry="retry-policy",
                    timeout=1,
                ),
                "batch.commit",
            ),
            "transaction factory": (
                lambda view: view.transaction(),
                "client.transaction",
            ),
            "transaction create": (
                lambda view: view.transaction().create(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "transaction.create",
            ),
            "transaction set": (
                lambda view: view.transaction().set(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "transaction.set",
            ),
            "transaction update": (
                lambda view: view.transaction().update(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "transaction.update",
            ),
            "transaction delete": (
                lambda view: view.transaction().delete(
                    arbitrary_document(view),
                    option="write-option",
                ),
                "transaction.delete",
            ),
            "transaction commit": (
                lambda view: view.transaction().commit(),
                "transaction.commit",
            ),
            "bulk writer factory": (
                lambda view: view.bulk_writer(),
                "client.bulk_writer",
            ),
            "bulk writer create": (
                lambda view: view.bulk_writer().create(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "bulk_writer.create",
            ),
            "bulk writer set": (
                lambda view: view.bulk_writer().set(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "bulk_writer.set",
            ),
            "bulk writer update": (
                lambda view: view.bulk_writer().update(
                    arbitrary_document(view), {"unexpected": True}
                ),
                "bulk_writer.update",
            ),
            "bulk writer delete": (
                lambda view: view.bulk_writer().delete(
                    arbitrary_document(view)
                ),
                "bulk_writer.delete",
            ),
            "bulk writer flush": (
                lambda view: view.bulk_writer().flush(),
                "bulk_writer.flush",
            ),
            "bulk writer close": (
                lambda view: view.bulk_writer().close(),
                "bulk_writer.close",
            ),
            "recursive delete": (
                lambda view: view.recursive_delete(arbitrary_document(view)),
                "client.recursive_delete",
            ),
        }

        for case, (attempt, expected_operation) in cases.items():
            with self.subTest(case=case):
                view = ReadOnlyCoordinatorHealthView(FakeFirestore())
                try:
                    attempt(view)
                except Exception:
                    pass
                self.assertTrue(
                    any(
                        operation == expected_operation
                        for operation, _path in view.writes
                    ),
                    view.writes,
                )

    def test_pending_admission_and_unsettled_ledger_warn_then_settlement_clears(self):
        backing, coordinator, identity, ledger = (
            build_ready_ordinary_source(complete_work=False)
        )
        pending, pending_logs, pending_view = self._collect_health(backing)

        self._settle_ordinary_source(
            coordinator,
            identity,
            ledger,
        )
        settled, settled_logs, settled_view = self._collect_health(backing)

        self.assertEqual("warning", pending["status"])
        self.assertEqual(
            1,
            pending["queues"].get(self.PENDING_ADMISSION_KEY),
        )
        self.assertEqual(
            1,
            pending["queues"].get(self.UNSETTLED_LEDGER_KEY),
        )
        self.assertEqual(
            0,
            settled["queues"].get(self.PENDING_ADMISSION_KEY),
        )
        self.assertEqual(
            0,
            settled["queues"].get(self.UNSETTLED_LEDGER_KEY),
        )
        self.assertEqual("healthy", settled["status"])
        self.assertEqual("", pending_logs)
        self.assertEqual("", settled_logs)
        self.assertEqual([], pending_view.writes)
        self.assertEqual([], settled_view.writes)

    def test_settlement_does_not_hide_legacy_terminal_quarantine(self):
        backing, coordinator, identity, ledger = (
            build_ready_ordinary_source(complete_work=False)
        )
        self._add_legacy_terminal_quarantine(backing)
        before, _before_logs, _before_view = self._collect_health(backing)

        self._settle_ordinary_source(
            coordinator,
            identity,
            ledger,
        )
        after, _after_logs, _after_view = self._collect_health(backing)

        self.assertEqual(
            1,
            before["queues"].get(self.LEGACY_TERMINAL_QUARANTINE_KEY),
        )
        self.assertEqual(
            1,
            after["queues"].get(self.LEGACY_TERMINAL_QUARANTINE_KEY),
        )
        self.assertEqual(
            0,
            after["queues"].get(self.PENDING_ADMISSION_KEY),
        )
        self.assertEqual(
            0,
            after["queues"].get(self.UNSETTLED_LEDGER_KEY),
        )
        self.assertEqual(
            0,
            after["queues"].get(self.ACTIVE_CLASSIFICATION_KEY),
        )
        self.assertEqual(
            0,
            after["queues"].get(self.AMBIGUOUS_CLASSIFICATION_KEY),
        )
        self.assertEqual("warning", after["status"])

    def test_health_payload_and_logs_are_aggregate_only_and_read_only(self):
        body_canary = "raw-ordinary-body-health-canary"
        proposal_canary = "raw-ordinary-proposal-health-canary"
        recipient_canary = "raw-ordinary-recipient-health@example.test"
        backing, _coordinator, identity, _ledger = (
            build_ready_ordinary_source(
                graph_id="raw-ordinary-graph-health-canary",
                complete_work=False,
                message_body=body_canary,
                proposal_value=proposal_canary,
                recipient=recipient_canary,
            )
        )
        legacy_identity = self._add_legacy_terminal_quarantine(backing)
        persisted = repr(backing.data)
        self.assertIn(body_canary, persisted)
        self.assertIn(proposal_canary, persisted)
        self.assertIn(recipient_canary, persisted)

        payload, logs, view = self._collect_health(backing)

        self.assertAggregateHealthContract(
            payload,
            logs,
            raw_values=(
                identity.canonical_source_id,
                legacy_identity.canonical_source_id,
                *(alias.key for alias in identity.aliases),
                *(alias.key for alias in legacy_identity.aliases),
                "raw-ordinary-graph-health-canary",
                "raw-legacy-graph-health-canary",
                "raw-legacy-address-health@example.test",
                recipient_canary,
                "raw-recipient-health@example.test",
                body_canary,
                "raw customer message body health canary",
                proposal_canary,
                "thread-enforced",
                "raw-legacy-customer-health-canary",
                "raw-legacy-thread-health-canary",
                "raw-legacy-saga-health-canary",
            ),
        )
        self.assertTrue(view.reads)
        self.assertEqual([], view.writes)


class SourceCoordinatorModeContainmentTests(unittest.TestCase):
    def set_mode(self, value):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        if value is None:
            os.environ.pop(MODE_ENV, None)
        else:
            os.environ[MODE_ENV] = value

    def assert_legacy_marker_sequence(self, module):
        fake = RecordingFirestore()
        user_id = "user-disabled"
        key = "graph-disabled"
        encoded = module.b64url_id(key)
        marker_path = f"users/{user_id}/processedMessages/{encoded}"

        with mock.patch.object(module, "_fs", fake):
            self.assertFalse(module.has_processed(user_id, key))
            self.assertIsNone(module.mark_processed(user_id, key))
            self.assertTrue(module.has_processed(user_id, key))

        expected_ref_events = [
            ("collection", "", "users"),
            ("document", "users", user_id),
            ("collection", f"users/{user_id}", "processedMessages"),
            (
                "document",
                f"users/{user_id}/processedMessages",
                encoded,
            ),
        ]
        self.assertEqual(expected_ref_events, fake.events[:4])
        self.assertEqual(("get", marker_path), fake.events[4])
        self.assertEqual(expected_ref_events, fake.events[5:9])
        self.assertEqual("set", fake.events[9][0])
        self.assertEqual(marker_path, fake.events[9][1])
        self.assertEqual({"processedAt": module.SERVER_TIMESTAMP}, fake.events[9][2])
        self.assertIs(True, fake.events[9][3])
        self.assertEqual(expected_ref_events, fake.events[10:14])
        self.assertEqual(("get", marker_path), fake.events[14])

    def test_unset_and_invalid_modes_preserve_exact_legacy_marker_behavior(self):
        for mode in (None, "unexpected-mode"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                with mock.patch.object(
                    source_coordinator,
                    "SourceCoordinator",
                    side_effect=AssertionError("coordinator constructed"),
                ) as constructor:
                    self.assert_legacy_marker_sequence(messaging)
                    self.assert_legacy_marker_sequence(scheduler_runner)
                constructor.assert_not_called()

    def test_disabled_marker_errors_keep_legacy_swallowing_contract(self):
        self.set_mode(None)
        for module in (messaging, scheduler_runner):
            with self.subTest(module=module.__name__):
                failing_read = RecordingFirestore(fail_get=True)
                failing_write = RecordingFirestore(fail_set=True)
                with mock.patch.object(module, "_fs", failing_read):
                    self.assertFalse(module.has_processed("user-1", "graph-1"))
                with mock.patch.object(module, "_fs", failing_write):
                    self.assertIsNone(module.mark_processed("user-1", "graph-1"))

    def test_unset_and_invalid_scanner_entry_begin_with_legacy_token_download(self):
        class LegacyEntryReached(RuntimeError):
            pass

        for mode in (None, "invalid"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                with mock.patch.object(
                    source_coordinator,
                    "SourceCoordinator",
                    side_effect=AssertionError("coordinator constructed"),
                ) as constructor, mock.patch.object(
                    main,
                    "download_token",
                    side_effect=LegacyEntryReached,
                ) as download:
                    with self.assertRaises(LegacyEntryReached):
                        main.refresh_and_process_user("user-disabled")
                download.assert_called_once_with(
                    main.FIREBASE_API_KEY,
                    output_file=main.TOKEN_CACHE,
                    user_id="user-disabled",
                )
                constructor.assert_not_called()

    def test_shadow_markers_return_falsey_alias_proposals_with_zero_effects(self):
        self.set_mode("shadow")
        fake_fs = ExplodingFirestore()
        alias = source_coordinator.normalize_source_alias(
            "graph",
            "graph-shadow",
        )
        expected_key = source_coordinator.source_alias_key("user-shadow", alias)

        with mock.patch.object(messaging, "_fs", fake_fs), mock.patch.object(
            source_coordinator,
            "SourceCoordinator",
            side_effect=AssertionError("coordinator constructed"),
        ) as constructor:
            checked = messaging.has_processed(
                "user-shadow",
                "graph-shadow",
                source_alias=alias,
            )
            marked = messaging.mark_processed(
                "user-shadow",
                "graph-shadow",
                source_alias=alias,
            )

        for disposition, operation in (
            (checked, "has_processed"),
            (marked, "mark_processed"),
        ):
            self.assertEqual("shadow_no_effect", disposition.effect)
            self.assertEqual(operation, disposition.operation)
            self.assertEqual(expected_key, disposition.alias_key)
            self.assertFalse(disposition)
        self.assertEqual([], fake_fs.calls)
        constructor.assert_not_called()

    def test_shadow_scanner_entries_stop_before_provider_or_domain_calls(self):
        self.set_mode("shadow")
        forbidden = {
            "download": mock.Mock(side_effect=AssertionError("token download")),
            "graph": mock.Mock(side_effect=AssertionError("Graph request")),
            "marker": mock.Mock(side_effect=AssertionError("marker write")),
            "cursor": mock.Mock(side_effect=AssertionError("cursor write")),
            "domain": mock.Mock(side_effect=AssertionError("domain call")),
        }
        with mock.patch.object(main, "download_token", forbidden["download"]), \
             mock.patch.object(scheduler_runner.requests, "get", forbidden["graph"]), \
             mock.patch.object(scheduler_runner, "mark_processed", forbidden["marker"]), \
             mock.patch.object(scheduler_runner, "set_last_scan_iso", forbidden["cursor"]), \
             mock.patch.object(scheduler_runner, "process_inbox_message", forbidden["domain"]):
            main_result = main.refresh_and_process_user("user-shadow")
            scheduler_result = scheduler_runner.scan_inbox_against_index(
                "user-shadow",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual("shadow_no_effect", main_result.effect)
        self.assertEqual("shadow_no_effect", scheduler_result.effect)
        for counter in forbidden.values():
            counter.assert_not_called()

    def test_shadow_processing_entries_stop_before_hydration_or_construction(self):
        self.set_mode("shadow")
        graph = mock.Mock(side_effect=AssertionError("Graph hydration"))
        constructor = mock.Mock(side_effect=AssertionError("coordinator construction"))
        with mock.patch.object(
            processing,
            "exponential_backoff_request",
            graph,
        ), mock.patch.object(
            processing,
            "SourceCoordinator",
            constructor,
        ):
            direct = processing.process_inbox_message(
                "user-shadow",
                {"Authorization": "Bearer fake"},
                {"id": "graph-shadow"},
            )
            scanned = processing.scan_inbox_against_index(
                "user-shadow",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual("shadow_no_effect", direct.state)
        self.assertEqual("shadow_no_effect", scanned.state)
        self.assertFalse(direct)
        self.assertFalse(scanned)
        graph.assert_not_called()
        constructor.assert_not_called()

    def test_enforced_main_entry_stops_before_provider_or_domain_calls(self):
        self.set_mode("enforced")
        forbidden = {
            "download": mock.Mock(side_effect=AssertionError("token download")),
            "outbox": mock.Mock(side_effect=AssertionError("outbox send")),
            "pending": mock.Mock(side_effect=AssertionError("pending response")),
            "followup": mock.Mock(side_effect=AssertionError("follow-up send")),
        }
        with mock.patch.object(main, "download_token", forbidden["download"]), \
             mock.patch.object(main, "send_outboxes", forbidden["outbox"]), \
             mock.patch.object(
                 main,
                 "process_pending_responses",
                 forbidden["pending"],
             ), \
             mock.patch.object(
                 main,
                 "check_and_send_followups",
                 forbidden["followup"],
             ):
            with self.assertRaises(
                source_coordinator.SourceCoordinatorConfigError
            ):
                main.refresh_and_process_user("user-enforced")

        for counter in forbidden.values():
            counter.assert_not_called()

    def test_nonlegacy_scheduler_entry_stops_before_lease_and_user_discovery(self):
        for mode in ("shadow", "enforced"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                lease = mock.Mock(side_effect=AssertionError("scheduler lease"))
                users = mock.Mock(side_effect=AssertionError("user discovery"))
                health = mock.Mock(side_effect=AssertionError("health write"))
                with mock.patch.object(
                    main,
                    "run_with_scheduler_lease",
                    lease,
                ), mock.patch.object(
                    main,
                    "list_user_ids",
                    users,
                ), mock.patch.object(
                    main,
                    "record_user_health",
                    health,
                ):
                    if mode == "shadow":
                        direct = main.run_all_users()
                        scheduled = main.run_scheduled_automation()
                        self.assertEqual("shadow_no_effect", direct.effect)
                        self.assertEqual("shadow_no_effect", scheduled.effect)
                    else:
                        with self.assertRaises(
                            source_coordinator.SourceCoordinatorConfigError
                        ):
                            main.run_all_users()
                        with self.assertRaises(
                            source_coordinator.SourceCoordinatorConfigError
                        ):
                            main.run_scheduled_automation()

                lease.assert_not_called()
                users.assert_not_called()
                health.assert_not_called()

    def test_disabled_scheduler_entry_preserves_lease_dispatch(self):
        for mode in (None, "invalid"):
            with self.subTest(mode=mode):
                self.set_mode(mode)
                expected = object()
                with mock.patch.object(
                    main,
                    "run_with_scheduler_lease",
                    return_value=expected,
                ) as lease:
                    actual = main.run_scheduled_automation()

                self.assertIs(expected, actual)
                lease.assert_called_once_with(main.run_all_users)


class SourceCoordinatorEnforcedMarkerTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)
    def test_context_free_direct_marker_writers_fail_before_legacy_storage(self):
        for module in (messaging, scheduler_runner):
            with self.subTest(module=module.__name__):
                fake_fs = ExplodingFirestore()
                with mock.patch.object(module, "_fs", fake_fs):
                    with self.assertRaises(
                        source_coordinator.SourceCoordinatorConfigError
                    ):
                        module.has_processed("user-enforced", "graph-enforced")
                    with self.assertRaises(
                        source_coordinator.SourceCoordinatorConfigError
                    ):
                        module.mark_processed("user-enforced", "graph-enforced")
                self.assertEqual([], fake_fs.calls)

    def test_valid_context_delegates_exactly_to_canonical_settlement(self):
        fake, coordinator, identity, ledger = build_ready_ordinary_source()
        alias = identity.aliases[0]
        context = messaging.CanonicalSettlementContext(
            coordinator=coordinator,
            user_id="user-enforced",
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            alias=alias,
        )

        disposition = messaging.mark_processed(
            "user-enforced",
            "graph-enforced",
            settlement_context=context,
        )

        self.assertEqual(
            identity.canonical_source_id,
            disposition.settlement.canonical_source_id,
        )
        self.assertEqual("canonical_settlement", disposition.effect)
        self.assertTrue(disposition)
        alias_key = source_coordinator.source_alias_key(
            "user-enforced",
            alias,
        )
        projection_path = f"users/user-enforced/processedMessages/{alias_key}"
        self.assertEqual(
            identity.canonical_source_id,
            fake.data[projection_path]["canonicalSourceId"],
        )

    def test_fabricated_coordinator_cannot_grant_settlement_authority(self):
        alias = source_coordinator.normalize_source_alias(
            "graph",
            "graph-fabricated",
        )
        fabricated = FakeSettlementCoordinator(
            source_coordinator.SourceSettlementResult(
                canonical_source_id="source-fabricated",
                settlement_hash="c" * 64,
                settlement_revision=999,
                alias_projection_count=1,
                repaired_projection_count=0,
            )
        )
        context = messaging.CanonicalSettlementContext(
            coordinator=fabricated,
            user_id="user-enforced",
            canonical_source_id="source-fabricated",
            ledger_hash="d" * 64,
            alias=alias,
        )

        with self.assertRaises(source_coordinator.SourceCoordinatorConfigError):
            messaging.mark_processed(
                "user-enforced",
                "graph-fabricated",
                settlement_context=context,
            )
        self.assertEqual([], fabricated.calls)

    def test_unowned_alias_cannot_inherit_an_unrelated_source_settlement(self):
        fake, coordinator, identity, ledger = build_ready_ordinary_source()
        unowned_alias = source_coordinator.normalize_source_alias(
            "graph",
            "graph-unowned",
        )
        context = messaging.CanonicalSettlementContext(
            coordinator=coordinator,
            user_id="user-enforced",
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            alias=unowned_alias,
        )
        before = deepcopy(fake.data)

        with self.assertRaises(source_coordinator.SourceCoordinatorError):
            messaging.mark_processed(
                "user-enforced",
                "graph-unowned",
                settlement_context=context,
            )

        self.assertEqual(before, fake.data)

    def test_alias_mismatch_and_scheduler_escalation_marker_fail_closed(self):
        alias = source_coordinator.normalize_source_alias("graph", "graph-owned")
        result = source_coordinator.SourceSettlementResult(
            canonical_source_id="source-owned",
            settlement_hash="c" * 64,
            settlement_revision=1,
            alias_projection_count=1,
            repaired_projection_count=0,
        )
        coordinator = FakeSettlementCoordinator(result)
        context = messaging.CanonicalSettlementContext(
            coordinator=coordinator,
            user_id="user-enforced",
            canonical_source_id="source-owned",
            ledger_hash="d" * 64,
            alias=alias,
        )

        with self.assertRaises(source_coordinator.SourceCoordinatorConfigError):
            messaging.mark_processed(
                "user-enforced",
                "graph-different",
                settlement_context=context,
            )
        with self.assertRaises(source_coordinator.SourceCoordinatorConfigError):
            scheduler_runner.mark_processed(
                "user-enforced",
                "escalation:thread-1",
            )
        self.assertEqual([], coordinator.calls)


class SourceCoordinatorProductionConsumerGateTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_default_closed_consumer_leaves_human_work_pending(self):
        fake = FakeFirestore()
        user_id = "user-default-closed"
        thread_id = "thread-default-closed"
        graph_id = "graph-default-closed"
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        identity = coordinator.admit_or_repair_source_identity(
            user_id=user_id,
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        coordinator.classify_source_once(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input={
                "schemaVersion": 1,
                "message": {"body": "Please call me."},
            },
            classifier=lambda: (
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [
                        {
                            "type": "call_requested",
                            "reason": "explicit callback request",
                        }
                    ],
                    "ordinaryObligations": [],
                },
                {
                    "schemaVersion": 1,
                    "evidenceKind": "model_capture",
                    "responseHash": "a" * 64,
                },
            ),
        )
        owner = coordinator.elect_transition_owner_from_snapshot(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        self.assertEqual("human_decision", owner["ownerKind"])
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        with mock.patch.object(processing, "_fs", fake):
            saved_history_binding, index_binding = (
                processing._persist_strict_source_history_and_index(
                    user_id=user_id,
                    thread_id=thread_id,
                    canonical_source_id=identity.canonical_source_id,
                    message_record={
                        "direction": "inbound",
                        "headers": {"internetMessageId": None},
                        "sourceMessage": {
                            "graphMessageId": graph_id,
                            "internetMessageId": None,
                        },
                    },
                )
            )
        transition = coordinator.claim_or_resume_thread_transition(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            received_at=FROZEN_NOW,
            sent_at=FROZEN_NOW,
            saved_history_binding=saved_history_binding,
            index_binding=index_binding,
        )
        self.assertEqual("claimed", transition.disposition)

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
            side_effect=AssertionError("default-closed consumer was called"),
        ) as consumer:
            self.assertFalse(processing._source_authority_consumer_available())
            processed_count = processing._drain_durable_source_queue(user_id)

        self.assertEqual(0, processed_count)
        consumer.assert_not_called()
        retained_ledger = fake.data[
            f"users/{user_id}/sourceWorkLedgers/{identity.canonical_source_id}"
        ]
        self.assertEqual(
            {"pending"},
            {entry["state"] for entry in retained_ledger["entries"]},
        )
        admission = fake.data[
            "users/"
            f"{user_id}/inboundPendingAdmissions/{identity.canonical_source_id}"
        ]
        self.assertEqual("processing", admission["admissionState"])
        self.assertEqual(
            "active",
            fake.data[
                f"users/{user_id}/threadTransitionHeads/{thread_id}"
            ]["activeState"],
        )


class SourceCoordinatorScannerTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.consumer_available = mock.patch.object(
            processing,
            "_source_authority_consumer_available",
            return_value=True,
        )
        self.consumer_available.start()
        self.addCleanup(self.consumer_available.stop)

    def test_healthy_enforced_scan_uses_strict_cursor_adapter_only(self):
        response = mock.Mock()
        response.json.return_value = {"value": []}
        fake = FakeFirestore()

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            return_value=(),
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
            side_effect=AssertionError("legacy cursor setter reached"),
        ) as legacy_cursor:
            result = processing.scan_inbox_against_index(
                "user-strict-cursor",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", result["status"], result)
        strict_cursor.assert_called_once()
        self.assertEqual("user-strict-cursor", strict_cursor.call_args.kwargs["user_id"])
        self.assertIn("last_scan_iso", strict_cursor.call_args.kwargs)
        legacy_cursor.assert_not_called()

    def _build_owned_source(
        self,
        *,
        fake,
        coordinator,
        graph_id,
        received_at,
        include_ordinary=False,
    ):
        user_id = "user-durable-drain"
        thread_id = "thread-durable-drain"
        identity = coordinator.admit_or_repair_source_identity(
            user_id=user_id,
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        snapshot = coordinator.classify_source_once(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input={
                "schemaVersion": 1,
                "canonicalSourceId": identity.canonical_source_id,
                "message": {"body": graph_id},
            },
            classifier=lambda: (
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [
                        {
                            "type": "needs_user_input",
                            "reason": "durable-drain-test",
                        }
                    ],
                    "ordinaryObligations": (
                        [
                            {
                                "type": "field_update",
                                "field": "stage",
                                "value": graph_id,
                            }
                        ]
                        if include_ordinary
                        else []
                    ),
                },
                {
                    "schemaVersion": 1,
                    "evidenceKind": "model_capture",
                    "responseHash": source_coordinator.canonical_json_hash(
                        {"graphMessageId": graph_id}
                    ),
                },
            ),
        )
        owner = coordinator.elect_transition_owner_from_snapshot(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
        )
        message_record = {
            "direction": "inbound",
            "headers": {"internetMessageId": f"<{graph_id}@example.test>"},
            "sourceMessage": {
                "graphMessageId": graph_id,
                "internetMessageId": f"<{graph_id}@example.test>",
            },
        }
        semantic_message = deepcopy(message_record)
        semantic_message["headers"].pop("internetMessageId")
        semantic_message["sourceMessage"].pop("graphMessageId")
        semantic_message["sourceMessage"].pop("internetMessageId")
        history_hash = source_coordinator.canonical_json_hash(
            {
                "schemaVersion": 1,
                "canonicalSourceId": identity.canonical_source_id,
                "threadId": thread_id,
                "message": message_record,
            }
        )
        semantic_history_hash = source_coordinator.canonical_json_hash(
            {
                "schemaVersion": 1,
                "canonicalSourceId": identity.canonical_source_id,
                "threadId": thread_id,
                "message": semantic_message,
            }
        )
        fake.data[
            "users/"
            f"{user_id}/threads/{thread_id}/messages/"
            f"{identity.canonical_source_id}"
        ] = {
            **deepcopy(message_record),
            "canonicalSourceId": identity.canonical_source_id,
            "historyHash": history_hash,
            "semanticHistoryHash": semantic_history_hash,
        }
        saved_history_binding = {
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": thread_id,
            "historyDocumentId": identity.canonical_source_id,
            "historyHash": history_hash,
        }
        index_binding = {
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": thread_id,
            "identityDocumentId": identity.canonical_source_id,
        }
        transition = coordinator.claim_or_resume_thread_transition(
            user_id=user_id,
            canonical_source_id=identity.canonical_source_id,
            received_at=received_at,
            sent_at=received_at,
            saved_history_binding=saved_history_binding,
            index_binding=index_binding,
        )
        return identity, snapshot, owner, ledger, transition

    def _seed_durable_queue_pair(self, *, second_has_ordinary=False):
        fake = FakeFirestore()
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        first = self._build_owned_source(
            fake=fake,
            coordinator=coordinator,
            graph_id="graph-durable-first",
            received_at=FROZEN_NOW,
        )
        second = self._build_owned_source(
            fake=fake,
            coordinator=coordinator,
            graph_id="graph-durable-second",
            received_at=FROZEN_NOW + timedelta(seconds=1),
            include_ordinary=second_has_ordinary,
        )
        first_identity, _snapshot, _owner, first_ledger, first_transition = first
        second_identity, _snapshot, _owner, _ledger, second_transition = second
        self.assertEqual("claimed", first_transition.disposition)
        self.assertEqual("blocked", second_transition.disposition)
        for entry in first_ledger["entries"]:
            work_arguments = {
                "user_id": "user-durable-drain",
                "canonical_source_id": first_identity.canonical_source_id,
                "ledger_hash": first_ledger["ledgerHash"],
                "work_key": entry["workKey"],
                "payload_hash": entry["payloadHash"],
            }
            if entry["dominanceOutcome"] == "delegate_owner":
                coordinator.delegate_source_work_entry(**work_arguments)
            elif entry["dominanceOutcome"] == "dominated_by_owner":
                coordinator.dominate_source_work_entry_from_selection(
                    **work_arguments
                )
            else:
                self.fail(
                    "durable queue seed produced unexpected first-source work"
                )
        coordinator.settle_source_markers_if_ready(
            user_id="user-durable-drain",
            canonical_source_id=first_identity.canonical_source_id,
            ledger_hash=first_ledger["ledgerHash"],
        )
        release = coordinator.release_settled_generation_if_needed(
            user_id="user-durable-drain",
            thread_id="thread-durable-drain",
            canonical_source_id=first_identity.canonical_source_id,
        )
        self.assertEqual(
            second_identity.canonical_source_id,
            release.next_canonical_source_id,
        )
        return fake, coordinator, first, second

    def _claim_seeded_second(self, *, fake, coordinator, second_identity):
        admission_path = (
            "users/user-durable-drain/inboundPendingAdmissions/"
            f"{second_identity.canonical_source_id}"
        )
        admission = deepcopy(fake.data[admission_path])
        result = coordinator.claim_or_resume_thread_transition(
            user_id="user-durable-drain",
            canonical_source_id=second_identity.canonical_source_id,
            received_at=admission["receivedAt"],
            sent_at=admission["sentAt"],
            saved_history_binding=admission["savedHistoryBinding"],
            index_binding=admission["indexBinding"],
        )
        self.assertEqual("claimed", result.disposition)
        return admission_path

    def test_empty_graph_scan_drains_retained_eligible_wake_once(self):
        fake = FakeFirestore()
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        first = self._build_owned_source(
            fake=fake,
            coordinator=coordinator,
            graph_id="graph-durable-first",
            received_at=FROZEN_NOW,
        )
        second = self._build_owned_source(
            fake=fake,
            coordinator=coordinator,
            graph_id="graph-durable-second",
            received_at=FROZEN_NOW + timedelta(seconds=1),
        )
        first_identity, _snapshot, _owner, first_ledger, first_transition = first
        second_identity, _snapshot, _owner, _ledger, second_transition = second
        self.assertEqual("claimed", first_transition.disposition)
        self.assertEqual("blocked", second_transition.disposition)
        first_entry = first_ledger["entries"][0]
        self.assertEqual("delegate_owner", first_entry["dominanceOutcome"])
        coordinator.delegate_source_work_entry(
            user_id="user-durable-drain",
            canonical_source_id=first_identity.canonical_source_id,
            ledger_hash=first_ledger["ledgerHash"],
            work_key=first_entry["workKey"],
            payload_hash=first_entry["payloadHash"],
        )
        coordinator.settle_source_markers_if_ready(
            user_id="user-durable-drain",
            canonical_source_id=first_identity.canonical_source_id,
            ledger_hash=first_ledger["ledgerHash"],
        )
        release = coordinator.release_settled_generation_if_needed(
            user_id="user-durable-drain",
            thread_id="thread-durable-drain",
            canonical_source_id=first_identity.canonical_source_id,
        )
        self.assertEqual(
            second_identity.canonical_source_id,
            release.next_canonical_source_id,
        )
        second_admission_path = (
            "users/user-durable-drain/inboundPendingAdmissions/"
            f"{second_identity.canonical_source_id}"
        )
        self.assertEqual("eligible", fake.data[second_admission_path]["wakeState"])

        response = mock.Mock()
        response.json.return_value = {"value": []}
        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ) as graph_list, mock.patch.object(
            processing,
            "process_inbox_message",
        ) as graph_message_process, mock.patch.object(
            processing,
            "_consume_source_authority",
            return_value={"state": "completed", "completionRecords": {}},
        ) as consumer, mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            wraps=processing.advance_scan_cursor_if_source_authority_clear,
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as legacy_cursor:
            first_scan = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )
            second_scan = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", first_scan["status"], first_scan)
        self.assertEqual(1, first_scan["processed"])
        self.assertEqual("healthy", second_scan["status"])
        self.assertEqual(0, second_scan["processed"])
        self.assertEqual(1, consumer.call_count)
        self.assertEqual(2, graph_list.call_count)
        graph_message_process.assert_not_called()
        self.assertEqual(2, strict_cursor.call_count)
        legacy_cursor.assert_not_called()
        self.assertEqual("settled", fake.data[second_admission_path]["admissionState"])
        head = fake.data[
            "users/user-durable-drain/threadTransitionHeads/thread-durable-drain"
        ]
        self.assertEqual("clear", head["activeState"])

    def test_empty_graph_scan_resumes_wake_after_claim_crash(self):
        fake, coordinator, _first, second = self._seed_durable_queue_pair()
        second_identity, _snapshot, _owner, _ledger, _transition = second
        admission_path = self._claim_seeded_second(
            fake=fake,
            coordinator=coordinator,
            second_identity=second_identity,
        )
        self.assertEqual("processing", fake.data[admission_path]["admissionState"])
        self.assertEqual("consumed", fake.data[admission_path]["wakeState"])
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
            return_value={"state": "completed", "completionRecords": {}},
        ) as consumer, mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            wraps=processing.advance_scan_cursor_if_source_authority_clear,
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as legacy_cursor:
            result = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", result["status"], result)
        self.assertEqual(1, result["processed"])
        self.assertEqual("settled", fake.data[admission_path]["admissionState"])
        self.assertEqual(1, consumer.call_count)
        strict_cursor.assert_called_once()
        legacy_cursor.assert_not_called()

    def test_empty_graph_scan_settles_pending_none_owner_after_crash(self):
        fake = FakeFirestore()
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        identity = coordinator.admit_or_repair_source_identity(
            user_id="user-pending-none-resume",
            hydrated_message={"id": "graph-pending-none-resume"},
            evidence_kind="graph_hydration",
            thread_id="thread-pending-none-resume",
        )
        coordinator.classify_source_once(
            user_id="user-pending-none-resume",
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input={"schemaVersion": 1, "message": {}},
            classifier=lambda: (
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [],
                    "ordinaryObligations": [],
                },
                {
                    "schemaVersion": 1,
                    "evidenceKind": "model_capture",
                    "responseHash": "a" * 64,
                },
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="user-pending-none-resume",
            canonical_source_id=identity.canonical_source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id="user-pending-none-resume",
            canonical_source_id=identity.canonical_source_id,
        )
        self.assertEqual([], ledger["entries"])
        with mock.patch.object(processing, "_fs", fake):
            saved_history_binding, index_binding = (
                processing._persist_strict_source_history_and_index(
                    user_id="user-pending-none-resume",
                    thread_id="thread-pending-none-resume",
                    canonical_source_id=identity.canonical_source_id,
                    message_record={
                        "direction": "inbound",
                        "headers": {"internetMessageId": None},
                        "sourceMessage": {
                            "graphMessageId": "graph-pending-none-resume",
                            "internetMessageId": None,
                        },
                    },
                )
            )
        coordinator.admit_pending_inbound(
            user_id="user-pending-none-resume",
            canonical_source_id=identity.canonical_source_id,
            received_at=FROZEN_NOW,
            sent_at=FROZEN_NOW,
            saved_history_binding=saved_history_binding,
            index_binding=index_binding,
        )
        admission_path = (
            "users/user-pending-none-resume/inboundPendingAdmissions/"
            f"{identity.canonical_source_id}"
        )
        self.assertEqual("pending", fake.data[admission_path]["admissionState"])
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
        ) as consumer, mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            wraps=processing.advance_scan_cursor_if_source_authority_clear,
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as legacy_cursor:
            result = processing.scan_inbox_against_index(
                "user-pending-none-resume",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", result["status"], result)
        self.assertEqual("settled", fake.data[admission_path]["admissionState"])
        consumer.assert_not_called()
        strict_cursor.assert_called_once()
        legacy_cursor.assert_not_called()

    def test_empty_graph_scan_does_not_replay_applying_work_after_crash(self):
        fake, coordinator, _first, second = self._seed_durable_queue_pair(
            second_has_ordinary=True
        )
        second_identity, _snapshot, _owner, second_ledger, _transition = second
        admission_path = self._claim_seeded_second(
            fake=fake,
            coordinator=coordinator,
            second_identity=second_identity,
        )
        applying_entry = None
        for entry in second_ledger["entries"]:
            work_arguments = {
                "user_id": "user-durable-drain",
                "canonical_source_id": second_identity.canonical_source_id,
                "ledger_hash": second_ledger["ledgerHash"],
                "work_key": entry["workKey"],
                "payload_hash": entry["payloadHash"],
            }
            if entry["dominanceOutcome"] == "delegate_owner":
                coordinator.delegate_source_work_entry(**work_arguments)
            elif entry["dominanceOutcome"] == "preserve":
                coordinator.record_source_work_applying(**work_arguments)
                applying_entry = entry
            else:
                self.fail("unexpected applying-crash work lane")
        self.assertIsNotNone(applying_entry)
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
            return_value={
                "state": "completed",
                "completionRecords": {},
            },
        ) as consumer, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"], result)
        self.assertEqual("processing", fake.data[admission_path]["admissionState"])
        ledger_path = (
            "users/user-durable-drain/sourceWorkLedgers/"
            f"{second_identity.canonical_source_id}"
        )
        recovered_entry = next(
            entry
            for entry in fake.data[ledger_path]["entries"]
            if entry["workKey"] == applying_entry["workKey"]
        )
        self.assertEqual("applying", recovered_entry["state"])
        consumer.assert_not_called()
        cursor.assert_not_called()

    def test_empty_graph_scan_releases_settled_head_after_crash(self):
        fake, coordinator, _first, second = self._seed_durable_queue_pair()
        second_identity, _snapshot, _owner, second_ledger, _transition = second
        admission_path = self._claim_seeded_second(
            fake=fake,
            coordinator=coordinator,
            second_identity=second_identity,
        )
        for entry in second_ledger["entries"]:
            coordinator.delegate_source_work_entry(
                user_id="user-durable-drain",
                canonical_source_id=second_identity.canonical_source_id,
                ledger_hash=second_ledger["ledgerHash"],
                work_key=entry["workKey"],
                payload_hash=entry["payloadHash"],
            )
        coordinator.settle_source_markers_if_ready(
            user_id="user-durable-drain",
            canonical_source_id=second_identity.canonical_source_id,
            ledger_hash=second_ledger["ledgerHash"],
        )
        head_path = (
            "users/user-durable-drain/threadTransitionHeads/thread-durable-drain"
        )
        self.assertEqual("active", fake.data[head_path]["activeState"])
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
        ) as consumer, mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            wraps=processing.advance_scan_cursor_if_source_authority_clear,
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as legacy_cursor:
            result = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", result["status"], result)
        self.assertEqual("settled", fake.data[admission_path]["admissionState"])
        self.assertEqual("clear", fake.data[head_path]["activeState"])
        consumer.assert_not_called()
        strict_cursor.assert_called_once()
        legacy_cursor.assert_not_called()

    def test_missing_retained_history_blocks_before_graph_list(self):
        fake, _coordinator, _first, second = self._seed_durable_queue_pair()
        second_identity = second[0]
        history_path = (
            "users/user-durable-drain/threads/thread-durable-drain/messages/"
            f"{second_identity.canonical_source_id}"
        )
        del fake.data[history_path]

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
        ) as graph_list, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertIn("retained history", result["error"])
        graph_list.assert_not_called()
        cursor.assert_not_called()

    def test_durable_wake_completes_even_when_graph_list_fails(self):
        fake, _coordinator, _first, second = self._seed_durable_queue_pair()
        second_identity = second[0]
        admission_path = (
            "users/user-durable-drain/inboundPendingAdmissions/"
            f"{second_identity.canonical_source_id}"
        )

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=RuntimeError("Graph unavailable"),
        ), mock.patch.object(
            processing,
            "_consume_source_authority",
            return_value={"state": "completed", "completionRecords": {}},
        ) as consumer, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-durable-drain",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("settled", fake.data[admission_path]["admissionState"])
        self.assertEqual(1, consumer.call_count)
        cursor.assert_not_called()

    def test_two_same_thread_sources_are_independently_settled(self):
        now = datetime.now(timezone.utc)
        messages = [
            {
                "id": f"graph-source-{index}",
                "internetMessageId": f"<source-{index}@example.test>",
                "conversationId": "conversation-shared",
                "subject": f"Source {index}",
                "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
                "sentDateTime": now.isoformat().replace("+00:00", "Z"),
            }
            for index in (1, 2)
        ]
        response = mock.Mock()
        response.json.return_value = {"value": messages}
        fake = FakeFirestore()
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        dispatched_source_ids = []
        classified_source_ids = []

        def settle_dispatched_source(_user_id, _headers, message):
            graph_id = message["id"]
            dispatched_source_ids.append(graph_id)
            identity = coordinator.admit_or_repair_source_identity(
                user_id="user-scanner",
                hydrated_message=deepcopy(message),
                evidence_kind="graph_hydration",
                thread_id="thread-shared",
            )

            def classify():
                classified_source_ids.append(graph_id)
                return (
                    {
                        "schemaVersion": 1,
                        "transitionCandidates": [],
                        "ordinaryObligations": [
                            {
                                "type": "field_update",
                                "field": "stage",
                                "value": graph_id,
                            }
                        ],
                    },
                    {
                        "schemaVersion": 1,
                        "evidenceKind": "model_capture",
                        "responseHash": source_coordinator.canonical_json_hash(
                            {"graphMessageId": graph_id}
                        ),
                    },
                )

            snapshot = coordinator.classify_source_once(
                user_id="user-scanner",
                canonical_source_id=identity.canonical_source_id,
                lease_seconds=60,
                classification_input={
                    "schemaVersion": 1,
                    "graphMessageId": graph_id,
                },
                classifier=classify,
            )
            owner = coordinator.elect_transition_owner_from_snapshot(
                user_id="user-scanner",
                canonical_source_id=identity.canonical_source_id,
            )
            ledger = coordinator.create_or_verify_source_work_ledger(
                user_id="user-scanner",
                canonical_source_id=identity.canonical_source_id,
            )
            coordinator.admit_pending_inbound(
                user_id="user-scanner",
                canonical_source_id=identity.canonical_source_id,
                received_at=now,
                sent_at=now,
                saved_history_binding={
                    "schemaVersion": 1,
                    "historyKey": graph_id,
                },
                index_binding={
                    "schemaVersion": 1,
                    "indexKey": graph_id,
                },
            )
            entry = ledger["entries"][0]
            work_arguments = {
                "user_id": "user-scanner",
                "canonical_source_id": identity.canonical_source_id,
                "ledger_hash": ledger["ledgerHash"],
                "work_key": entry["workKey"],
                "payload_hash": entry["payloadHash"],
            }
            coordinator.record_source_work_applying(**work_arguments)
            coordinator.complete_source_work_entry(
                **work_arguments,
                completion_record={
                    "schemaVersion": 1,
                    "evidenceKind": "work_completion",
                    "workKind": "field_update",
                    "resultHash": source_coordinator.canonical_json_hash(
                        {"completedGraphMessageId": graph_id}
                    ),
                },
            )
            settlement = coordinator.settle_source_markers_if_ready(
                user_id="user-scanner",
                canonical_source_id=identity.canonical_source_id,
                ledger_hash=ledger["ledgerHash"],
            )
            return processing.SourceProcessingDisposition(
                mode=source_coordinator.CoordinatorMode.ENFORCED,
                state="settled",
                authority=processing.SourceProcessingAuthority(
                    canonical_source_id=identity.canonical_source_id,
                    snapshot_hash=snapshot.snapshot_immutable_hash,
                    selection_hash=snapshot.selection_hash,
                    owner_kind=owner["ownerKind"],
                    owner_key=owner["ownerKey"],
                    ledger_hash=ledger["ledgerHash"],
                ),
                settlement=settlement,
                thread_id="thread-shared",
                source_alias_keys=tuple(
                    sorted(alias.key for alias in identity.aliases)
                ),
            )

        cursor_settlement_counts = []

        strict_cursor_adapter = (
            processing.advance_scan_cursor_if_source_authority_clear
        )

        def record_cursor(*args, **kwargs):
            cursor_settlement_counts.append(
                sum(
                    "/sourceSettlements/" in path
                    for path in fake.data
                )
            )
            return strict_cursor_adapter(*args, **kwargs)

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-shared",
        ), mock.patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
        ), mock.patch.object(
            processing,
            "has_processed",
            return_value=False,
        ) as has_processed, mock.patch.object(
            processing,
            "_save_message_to_thread",
        ) as history_only, mock.patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ), mock.patch.object(
            processing,
            "process_inbox_message",
            side_effect=settle_dispatched_source,
        ), mock.patch.object(
            processing,
            "mark_processed",
        ) as legacy_marker, mock.patch.object(
            processing,
            "_clear_ai_processing_failure",
        ), mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            side_effect=record_cursor,
        ) as strict_cursor, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as legacy_cursor, mock.patch.object(
            processing.time,
            "sleep",
        ):
            result = processing.scan_inbox_against_index(
                "user-scanner",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=2,
            )

        self.assertEqual(
            ["graph-source-1", "graph-source-2"],
            dispatched_source_ids,
            "enforced scanner must process every exact same-thread source oldest-first",
        )
        self.assertEqual(dispatched_source_ids, classified_source_ids)
        for collection_name in (
            "sourceIdentities",
            "sourceClassifications",
            "sourceTransitionOwners",
            "sourceWorkLedgers",
            "inboundPendingAdmissions",
            "sourceSettlements",
        ):
            prefix = f"users/user-scanner/{collection_name}/"
            records = [
                payload
                for path, payload in fake.data.items()
                if path.startswith(prefix)
            ]
            self.assertEqual(2, len(records), collection_name)
        classification_records = [
            payload
            for path, payload in fake.data.items()
            if "/sourceClassifications/" in path
        ]
        self.assertTrue(
            all(
                record["classificationState"] == "snapshot_ready"
                for record in classification_records
            )
        )
        owner_records = [
            payload
            for path, payload in fake.data.items()
            if "/sourceTransitionOwners/" in path
        ]
        self.assertTrue(
            all(record["ownerKind"] == "none" for record in owner_records)
        )
        ledger_records = [
            payload
            for path, payload in fake.data.items()
            if "/sourceWorkLedgers/" in path
        ]
        self.assertTrue(
            all(
                all(entry["state"] == "completed" for entry in record["entries"])
                for record in ledger_records
            )
        )
        legacy_marker.assert_not_called()
        history_only.assert_not_called()
        has_processed.assert_not_called()
        self.assertEqual([2], cursor_settlement_counts)
        strict_cursor.assert_called_once()
        legacy_cursor.assert_not_called()
        self.assertEqual(2, result["processed"])
        self.assertEqual(0, result["batched"])

    def test_first_source_failure_preserves_same_thread_enumerability(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        messages = [
            {
                "id": f"graph-failure-{index}",
                "internetMessageId": f"<failure-{index}@example.test>",
                "receivedDateTime": now,
                "sentDateTime": now,
            }
            for index in (1, 2)
        ]
        response = mock.Mock()
        response.json.return_value = {"value": messages}

        with mock.patch.object(
            processing,
            "_fs",
            FakeFirestore(),
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-failure",
        ), mock.patch.object(
            processing,
            "process_inbox_message",
            side_effect=processing.RetryableProcessingError(
                "source one remains retryable"
            ),
        ) as process, mock.patch.object(
            processing,
            "_client_id_for_processing_failure",
            return_value="client-failure",
        ), mock.patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=True,
        ) as failure, mock.patch.object(
            processing,
            "has_processed",
        ) as has_processed, mock.patch.object(
            processing,
            "_save_message_to_thread",
        ) as history_only, mock.patch.object(
            processing,
            "mark_processed",
        ) as legacy_marker, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-failure",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=2,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["processed"])
        self.assertEqual("graph-failure-1", process.call_args.args[2]["id"])
        self.assertEqual(1, process.call_count)
        failure.assert_not_called()
        has_processed.assert_not_called()
        history_only.assert_not_called()
        legacy_marker.assert_not_called()
        cursor.assert_not_called()

    def test_unrelated_well_typed_settlement_cannot_advance_cursor(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message = {
            "id": "graph-binding-target",
            "internetMessageId": "<binding-target@example.test>",
            "receivedDateTime": now,
            "sentDateTime": now,
        }
        response = mock.Mock()
        response.json.return_value = {"value": [message]}
        fake = FakeFirestore()
        unrelated = processing.SourceProcessingDisposition(
            mode=source_coordinator.CoordinatorMode.ENFORCED,
            state="settled",
            authority=processing.SourceProcessingAuthority(
                canonical_source_id="source-unrelated",
                snapshot_hash="a" * 64,
                selection_hash="b" * 64,
                owner_kind="none",
                owner_key=None,
                ledger_hash="c" * 64,
            ),
            settlement=source_coordinator.SourceSettlementResult(
                canonical_source_id="source-unrelated",
                settlement_hash="d" * 64,
                settlement_revision=1,
                alias_projection_count=2,
                repaired_projection_count=0,
            ),
            thread_id="thread-binding-target",
            source_alias_keys=processing._source_alias_keys_for_message(
                "user-binding",
                message,
            ),
        )

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-binding-target",
        ), mock.patch.object(
            processing,
            "process_inbox_message",
            return_value=unrelated,
        ), mock.patch.object(
            processing,
            "_record_ai_processing_failure",
        ) as failure, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-binding",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["unsettled"])
        failure.assert_not_called()
        cursor.assert_not_called()

    def test_durable_pending_admission_outside_graph_window_blocks_cursor(self):
        fake, _coordinator, identity, _ledger = build_ready_ordinary_source(
            graph_id="graph-outside-window",
            complete_work=False,
        )
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "process_inbox_message",
        ) as process, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                "user-enforced",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["unsettled"])
        self.assertIn("outside the current Graph scan window", result["error"])
        admission_path = (
            "users/user-enforced/inboundPendingAdmissions/"
            f"{identity.canonical_source_id}"
        )
        self.assertEqual("pending", fake.data[admission_path]["admissionState"])
        process.assert_not_called()
        cursor.assert_not_called()

    def test_malformed_graph_inbox_page_cannot_advance_cursor(self):
        malformed_pages = (
            {},
            {"value": None},
            {"value": [{"subject": "missing exact identity"}]},
            {"value": [], "@odata.nextLink": 42},
        )
        for payload in malformed_pages:
            with self.subTest(payload=payload):
                response = mock.Mock()
                response.json.return_value = deepcopy(payload)
                with mock.patch.object(
                    processing,
                    "_fs",
                    FakeFirestore(),
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    return_value=response,
                ), mock.patch.object(
                    processing,
                    "set_last_scan_iso",
                ) as cursor:
                    result = processing.scan_inbox_against_index(
                        "user-malformed-graph-page",
                        {"Authorization": "Bearer local-test"},
                        only_unread=False,
                        top=1,
                    )

                self.assertEqual("error", result["status"])
                cursor.assert_not_called()

    def test_enforced_graph_pagination_is_origin_bound_acyclic_and_bounded(self):
        graph_page = (
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
        )
        cases = (
            (
                "off_origin",
                ({"value": [], "@odata.nextLink": "https://example.test/steal"},),
                100,
                1,
            ),
            (
                "cycle",
                (
                    {"value": [], "@odata.nextLink": f"{graph_page}?page=one"},
                    {"value": [], "@odata.nextLink": f"{graph_page}?page=one"},
                ),
                100,
                2,
            ),
            (
                "page_bound",
                (
                    {"value": [], "@odata.nextLink": f"{graph_page}?page=one"},
                    {"value": [], "@odata.nextLink": f"{graph_page}?page=two"},
                ),
                2,
                2,
            ),
        )
        for label, payloads, page_limit, expected_calls in cases:
            with self.subTest(label=label):
                responses = []
                for payload in payloads:
                    response = mock.Mock()
                    response.json.return_value = deepcopy(payload)
                    responses.append(response)
                with mock.patch.object(
                    processing,
                    "_fs",
                    FakeFirestore(),
                ), mock.patch.object(
                    processing,
                    "MAX_ENFORCED_INBOX_SCAN_PAGES",
                    page_limit,
                    create=True,
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    side_effect=responses,
                ) as graph_request, mock.patch.object(
                    processing,
                    "advance_scan_cursor_if_source_authority_clear",
                ) as strict_cursor:
                    result = processing.scan_inbox_against_index(
                        f"user-pagination-{label}",
                        {"Authorization": "Bearer local-test"},
                        only_unread=False,
                        top=1,
                    )

                self.assertEqual("error", result["status"])
                self.assertEqual(expected_calls, graph_request.call_count)
                strict_cursor.assert_not_called()

    def test_confirmed_untracked_message_is_skipped_without_blocking_cursor(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message = {
            "id": "graph-untracked",
            "internetMessageId": "<untracked@example.test>",
            "conversationId": "conversation-untracked",
            "receivedDateTime": now,
            "sentDateTime": now,
        }
        response = mock.Mock()
        response.json.return_value = {"value": [message]}

        with mock.patch.object(
            processing,
            "_fs",
            FakeFirestore(),
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
            return_value=None,
        ), mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            return_value=(),
        ) as strict_cursor, mock.patch.object(
            processing,
            "process_inbox_message",
        ) as process:
            result = processing.scan_inbox_against_index(
                "user-untracked",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("healthy", result["status"], result)
        self.assertEqual(1, result["skipped"])
        self.assertEqual(0, result["orphaned"])
        process.assert_not_called()
        strict_cursor.assert_called_once()

    def test_enforced_graph_message_budget_blocks_before_matching(self):
        response = mock.Mock()
        response.json.return_value = {
            "value": [
                {
                    "id": f"graph-budget-{index}",
                    "internetMessageId": f"<budget-{index}@example.test>",
                }
                for index in range(2)
            ]
        }

        with mock.patch.object(
            processing,
            "_fs",
            FakeFirestore(),
        ), mock.patch.object(
            processing,
            "MAX_ENFORCED_INBOX_SCAN_MESSAGES",
            1,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
        ) as match, mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
        ) as strict_cursor:
            result = processing.scan_inbox_against_index(
                "user-message-budget",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=2,
            )

        self.assertEqual("error", result["status"])
        match.assert_not_called()
        strict_cursor.assert_not_called()

    def test_strict_thread_match_hydration_failure_is_retryable(self):
        with mock.patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=RuntimeError("Graph headers unavailable"),
        ):
            with self.assertRaises(
                source_coordinator.SourceCoordinatorRetryable
            ):
                processing._match_message_to_thread(
                    "user-match",
                    {
                        "id": "graph-match",
                        "conversationId": "conversation-match",
                    },
                    {"Authorization": "Bearer local-test"},
                    strict=True,
                )

    def test_strict_thread_match_index_read_failure_is_retryable(self):
        with mock.patch.object(
            processing,
            "_fs",
            ExplodingFirestore(),
        ):
            with self.assertRaises(
                source_coordinator.SourceCoordinatorRetryable
            ):
                processing._match_message_to_thread(
                    "user-match-index",
                    {
                        "id": "graph-match-index",
                        "conversationId": "conversation-match-index",
                        "internetMessageHeaders": [],
                    },
                    {"Authorization": "Bearer local-test"},
                    strict=True,
                )

    def test_enforced_scan_processes_sources_globally_oldest_first(self):
        now = datetime.now(timezone.utc)
        messages = [
            {
                "id": graph_id,
                "internetMessageId": f"<{graph_id}@example.test>",
                "conversationId": thread_id,
                "receivedDateTime": received_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "sentDateTime": received_at.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            }
            for graph_id, thread_id, received_at in (
                ("graph-a1", "thread-a", now - timedelta(minutes=3)),
                ("graph-b1", "thread-b", now - timedelta(minutes=2)),
                ("graph-a2", "thread-a", now - timedelta(minutes=1)),
            )
        ]
        response = mock.Mock()
        response.json.return_value = {"value": messages}
        dispatch_order = []

        def settle_source(user_id, _headers, message):
            graph_id = message["id"]
            dispatch_order.append(graph_id)
            return processing.SourceProcessingDisposition(
                mode=source_coordinator.CoordinatorMode.ENFORCED,
                state="settled",
                authority=processing.SourceProcessingAuthority(
                    canonical_source_id=f"source-{graph_id}",
                    snapshot_hash="a" * 64,
                    selection_hash="b" * 64,
                    owner_kind="none",
                    owner_key=None,
                    ledger_hash="c" * 64,
                ),
                settlement=source_coordinator.SourceSettlementResult(
                    canonical_source_id=f"source-{graph_id}",
                    settlement_hash="d" * 64,
                    settlement_revision=1,
                    alias_projection_count=2,
                    repaired_projection_count=0,
                ),
                thread_id=message["conversationId"],
                source_alias_keys=processing._source_alias_keys_for_message(
                    user_id,
                    message,
                ),
            )

        with mock.patch.object(
            processing,
            "_fs",
            FakeFirestore(),
        ), mock.patch.object(
            processing,
            "_drain_durable_source_queue",
            return_value=0,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "_match_message_to_thread",
            side_effect=lambda _user_id, message, _headers, **_kwargs: (
                message["conversationId"]
            ),
        ), mock.patch.object(
            processing,
            "process_inbox_message",
            side_effect=settle_source,
        ), mock.patch.object(
            processing,
            "_is_bound_exact_source_settlement",
            return_value=True,
        ), mock.patch.object(
            processing,
            "verify_settled_source_dispatch_binding",
            return_value=True,
        ), mock.patch.object(
            processing,
            "advance_scan_cursor_if_source_authority_clear",
            return_value=(),
        ):
            result = processing.scan_inbox_against_index(
                "user-global-order",
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=3,
            )

        self.assertEqual("healthy", result["status"], result)
        self.assertEqual(
            ["graph-a1", "graph-b1", "graph-a2"],
            dispatch_order,
        )

    def _assert_empty_scan_blocks_canonical_authority(self, *, fake, user_id):
        response = mock.Mock()
        response.json.return_value = {"value": []}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), mock.patch.object(
            processing,
            "process_inbox_message",
        ) as process, mock.patch.object(
            processing,
            "set_last_scan_iso",
        ) as cursor:
            result = processing.scan_inbox_against_index(
                user_id,
                {"Authorization": "Bearer local-test"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertGreaterEqual(result["unsettled"], 1)
        process.assert_not_called()
        cursor.assert_not_called()

    def test_identity_only_authority_outside_graph_window_blocks_cursor(self):
        fake = FakeFirestore()
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        coordinator.admit_or_repair_source_identity(
            user_id="user-identity-only",
            hydrated_message={"id": "graph-identity-only"},
            evidence_kind="graph_hydration",
            thread_id="thread-identity-only",
        )

        self._assert_empty_scan_blocks_canonical_authority(
            fake=fake,
            user_id="user-identity-only",
        )

    def test_unknown_admission_state_outside_graph_window_blocks_cursor(self):
        fake, _coordinator, identity, _ledger = build_ready_ordinary_source(
            graph_id="graph-unknown-admission-state"
        )
        admission_path = (
            "users/user-enforced/inboundPendingAdmissions/"
            f"{identity.canonical_source_id}"
        )
        fake.data[admission_path]["admissionState"] = "corrupt"

        self._assert_empty_scan_blocks_canonical_authority(
            fake=fake,
            user_id="user-enforced",
        )

    def test_corrupt_schema_pair_cannot_hide_from_cursor_audit(self):
        fake, _coordinator, identity, _ledger = build_ready_ordinary_source(
            graph_id="graph-corrupt-schema-pair"
        )
        identity_path = (
            "users/user-enforced/sourceIdentities/"
            f"{identity.canonical_source_id}"
        )
        admission_path = (
            "users/user-enforced/inboundPendingAdmissions/"
            f"{identity.canonical_source_id}"
        )
        fake.data[identity_path]["schemaVersion"] = 2
        fake.data[admission_path]["schemaVersion"] = 2

        self._assert_empty_scan_blocks_canonical_authority(
            fake=fake,
            user_id="user-enforced",
        )


class SourceCoordinatorIgnoredSourceTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def test_auto_reply_and_self_sender_receive_local_canonical_settlement(self):
        cases = (
            (
                "auto_reply",
                "sender@example.test",
                [
                    {
                        "name": "In-Reply-To",
                        "value": "<tracked-root@example.test>",
                    },
                    {
                        "name": "Auto-Submitted",
                        "value": "auto-replied",
                    },
                ],
                "local_ignore_auto_reply",
            ),
            (
                "self_sender",
                "owner@example.test",
                [
                    {
                        "name": "In-Reply-To",
                        "value": "<tracked-root@example.test>",
                    }
                ],
                "local_ignore_self_sender",
            ),
        )
        for label, from_address, message_headers, expected_evidence_kind in cases:
            with self.subTest(label=label):
                fake = FakeFirestore()
                user_id = f"user-{label}"
                thread_id = f"thread-{label}"
                fake.data[f"users/{user_id}/threads/{thread_id}"] = {
                    "clientId": f"client-{label}",
                    "status": processing.THREAD_STATUS["active"],
                }
                message = {
                    "id": f"graph-{label}",
                    "internetMessageId": f"<{label}@example.test>",
                    "conversationId": f"conversation-{label}",
                    "subject": (
                        "Automatic reply" if label == "auto_reply" else "Forward"
                    ),
                    "from": {
                        "emailAddress": {
                            "address": from_address,
                            "name": "Sender",
                        }
                    },
                    "receivedDateTime": "2026-08-03T12:00:00Z",
                    "sentDateTime": "2026-08-03T12:00:00Z",
                    "bodyPreview": "Local policy source",
                    "hasAttachments": False,
                    "internetMessageHeaders": message_headers,
                }
                full_message = mock.Mock()
                full_message.json.return_value = {
                    "body": {
                        "contentType": "Text",
                        "content": "Local policy source",
                    },
                    "hasAttachments": False,
                }
                me_response = mock.Mock(status_code=200)
                me_response.json.return_value = {"mail": "owner@example.test"}
                with mock.patch.object(
                    processing,
                    "_fs",
                    fake,
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    return_value=full_message,
                ), mock.patch.object(
                    processing.requests,
                    "get",
                    return_value=me_response,
                ), mock.patch.object(
                    processing.requests,
                    "post",
                ) as post, mock.patch.object(
                    processing.requests,
                    "patch",
                ) as patch_request, mock.patch.object(
                    processing.requests,
                    "put",
                ) as put, mock.patch.object(
                    processing.requests,
                    "delete",
                ) as delete, mock.patch.object(
                    processing,
                    "lookup_thread_by_message_id",
                    return_value=thread_id,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_conversation_id",
                    return_value=None,
                ), mock.patch.object(
                    processing,
                    "_consume_source_authority",
                ) as downstream, mock.patch.object(
                    processing,
                    "_classify_source_proposal",
                ) as classifier:
                    result = processing.process_inbox_message(
                        user_id,
                        {"Authorization": "Bearer local-test"},
                        message,
                    )

                self.assertEqual("settled", result.state)
                self.assertEqual(thread_id, result.thread_id)
                self.assertEqual(
                    processing._source_alias_keys_for_message(user_id, message),
                    result.source_alias_keys,
                )
                self.assertEqual(2, result.settlement.alias_projection_count)
                canonical_source_id = result.authority.canonical_source_id
                classification = fake.data[
                    f"users/{user_id}/sourceClassifications/{canonical_source_id}"
                ]
                classifier.assert_not_called()
                self.assertIsNone(classification["modelRequestKey"])
                self.assertEqual(
                    "not_applicable",
                    classification["modelRequestState"],
                )
                self.assertEqual(
                    expected_evidence_kind,
                    classification["deterministicEvidence"]["evidenceKind"],
                )
                self.assertIsNone(classification["proposalEvidence"])
                self.assertEqual([], classification["transitionCandidates"])
                self.assertEqual([], classification["ordinaryObligations"])
                ledger = fake.data[
                    f"users/{user_id}/sourceWorkLedgers/{canonical_source_id}"
                ]
                self.assertEqual(0, ledger["entryCount"])
                admission = fake.data[
                    "users/"
                    f"{user_id}/inboundPendingAdmissions/{canonical_source_id}"
                ]
                self.assertEqual("settled", admission["admissionState"])
                downstream.assert_not_called()
                post.assert_not_called()
                patch_request.assert_not_called()
                put.assert_not_called()
                delete.assert_not_called()

    def test_missing_or_malformed_graph_headers_fail_retryably(self):
        cases = (
            ("missing", {}),
            (
                "malformed",
                {"internetMessageHeaders": "not-a-header-list"},
            ),
        )
        for label, header_payload in cases:
            with self.subTest(label=label):
                fake = FakeFirestore()
                user_id = f"user-header-{label}"
                thread_id = f"thread-header-{label}"
                fake.data[f"users/{user_id}/threads/{thread_id}"] = {
                    "clientId": f"client-header-{label}",
                    "status": processing.THREAD_STATUS["active"],
                }
                message = {
                    "id": f"graph-header-{label}",
                    "internetMessageId": f"<header-{label}@example.test>",
                    "conversationId": f"conversation-header-{label}",
                    "subject": "Tracked reply",
                    "from": {
                        "emailAddress": {
                            "address": "sender@example.test",
                            "name": "Sender",
                        }
                    },
                    "receivedDateTime": "2026-08-03T12:00:00Z",
                    "sentDateTime": "2026-08-03T12:00:00Z",
                    "bodyPreview": "Exact source body",
                    "hasAttachments": False,
                }
                body_response = mock.Mock()
                body_response.json.return_value = {
                    "body": {
                        "contentType": "Text",
                        "content": "Exact source body",
                    },
                    "hasAttachments": False,
                }
                header_response = mock.Mock()
                header_response.json.return_value = header_payload
                me_response = mock.Mock(status_code=200)
                me_response.json.return_value = {"mail": "owner@example.test"}
                empty_proposal = (
                    {
                        "schemaVersion": 1,
                        "transitionCandidates": [],
                        "ordinaryObligations": [],
                    },
                    {
                        "schemaVersion": 1,
                        "evidenceKind": "model_capture",
                        "responseHash": "e" * 64,
                    },
                )

                with mock.patch.object(
                    processing,
                    "_fs",
                    fake,
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    side_effect=[body_response, header_response],
                ), mock.patch.object(
                    processing.requests,
                    "get",
                    return_value=me_response,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_message_id",
                    return_value=None,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_conversation_id",
                    return_value=thread_id,
                ), mock.patch.object(
                    processing,
                    "_classify_source_proposal",
                    return_value=empty_proposal,
                ) as classifier:
                    with self.assertRaises(
                        source_coordinator.SourceCoordinatorRetryable
                    ):
                        processing.process_inbox_message(
                            user_id,
                            {"Authorization": "Bearer local-test"},
                            message,
                        )

                classifier.assert_not_called()
                self.assertFalse(
                    any("/sourceIdentities/" in path for path in fake.data)
                )

    def test_unavailable_or_empty_self_identity_fails_retryably(self):
        cases = (
            (
                "non_200",
                (503, {"error": {"code": "ServiceUnavailable"}}),
                (503, {"error": {"code": "ServiceUnavailable"}}),
            ),
            (
                "empty",
                (200, {"mail": "", "userPrincipalName": ""}),
                (200, {"value": []}),
            ),
        )
        for label, me_result, sent_result in cases:
            with self.subTest(label=label):
                fake = FakeFirestore()
                user_id = f"user-self-identity-{label}"
                thread_id = f"thread-self-identity-{label}"
                fake.data[f"users/{user_id}/threads/{thread_id}"] = {
                    "clientId": f"client-self-identity-{label}",
                    "status": processing.THREAD_STATUS["active"],
                }
                message = {
                    "id": f"graph-self-identity-{label}",
                    "internetMessageId": (
                        f"<self-identity-{label}@example.test>"
                    ),
                    "conversationId": f"conversation-self-identity-{label}",
                    "subject": "Forwarded message",
                    "from": {
                        "emailAddress": {
                            "address": "owner@example.test",
                            "name": "Owner",
                        }
                    },
                    "receivedDateTime": "2026-08-03T12:00:00Z",
                    "sentDateTime": "2026-08-03T12:00:00Z",
                    "bodyPreview": "Forwarded source",
                    "hasAttachments": False,
                    "internetMessageHeaders": [
                        {
                            "name": "In-Reply-To",
                            "value": "<tracked-root@example.test>",
                        }
                    ],
                }
                body_response = mock.Mock()
                body_response.json.return_value = {
                    "body": {
                        "contentType": "Text",
                        "content": "Forwarded source",
                    },
                    "hasAttachments": False,
                }
                me_response = mock.Mock(status_code=me_result[0])
                me_response.json.return_value = me_result[1]
                sent_response = mock.Mock(status_code=sent_result[0])
                sent_response.json.return_value = sent_result[1]
                empty_proposal = (
                    {
                        "schemaVersion": 1,
                        "transitionCandidates": [],
                        "ordinaryObligations": [],
                    },
                    {
                        "schemaVersion": 1,
                        "evidenceKind": "model_capture",
                        "responseHash": "e" * 64,
                    },
                )

                with mock.patch.object(
                    processing,
                    "_fs",
                    fake,
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    return_value=body_response,
                ), mock.patch.object(
                    processing.requests,
                    "get",
                    side_effect=[me_response, sent_response],
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_message_id",
                    return_value=thread_id,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_conversation_id",
                    return_value=None,
                ), mock.patch.object(
                    processing,
                    "_classify_source_proposal",
                    return_value=empty_proposal,
                ) as classifier:
                    with self.assertRaises(
                        source_coordinator.SourceCoordinatorRetryable
                    ):
                        processing.process_inbox_message(
                            user_id,
                            {"Authorization": "Bearer local-test"},
                            message,
                        )

                classifier.assert_not_called()
                self.assertFalse(
                    any("/sourceIdentities/" in path for path in fake.data)
                )


class SourceCoordinatorLateAliasProcessingTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)

    @staticmethod
    def message(*, graph_id, internet_message_id):
        headers = [
            {
                "name": "In-Reply-To",
                "value": "<tracked-root@example.test>",
            }
        ]
        message = {
            "id": graph_id,
            "conversationId": "conversation-late-alias",
            "subject": "Alias-independent source",
            "from": {
                "emailAddress": {
                    "address": "sender@example.test",
                    "name": "Sender",
                }
            },
            "receivedDateTime": "2026-08-03T12:00:00Z",
            "sentDateTime": "2026-08-03T12:00:00Z",
            "bodyPreview": "Please update the exact source.",
            "hasAttachments": False,
        }
        if internet_message_id is not None:
            message["internetMessageId"] = internet_message_id
            headers.append(
                {
                    "name": "Message-ID",
                    "value": internet_message_id,
                }
            )
        message["internetMessageHeaders"] = headers
        return message

    def test_alias_addition_and_omission_reuse_frozen_process_authority(self):
        cases = (
            ("graph_then_rfc", None, "<late-alias@example.test>"),
            ("rfc_then_graph", "<retained-alias@example.test>", None),
        )
        for label, first_internet_id, second_internet_id in cases:
            with self.subTest(label=label):
                fake = FakeFirestore()
                user_id = f"user-{label}"
                thread_id = f"thread-{label}"
                graph_id = f"graph-{label}"
                fake.data[f"users/{user_id}/threads/{thread_id}"] = {
                    "clientId": f"client-{label}",
                    "status": processing.THREAD_STATUS["active"],
                }
                coordinator = source_coordinator.SourceCoordinator(
                    fake,
                    uuid_factory=SequentialIds(),
                    now_factory=MutableClock(FROZEN_NOW),
                    retained_terminal_authority_loader=lambda *_args, **_kwargs: {
                        "kind": "ordinary",
                        "saga": None,
                        "settlement": None,
                        "exactSourceConfirmed": False,
                    },
                )
                first_message = self.message(
                    graph_id=graph_id,
                    internet_message_id=first_internet_id,
                )
                second_message = self.message(
                    graph_id=graph_id,
                    internet_message_id=second_internet_id,
                )
                classifier_inputs = []
                downstream_calls = []

                def classify(classification_input):
                    classifier_inputs.append(deepcopy(dict(classification_input)))
                    return (
                        {
                            "schemaVersion": 1,
                            "transitionCandidates": [],
                            "ordinaryObligations": [
                                {
                                    "type": "field_update",
                                    "field": "stage",
                                    "value": "warm",
                                }
                            ],
                        },
                        {
                            "schemaVersion": 1,
                            "evidenceKind": "model_capture",
                            "responseHash": "e" * 64,
                        },
                    )

                def consume(_authority, _snapshot, ledger):
                    downstream_calls.append(ledger["ledgerHash"])
                    entry = ledger["entries"][0]
                    return {
                        "state": "completed",
                        "completionRecords": {
                            entry["workKey"]: {
                                "schemaVersion": 1,
                                "evidenceKind": "work_completion",
                                "workKind": entry["kind"],
                                "resultHash": "f" * 64,
                            }
                        },
                    }

                full_message = mock.Mock()
                full_message.json.return_value = {
                    "body": {
                        "contentType": "Text",
                        "content": "Please update the exact source.",
                    },
                    "hasAttachments": False,
                }
                me_response = mock.Mock(status_code=200)
                me_response.json.return_value = {"mail": "owner@example.test"}

                with mock.patch.object(
                    processing,
                    "_fs",
                    fake,
                ), mock.patch.object(
                    processing,
                    "SourceCoordinator",
                    return_value=coordinator,
                ), mock.patch.object(
                    processing,
                    "exponential_backoff_request",
                    return_value=full_message,
                ), mock.patch.object(
                    processing.requests,
                    "get",
                    return_value=me_response,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_message_id",
                    return_value=thread_id,
                ), mock.patch.object(
                    processing,
                    "lookup_thread_by_conversation_id",
                    return_value=None,
                ), mock.patch.object(
                    processing,
                    "_classify_source_proposal",
                    side_effect=classify,
                ), mock.patch.object(
                    processing,
                    "_consume_source_authority",
                    side_effect=consume,
                ), mock.patch.object(
                    processing,
                    "_source_authority_consumer_available",
                    return_value=True,
                ):
                    first = processing.process_inbox_message(
                        user_id,
                        {"Authorization": "Bearer local-test"},
                        first_message,
                    )
                    history_path = (
                        f"users/{user_id}/threads/{thread_id}/messages/"
                        f"{first.authority.canonical_source_id}"
                    )
                    first_history = deepcopy(fake.data[history_path])
                    second = processing.process_inbox_message(
                        user_id,
                        {"Authorization": "Bearer local-test"},
                        second_message,
                    )

                self.assertEqual("settled", first.state)
                self.assertEqual("settled", second.state)
                self.assertEqual(
                    first.authority.canonical_source_id,
                    second.authority.canonical_source_id,
                )
                self.assertEqual(
                    first.settlement.settlement_hash,
                    second.settlement.settlement_hash,
                )
                self.assertEqual(1, len(classifier_inputs))
                self.assertEqual(1, len(downstream_calls))
                self.assertNotIn(
                    "graphMessageId",
                    classifier_inputs[0]["message"],
                )
                self.assertNotIn(
                    "internetMessageId",
                    classifier_inputs[0]["message"],
                )
                self.assertEqual(first_history, fake.data[history_path])
                identity = fake.data[
                    "users/"
                    f"{user_id}/sourceIdentities/"
                    f"{first.authority.canonical_source_id}"
                ]
                self.assertEqual(2, len(identity["verifiedAliases"]))
                self.assertEqual(2, second.settlement.alias_projection_count)
                self.assertEqual(
                    processing._source_alias_keys_for_message(
                        user_id,
                        second_message,
                    ),
                    second.source_alias_keys,
                )
                self.assertTrue(
                    processing._is_bound_exact_source_settlement(
                        second,
                        user_id=user_id,
                        thread_id=thread_id,
                        message=second_message,
                    )
                )

    def test_semantic_drift_does_not_attach_a_late_alias(self):
        fake = FakeFirestore()
        user_id = "user-late-alias-drift"
        thread_id = "thread-late-alias-drift"
        graph_id = "graph-late-alias-drift"
        internet_message_id = "<late-alias-drift@example.test>"
        fake.data[f"users/{user_id}/threads/{thread_id}"] = {
            "clientId": "client-late-alias-drift",
            "status": processing.THREAD_STATUS["active"],
        }
        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
            retained_terminal_authority_loader=lambda *_args, **_kwargs: {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
        )
        first_message = self.message(
            graph_id=graph_id,
            internet_message_id=None,
        )
        drifted_message = self.message(
            graph_id=graph_id,
            internet_message_id=internet_message_id,
        )
        drifted_message["subject"] = "Semantically different source"
        drifted_message["bodyPreview"] = "Changed exact-source body"
        stable_body = mock.Mock()
        stable_body.json.return_value = {
            "body": {
                "contentType": "Text",
                "content": "Please update the exact source.",
            },
            "hasAttachments": False,
        }
        drifted_body = mock.Mock()
        drifted_body.json.return_value = {
            "body": {
                "contentType": "Text",
                "content": "Changed exact-source body",
            },
            "hasAttachments": False,
        }
        me_response = mock.Mock(status_code=200)
        me_response.json.return_value = {"mail": "owner@example.test"}
        empty_proposal = (
            {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [],
            },
            {
                "schemaVersion": 1,
                "evidenceKind": "model_capture",
                "responseHash": "e" * 64,
            },
        )

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "SourceCoordinator",
            return_value=coordinator,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=[stable_body, drifted_body],
        ), mock.patch.object(
            processing.requests,
            "get",
            return_value=me_response,
        ), mock.patch.object(
            processing,
            "lookup_thread_by_message_id",
            return_value=thread_id,
        ), mock.patch.object(
            processing,
            "lookup_thread_by_conversation_id",
            return_value=None,
        ), mock.patch.object(
            processing,
            "_classify_source_proposal",
            return_value=empty_proposal,
        ) as classifier:
            first = processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer local-test"},
                first_message,
            )
            with self.assertRaises(source_coordinator.SourceCoordinatorAmbiguous):
                processing.process_inbox_message(
                    user_id,
                    {"Authorization": "Bearer local-test"},
                    drifted_message,
                )

        identity_path = (
            f"users/{user_id}/sourceIdentities/"
            f"{first.authority.canonical_source_id}"
        )
        late_alias = source_coordinator.normalize_source_alias(
            "internet_message_id",
            internet_message_id,
        )
        late_alias_key = source_coordinator.source_alias_key(
            user_id,
            late_alias,
        )
        self.assertEqual(1, len(fake.data[identity_path]["verifiedAliases"]))
        self.assertNotIn(
            f"users/{user_id}/sourceAliases/{late_alias_key}",
            fake.data,
        )
        self.assertEqual(1, classifier.call_count)

    def test_history_allows_alias_repair_but_rejects_semantic_drift(self):
        fake = FakeFirestore()
        base_record = {
            "direction": "inbound",
            "from": "sender@example.test",
            "headers": {
                "internetMessageId": None,
                "inReplyTo": "tracked-root@example.test",
                "references": [],
            },
            "body": {
                "contentType": "Text",
                "content": "Stable body",
            },
            "sourceMessage": {
                "graphMessageId": "graph-history",
                "subject": "Stable subject",
            },
        }
        repaired_record = deepcopy(base_record)
        repaired_record["headers"]["internetMessageId"] = (
            "<history@example.test>"
        )
        repaired_record["sourceMessage"]["internetMessageId"] = (
            "<history@example.test>"
        )
        drifted_record = deepcopy(repaired_record)
        drifted_record["body"]["content"] = "Changed body"

        with mock.patch.object(processing, "_fs", fake):
            first = processing._persist_strict_source_history_and_index(
                user_id="user-history",
                thread_id="thread-history",
                canonical_source_id="source-history",
                message_record=base_record,
            )
            repaired = processing._persist_strict_source_history_and_index(
                user_id="user-history",
                thread_id="thread-history",
                canonical_source_id="source-history",
                message_record=repaired_record,
            )
            with self.assertRaises(
                source_coordinator.SourceCoordinatorAmbiguous
            ):
                processing._persist_strict_source_history_and_index(
                    user_id="user-history",
                    thread_id="thread-history",
                    canonical_source_id="source-history",
                    message_record=drifted_record,
                )

        self.assertEqual(first, repaired)


class SourceCoordinatorScannerLegacyBridgeTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)

    def run_bridge_case(self, *, retained_kind="ordinary", marker_payload=None):
        fake = FakeFirestore()
        user_id = "user-scanner-legacy-bridge"
        thread_id = "thread-scanner-legacy-bridge"
        client_id = "client-scanner-legacy-bridge"
        graph_id = "graph-scanner-legacy-bridge"
        internet_id = "<scanner-legacy-bridge@example.test>"
        fake.data[f"users/{user_id}/threads/{thread_id}"] = {
            "clientId": client_id,
            "status": processing.THREAD_STATUS["active"],
        }
        if marker_payload is not None:
            fake.data[
                "users/"
                f"{user_id}/processedMessages/{processing.b64url_id(graph_id)}"
            ] = deepcopy(marker_payload)

        def retained_loader(*_args, **_kwargs):
            if retained_kind == "ordinary":
                return {
                    "kind": "ordinary",
                    "saga": None,
                    "settlement": None,
                    "exactSourceConfirmed": False,
                }
            return {
                "kind": retained_kind,
                "saga": {
                    "clientId": client_id,
                    "sourceMessageKey": internet_id,
                    "sourceGraphMessageId": graph_id,
                    "sourceInternetMessageId": internet_id,
                    "sagaKey": "scanner-legacy-bridge-saga",
                    "immutableHash": "c" * 64,
                    "phase": "classified",
                },
                "settlement": None,
                "exactSourceConfirmed": True,
            }

        coordinator = source_coordinator.SourceCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
            retained_terminal_authority_loader=retained_loader,
        )
        message = {
            "id": graph_id,
            "internetMessageId": internet_id,
            "conversationId": "conversation-scanner-legacy-bridge",
            "subject": "Retained source",
            "from": {
                "emailAddress": {
                    "address": "sender@example.test",
                    "name": "Sender",
                }
            },
            "receivedDateTime": "2026-08-03T12:00:00Z",
            "sentDateTime": "2026-08-03T12:00:00Z",
            "bodyPreview": "Retained source body",
            "hasAttachments": False,
            "internetMessageHeaders": [
                {
                    "name": "In-Reply-To",
                    "value": "<tracked-root@example.test>",
                }
            ],
        }
        full_message = mock.Mock()
        full_message.json.return_value = {
            "body": {
                "contentType": "Text",
                "content": "Retained source body",
            },
            "hasAttachments": False,
        }
        me_response = mock.Mock(status_code=200)
        me_response.json.return_value = {"mail": "owner@example.test"}

        with mock.patch.object(
            processing,
            "_fs",
            fake,
        ), mock.patch.object(
            processing,
            "SourceCoordinator",
            return_value=coordinator,
        ), mock.patch.object(
            processing,
            "exponential_backoff_request",
            return_value=full_message,
        ), mock.patch.object(
            processing.requests,
            "get",
            return_value=me_response,
        ), mock.patch.object(
            processing.requests,
            "post",
        ) as post, mock.patch.object(
            processing.requests,
            "patch",
        ) as patch_request, mock.patch.object(
            processing.requests,
            "put",
        ) as put, mock.patch.object(
            processing.requests,
            "delete",
        ) as delete, mock.patch.object(
            processing,
            "lookup_thread_by_message_id",
            return_value=thread_id,
        ), mock.patch.object(
            processing,
            "lookup_thread_by_conversation_id",
            return_value=None,
        ), mock.patch.object(
            processing,
            "_classify_source_proposal",
            return_value=(
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [
                        {
                            "type": "needs_user_input",
                            "reason": "legacy_bridge_test",
                        }
                    ],
                    "ordinaryObligations": [],
                },
                {
                    "schemaVersion": 1,
                    "evidenceKind": "model_capture",
                    "responseHash": "e" * 64,
                },
            ),
        ) as classifier, mock.patch.object(
            processing,
            "_consume_source_authority",
        ) as downstream, mock.patch.object(
            processing,
            "_source_authority_consumer_available",
            return_value=False,
        ):
            result = processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer local-test"},
                message,
            )

        classifier.assert_not_called()
        downstream.assert_not_called()
        post.assert_not_called()
        patch_request.assert_not_called()
        put.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(thread_id, result.thread_id)
        self.assertEqual(
            processing._source_alias_keys_for_message(user_id, message),
            result.source_alias_keys,
        )
        self.assertFalse(
            any(
                collection in path
                for path in fake.data
                for collection in (
                    "/sourceTransitionOwners/",
                    "/sourceWorkLedgers/",
                    "/inboundPendingAdmissions/",
                    "/sourceSettlements/",
                )
            )
        )
        return fake, result

    def test_normal_enforced_path_quarantines_retained_terminal_before_classifier(self):
        fake, result = self.run_bridge_case(retained_kind="active")

        self.assertEqual("legacy_terminal_authority_retained", result.state)
        classifications = [
            data
            for path, data in fake.data.items()
            if "/sourceClassifications/" in path
        ]
        self.assertEqual(1, len(classifications))
        self.assertEqual(
            "legacy_terminal_quarantined",
            classifications[0]["classificationState"],
        )

    def test_normal_enforced_path_blocks_legacy_markers_before_classifier(self):
        cases = (
            (
                "legacy_marker_only_ambiguous",
                {"processedAt": FROZEN_NOW},
            ),
            (
                "legacy_replay_claim_quarantined",
                {
                    "status": "operator_replay_in_progress",
                    "replayAttemptId": "scanner-legacy-replay-attempt",
                    "claimedAt": FROZEN_NOW,
                },
            ),
        )
        for expected_state, marker_payload in cases:
            with self.subTest(expected_state=expected_state):
                fake, result = self.run_bridge_case(
                    marker_payload=marker_payload,
                )

                self.assertEqual(expected_state, result.state)
                self.assertFalse(
                    any(
                        "/sourceClassifications/" in path
                        for path in fake.data
                    )
                )


class SourceCoordinatorAuthorityOrderTests(unittest.TestCase):
    def setUp(self):
        self.mode = mock.patch.dict(
            os.environ,
            {MODE_ENV: "enforced"},
            clear=False,
        )
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.consumer_available = mock.patch.object(
            processing,
            "_source_authority_consumer_available",
            return_value=True,
        )
        self.consumer_available.start()
        self.addCleanup(self.consumer_available.stop)

    def run_authority_order_case(
        self,
        *,
        candidate_type,
        deterministic=False,
        settle_and_retry=False,
        crash_after_settlement=False,
    ):
        timeline = []
        forbidden_events = []
        callback_count = 0
        release_attempts = 0
        fake = FakeFirestore()
        user_id = "user-order"
        thread_id = "thread-order"
        graph_id = f"graph-{candidate_type}"
        internet_id = f"<{candidate_type}@example.test>"
        thread_root_path = f"users/{user_id}/threads/{thread_id}"
        seeded_thread_root = {
            "clientId": "client-order",
            "status": processing.THREAD_STATUS["active"],
            "email": ["sender@example.test"],
            "rowNumber": 7,
        }
        fake.data[thread_root_path] = deepcopy(seeded_thread_root)
        client_root_path = f"users/{user_id}/clients/client-order"
        seeded_client_root = {
            "status": "active",
            "notificationCount": 0,
        }
        fake.data[client_root_path] = deepcopy(seeded_client_root)

        classification_input = {
            "schemaVersion": 1,
            "message": {
                "graphMessageId": graph_id,
                "internetMessageId": internet_id,
                "threadId": thread_id,
                "body": "Please handle this exact source.",
            },
        }
        classification_input_hash = source_coordinator.canonical_json_hash(
            classification_input
        )

        verifier = None
        if deterministic:
            self.assertEqual(
                "1d4fa0152f03ea202f6ac138d480ee800850f579e292a12550efe1eacffd2524",
                classification_input_hash,
                "deterministic authority fixture changed without rebinding its hash",
            )

            def verify_hard_optout(actual_input):
                exact_input = source_coordinator._thaw_json(actual_input)
                self.assertEqual(classification_input, exact_input)
                self.assertEqual(
                    classification_input_hash,
                    source_coordinator.canonical_json_hash(exact_input),
                )
                return {
                    "schemaVersion": 1,
                    "evidenceKind": "header_list_unsubscribe",
                    "evidenceHash": "d" * 64,
                }

            verifier = verify_hard_optout

        class RecordingCoordinator(source_coordinator.SourceCoordinator):
            def admit_or_repair_source_identity(self, **kwargs):
                result = super().admit_or_repair_source_identity(**kwargs)
                timeline.append("identity")
                return result

            def claim_source_classification(self, **kwargs):
                result = super().claim_source_classification(**kwargs)
                timeline.append("classification_claimed")
                return result

            def record_classification_request_started(self, **kwargs):
                result = super().record_classification_request_started(**kwargs)
                timeline.append("classification_request_started")
                return result

            def persist_deterministic_classification_snapshot(self, **kwargs):
                result = super().persist_deterministic_classification_snapshot(
                    **kwargs
                )
                if result is not None:
                    timeline.extend(["model_not_applicable", "snapshot_ready"])
                return result

            def persist_complete_classification_snapshot(self, **kwargs):
                result = super().persist_complete_classification_snapshot(**kwargs)
                timeline.append("snapshot_ready")
                return result

            def elect_transition_owner_from_snapshot(self, **kwargs):
                result = super().elect_transition_owner_from_snapshot(**kwargs)
                timeline.append("transition_decision")
                return result

            def create_or_verify_source_work_ledger(self, **kwargs):
                result = super().create_or_verify_source_work_ledger(**kwargs)
                timeline.append("source_work_ledger")
                return result

            def claim_or_block_thread_transition(self, **kwargs):
                result = super().claim_or_block_thread_transition(**kwargs)
                timeline.append("required_thread_head")
                return result

            def release_settled_generation_if_needed(self, **kwargs):
                nonlocal release_attempts
                release_attempts += 1
                if crash_after_settlement and release_attempts == 1:
                    raise source_coordinator.SourceCoordinatorRetryable(
                        "simulated crash after settlement before release"
                    )
                return super().release_settled_generation_if_needed(**kwargs)

        coordinator = RecordingCoordinator(
            fake,
            uuid_factory=SequentialIds(),
            now_factory=MutableClock(FROZEN_NOW),
            hard_optout_verifier=verifier,
            retained_terminal_authority_loader=lambda *_args, **_kwargs: {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
        )
        complete_proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {
                    "type": candidate_type,
                    "reason": f"{candidate_type}_test",
                }
            ],
            "ordinaryObligations": [],
        }

        def source_classifier(actual_input):
            nonlocal callback_count
            callback_count += 1
            classification_paths = [
                path
                for path in fake.data
                if "/sourceClassifications/" in path
            ]
            self.assertEqual(1, len(classification_paths))
            retained = fake.data[classification_paths[0]]
            self.assertEqual("request_started", retained["classificationState"])
            self.assertEqual("started", retained["modelRequestState"])
            self.assertEqual(
                source_coordinator.canonical_json_hash(actual_input),
                retained["classificationInputHash"],
            )
            self.assertTrue(retained["modelRequestKey"])
            timeline.append("classifier")
            return (
                deepcopy(complete_proposal),
                {
                    "schemaVersion": 1,
                    "evidenceKind": "model_capture",
                    "responseHash": "e" * 64,
                },
            )

        def downstream_consumer(authority, *_args, **_kwargs):
            authority_type = getattr(
                processing,
                "SourceProcessingAuthority",
                None,
            )
            self.assertIsNotNone(authority_type)
            self.assertIsInstance(authority, authority_type)
            self.assertEqual(
                seeded_thread_root,
                fake.data[thread_root_path],
                "legacy thread root changed before exact-source authority barrier",
            )
            self.assertEqual(
                seeded_client_root,
                fake.data[client_root_path],
                "legacy client root changed before exact-source authority barrier",
            )
            timeline.append("downstream_consumer")
            return {
                "state": (
                    "completed"
                    if settle_and_retry or crash_after_settlement
                    else "blocked"
                ),
            }

        def forbidden(label):
            def record(*_args, **_kwargs):
                forbidden_events.append(label)
                if label == "sheet_input_and_format":
                    return (
                        "client-order",
                        "sheet-order",
                        ["Property Address", "Email"],
                        7,
                        ["100 Test St", "sender@example.test"],
                        None,
                        [],
                    )
                if label in {"asset_upload", "linked_asset_upload"}:
                    return []
                if label == "legacy_classifier":
                    return {
                        "updates": [],
                        "events": [],
                        "response_email": None,
                        "skip_response": True,
                    }
                return None

            return record

        msg = {
            "id": graph_id,
            "internetMessageId": internet_id,
            "conversationId": "conversation-order",
            "subject": "Authority order",
            "from": {
                "emailAddress": {
                    "address": "sender@example.test",
                    "name": "Sender",
                }
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": "owner@example.test",
                    }
                }
            ],
            "receivedDateTime": "2026-08-03T12:00:00Z",
            "sentDateTime": "2026-08-03T12:00:00Z",
            "bodyPreview": "Please handle this exact source.",
            "hasAttachments": False,
            "internetMessageHeaders": [
                {
                    "name": "In-Reply-To",
                    "value": "<tracked-root@example.test>",
                }
            ],
        }
        full_message = mock.Mock()
        full_message.json.return_value = {
            "body": {
                "contentType": "Text",
                "content": "Please handle this exact source.",
            },
            "hasAttachments": False,
        }
        me_response = mock.Mock(status_code=200)
        me_response.json.return_value = {"mail": "owner@example.test"}
        allowed_decision = mock.Mock()

        patchers = [
            mock.patch.object(processing, "_fs", fake),
            mock.patch.object(messaging, "_fs", fake),
            mock.patch.object(
                processing,
                "SourceCoordinator",
                return_value=coordinator,
                create=True,
            ),
            mock.patch.object(
                processing,
                "_read_source_classification_input",
                return_value=deepcopy(classification_input),
                create=True,
            ),
            mock.patch.object(
                processing,
                "_classify_source_proposal",
                side_effect=source_classifier,
                create=True,
            ),
            mock.patch.object(
                processing,
                "_consume_source_authority",
                side_effect=downstream_consumer,
                create=True,
            ),
            mock.patch.object(
                processing,
                "exponential_backoff_request",
                return_value=full_message,
            ),
            mock.patch.object(
                processing.requests,
                "get",
                return_value=me_response,
            ),
            mock.patch.object(
                processing.requests,
                "post",
                side_effect=forbidden("requests_post"),
            ),
            mock.patch.object(
                processing.requests,
                "patch",
                side_effect=forbidden("requests_patch"),
            ),
            mock.patch.object(
                processing.requests,
                "put",
                side_effect=forbidden("requests_put"),
            ),
            mock.patch.object(
                processing.requests,
                "delete",
                side_effect=forbidden("requests_delete"),
            ),
            mock.patch.object(
                processing,
                "lookup_thread_by_message_id",
                return_value=thread_id,
            ),
            mock.patch.object(
                processing,
                "lookup_thread_by_conversation_id",
                return_value=None,
            ),
            mock.patch.object(
                processing,
                "get_client_automation_decision",
                return_value=allowed_decision,
            ),
            mock.patch.object(
                processing,
                "classify_campaign_suppression",
                return_value=None,
            ),
            mock.patch.object(
                processing,
                "_late_reply_after_followup_exhaustion_patch",
                return_value=None,
            ),
            mock.patch.object(
                processing,
                "_active_replacement_context",
                return_value=None,
            ),
            mock.patch.object(
                processing,
                "_should_skip_processing_for_terminal_thread",
                return_value=False,
            ),
            mock.patch.object(
                processing,
                "_persist_inbound_message_history",
                side_effect=forbidden("thread_timestamp"),
            ),
            mock.patch.object(
                processing,
                "save_thread_root",
                side_effect=forbidden("thread_root_write"),
            ),
            mock.patch.object(
                processing,
                "save_message",
                side_effect=forbidden("legacy_message_write"),
            ),
            mock.patch.object(
                processing,
                "index_message_id",
                side_effect=forbidden("legacy_message_index_write"),
            ),
            mock.patch.object(
                processing,
                "index_conversation_id",
                side_effect=forbidden("legacy_conversation_index_write"),
            ),
            mock.patch.object(
                processing,
                "update_thread_status",
                side_effect=forbidden("thread_status_write"),
            ),
            mock.patch.object(
                processing,
                "_fenced_terminal_thread_update",
                side_effect=forbidden("terminal_thread_write"),
            ),
            mock.patch.object(
                processing,
                "_maybe_mark_client_completed",
                side_effect=forbidden("client_status_write"),
            ),
            mock.patch.object(
                processing,
                "mark_processed",
                side_effect=forbidden("processed_marker_write"),
            ),
            mock.patch.object(
                processing,
                "set_last_scan_iso",
                side_effect=forbidden("scan_cursor_write"),
            ),
            mock.patch.object(
                processing,
                "mark_event_handled",
                side_effect=forbidden("handled_event_write"),
            ),
            mock.patch.object(
                processing,
                "write_notification",
                side_effect=forbidden("notification_write"),
            ),
            mock.patch.object(
                processing,
                "add_client_notifications",
                side_effect=forbidden("notification_counter_write"),
            ),
            mock.patch.object(
                processing,
                "delete_notification_and_decrement_counters",
                side_effect=forbidden("notification_counter_delete"),
            ),
            mock.patch(
                "email_automation.followup.cancel_followup_on_response",
                side_effect=forbidden("followup_state"),
            ),
            mock.patch.object(
                processing,
                "_stage_terminal_saga",
                side_effect=forbidden("terminal_saga_stage"),
            ),
            mock.patch.object(
                processing,
                "_resume_exact_terminal_saga",
                side_effect=forbidden("terminal_saga_resume"),
            ),
            mock.patch.object(
                processing,
                "_persist_terminal_settlement_projection",
                side_effect=forbidden("terminal_saga_settlement"),
            ),
            mock.patch.object(
                processing,
                "_settle_terminal_notification_obligation",
                side_effect=forbidden("terminal_saga_notification"),
            ),
            mock.patch.object(
                processing,
                "_settle_terminal_reply_obligation",
                side_effect=forbidden("terminal_saga_reply"),
            ),
            mock.patch.object(
                processing,
                "_ensure_terminal_reply_queue",
                side_effect=forbidden("terminal_reply_queue"),
            ),
            mock.patch.object(
                processing,
                "queue_pending_response",
                side_effect=forbidden("pending_response_queue"),
            ),
            mock.patch.object(
                processing,
                "record_sent_unindexed_response",
                side_effect=forbidden("sent_unindexed_response_write"),
            ),
            mock.patch.object(
                processing,
                "send_reply_in_thread",
                side_effect=forbidden("reply_send"),
            ),
            mock.patch.object(
                processing,
                "_store_contact_optout",
                side_effect=forbidden("contact_optout_write"),
            ),
            mock.patch.object(
                processing,
                "apply_proposal_to_sheet",
                side_effect=forbidden("sheet_write"),
            ),
            mock.patch.object(processing, "dump_thread_from_firestore"),
            mock.patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                side_effect=forbidden("sheet_input_and_format"),
            ),
            mock.patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": "sender@example.test",
                    "contact_name": "Sender",
                    "original_email": "sender@example.test",
                    "source": "test",
                },
            ),
            mock.patch.object(
                processing,
                "fetch_and_process_pdfs",
                side_effect=forbidden("asset_upload"),
            ),
            mock.patch.object(
                processing,
                "fetch_and_process_linked_assets",
                side_effect=forbidden("linked_asset_upload"),
            ),
            mock.patch.object(
                processing,
                "write_message_order_test",
                side_effect=forbidden("message_order_write"),
            ),
            mock.patch.object(
                processing,
                "propose_sheet_updates",
                side_effect=forbidden("legacy_classifier"),
            ),
            mock.patch.object(
                processing,
                "_sheets_client",
                return_value=mock.Mock(),
            ),
            mock.patch.object(
                processing,
                "check_missing_required_fields",
                return_value=[],
            ),
            mock.patch.object(processing.time, "sleep"),
        ]
        with ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            if crash_after_settlement:
                with self.assertRaises(
                    source_coordinator.SourceCoordinatorRetryable
                ):
                    processing.process_inbox_message(
                        user_id,
                        {"Authorization": "Bearer local-test"},
                        msg,
                    )
            else:
                processing.process_inbox_message(
                    user_id,
                    {"Authorization": "Bearer local-test"},
                    msg,
                )
            if settle_and_retry or crash_after_settlement:
                processing.process_inbox_message(
                    user_id,
                    {"Authorization": "Bearer local-test"},
                    msg,
                )

        expected = [
            "identity",
            "classification_claimed",
        ]
        if deterministic:
            expected.extend(["model_not_applicable", "snapshot_ready"])
        else:
            expected.extend(
                [
                    "classification_request_started",
                    "classifier",
                    "snapshot_ready",
                ]
            )
        expected.extend(
            [
                "transition_decision",
                "source_work_ledger",
                "required_thread_head",
                "downstream_consumer",
            ]
        )
        if settle_and_retry or crash_after_settlement:
            expected.extend(
                [
                    "identity",
                    "transition_decision",
                    "source_work_ledger",
                ]
            )
        self.assertEqual(expected, timeline)
        self.assertEqual([], forbidden_events)
        self.assertEqual(0 if deterministic else 1, callback_count)
        if settle_and_retry or crash_after_settlement:
            head = fake.data[
                f"users/{user_id}/threadTransitionHeads/{thread_id}"
            ]
            self.assertEqual("clear", head["activeState"])
            admissions = [
                data
                for path, data in fake.data.items()
                if "/inboundPendingAdmissions/" in path
            ]
            self.assertEqual(1, len(admissions))
            self.assertEqual("settled", admissions[0]["admissionState"])
        if deterministic:
            classification_records = [
                data
                for path, data in fake.data.items()
                if "/sourceClassifications/" in path
            ]
            self.assertEqual(1, len(classification_records))
            self.assertEqual(
                classification_input_hash,
                classification_records[0]["classificationInputHash"],
            )

    def test_model_terminal_authority_precedes_effects(self):
        self.run_authority_order_case(candidate_type="property_unavailable")

    def test_model_human_authority_precedes_effects(self):
        self.run_authority_order_case(candidate_type="call_requested")

    def test_deterministic_hard_optout_skips_classifier_before_effects(self):
        self.run_authority_order_case(
            candidate_type="contact_optout",
            deterministic=True,
        )

    def test_settled_source_retry_reuses_snapshot_without_consumer(self):
        self.run_authority_order_case(
            candidate_type="property_unavailable",
            settle_and_retry=True,
        )

    def test_retry_releases_generation_after_post_settlement_crash(self):
        self.run_authority_order_case(
            candidate_type="property_unavailable",
            crash_after_settlement=True,
        )


if __name__ == "__main__":
    unittest.main()
