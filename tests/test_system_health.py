import os
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import send_permits, source_coordinator, system_health
from tests.source_coordinator_fakes import (
    FakeFirestore as CoordinatorFakeFirestore,
    MutableClock,
)


B1_COLLECTIONS = (
    "sourceIdentities",
    "sourceAliases",
    "sourceClassifications",
    "sourceTransitionOwners",
    "sourceWorkLedgers",
    "sourceDeferredWork",
    "inboundPendingAdmissions",
    "threadTransitionHeads",
    "blockedSources",
    "sourceSettlements",
)
B1_HEALTH_KEYS = (
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
)
B1_SCAN_COLLECTIONS = (*B1_COLLECTIONS, "processedMessages")
FROZEN_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
CLASSIFICATION_INPUT = {
    "schemaVersion": 1,
    "message": {"id": "health-source"},
}
TRANSITION_PROPOSAL = {
    "schemaVersion": 1,
    "transitionCandidates": [
        {"type": "needs_user_input", "reason": "health_review"},
    ],
    "ordinaryObligations": [],
}
ORDINARY_PROPOSAL = {
    "schemaVersion": 1,
    "transitionCandidates": [],
    "ordinaryObligations": [
        {"type": "field_update", "field": "stage", "value": "warm"},
    ],
}
MODEL_EVIDENCE = {
    "schemaVersion": 1,
    "evidenceKind": "model_capture",
    "responseHash": "a" * 64,
}


class _SequentialIds:
    def __init__(self, prefix="health-source"):
        self.value = 0
        self.prefix = prefix

    def __call__(self):
        self.value += 1
        return f"{self.prefix}-{self.value:04d}"


def _healthy_queue_counts():
    return {
        "outbox": 0,
        "deadLetterQueue": 0,
        "pendingResponses": 0,
        "processingFailures": 0,
        "terminalGraphSendReviews": 0,
        "threads": 0,
    }


def _health_documents_from_store(store):
    documents = {name: [] for name in B1_SCAN_COLLECTIONS}
    prefix = "users/uid-1/"
    for path, data in store.data.items():
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix):].split("/")
        if len(remainder) != 2 or remainder[0] not in documents:
            continue
        documents[remainder[0]].append(
            FakeHealthDoc(data, doc_id=remainder[1])
        )
    return documents


def _coordinator_health_documents():
    store = CoordinatorFakeFirestore()
    clock = MutableClock(FROZEN_NOW)
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=clock,
    )

    def admit(graph_id):
        return coordinator.admit_or_repair_source_identity(
            user_id="uid-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=f"thread-{graph_id}",
        )

    claimed = admit("claimed")
    coordinator.claim_source_classification(
        user_id="uid-1",
        canonical_source_id=claimed.canonical_source_id,
        lease_seconds=60,
    )

    started = admit("started")
    started_claim = coordinator.claim_source_classification(
        user_id="uid-1",
        canonical_source_id=started.canonical_source_id,
        lease_seconds=60,
    )
    coordinator.record_classification_request_started(
        user_id="uid-1",
        canonical_source_id=started.canonical_source_id,
        classification_epoch=started_claim.classification_epoch,
        classification_claim_id=started_claim.classification_claim_id,
        model_request_key="health-model-request",
        classification_input=deepcopy(CLASSIFICATION_INPUT),
    )

    ambiguous = admit("ambiguous")
    with unittest.TestCase().assertRaises(
        source_coordinator.ClassificationRequestAmbiguous
    ):
        coordinator.classify_source_once(
            user_id="uid-1",
            canonical_source_id=ambiguous.canonical_source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (_ for _ in ()).throw(
                RuntimeError("classifier outcome unknown")
            ),
        )

    quarantined = admit("terminal")
    coordinator.claim_source_classification(
        user_id="uid-1",
        canonical_source_id=quarantined.canonical_source_id,
        lease_seconds=60,
    )
    quarantine_path = (
        "users/uid-1/sourceClassifications/"
        f"{quarantined.canonical_source_id}"
    )
    quarantine = store.data[quarantine_path]
    quarantine.update(
        {
            "classificationState": "legacy_terminal_quarantined",
            "classificationEpoch": 0,
            "classificationClaimId": None,
            "leaseExpiresAt": None,
            "modelRequestState": "not_applicable",
            "retainedTerminalKind": "active",
            "retainedTerminalImmutableHash": "1" * 64,
            "retainedTerminalRecordHash": "2" * 64,
            "retainedTerminalBindingHash": "3" * 64,
        }
    )

    pending = admit("pending")
    coordinator.classify_source_once(
        user_id="uid-1",
        canonical_source_id=pending.canonical_source_id,
        lease_seconds=60,
        classification_input=deepcopy(CLASSIFICATION_INPUT),
        classifier=lambda: (
            deepcopy(ORDINARY_PROPOSAL),
            deepcopy(MODEL_EVIDENCE),
        ),
    )
    coordinator.elect_transition_owner_from_snapshot(
        user_id="uid-1",
        canonical_source_id=pending.canonical_source_id,
    )
    coordinator.create_or_verify_source_work_ledger(
        user_id="uid-1",
        canonical_source_id=pending.canonical_source_id,
    )
    coordinator.admit_pending_inbound(
        user_id="uid-1",
        canonical_source_id=pending.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding={
            "schemaVersion": 1,
            "canonicalSourceId": pending.canonical_source_id,
            "threadId": "thread-pending",
            "historyDocumentId": pending.canonical_source_id,
            "historyHash": "4" * 64,
        },
        index_binding={
            "schemaVersion": 1,
            "canonicalSourceId": pending.canonical_source_id,
            "threadId": "thread-pending",
            "identityDocumentId": pending.canonical_source_id,
        },
    )

    claimed_alias_path = next(
        path
        for path, data in store.data.items()
        if "/sourceAliases/" in path
        and data.get("canonicalSourceId") == claimed.canonical_source_id
    )
    store.data[claimed_alias_path]["canonicalSourceId"] = (
        started.canonical_source_id
    )
    store.data["users/uid-1/processedMessages/legacy-marker"] = {
        "processedAt": FROZEN_NOW,
    }
    for suffix in ("a", "b"):
        store.data[f"users/uid-1/processedMessages/replay-{suffix}"] = {
            "status": "operator_replay_in_progress",
            "replayAttemptId": "replay-attempt-1",
            "claimedAt": FROZEN_NOW,
        }

    return _health_documents_from_store(store)


def _authority_stage_health_documents(stage):
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    identity = coordinator.admit_or_repair_source_identity(
        user_id="uid-1",
        hydrated_message={"id": f"authority-stage-{stage}"},
        evidence_kind="graph_hydration",
        thread_id=f"authority-stage-thread-{stage}",
    )
    if stage == "identity":
        return _health_documents_from_store(store)

    coordinator.classify_source_once(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        lease_seconds=60,
        classification_input=deepcopy(CLASSIFICATION_INPUT),
        classifier=lambda: (
            deepcopy(ORDINARY_PROPOSAL),
            deepcopy(MODEL_EVIDENCE),
        ),
    )
    if stage == "snapshot":
        return _health_documents_from_store(store)

    coordinator.elect_transition_owner_from_snapshot(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    if stage == "owner":
        return _health_documents_from_store(store)

    if stage != "ledger":
        raise AssertionError(f"unsupported authority stage: {stage}")
    coordinator.create_or_verify_source_work_ledger(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    return _health_documents_from_store(store)


def _active_head_health_documents():
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    identity = coordinator.admit_or_repair_source_identity(
        user_id="uid-1",
        hydrated_message={"id": "active-head-source"},
        evidence_kind="graph_hydration",
        thread_id="active-head-thread",
    )
    coordinator.classify_source_once(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        lease_seconds=60,
        classification_input=deepcopy(CLASSIFICATION_INPUT),
        classifier=lambda: (
            deepcopy(TRANSITION_PROPOSAL),
            deepcopy(MODEL_EVIDENCE),
        ),
    )
    coordinator.elect_transition_owner_from_snapshot(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    coordinator.create_or_verify_source_work_ledger(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    coordinator.admit_pending_inbound(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "active-head-thread",
            "historyDocumentId": identity.canonical_source_id,
            "historyHash": "5" * 64,
        },
        index_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "active-head-thread",
            "identityDocumentId": identity.canonical_source_id,
        },
    )
    coordinator.claim_or_block_thread_transition(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    return _health_documents_from_store(store)


def _blocked_health_documents():
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )

    def prepare(graph_id, history_digit):
        identity = coordinator.admit_or_repair_source_identity(
            user_id="uid-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id="blocked-health-thread",
        )
        coordinator.classify_source_once(
            user_id="uid-1",
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                deepcopy(TRANSITION_PROPOSAL),
                deepcopy(MODEL_EVIDENCE),
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="uid-1",
            canonical_source_id=identity.canonical_source_id,
        )
        coordinator.create_or_verify_source_work_ledger(
            user_id="uid-1",
            canonical_source_id=identity.canonical_source_id,
        )
        coordinator.admit_pending_inbound(
            user_id="uid-1",
            canonical_source_id=identity.canonical_source_id,
            received_at=FROZEN_NOW,
            sent_at=FROZEN_NOW,
            saved_history_binding={
                "schemaVersion": 1,
                "canonicalSourceId": identity.canonical_source_id,
                "threadId": "blocked-health-thread",
                "historyDocumentId": identity.canonical_source_id,
                "historyHash": history_digit * 64,
            },
            index_binding={
                "schemaVersion": 1,
                "canonicalSourceId": identity.canonical_source_id,
                "threadId": "blocked-health-thread",
                "identityDocumentId": identity.canonical_source_id,
            },
        )
        return identity.canonical_source_id

    first = prepare("blocked-health-first", "6")
    second = prepare("blocked-health-second", "7")
    coordinator.claim_or_block_thread_transition(
        user_id="uid-1",
        canonical_source_id=first,
    )
    result = coordinator.claim_or_block_thread_transition(
        user_id="uid-1",
        canonical_source_id=second,
    )
    if result.disposition != "blocked":
        raise AssertionError("blocked health fixture did not create a block")
    return _health_documents_from_store(store)


def _settled_health_documents():
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    identity = coordinator.admit_or_repair_source_identity(
        user_id="uid-1",
        hydrated_message={"id": "settled-health-source"},
        evidence_kind="graph_hydration",
        thread_id="settled-health-thread",
    )
    coordinator.classify_source_once(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        lease_seconds=60,
        classification_input=deepcopy(CLASSIFICATION_INPUT),
        classifier=lambda: (
            deepcopy(ORDINARY_PROPOSAL),
            deepcopy(MODEL_EVIDENCE),
        ),
    )
    coordinator.elect_transition_owner_from_snapshot(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    ledger = coordinator.create_or_verify_source_work_ledger(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    coordinator.admit_pending_inbound(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "settled-health-thread",
            "historyDocumentId": identity.canonical_source_id,
            "historyHash": "8" * 64,
        },
        index_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "settled-health-thread",
            "identityDocumentId": identity.canonical_source_id,
        },
    )
    entry = ledger["entries"][0]
    work_arguments = {
        "user_id": "uid-1",
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
            "resultHash": "9" * 64,
        },
    )
    coordinator.settle_source_markers_if_ready(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        ledger_hash=ledger["ledgerHash"],
    )
    return _health_documents_from_store(store)


def _settled_owned_health_documents(*, release=False, prior_clear_cycles=0):
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    thread_id = "settled-owned-health-thread"

    def settle_owned_source(graph_id, history_digit, timestamp_offset):
        identity = coordinator.admit_or_repair_source_identity(
            user_id="uid-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        source_id = identity.canonical_source_id
        coordinator.classify_source_once(
            user_id="uid-1",
            canonical_source_id=source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                deepcopy(TRANSITION_PROPOSAL),
                deepcopy(MODEL_EVIDENCE),
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        timestamp = FROZEN_NOW.replace(microsecond=timestamp_offset)
        coordinator.admit_pending_inbound(
            user_id="uid-1",
            canonical_source_id=source_id,
            received_at=timestamp,
            sent_at=timestamp,
            saved_history_binding={
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "historyDocumentId": source_id,
                "historyHash": history_digit * 64,
            },
            index_binding={
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "identityDocumentId": source_id,
            },
        )
        coordinator.claim_or_block_thread_transition(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        for entry in ledger["entries"]:
            coordinator.delegate_source_work_entry(
                user_id="uid-1",
                canonical_source_id=source_id,
                ledger_hash=ledger["ledgerHash"],
                work_key=entry["workKey"],
                payload_hash=entry["payloadHash"],
            )
        coordinator.settle_source_markers_if_ready(
            user_id="uid-1",
            canonical_source_id=source_id,
            ledger_hash=ledger["ledgerHash"],
        )
        return source_id

    history_digits = "abcdef0123456789"
    for index in range(prior_clear_cycles):
        prior_source_id = settle_owned_source(
            f"settled-owned-health-prior-{index}",
            history_digits[index],
            index,
        )
        coordinator.release_generation_and_wake_oldest(
            user_id="uid-1",
            thread_id=thread_id,
            canonical_source_id=prior_source_id,
        )

    source_id = settle_owned_source(
        "settled-owned-health-source",
        history_digits[prior_clear_cycles],
        prior_clear_cycles,
    )
    if release:
        coordinator.release_generation_and_wake_oldest(
            user_id="uid-1",
            thread_id=thread_id,
            canonical_source_id=source_id,
        )
    return _health_documents_from_store(store)


def _consumed_wake_health_fixture(
    *,
    thread_id="consumed-wake-health-thread",
    fixture_label="consumed-wake-health",
):
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(fixture_label),
        now_factory=MutableClock(FROZEN_NOW),
    )

    def prepare(graph_id, history_digit):
        identity = coordinator.admit_or_repair_source_identity(
            user_id="uid-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        source_id = identity.canonical_source_id
        coordinator.classify_source_once(
            user_id="uid-1",
            canonical_source_id=source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                deepcopy(TRANSITION_PROPOSAL),
                deepcopy(MODEL_EVIDENCE),
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        admission_arguments = {
            "user_id": "uid-1",
            "canonical_source_id": source_id,
            "received_at": FROZEN_NOW,
            "sent_at": FROZEN_NOW,
            "saved_history_binding": {
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "historyDocumentId": source_id,
                "historyHash": history_digit * 64,
            },
            "index_binding": {
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "identityDocumentId": source_id,
            },
        }
        return source_id, ledger, admission_arguments

    def settle(source_id, ledger):
        for entry in ledger["entries"]:
            coordinator.delegate_source_work_entry(
                user_id="uid-1",
                canonical_source_id=source_id,
                ledger_hash=ledger["ledgerHash"],
                work_key=entry["workKey"],
                payload_hash=entry["payloadHash"],
            )
        coordinator.settle_source_markers_if_ready(
            user_id="uid-1",
            canonical_source_id=source_id,
            ledger_hash=ledger["ledgerHash"],
        )

    first_id, first_ledger, first_admission = prepare(
        f"{fixture_label}-first",
        "d",
    )
    coordinator.admit_pending_inbound(**first_admission)
    coordinator.claim_or_block_thread_transition(
        user_id="uid-1",
        canonical_source_id=first_id,
    )

    second_id, second_ledger, second_admission = prepare(
        f"{fixture_label}-second",
        "e",
    )
    second_admission["received_at"] = FROZEN_NOW.replace(microsecond=1)
    second_admission["sent_at"] = FROZEN_NOW.replace(microsecond=1)
    coordinator.enqueue_blocked_source(**second_admission)
    settle(first_id, first_ledger)
    released = coordinator.release_generation_and_wake_oldest(
        user_id="uid-1",
        thread_id=thread_id,
        canonical_source_id=first_id,
    )
    return {
        "store": store,
        "coordinator": coordinator,
        "thread_id": thread_id,
        "first_id": first_id,
        "second_id": second_id,
        "second_ledger": second_ledger,
        "released": released,
        "prepare": prepare,
        "settle": settle,
        "settle_second": lambda: settle(second_id, second_ledger),
    }


def _three_generation_consumed_wake_health_fixture():
    fixture = _consumed_wake_health_fixture()
    coordinator = fixture["coordinator"]
    coordinator.claim_wake_and_rebind_generation(
        user_id="uid-1",
        thread_id=fixture["thread_id"],
        canonical_source_id=fixture["second_id"],
        wake_token=fixture["released"].wake_token,
        wake_claim_id="health-second-generation-claim",
    )
    third_id, third_ledger, third_admission = fixture["prepare"](
        "consumed-wake-health-third",
        "f",
    )
    third_admission["received_at"] = FROZEN_NOW.replace(microsecond=2)
    third_admission["sent_at"] = FROZEN_NOW.replace(microsecond=2)
    coordinator.enqueue_blocked_source(**third_admission)
    fixture["settle_second"]()
    released = coordinator.release_generation_and_wake_oldest(
        user_id="uid-1",
        thread_id=fixture["thread_id"],
        canonical_source_id=fixture["second_id"],
    )
    coordinator.claim_wake_and_rebind_generation(
        user_id="uid-1",
        thread_id=fixture["thread_id"],
        canonical_source_id=third_id,
        wake_token=released.wake_token,
        wake_claim_id="health-third-generation-claim",
    )
    fixture.update(
        {
            "third_id": third_id,
            "third_ledger": third_ledger,
            "settle_third": lambda: fixture["settle"](
                third_id,
                third_ledger,
            ),
        }
    )
    return fixture


def _direct_claim_clear_cycle_consumed_wake_health_fixture():
    thread_id = "direct-clear-cycle-health-thread"
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds("direct-clear-cycle-health"),
        now_factory=MutableClock(FROZEN_NOW),
    )

    def prepare(graph_id, history_digit, timestamp_offset):
        identity = coordinator.admit_or_repair_source_identity(
            user_id="uid-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        source_id = identity.canonical_source_id
        coordinator.classify_source_once(
            user_id="uid-1",
            canonical_source_id=source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                deepcopy(TRANSITION_PROPOSAL),
                deepcopy(MODEL_EVIDENCE),
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        timestamp = FROZEN_NOW.replace(microsecond=timestamp_offset)
        admission_arguments = {
            "user_id": "uid-1",
            "canonical_source_id": source_id,
            "received_at": timestamp,
            "sent_at": timestamp,
            "saved_history_binding": {
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "historyDocumentId": source_id,
                "historyHash": history_digit * 64,
            },
            "index_binding": {
                "schemaVersion": 1,
                "canonicalSourceId": source_id,
                "threadId": thread_id,
                "identityDocumentId": source_id,
            },
        }
        return source_id, ledger, admission_arguments

    def settle(source_id, ledger):
        for entry in ledger["entries"]:
            coordinator.delegate_source_work_entry(
                user_id="uid-1",
                canonical_source_id=source_id,
                ledger_hash=ledger["ledgerHash"],
                work_key=entry["workKey"],
                payload_hash=entry["payloadHash"],
            )
        coordinator.settle_source_markers_if_ready(
            user_id="uid-1",
            canonical_source_id=source_id,
            ledger_hash=ledger["ledgerHash"],
        )

    def direct_claim_then_clear(graph_id, history_digit, timestamp_offset):
        source_id, ledger, admission = prepare(
            graph_id,
            history_digit,
            timestamp_offset,
        )
        coordinator.admit_pending_inbound(**admission)
        coordinator.claim_or_block_thread_transition(
            user_id="uid-1",
            canonical_source_id=source_id,
        )
        settle(source_id, ledger)
        coordinator.release_generation_and_wake_oldest(
            user_id="uid-1",
            thread_id=thread_id,
            canonical_source_id=source_id,
        )
        return source_id

    first_id = direct_claim_then_clear(
        "direct-clear-cycle-first",
        "a",
        0,
    )
    direct_claim_then_clear(
        "direct-clear-cycle-second",
        "b",
        1,
    )
    third_id, third_ledger, third_admission = prepare(
        "direct-clear-cycle-third",
        "c",
        2,
    )
    coordinator.admit_pending_inbound(**third_admission)
    coordinator.claim_or_block_thread_transition(
        user_id="uid-1",
        canonical_source_id=third_id,
    )

    fourth_id, fourth_ledger, fourth_admission = prepare(
        "direct-clear-cycle-fourth",
        "d",
        3,
    )
    coordinator.enqueue_blocked_source(**fourth_admission)
    settle(third_id, third_ledger)
    released = coordinator.release_generation_and_wake_oldest(
        user_id="uid-1",
        thread_id=thread_id,
        canonical_source_id=third_id,
    )
    coordinator.claim_wake_and_rebind_generation(
        user_id="uid-1",
        thread_id=thread_id,
        canonical_source_id=fourth_id,
        wake_token=released.wake_token,
        wake_claim_id="direct-clear-cycle-fourth-claim",
    )
    return {
        "store": store,
        "coordinator": coordinator,
        "thread_id": thread_id,
        "first_id": first_id,
        "fourth_id": fourth_id,
        "settle_fourth": lambda: settle(fourth_id, fourth_ledger),
    }


def _late_alias_settled_health_documents(*, repair):
    store = CoordinatorFakeFirestore()
    coordinator = source_coordinator.SourceCoordinator(
        store,
        uuid_factory=_SequentialIds(),
        now_factory=MutableClock(FROZEN_NOW),
    )
    identity = coordinator.admit_or_repair_source_identity(
        user_id="uid-1",
        hydrated_message={"id": "late-alias-health-source"},
        evidence_kind="graph_hydration",
        thread_id="late-alias-health-thread",
    )
    coordinator.classify_source_once(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        lease_seconds=60,
        classification_input=deepcopy(CLASSIFICATION_INPUT),
        classifier=lambda: (
            deepcopy(ORDINARY_PROPOSAL),
            deepcopy(MODEL_EVIDENCE),
        ),
    )
    coordinator.elect_transition_owner_from_snapshot(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    ledger = coordinator.create_or_verify_source_work_ledger(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
    )
    coordinator.admit_pending_inbound(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        received_at=FROZEN_NOW,
        sent_at=FROZEN_NOW,
        saved_history_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "late-alias-health-thread",
            "historyDocumentId": identity.canonical_source_id,
            "historyHash": "b" * 64,
        },
        index_binding={
            "schemaVersion": 1,
            "canonicalSourceId": identity.canonical_source_id,
            "threadId": "late-alias-health-thread",
            "identityDocumentId": identity.canonical_source_id,
        },
    )
    entry = ledger["entries"][0]
    work_arguments = {
        "user_id": "uid-1",
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
            "resultHash": "c" * 64,
        },
    )
    coordinator.settle_source_markers_if_ready(
        user_id="uid-1",
        canonical_source_id=identity.canonical_source_id,
        ledger_hash=ledger["ledgerHash"],
    )
    settlement_path = (
        "users/uid-1/sourceSettlements/"
        f"{identity.canonical_source_id}"
    )
    frozen_settlement = deepcopy(store.data[settlement_path])

    coordinator.admit_or_repair_source_identity(
        user_id="uid-1",
        hydrated_message={
            "id": "late-alias-health-source",
            "internetMessageId": "<late-alias-health@example.test>",
        },
        evidence_kind="graph_hydration",
        thread_id="late-alias-health-thread",
    )
    identity_path = (
        "users/uid-1/sourceIdentities/"
        f"{identity.canonical_source_id}"
    )
    late_descriptor = next(
        descriptor
        for descriptor in store.data[identity_path]["verifiedAliases"]
        if descriptor not in frozen_settlement["aliases"]
    )
    if repair:
        coordinator.settle_source_markers_if_ready(
            user_id="uid-1",
            canonical_source_id=identity.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
        )
    return (
        _health_documents_from_store(store),
        frozen_settlement,
        deepcopy(store.data[settlement_path]),
        late_descriptor,
    )


class FakeCollection:
    def __init__(
        self,
        count=0,
        docs=None,
        *,
        root=None,
        collection_name=None,
        query_limit=None,
    ):
        self.count = count
        self.docs = docs
        self.root = root
        self.collection_name = collection_name
        self.query_limit = query_limit

    def limit(self, count):
        if self.root is not None:
            self.root.scan_limits.append((self.collection_name, count))
        return FakeCollection(
            self.count,
            self.docs,
            root=self.root,
            collection_name=self.collection_name,
            query_limit=count,
        )

    def where(self, *, filter):
        return FakeFilteredCollection(self, filters=(filter,))

    def stream(self):
        if self.docs is not None:
            if self.query_limit is None or not isinstance(self.docs, list):
                return self.docs
            return self.docs[:self.query_limit]
        count = self.count
        if self.query_limit is not None:
            count = min(count, self.query_limit)
        return [object() for _ in range(count)]


class FakeFilteredCollection:
    def __init__(self, source, *, filters=(), query_limit=None):
        self.source = source
        self.filters = tuple(filters)
        self.query_limit = query_limit

    def where(self, *, filter):
        return FakeFilteredCollection(
            self.source,
            filters=(*self.filters, filter),
            query_limit=self.query_limit,
        )

    def limit(self, count):
        return FakeFilteredCollection(
            self.source,
            filters=self.filters,
            query_limit=count,
        )

    def stream(self):
        docs = list(self.source.stream())
        for field_filter in self.filters:
            docs = [
                doc
                for doc in docs
                if getattr(doc, "to_dict", lambda: {})().get(
                    field_filter.field_path
                ) == field_filter.value
            ]
        if self.query_limit is not None:
            docs = docs[:self.query_limit]
        return docs


class FakeHealthDoc:
    def __init__(self, data, *, doc_id=None, reference=None):
        self._data = dict(data)
        self.id = doc_id
        self.reference = reference

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    def collection(self, name):
        if name in self.root.docs_by_collection:
            return FakeCollection(
                docs=self.root.docs_by_collection[name],
                root=self.root,
                collection_name=name,
            )
        if name in self.root.counts:
            return FakeCollection(
                self.root.counts[name],
                root=self.root,
                collection_name=name,
            )
        return FakeNode(self.root, list(self.path) + ["collection", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((self.path, data, merge))


class FakeNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        key = name
        if key in self.root.docs_by_collection:
            return FakeCollection(
                docs=self.root.docs_by_collection[key],
                root=self.root,
                collection_name=key,
            )
        if key in self.root.counts:
            return FakeCollection(
                self.root.counts[key],
                root=self.root,
                collection_name=key,
            )
        return FakeNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeDocRef(self.root, self.path + ["document", name])


class FakeFirestore:
    def __init__(self, counts, docs_by_collection=None):
        self.counts = dict(counts)
        self.counts.setdefault("graphSendDraftReviews", 0)
        self.counts.setdefault(
            "pendingResponseCompletionObligations",
            0,
        )
        self.docs_by_collection = dict(docs_by_collection or {})
        for collection_name in B1_SCAN_COLLECTIONS:
            self.docs_by_collection.setdefault(collection_name, [])
        self.set_calls = []
        self.scan_limits = []

    def collection(self, name):
        return FakeNode(self, ["collection", name])


class _BoomStream:
    """Iterating this raises, simulating a Firestore read outage mid-stream."""

    def __iter__(self):
        raise RuntimeError("firestore read failed")


class _PrivateBoomStream:
    def __init__(self, private_value):
        self.private_value = private_value

    def __iter__(self):
        raise RuntimeError(self.private_value)


class SystemHealthTests(unittest.TestCase):
    def test_b1_health_keys_are_zero_for_empty_authority(self):
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(_healthy_queue_counts()),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(
            {key: 0 for key in B1_HEALTH_KEYS},
            {key: payload["queues"][key] for key in B1_HEALTH_KEYS},
        )
        self.assertEqual("healthy", payload["status"])
        self.assertEqual([], payload["countErrors"])

    def test_b1_health_counts_exact_state_mapping(self):
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=_coordinator_health_documents(),
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(
            {
                "b1ActiveClassifications": 2,
                "b1AmbiguousClassifications": 1,
                "b1BlockedSources": 0,
                "b1NonsettledPendingAdmissions": 1,
                "b1UnsettledWorkLedgers": 1,
                "b1AliasConflicts": 1,
                "b1MarkerOrSettlementAmbiguities": 0,
                "b1LegacyTerminalQuarantined": 1,
                "b1LegacyMarkerOnlyAmbiguous": 1,
                "b1LegacyReplayClaimQuarantined": 1,
            },
            {key: payload["queues"][key] for key in B1_HEALTH_KEYS},
        )
        self.assertEqual("warning", payload["status"])
        self.assertEqual([], payload["countErrors"])

    def test_b1_reverse_completeness_fails_closed_at_each_crash_boundary(self):
        cases = {
            "identity": {
                "b1ActiveClassifications",
                "b1AmbiguousClassifications",
                "b1NonsettledPendingAdmissions",
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
                "b1LegacyTerminalQuarantined",
            },
            "snapshot": {
                "b1NonsettledPendingAdmissions",
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
            },
            "owner": {
                "b1NonsettledPendingAdmissions",
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
            },
            "ledger": {
                "b1BlockedSources",
                "b1NonsettledPendingAdmissions",
                "b1MarkerOrSettlementAmbiguities",
            },
        }
        for stage, expected_error_keys in cases.items():
            with self.subTest(stage=stage):
                payload = system_health.collect_user_health(
                    "uid-1",
                    fs_client=FakeFirestore(
                        _healthy_queue_counts(),
                        docs_by_collection=(
                            _authority_stage_health_documents(stage)
                        ),
                    ),
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                )

                self.assertEqual("error", payload["status"])
                self.assertEqual(
                    expected_error_keys,
                    set(payload["countErrors"]),
                )
                self.assertTrue(
                    all(
                        payload["queues"][key] == system_health.COUNT_ERROR
                        for key in expected_error_keys
                    )
                )

    def test_b1_foreign_blocker_cannot_match_an_unrelated_thread_head(self):
        documents = _blocked_health_documents()
        blocked_admission = next(
            document
            for document in documents["inboundPendingAdmissions"]
            if document._data["admissionState"] == "blocked"
        )
        blocked_projection = documents["blockedSources"][0]
        foreign_blocker = deepcopy(blocked_admission._data["currentBlocker"])
        foreign_blocker.update(
            {
                "canonicalSourceId": "foreign-blocker-source",
                "generation": foreign_blocker["generation"] + 10,
                "threadHeadRevision": (
                    foreign_blocker["threadHeadRevision"] + 10
                ),
                "headHash": "f" * 64,
            }
        )
        blocked_admission._data["currentBlocker"] = deepcopy(foreign_blocker)
        blocked_projection._data["currentBlocker"] = deepcopy(foreign_blocker)

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        for key in (
            "b1BlockedSources",
            "b1NonsettledPendingAdmissions",
        ):
            self.assertEqual(system_health.COUNT_ERROR, payload["queues"][key])
            self.assertIn(key, payload["countErrors"])

    def test_b1_settled_owned_source_requires_retained_head_outcome(self):
        documents = _settled_owned_health_documents()
        baseline = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        self.assertEqual("healthy", baseline["status"])
        self.assertEqual([], baseline["countErrors"])

        documents["threadTransitionHeads"] = []
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(
            system_health.COUNT_ERROR,
            payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )
        self.assertIn(
            "b1MarkerOrSettlementAmbiguities",
            payload["countErrors"],
        )

    def test_b1_settled_owned_source_accepts_clear_post_release_head(self):
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=(
                    _settled_owned_health_documents(release=True)
                ),
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("healthy", payload["status"])
        self.assertEqual([], payload["countErrors"])

    def test_b1_settled_direct_claim_rejects_head_before_its_bound_generation(self):
        documents = _settled_owned_health_documents(prior_clear_cycles=1)
        baseline = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        self.assertEqual("healthy", baseline["status"])
        self.assertEqual([], baseline["countErrors"])

        documents["threadTransitionHeads"][0]._data = (
            source_coordinator._build_thread_head_document(
                thread_id="settled-owned-health-thread",
                canonical_source_id=None,
                owner_data=None,
                generation=1,
                state="clear",
                revision=2,
                now=FROZEN_NOW,
            )
        )
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(
            system_health.COUNT_ERROR,
            payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )
        self.assertIn(
            "b1MarkerOrSettlementAmbiguities",
            payload["countErrors"],
        )

    def test_b1_consumed_wake_lineage_accepts_real_release_claim_and_settlement(self):
        fixture = _consumed_wake_health_fixture()

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        releasing = health()
        self.assertEqual([], releasing["countErrors"])

        fixture["coordinator"].claim_wake_and_rebind_generation(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            canonical_source_id=fixture["second_id"],
            wake_token=fixture["released"].wake_token,
            wake_claim_id="health-consumed-wake-claim",
        )
        processing = health()
        self.assertEqual([], processing["countErrors"])

        fixture["settle_second"]()
        settled = health()
        self.assertEqual("healthy", settled["status"])
        self.assertEqual([], settled["countErrors"])

    def test_b1_consumed_wake_rejects_forged_predecessor_before_and_after_settlement(self):
        fixture = _consumed_wake_health_fixture()
        fixture["coordinator"].claim_wake_and_rebind_generation(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            canonical_source_id=fixture["second_id"],
            wake_token=fixture["released"].wake_token,
            wake_claim_id="health-forged-wake-claim",
        )
        admission_path = (
            "users/uid-1/inboundPendingAdmissions/"
            f"{fixture['second_id']}"
        )
        projection_path = (
            "users/uid-1/blockedSources/"
            f"{fixture['second_id']}"
        )
        admission = fixture["store"].data[admission_path]
        foreign_blocker = deepcopy(admission["currentBlocker"])
        foreign_blocker["canonicalSourceId"] = "nonexistent-wake-predecessor"
        foreign_blocker["headHash"] = source_coordinator.canonical_json_hash(
            source_coordinator._thread_head_hash_material(
                {
                    "schemaVersion": 1,
                    "threadId": fixture["thread_id"],
                    "threadHeadRevision": foreign_blocker[
                        "threadHeadRevision"
                    ],
                    "activeOwnerKey": foreign_blocker["ownerKey"],
                    "activeOwnerKind": foreign_blocker["ownerKind"],
                    "activeCanonicalSourceId": foreign_blocker[
                        "canonicalSourceId"
                    ],
                    "activeGeneration": foreign_blocker["generation"],
                    "activeState": "active",
                }
            )
        )
        admission["currentBlocker"] = foreign_blocker
        admission["wakeToken"] = source_coordinator._wake_token_for_release(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            admission=admission,
            released_blocker=foreign_blocker,
            wake_generation=admission["wakeGeneration"],
        )
        projection = fixture["store"].data[projection_path]
        fixture["store"].data[projection_path] = (
            source_coordinator._blocked_projection_from_admission(
                admission,
                now=projection["updatedAt"],
                created_at=projection["createdAt"],
            )
        )

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        processing = health()
        fixture["settle_second"]()
        settled = health()

        for state, payload in (
            ("processing", processing),
            ("settled", settled),
        ):
            with self.subTest(state=state):
                self.assertEqual("error", payload["status"])
                for key in (
                    "b1BlockedSources",
                    "b1NonsettledPendingAdmissions",
                    "b1MarkerOrSettlementAmbiguities",
                ):
                    self.assertEqual(
                        system_health.COUNT_ERROR,
                        payload["queues"][key],
                    )
                    self.assertIn(key, payload["countErrors"])

    def test_b1_consumed_wake_rejects_an_earlier_real_generation_as_predecessor(self):
        fixture = _three_generation_consumed_wake_health_fixture()

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        baseline = health()
        self.assertEqual([], baseline["countErrors"])

        admission_path = (
            "users/uid-1/inboundPendingAdmissions/"
            f"{fixture['third_id']}"
        )
        projection_path = (
            "users/uid-1/blockedSources/"
            f"{fixture['third_id']}"
        )
        first_owner = fixture["store"].data[
            "users/uid-1/sourceTransitionOwners/"
            f"{fixture['first_id']}"
        ]
        admission = fixture["store"].data[admission_path]
        earlier_blocker = deepcopy(admission["currentBlocker"])
        earlier_blocker.update(
            {
                "canonicalSourceId": fixture["first_id"],
                "ownerKind": first_owner["ownerKind"],
                "ownerKey": first_owner["ownerKey"],
            }
        )
        earlier_blocker["headHash"] = source_coordinator.canonical_json_hash(
            source_coordinator._thread_head_hash_material(
                {
                    "schemaVersion": 1,
                    "threadId": fixture["thread_id"],
                    "threadHeadRevision": earlier_blocker[
                        "threadHeadRevision"
                    ],
                    "activeOwnerKey": earlier_blocker["ownerKey"],
                    "activeOwnerKind": earlier_blocker["ownerKind"],
                    "activeCanonicalSourceId": earlier_blocker[
                        "canonicalSourceId"
                    ],
                    "activeGeneration": earlier_blocker["generation"],
                    "activeState": "active",
                }
            )
        )
        admission["currentBlocker"] = earlier_blocker
        admission["wakeToken"] = source_coordinator._wake_token_for_release(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            admission=admission,
            released_blocker=earlier_blocker,
            wake_generation=admission["wakeGeneration"],
        )
        projection = fixture["store"].data[projection_path]
        fixture["store"].data[projection_path] = (
            source_coordinator._blocked_projection_from_admission(
                admission,
                now=projection["updatedAt"],
                created_at=projection["createdAt"],
            )
        )

        processing = health()
        fixture["settle_third"]()
        settled = health()

        for state, payload in (
            ("processing", processing),
            ("settled", settled),
        ):
            with self.subTest(state=state):
                self.assertEqual("error", payload["status"])
                for key in (
                    "b1BlockedSources",
                    "b1NonsettledPendingAdmissions",
                    "b1MarkerOrSettlementAmbiguities",
                ):
                    self.assertEqual(
                        system_health.COUNT_ERROR,
                        payload["queues"][key],
                    )
                    self.assertIn(key, payload["countErrors"])

    def test_b1_consumed_wake_rejects_direct_claim_from_an_earlier_clear_cycle(self):
        fixture = _direct_claim_clear_cycle_consumed_wake_health_fixture()

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        baseline = health()
        self.assertEqual([], baseline["countErrors"])

        admission_path = (
            "users/uid-1/inboundPendingAdmissions/"
            f"{fixture['fourth_id']}"
        )
        projection_path = (
            "users/uid-1/blockedSources/"
            f"{fixture['fourth_id']}"
        )
        first_owner = fixture["store"].data[
            "users/uid-1/sourceTransitionOwners/"
            f"{fixture['first_id']}"
        ]
        admission = fixture["store"].data[admission_path]
        earlier_blocker = deepcopy(admission["currentBlocker"])
        earlier_blocker.update(
            {
                "canonicalSourceId": fixture["first_id"],
                "ownerKind": first_owner["ownerKind"],
                "ownerKey": first_owner["ownerKey"],
            }
        )
        earlier_blocker["headHash"] = source_coordinator.canonical_json_hash(
            source_coordinator._thread_head_hash_material(
                {
                    "schemaVersion": 1,
                    "threadId": fixture["thread_id"],
                    "threadHeadRevision": earlier_blocker[
                        "threadHeadRevision"
                    ],
                    "activeOwnerKey": earlier_blocker["ownerKey"],
                    "activeOwnerKind": earlier_blocker["ownerKind"],
                    "activeCanonicalSourceId": earlier_blocker[
                        "canonicalSourceId"
                    ],
                    "activeGeneration": earlier_blocker["generation"],
                    "activeState": "active",
                }
            )
        )
        admission["initialBlocker"] = deepcopy(earlier_blocker)
        admission["currentBlocker"] = earlier_blocker
        admission["wakeToken"] = source_coordinator._wake_token_for_release(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            admission=admission,
            released_blocker=earlier_blocker,
            wake_generation=admission["wakeGeneration"],
        )
        projection = fixture["store"].data[projection_path]
        fixture["store"].data[projection_path] = (
            source_coordinator._blocked_projection_from_admission(
                admission,
                now=projection["updatedAt"],
                created_at=projection["createdAt"],
            )
        )

        processing = health()
        fixture["settle_fourth"]()
        settled = health()

        for state, payload in (
            ("processing", processing),
            ("settled", settled),
        ):
            with self.subTest(state=state):
                self.assertEqual("error", payload["status"])
                for key in (
                    "b1BlockedSources",
                    "b1NonsettledPendingAdmissions",
                    "b1MarkerOrSettlementAmbiguities",
                ):
                    self.assertEqual(
                        system_health.COUNT_ERROR,
                        payload["queues"][key],
                    )
                    self.assertIn(key, payload["countErrors"])

    def test_b1_consumed_wake_lineage_accepts_three_generation_rebind(self):
        fixture = _three_generation_consumed_wake_health_fixture()

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        processing = health()
        self.assertEqual([], processing["countErrors"])

        fixture["settle_third"]()
        settled = health()
        self.assertEqual("healthy", settled["status"])
        self.assertEqual([], settled["countErrors"])

    def test_b1_consumed_wake_rejects_altered_predecessor_head_revision(self):
        fixture = _three_generation_consumed_wake_health_fixture()

        def health():
            return system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=_health_documents_from_store(
                        fixture["store"]
                    ),
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )

        admission_path = (
            "users/uid-1/inboundPendingAdmissions/"
            f"{fixture['third_id']}"
        )
        projection_path = (
            "users/uid-1/blockedSources/"
            f"{fixture['third_id']}"
        )
        admission = fixture["store"].data[admission_path]
        altered_blocker = deepcopy(admission["currentBlocker"])
        altered_blocker["threadHeadRevision"] -= 2
        altered_blocker["headHash"] = source_coordinator.canonical_json_hash(
            source_coordinator._thread_head_hash_material(
                {
                    "schemaVersion": 1,
                    "threadId": fixture["thread_id"],
                    "threadHeadRevision": altered_blocker[
                        "threadHeadRevision"
                    ],
                    "activeOwnerKey": altered_blocker["ownerKey"],
                    "activeOwnerKind": altered_blocker["ownerKind"],
                    "activeCanonicalSourceId": altered_blocker[
                        "canonicalSourceId"
                    ],
                    "activeGeneration": altered_blocker["generation"],
                    "activeState": "active",
                }
            )
        )
        admission["initialBlocker"] = deepcopy(altered_blocker)
        admission["currentBlocker"] = altered_blocker
        admission["wakeToken"] = source_coordinator._wake_token_for_release(
            user_id="uid-1",
            thread_id=fixture["thread_id"],
            admission=admission,
            released_blocker=altered_blocker,
            wake_generation=admission["wakeGeneration"],
        )
        projection = fixture["store"].data[projection_path]
        fixture["store"].data[projection_path] = (
            source_coordinator._blocked_projection_from_admission(
                admission,
                now=projection["updatedAt"],
                created_at=projection["createdAt"],
            )
        )

        processing = health()
        fixture["settle_third"]()
        settled = health()

        for state, payload in (
            ("processing", processing),
            ("settled", settled),
        ):
            with self.subTest(state=state):
                self.assertEqual("error", payload["status"])
                for key in (
                    "b1BlockedSources",
                    "b1NonsettledPendingAdmissions",
                    "b1MarkerOrSettlementAmbiguities",
                ):
                    self.assertEqual(
                        system_health.COUNT_ERROR,
                        payload["queues"][key],
                    )
                    self.assertIn(key, payload["countErrors"])

    def test_b1_consumed_wake_generations_are_scoped_per_thread(self):
        fixtures = (
            _consumed_wake_health_fixture(
                thread_id="consumed-wake-thread-a",
                fixture_label="consumed-wake-a",
            ),
            _consumed_wake_health_fixture(
                thread_id="consumed-wake-thread-b",
                fixture_label="consumed-wake-b",
            ),
        )
        combined = {name: [] for name in B1_SCAN_COLLECTIONS}
        for index, fixture in enumerate(fixtures):
            fixture["coordinator"].claim_wake_and_rebind_generation(
                user_id="uid-1",
                thread_id=fixture["thread_id"],
                canonical_source_id=fixture["second_id"],
                wake_token=fixture["released"].wake_token,
                wake_claim_id=f"health-per-thread-wake-{index}",
            )
            documents = _health_documents_from_store(fixture["store"])
            for collection_name in B1_SCAN_COLLECTIONS:
                combined[collection_name].extend(
                    documents[collection_name]
                )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=combined,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual([], payload["countErrors"])
        self.assertEqual(2, payload["queues"]["b1BlockedSources"])
        self.assertEqual(
            2,
            payload["queues"]["b1NonsettledPendingAdmissions"],
        )

    def test_b1_scans_fail_closed_on_unreadable_overflow_and_private_errors(self):
        collection_to_keys = {
            "sourceIdentities": {
                "b1AliasConflicts",
                "b1MarkerOrSettlementAmbiguities",
            },
            "sourceAliases": {"b1AliasConflicts"},
            "sourceClassifications": {
                "b1ActiveClassifications",
                "b1AmbiguousClassifications",
                "b1LegacyTerminalQuarantined",
            },
            "sourceTransitionOwners": {
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
            },
            "sourceWorkLedgers": {
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
            },
            "sourceDeferredWork": {"b1UnsettledWorkLedgers"},
            "inboundPendingAdmissions": {
                "b1BlockedSources",
                "b1NonsettledPendingAdmissions",
                "b1MarkerOrSettlementAmbiguities",
            },
            "threadTransitionHeads": {
                "b1BlockedSources",
                "b1NonsettledPendingAdmissions",
            },
            "blockedSources": {"b1BlockedSources"},
            "sourceSettlements": {
                "b1MarkerOrSettlementAmbiguities",
            },
            "processedMessages": {
                "b1MarkerOrSettlementAmbiguities",
                "b1LegacyMarkerOnlyAmbiguous",
                "b1LegacyReplayClaimQuarantined",
            },
        }
        private_value = "broker-private-value@example.test"
        for collection_name, expected_error_keys in collection_to_keys.items():
            with self.subTest(collection=collection_name):
                output = StringIO()
                with redirect_stdout(output):
                    payload = system_health.collect_user_health(
                        "uid-1",
                        fs_client=FakeFirestore(
                            _healthy_queue_counts(),
                            docs_by_collection={
                                collection_name: _PrivateBoomStream(
                                    private_value
                                )
                            },
                        ),
                        token_state={"status": "healthy"},
                        graph_state={"status": "healthy"},
                    )
                self.assertEqual("error", payload["status"])
                self.assertTrue(
                    expected_error_keys <= set(payload["countErrors"])
                )
                self.assertTrue(
                    all(
                        payload["queues"][key] == system_health.COUNT_ERROR
                        for key in expected_error_keys
                    )
                )
                self.assertNotIn(private_value, output.getvalue())

        overflow = [FakeHealthDoc({}) for _ in range(501)]
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection={"sourceClassifications": overflow},
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        for key in (
            "b1ActiveClassifications",
            "b1AmbiguousClassifications",
            "b1LegacyTerminalQuarantined",
        ):
            self.assertEqual(system_health.COUNT_ERROR, payload["queues"][key])
            self.assertIn(key, payload["countErrors"])

    def test_b1_physical_scans_use_501_and_allow_exactly_500(self):
        documents = [
            FakeHealthDoc(
                {"processedAt": FROZEN_NOW},
                doc_id=f"legacy-{index:03d}",
            )
            for index in range(500)
        ]
        fs = FakeFirestore(
            _healthy_queue_counts(),
            docs_by_collection={"processedMessages": documents},
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(
            500,
            payload["queues"]["b1LegacyMarkerOnlyAmbiguous"],
        )
        self.assertNotIn(
            "b1LegacyMarkerOnlyAmbiguous",
            payload["countErrors"],
        )
        for collection_name in B1_SCAN_COLLECTIONS:
            with self.subTest(collection=collection_name):
                self.assertEqual(
                    1,
                    fs.scan_limits.count(
                        (collection_name, system_health.HEALTH_SCAN_LIMIT + 1)
                    ),
                )

    def test_b1_malformed_documents_and_missing_joins_fail_closed(self):
        collection_to_keys = {
            "sourceIdentities": {
                "b1AliasConflicts",
                "b1MarkerOrSettlementAmbiguities",
            },
            "sourceAliases": {"b1AliasConflicts"},
            "sourceClassifications": {
                "b1ActiveClassifications",
                "b1AmbiguousClassifications",
                "b1LegacyTerminalQuarantined",
            },
            "sourceTransitionOwners": {
                "b1UnsettledWorkLedgers",
                "b1MarkerOrSettlementAmbiguities",
            },
            "sourceWorkLedgers": {"b1UnsettledWorkLedgers"},
            "sourceDeferredWork": {"b1UnsettledWorkLedgers"},
            "inboundPendingAdmissions": {
                "b1BlockedSources",
                "b1NonsettledPendingAdmissions",
            },
            "threadTransitionHeads": {
                "b1BlockedSources",
                "b1NonsettledPendingAdmissions",
            },
            "blockedSources": {"b1BlockedSources"},
            "sourceSettlements": {
                "b1MarkerOrSettlementAmbiguities",
            },
            "processedMessages": {
                "b1MarkerOrSettlementAmbiguities",
                "b1LegacyMarkerOnlyAmbiguous",
                "b1LegacyReplayClaimQuarantined",
            },
        }
        for collection_name, expected_error_keys in collection_to_keys.items():
            with self.subTest(collection=collection_name):
                documents = _coordinator_health_documents()
                documents[collection_name] = [
                    FakeHealthDoc({}, doc_id="malformed")
                ]
                payload = system_health.collect_user_health(
                    "uid-1",
                    fs_client=FakeFirestore(
                        _healthy_queue_counts(),
                        docs_by_collection=documents,
                    ),
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                )
                self.assertTrue(
                    expected_error_keys <= set(payload["countErrors"])
                )
                self.assertTrue(
                    all(
                        payload["queues"][key] == system_health.COUNT_ERROR
                        for key in expected_error_keys
                    )
                )

        documents = _coordinator_health_documents()
        documents["sourceTransitionOwners"] = []
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        self.assertEqual(
            system_health.COUNT_ERROR,
            payload["queues"]["b1UnsettledWorkLedgers"],
        )

    def test_b1_unknown_states_fail_closed_without_rendering_values(self):
        private_state = "private-unknown-state@example.test"
        cases = (
            (
                "sourceClassifications",
                lambda documents: next(
                    doc
                    for doc in documents["sourceClassifications"]
                    if doc._data["classificationState"] == "snapshot_ready"
                )._data.__setitem__("classificationState", private_state),
                {
                    "b1ActiveClassifications",
                    "b1AmbiguousClassifications",
                    "b1LegacyTerminalQuarantined",
                },
            ),
            (
                "sourceWorkLedgers",
                lambda documents: documents["sourceWorkLedgers"][0]._data[
                    "entries"
                ][0].__setitem__("state", private_state),
                {"b1UnsettledWorkLedgers"},
            ),
            (
                "inboundPendingAdmissions",
                lambda documents: documents[
                    "inboundPendingAdmissions"
                ][0]._data.__setitem__("admissionState", private_state),
                {
                    "b1BlockedSources",
                    "b1NonsettledPendingAdmissions",
                },
            ),
            (
                "processedMessages",
                lambda documents: documents["processedMessages"].append(
                    FakeHealthDoc(
                        {
                            "status": private_state,
                            "replayAttemptId": "unknown-attempt",
                            "claimedAt": FROZEN_NOW,
                        },
                        doc_id="unknown-status",
                    )
                ),
                {
                    "b1MarkerOrSettlementAmbiguities",
                    "b1LegacyMarkerOnlyAmbiguous",
                    "b1LegacyReplayClaimQuarantined",
                },
            ),
        )
        for collection_name, mutate, expected_error_keys in cases:
            with self.subTest(collection=collection_name):
                documents = _coordinator_health_documents()
                mutate(documents)
                output = StringIO()
                with redirect_stdout(output):
                    payload = system_health.collect_user_health(
                        "uid-1",
                        fs_client=FakeFirestore(
                            _healthy_queue_counts(),
                            docs_by_collection=documents,
                        ),
                        token_state={"status": "healthy"},
                        graph_state={"status": "healthy"},
                    )
                self.assertTrue(
                    expected_error_keys <= set(payload["countErrors"])
                )
                self.assertTrue(
                    all(
                        payload["queues"][key] == system_health.COUNT_ERROR
                        for key in expected_error_keys
                    )
                )
                self.assertNotIn(private_state, output.getvalue())

    def test_b1_unknown_alias_schema_and_malformed_settlement_hash_fail_closed(self):
        alias_documents = _coordinator_health_documents()
        alias_documents["sourceAliases"][0]._data["schemaVersion"] = 2
        alias_payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=alias_documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        for key in (
            "b1AliasConflicts",
            "b1MarkerOrSettlementAmbiguities",
        ):
            self.assertEqual(system_health.COUNT_ERROR, alias_payload["queues"][key])
            self.assertIn(key, alias_payload["countErrors"])

        private_hash = "private-malformed-settlement@example.test"
        settlement_documents = _settled_health_documents()
        settlement_documents["sourceSettlements"][0]._data[
            "identityHash"
        ] = private_hash
        output = StringIO()
        with redirect_stdout(output):
            settlement_payload = system_health.collect_user_health(
                "uid-1",
                fs_client=FakeFirestore(
                    _healthy_queue_counts(),
                    docs_by_collection=settlement_documents,
                ),
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )
        self.assertEqual(
            system_health.COUNT_ERROR,
            settlement_payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )
        self.assertIn(
            "b1MarkerOrSettlementAmbiguities",
            settlement_payload["countErrors"],
        )
        self.assertNotIn(private_hash, output.getvalue())

    def test_b1_active_head_without_matching_admission_fails_closed(self):
        documents = _active_head_health_documents()
        baseline = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        self.assertEqual([], baseline["countErrors"])
        self.assertEqual(
            1,
            baseline["queues"]["b1NonsettledPendingAdmissions"],
        )

        documents["inboundPendingAdmissions"] = []
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        for key in (
            "b1BlockedSources",
            "b1NonsettledPendingAdmissions",
        ):
            self.assertEqual(system_health.COUNT_ERROR, payload["queues"][key])
            self.assertIn(key, payload["countErrors"])

    def test_b1_active_head_revision_must_match_its_generation(self):
        documents = _active_head_health_documents()
        head = documents["threadTransitionHeads"][0]._data
        head["threadHeadRevision"] += 2
        head["headHash"] = source_coordinator.canonical_json_hash(
            source_coordinator._thread_head_hash_material(head)
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        for key in (
            "b1BlockedSources",
            "b1NonsettledPendingAdmissions",
        ):
            self.assertEqual(system_health.COUNT_ERROR, payload["queues"][key])
            self.assertIn(key, payload["countErrors"])

    def test_b1_blocked_projection_has_an_exact_positive_count(self):
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=_blocked_health_documents(),
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual([], payload["countErrors"])
        self.assertEqual(1, payload["queues"]["b1BlockedSources"])
        self.assertEqual(
            2,
            payload["queues"]["b1NonsettledPendingAdmissions"],
        )
        self.assertEqual(2, payload["queues"]["b1UnsettledWorkLedgers"])
        self.assertEqual(
            0,
            payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )

    def test_b1_missing_canonical_alias_projection_counts_one_ambiguity(self):
        documents = _settled_health_documents()
        baseline = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )
        self.assertEqual("healthy", baseline["status"])
        self.assertEqual([], baseline["countErrors"])

        documents["processedMessages"] = []
        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual([], payload["countErrors"])
        self.assertEqual(
            1,
            payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )
        self.assertEqual(0, payload["queues"]["b1LegacyMarkerOnlyAmbiguous"])
        self.assertEqual(
            0,
            payload["queues"]["b1LegacyReplayClaimQuarantined"],
        )

    def test_b1_late_alias_projection_obligation_uses_current_identity_aliases(self):
        (
            before_documents,
            before_frozen_settlement,
            before_retained_settlement,
            late_descriptor,
        ) = _late_alias_settled_health_documents(repair=False)
        self.assertEqual(before_frozen_settlement, before_retained_settlement)
        before = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=before_documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual([], before["countErrors"])
        self.assertEqual(
            1,
            before["queues"]["b1MarkerOrSettlementAmbiguities"],
        )

        (
            after_documents,
            after_frozen_settlement,
            after_retained_settlement,
            after_late_descriptor,
        ) = _late_alias_settled_health_documents(repair=True)
        self.assertEqual(late_descriptor, after_late_descriptor)
        self.assertEqual(after_frozen_settlement, after_retained_settlement)
        after = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=after_documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("healthy", after["status"])
        self.assertEqual([], after["countErrors"])
        self.assertEqual(
            0,
            after["queues"]["b1MarkerOrSettlementAmbiguities"],
        )
        late_projection = next(
            document._data
            for document in after_documents["processedMessages"]
            if document.id == after_late_descriptor["sourceAliasKey"]
        )
        self.assertEqual(
            after_frozen_settlement["settlementHash"],
            late_projection["settlementHash"],
        )
        self.assertEqual(
            after_frozen_settlement["settlementRevision"],
            late_projection["settlementRevision"],
        )

    def test_b1_legacy_replay_health_groups_completed_and_partial_attempts(self):
        completed = {
            "status": "processed",
            "replayAttemptId": "completed-attempt",
            "claimedAt": FROZEN_NOW,
            "processedAt": FROZEN_NOW,
        }
        mixed_in_progress = {
            "status": "operator_replay_in_progress",
            "replayAttemptId": "mixed-attempt",
            "claimedAt": FROZEN_NOW,
        }
        mixed_completed = {
            "status": "processed",
            "replayAttemptId": "mixed-attempt",
            "claimedAt": FROZEN_NOW,
            "processedAt": FROZEN_NOW,
        }
        cases = (
            (
                "completed pair",
                [deepcopy(completed), deepcopy(completed)],
                1,
                0,
            ),
            (
                "mixed partial pair",
                [mixed_in_progress, mixed_completed],
                0,
                1,
            ),
        )
        for name, replay_records, expected_markers, expected_replays in cases:
            with self.subTest(case=name):
                documents = [
                    FakeHealthDoc(record, doc_id=f"replay-record-{index}")
                    for index, record in enumerate(replay_records)
                ]
                payload = system_health.collect_user_health(
                    "uid-1",
                    fs_client=FakeFirestore(
                        _healthy_queue_counts(),
                        docs_by_collection={
                            "processedMessages": documents,
                        },
                    ),
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                )

                self.assertEqual([], payload["countErrors"])
                self.assertEqual(
                    expected_markers,
                    payload["queues"]["b1LegacyMarkerOnlyAmbiguous"],
                )
                self.assertEqual(
                    expected_replays,
                    payload["queues"]["b1LegacyReplayClaimQuarantined"],
                )

    def test_b1_settlement_against_unsettled_ledger_counts_ambiguity(self):
        documents = _settled_health_documents()
        entry = documents["sourceWorkLedgers"][0]._data["entries"][0]
        entry.update(
            {
                "state": "pending",
                "resolutionEvidence": None,
                "resolutionEvidenceHash": None,
            }
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual([], payload["countErrors"])
        self.assertEqual(1, payload["queues"]["b1UnsettledWorkLedgers"])
        self.assertEqual(
            1,
            payload["queues"]["b1MarkerOrSettlementAmbiguities"],
        )

    def test_b1_partial_canonical_marker_shape_fails_all_processed_keys(self):
        documents = _coordinator_health_documents()
        documents["processedMessages"].append(
            FakeHealthDoc(
                {"canonicalSourceId": "partial-authority"},
                doc_id="partial-authority-marker",
            )
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=FakeFirestore(
                _healthy_queue_counts(),
                docs_by_collection=documents,
            ),
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        for key in (
            "b1MarkerOrSettlementAmbiguities",
            "b1LegacyMarkerOnlyAmbiguous",
            "b1LegacyReplayClaimQuarantined",
        ):
            self.assertEqual(system_health.COUNT_ERROR, payload["queues"][key])
            self.assertIn(key, payload["countErrors"])

    def test_pending_retained_draft_review_is_visible_in_canonical_health_queue(self):
        now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "graphSendDraftReviews": [
                    FakeHealthDoc({
                        "threadId": "thread-1",
                        "clientId": "client-1",
                        "pendingDocumentId": "pending-1",
                        "status": "manual_review",
                        "source": "pendingGraphSendProtocol",
                        "authoritative": True,
                        "alreadySent": False,
                        "providerSendStarted": False,
                        "sendOutcomeUnknown": False,
                        "retryAllowed": False,
                        "automaticDeleteAttempted": False,
                        "graphSendPermitId": "graph-send-permit-1",
                        "graphSendPermitHash": "a" * 64,
                        "sourceGraphMessageId": "source-1",
                        "draftId": "draft-1",
                        "draftMutationState": "prepared",
                        "draftResolutionEvidenceHash": "b" * 64,
                        "failureReason": "Retained draft requires operator review.",
                        "createdAt": now,
                        "updatedAt": now,
                    }),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["graphSendDraftReviews"])

    def test_malformed_or_unreadable_draft_review_queue_fails_health_closed(self):
        malformed = FakeHealthDoc({
            "status": "manual_review",
            "source": "pendingGraphSendProtocol",
            "retryAllowed": True,
        })
        cases = {
            "malformed": [malformed],
            "malformed_resolved": [
                FakeHealthDoc({
                    "status": "resolved_not_actionable",
                    "resolution": "retained_draft_not_actionable",
                    "retryAllowed": True,
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                    "originalReviewEvidenceHash": "arbitrary-nonempty",
                    "operatorSettlementId": "arbitrary-nonempty",
                    "resolvedBy": "arbitrary-nonempty",
                }),
            ],
            "unreadable": _BoomStream(),
        }
        for label, docs in cases.items():
            with self.subTest(case=label):
                fs = FakeFirestore(
                    {
                        "outbox": 0,
                        "deadLetterQueue": 0,
                        "pendingResponses": 0,
                        "processingFailures": 0,
                        "terminalGraphSendReviews": 0,
                        "threads": 0,
                    },
                    docs_by_collection={"graphSendDraftReviews": docs},
                )

                payload = system_health.collect_user_health(
                    "uid-1",
                    fs_client=fs,
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                )

                self.assertEqual("error", payload["status"])
                self.assertEqual(
                    -1,
                    payload["queues"]["graphSendDraftReviews"],
                )
                self.assertIn(
                    "graphSendDraftReviews",
                    payload["countErrors"],
                )

    def test_resolved_draft_review_health_requires_exact_audit_and_permit_linkage(self):
        now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)

        def linked_health_count(*, drift=None):
            user_ref = MagicMock(name="user_ref")
            user_ref.path = "users/uid-1"
            review_collection = MagicMock(name="draft_review_collection")
            review_collection.limit.return_value = review_collection
            review_ref = MagicMock(name="review_ref")
            review_ref.path = (
                "users/uid-1/graphSendDraftReviews/"
                "pending-graph-send-permit-1"
            )
            review_ref.id = "pending-graph-send-permit-1"
            audit_ref = MagicMock(name="audit_ref")
            audit_ref.path = (
                "users/uid-1/graphSendDraftReviewSettlements/"
                "draft-review-settlement-1"
            )
            audit_ref.id = "draft-review-settlement-1"
            original = {
                "threadId": "thread-1",
                "clientId": "client-1",
                "pendingDocumentId": "pending-1",
                "status": "manual_review",
                "source": "pendingGraphSendProtocol",
                "authoritative": True,
                "alreadySent": False,
                "providerSendStarted": False,
                "sendOutcomeUnknown": False,
                "retryAllowed": False,
                "automaticDeleteAttempted": False,
                "failureReason": "Retained draft requires operator review.",
                "graphSendPermitId": "graph-send-permit-1",
                "graphSendPermitHash": "a" * 64,
                "sourceGraphMessageId": "source-1",
                "preparedEnvelopeHash": "b" * 64,
                "draftId": "draft-1",
                "draftMutationState": "prepared",
                "draftResolutionEvidenceHash": "c" * 64,
                "createdAt": now,
                "updatedAt": now,
            }
            original_hash = send_permits._stable_evidence_hash(original)
            resolved = {
                **original,
                "status": "resolved_not_actionable",
                "resolution": "retained_draft_not_actionable",
                "retryAllowed": True,
                "originalReviewEvidenceHash": original_hash,
                "operatorSettlementAuditRef": audit_ref,
                "operatorSettlementId": "draft-review-settlement-1",
                "resolvedBy": "authenticated-operator-uid",
                "operatorReason": "The retained draft was manually discarded.",
                "resolvedAt": now,
                "updatedAt": now,
            }
            review_hash = send_permits._stable_evidence_hash(resolved)
            audit = {
                "version": 1,
                "settlementId": "draft-review-settlement-1",
                "action": "confirm_retained_draft_not_actionable",
                "operatorId": "authenticated-operator-uid",
                "operatorReason": "The retained draft was manually discarded.",
                "threadId": "thread-1",
                "clientId": "client-1",
                "pendingDocumentId": "pending-1",
                "graphSendPermitId": "graph-send-permit-1",
                "graphSendPermitHash": "a" * 64,
                "reviewEvidenceHash": original_hash,
                "reviewEvidenceRef": review_ref,
                "resolution": "retained_draft_not_actionable",
                "providerSendStarted": False,
                "automaticDeleteAttempted": False,
                "retryAllowed": True,
                "resolvedAt": now,
            }
            permit = {
                "permitId": "graph-send-permit-1",
                "immutableHash": "a" * 64,
                "issuerKind": "pending_response",
                "issuerDocumentId": "pending-1",
                "threadId": "thread-1",
                "clientId": "client-1",
                "status": "settled_draft_review_resolved",
                "draftReviewRequired": False,
                "draftReviewEvidenceRef": review_ref,
                "draftReviewEvidenceHash": review_hash,
                "pendingReconciliationEvidenceHash": original_hash,
                "operatorSettlementAuditRef": audit_ref,
                "operatorSettlementAuditHash": (
                    send_permits._stable_evidence_hash(audit)
                ),
                "operatorOriginalReconciliationEvidenceHash": original_hash,
                "operatorResolvedReviewEvidenceHash": review_hash,
                "operatorResolution": "retained_draft_not_actionable",
            }
            if drift == "audit":
                audit["operatorReason"] = "drifted audit reason"
            elif drift == "permit":
                permit["operatorResolvedReviewEvidenceHash"] = "d" * 64
            elif drift == "timestamp":
                resolved["createdAt"] = "not-an-authoritative-timestamp"

            review_snapshot = FakeHealthDoc(
                resolved,
                doc_id=review_ref.id,
                reference=review_ref,
            )
            review_snapshot.exists = True
            review_collection.stream.return_value = [review_snapshot]
            review_collection.document.return_value = review_ref
            audit_snapshot = FakeHealthDoc(audit)
            audit_snapshot.exists = True
            audit_ref.get.return_value = audit_snapshot
            permit_snapshot = FakeHealthDoc(permit)
            permit_snapshot.exists = True
            permit_ref = MagicMock(name="permit_ref")
            permit_ref.get.return_value = permit_snapshot
            permit_collection = MagicMock(name="permit_collection")
            permit_collection.document.return_value = permit_ref
            thread_ref = MagicMock(name="thread_ref")
            thread_ref.path = "users/uid-1/threads/thread-1"
            thread_ref.collection.return_value = permit_collection
            threads_collection = MagicMock(name="threads_collection")
            threads_collection.document.return_value = thread_ref

            def collection(name):
                return {
                    "graphSendDraftReviews": review_collection,
                    "threads": threads_collection,
                }[name]

            user_ref.collection.side_effect = collection
            return system_health._count_active_pending_draft_reviews(user_ref)

        self.assertEqual(0, linked_health_count())
        self.assertEqual(-1, linked_health_count(drift="audit"))
        self.assertEqual(-1, linked_health_count(drift="permit"))
        self.assertEqual(-1, linked_health_count(drift="timestamp"))

    def test_collect_user_health_warns_on_backlog_counts(self):
        fs = FakeFirestore({
            "outbox": 2,
            "deadLetterQueue": 1,
            "pendingResponses": 0,
            "processingFailures": 3,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy", "source": "cached_access_token"},
            graph_state={"status": "healthy"},
            now=now,
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(2, payload["queues"]["outbox"])
        self.assertEqual(1, payload["queues"]["deadLetterQueue"])
        self.assertEqual(3, payload["queues"]["processingFailures"])
        self.assertEqual("healthy", payload["token"]["status"])
        self.assertEqual("healthy", payload["graph"]["status"])

    def test_collect_user_health_ignores_resolved_dead_letters(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": [
                    FakeHealthDoc({"status": "requeued", "recoveryStatus": "requeued"}),
                    FakeHealthDoc({"status": "discarded", "resolution": "discard"}),
                    FakeHealthDoc({"status": "dead_lettered", "failureReason": "still needs review"}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["deadLetterQueue"])

    def test_terminal_send_review_stays_visible_after_generic_dead_letter_discard(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": [
                    FakeHealthDoc({"status": "discarded", "resolution": "discard"}),
                ],
                "terminalGraphSendReviews": [
                    FakeHealthDoc({
                        "status": "needs_reconciliation",
                        "source": "terminalGraphSendProtocol",
                        "retryAllowed": False,
                    }),
                    FakeHealthDoc({"status": "acknowledged"}),
                    FakeHealthDoc({"status": "reconciled_sent"}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(0, payload["queues"]["deadLetterQueue"])
        self.assertEqual(2, payload["queues"]["terminalGraphSendReviews"])

    def test_owed_terminal_thread_warns_with_every_queue_and_review_empty(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={
                "threads": [
                    FakeHealthDoc({
                        "clientId": "client-1",
                        "status": "stopped",
                        "terminalReplyOwed": True,
                        "terminalSagaKey": "saga-1",
                    }),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["terminalProtocolThreads"])
        self.assertEqual(0, payload["queues"]["terminalGraphSendReviews"])

    def test_unreadable_terminal_thread_scan_fails_health_closed(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={"threads": _BoomStream()},
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalProtocolThreads"])
        self.assertEqual(["terminalProtocolThreads"], payload["countErrors"])

    def test_terminal_thread_scan_bound_cannot_hide_later_active_obligation(self):
        clean_threads = [
            FakeHealthDoc({"status": "completed"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={
                "threads": clean_threads + [
                    FakeHealthDoc({"terminalNotificationOwed": True}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalProtocolThreads"])
        self.assertIn("terminalProtocolThreads", payload["countErrors"])

    def test_dead_letter_scan_bound_cannot_hide_later_unresolved_work(self):
        resolved = [
            FakeHealthDoc({"status": "discarded"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": resolved
                + [FakeHealthDoc({"status": "dead_lettered"})],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["deadLetterQueue"])
        self.assertIn("deadLetterQueue", payload["countErrors"])

    def test_terminal_review_scan_bound_cannot_hide_later_unresolved_work(self):
        resolved = [
            FakeHealthDoc({"status": "reconciled_sent"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "threads": 0,
            },
            docs_by_collection={
                "terminalGraphSendReviews": resolved
                + [FakeHealthDoc({"status": "needs_reconciliation"})],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalGraphSendReviews"])
        self.assertIn("terminalGraphSendReviews", payload["countErrors"])

    def test_collect_user_health_errors_on_token_failure(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "error", "error": "silent_auth_failed"},
            graph_state={"status": "unknown"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual("silent_auth_failed", payload["token"]["error"])

    def test_unreadable_queue_count_cannot_report_healthy(self):
        # Firestore read outage: every queue count fails (-1). Token + graph are
        # healthy. Health must NOT report healthy — a queue we cannot read may be
        # hiding an unbounded backlog of stuck / misdirected sends (fail closed).
        fs = FakeFirestore(
            {},
            docs_by_collection={
                "outbox": _BoomStream(),
                "deadLetterQueue": _BoomStream(),
                "pendingResponses": _BoomStream(),
                "processingFailures": _BoomStream(),
                "terminalGraphSendReviews": _BoomStream(),
                "threads": _BoomStream(),
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(-1, payload["queues"]["outbox"])
        self.assertNotEqual("healthy", payload["status"])
        self.assertEqual("error", payload["status"])
        # Per-queue count-error flags surfaced so the outage is observable.
        self.assertIn("outbox", payload["countErrors"])
        self.assertIn("deadLetterQueue", payload["countErrors"])

    def test_partial_count_error_cannot_report_healthy(self):
        # Only one queue fails to read; the rest are empty. Still must not be healthy.
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": _BoomStream(),
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(-1, payload["queues"]["deadLetterQueue"])
        self.assertEqual("error", payload["status"])
        self.assertEqual(["deadLetterQueue"], payload["countErrors"])

    def test_count_error_severity_env_downgrade_to_warning(self):
        # Operators may downgrade count-error severity to warning, but never to
        # healthy. Absence of the env var defaults to the fail-closed (error) path.
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={"deadLetterQueue": _BoomStream()},
        )
        prev = os.environ.get("HEALTH_COUNT_ERROR_SEVERITY")
        os.environ["HEALTH_COUNT_ERROR_SEVERITY"] = "warning"
        try:
            payload = system_health.collect_user_health(
                "uid-1",
                fs_client=fs,
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )
        finally:
            if prev is None:
                os.environ.pop("HEALTH_COUNT_ERROR_SEVERITY", None)
            else:
                os.environ["HEALTH_COUNT_ERROR_SEVERITY"] = prev

        self.assertEqual("warning", payload["status"])
        self.assertEqual(["deadLetterQueue"], payload["countErrors"])

    def test_no_count_error_leaves_healthy_intact(self):
        # Regression: clean reads with healthy token/graph stay healthy and
        # surface an empty countErrors list.
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("healthy", payload["status"])
        self.assertEqual([], payload["countErrors"])

    def test_write_user_health_replaces_dashboard_snapshot(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })
        payload = {"status": "healthy", "queues": {}}

        system_health.write_user_health("uid-1", payload, fs_client=fs)

        self.assertEqual(1, len(fs.set_calls))
        self.assertEqual(
            ("collection", "users", "document", "uid-1", "collection", "systemHealth", "document", "emailAutomation"),
            fs.set_calls[0][0],
        )
        self.assertFalse(fs.set_calls[0][2])


if __name__ == "__main__":
    unittest.main()
