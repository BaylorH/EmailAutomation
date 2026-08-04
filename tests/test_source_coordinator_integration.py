import os
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest import mock

from tests.source_coordinator_fakes import FakeFirestore, MutableClock


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("AZURE_API_APP_ID", "test-client-id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-api-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-api-key")
with mock.patch("google.cloud.firestore.Client", return_value=mock.Mock()):
    import main
    import scheduler_runner
    from email_automation import messaging, source_coordinator


MODE_ENV = source_coordinator.SOURCE_COORDINATOR_MODE_ENV
FROZEN_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


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


def build_ready_ordinary_source(*, graph_id="graph-enforced"):
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
                "body": "Please update the stage.",
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
                        "value": "warm",
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
    coordinator.admit_pending_inbound(
        user_id="user-enforced",
        canonical_source_id=identity.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding={
            "schemaVersion": 1,
            "historyKey": "history-enforced",
        },
        index_binding={
            "schemaVersion": 1,
            "indexKey": "index-enforced",
        },
    )
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
    return fake, coordinator, identity, ledger


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


if __name__ == "__main__":
    unittest.main()
