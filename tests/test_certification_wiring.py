"""The wiring that turns two structurally-correct routes into two that run.

``/certification/review-input`` and ``/certification/recover`` were both
implemented, tested, and INERT: nothing in the instrument produced the review
pack the first serves, and no shipped ledger could answer the observation the
second gates on. A route that is structurally correct and never exercised is
exactly the failure this project already hit once -- three phases proved
properties by test, and the first actual run found a real ordering bug.

So this module does not assert that the wiring exists. It DRIVES it:

* the real bootstrap scenario through the real runner, and then asks the real
  ``review_input`` for the pack that run produced;
* the real ``FirestoreRunLedger`` -- the same class the twin will hold -- to
  CLAIMED, and then recovers it, with every gate evaluated against values the
  STORE assigned rather than values a test handed in.

The two doubles here are provider doubles, not artifact doubles. Every value
under test comes from the shipped registry, the shipped runner, the shipped
state machine and the shipped lifecycle; what is faked is the socket underneath
them, because a certification test may not make a provider call.
"""

from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import evidence as ev
from email_automation.certification import input_handoff
from email_automation.certification import ledger as ledger_module
from email_automation.certification import lifecycle
from email_automation.certification import runner as runner_module
from email_automation.certification import scenarios
from email_automation.certification.canonical_json import canonical_digest
from email_automation.certification.models import (
    CertificationRequest,
    RunAuthorization,
)

REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
BOOTSTRAP = "campaign-one-property"
REFUTATION = "campaign-one-property-impossible-oracle"

TWIN_ENV = {
    "K_SERVICE": "process-user-certification",
    "K_REVISION": "process-user-certification-00001-abc",
    "SITESIFT_SOURCE_REVISION": REVISION,
    "SITESIFT_IMAGE_DIGEST": "sha256:" + "b" * 64,
    "SITESIFT_PRODUCTION_CANDIDATE_REVISION": "process-user-00042-xyz",
    "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION": "7",
    "SITESIFT_FIXTURE_CONFIG_DIGEST": "d" * 64,
}


def authorization_for(run_id: str, scenario_id: str = BOOTSTRAP) -> RunAuthorization:
    return RunAuthorization.create(
        scenario_id=scenario_id,
        run_id=run_id,
        source_revision=REVISION,
        image_digest="sha256:" + "b" * 64,
        certification_service="process-user-certification",
        certification_revision="process-user-certification-00001-abc",
        production_candidate_revision="process-user-00042-xyz",
        caller_identity_digest="c" * 64,
        fixture_config_secret_version="7",
        fixture_config_digest="d" * 64,
        scenario_registry_digest=scenarios.registry_digest(),
        launch_class="agent_safe",
        input_producer_kind="backend_registry_v1",
        canonical_input_digest=canonical_digest({"k": "v"}),
        input_producer_artifact_digest=canonical_digest({"p": "v"}),
        authorization_expires_at="2099-01-01T00:00:00Z",
    )


def install_review_store(case):
    """Install a fresh transient store as the lifecycle default for one test.

    ``run_scenario`` resolves the store from the lifecycle rather than from a
    parameter, so this is how a test drives the REAL destination without letting
    packs leak between tests.
    """
    store = input_handoff.TransientReviewStore()
    patcher = patch.object(lifecycle, "_DEFAULT_REVIEW_STORE", store)
    patcher.start()
    case.addCleanup(patcher.stop)
    return store


def drive_to_claimed(ledger, run_id, *, scenario_id=BOOTSTRAP, running=False):
    """Drive ANY ledger to CLAIMED (or RUNNING) through its real transitions."""
    request = CertificationRequest(
        scenario_id=scenario_id, run_id=run_id, expected_revision=REVISION)
    authorization = authorization_for(run_id, scenario_id)
    ledger.begin_preparing(request)
    ledger.mark_prepared(request, authorization)
    ledger.claim(request, authorization)
    if running:
        ledger.mark_running(run_id, "execute")
    return request


# ---------------------------------------------------------------------------
# The store double
# ---------------------------------------------------------------------------
#
# Shaped like the Firestore surface the ledger calls, with ONE addition over the
# double in ``test_certification_firestore_ledger``: server-assigned
# ``update_time`` and ``read_time`` on every snapshot, because those two values
# are the entire basis on which the durable ledger may claim a record age.
#
# The clock is the STORE's, not the caller's. That is the property under test:
# a caller that could set the age could declare its own run recoverable.


class _Snapshot:
    def __init__(self, ref, data, update_time, read_time):
        self.reference = ref
        self.id = ref.id
        self._data = data
        self.exists = data is not None
        self.update_time = update_time
        self.create_time = update_time
        self.read_time = read_time

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _DocumentRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.rsplit("/", 1)[-1]

    def get(self, transaction=None, timeout=None, **kwargs):
        if timeout is None:
            # In production this HANGS rather than erroring, and no `except`
            # clause catches a hang. Loud here, so it cannot pass unnoticed.
            raise AssertionError(f"read of {self._path} carried no deadline")
        self._store.reads.append(self._path)
        return _Snapshot(self, self._store.docs.get(self._path),
                         self._store.update_times.get(self._path),
                         self._store.now())

    def set(self, *_args, **_kwargs):
        raise AssertionError(f"{self._path} written outside a transaction")

    update = delete = create = set


class _CollectionRef:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _DocumentRef(self._store, f"{self._name}/{doc_id}")

    def stream(self, timeout=None, **kwargs):
        if timeout is None:
            raise AssertionError(f"stream of {self._name} carried no deadline")
        prefix = f"{self._name}/"
        for path in sorted(self._store.docs):
            if path.startswith(prefix):
                yield _Snapshot(_DocumentRef(self._store, path),
                                self._store.docs[path],
                                self._store.update_times.get(path),
                                self._store.now())


class _Transaction:
    """Duck-typed to what the real ``_Transactional`` driver actually touches."""

    def __init__(self, store, max_attempts=5, read_only=False):
        self._store = store
        self._max_attempts = max_attempts
        self._read_only = read_only
        self._id = None
        self._writes = []

    @property
    def in_progress(self):
        return self._id is not None

    def _clean_up(self):
        self._id = None
        self._writes = []

    def _begin(self, retry_id=None):
        self._store.begins += 1
        self._id = b"tx-%d" % self._store.begins

    def _rollback(self):
        self._clean_up()

    def _commit(self):
        if not self.in_progress:
            raise ValueError("cannot commit a transaction that has not begun")
        self._store.commit(self._writes)
        self._clean_up()
        return []

    def set(self, ref, data, merge=False):
        self._writes.append(("set", ref._path, copy.deepcopy(data)))

    def delete(self, ref):
        self._writes.append(("delete", ref._path, None))


class ClockedFirestore:
    """An in-process store that stamps every commit with ITS OWN clock."""

    def __init__(self, start=None):
        self.docs = {}
        self.update_times = {}
        self.reads = []
        self.begins = 0
        self._now = start or datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    # -- the store's clock -------------------------------------------------

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)

    # -- the surface -------------------------------------------------------

    def collection(self, name):
        return _CollectionRef(self, name)

    def transaction(self, max_attempts=5, **kwargs):
        return _Transaction(self, max_attempts=max_attempts)

    def commit(self, writes):
        for kind, path, data in writes:
            if kind == "set":
                self.docs[path] = copy.deepcopy(data)
                self.update_times[path] = self._now
            else:
                self.docs.pop(path, None)
                self.update_times.pop(path, None)


def durable_ledger(store=None):
    return ledger_module.FirestoreRunLedger(store or ClockedFirestore())


# ---------------------------------------------------------------------------
# Gap 1: review-input has a producer
# ---------------------------------------------------------------------------


class ReviewPackProducerTests(unittest.TestCase):
    """The runner deposits the pack; nothing else in the instrument can.

    The store is installed by PATCHING the lifecycle default rather than passed
    in, because ``run_scenario`` deliberately takes no store parameter: the
    review store is the one place raw captured prose lives, and a caller that
    could name a different destination would be choosing where the fixture's
    words go.
    """

    def setUp(self):
        self.store = install_review_store(self)

    def test_a_real_bootstrap_run_deposits_a_reviewable_pack(self):
        store = self.store
        record, detail = runner_module.run_scenario(
            BOOTSTRAP, run_id="cert-wiring-deposit", revision=REVISION)
        self.assertEqual(record.outcome, "pass", detail.get("mismatches"))

        pack = store.get("cert-wiring-deposit", now_epoch=0)
        self.assertIsNotNone(pack, "the run captured a message and deposited nothing")
        self.assertEqual(len(pack.messages), detail["observed"]["captured_outreach"])
        self.assertEqual([m.ordinal for m in pack.messages],
                         list(range(1, len(pack.messages) + 1)))
        for message in pack.messages:
            self.assertIn(message.kind, input_handoff.ALLOWED_REVIEW_KINDS)
            self.assertTrue(message.subject.strip())
            self.assertTrue(message.body.strip())

    def test_the_deposited_pack_carries_no_fixture_address(self):
        from email_automation.certification import fixtures as fx
        runner_module.run_scenario(
            BOOTSTRAP, run_id="cert-wiring-redacted", revision=REVISION)
        pack = self.store.get("cert-wiring-redacted", now_epoch=0)
        blob = " ".join(m.subject + " " + m.body for m in pack.messages)
        self.assertNotIn("@", blob)
        self.assertNotIn(fx.FIXTURE_RECIPIENT, blob)

    def test_the_deposit_happens_before_replay_and_before_cleanup(self):
        """Ordering, not politeness.

        Teardown is a stream of DELETEs against the fixture, and replay
        re-executes the lane. A pack deposited after either is a pack captured
        from a dismantled or re-run fixture, which is the same class of bug that
        made fixture_audit come back at 2 for a run that emitted one.
        """
        order = []
        store = self.store
        real_deposit = store.deposit
        real_replay = runner_module._replay
        real_cleanup = runner_module.CleanupHandle.run

        def watched_deposit(*args, **kwargs):
            order.append("deposit")
            return real_deposit(*args, **kwargs)

        def watched_replay(*args, **kwargs):
            order.append("replay")
            return real_replay(*args, **kwargs)

        def watched_cleanup(self, *args, **kwargs):
            order.append("cleanup")
            return real_cleanup(self, *args, **kwargs)

        with patch.object(store, "deposit", watched_deposit), \
                patch.object(runner_module, "_replay", watched_replay), \
                patch.object(runner_module.CleanupHandle, "run", watched_cleanup):
            runner_module.run_scenario(
                BOOTSTRAP, run_id="cert-wiring-order", revision=REVISION)

        self.assertEqual(order, ["deposit", "replay", "cleanup"])

    def test_the_review_kind_map_is_an_allowlist_over_the_real_kinds(self):
        """Every kind the map can produce is one the projection already accepts."""
        from email_automation.message_transport import DeliveryKind

        self.assertEqual(
            set(runner_module.REVIEW_KIND_BY_DELIVERY_KIND),
            {kind.value for kind in DeliveryKind},
            "a delivery kind the product can emit has no review kind",
        )
        self.assertLessEqual(
            set(runner_module.REVIEW_KIND_BY_DELIVERY_KIND.values()),
            set(input_handoff.ALLOWED_REVIEW_KINDS),
        )

    def test_an_unmapped_kind_refuses_the_pack_rather_than_serving_it(self):
        """MUTATION. Point the map at a kind nobody wrote a rubric for.

        The projection's own allowlist has to refuse it, and the refusal must
        cost the run nothing but the pack -- a review artifact is not a verdict.
        """
        broken = dict(runner_module.REVIEW_KIND_BY_DELIVERY_KIND)
        broken["new"] = "invented_later"
        with patch.object(runner_module, "REVIEW_KIND_BY_DELIVERY_KIND", broken):
            record, detail = runner_module.run_scenario(
                BOOTSTRAP, run_id="cert-wiring-badkind", revision=REVISION)
        self.assertIsNone(self.store.get("cert-wiring-badkind", now_epoch=0))
        self.assertEqual(record.outcome, "pass")
        self.assertIn("kind", detail["review_deposit"])

    def test_a_refused_deposit_never_changes_the_verdict_or_the_counts(self):
        class RefusingStore(input_handoff.TransientReviewStore):
            def deposit(self, run_id, messages, *, now_epoch):
                raise input_handoff.ReviewProjectionRefused("refused on purpose")

        clean, _ = runner_module.run_scenario(
            BOOTSTRAP, run_id="cert-wiring-clean", revision=REVISION)
        with patch.object(lifecycle, "_DEFAULT_REVIEW_STORE", RefusingStore()):
            refused, detail = runner_module.run_scenario(
                BOOTSTRAP, run_id="cert-wiring-refused", revision=REVISION)
        self.assertEqual(refused.outcome, clean.outcome)
        self.assertEqual(dict(refused.counts), dict(clean.counts))
        self.assertEqual(detail["review_deposit"], "refused on purpose")

    def test_a_deposit_failure_that_is_not_a_refusal_is_never_swallowed(self):
        """A name this module does not own must fail LOUDLY.

        Broad ``except Exception`` around a deposit would turn a NameError into
        a tidy-looking run with no pack, which is indistinguishable from a run
        that captured nothing.
        """
        class ExplodingStore(input_handoff.TransientReviewStore):
            def deposit(self, run_id, messages, *, now_epoch):
                raise NameError("someone renamed a field")

        with patch.object(lifecycle, "_DEFAULT_REVIEW_STORE", ExplodingStore()):
            with self.assertRaises(NameError):
                runner_module.run_scenario(
                    BOOTSTRAP, run_id="cert-wiring-explode", revision=REVISION)


class ReviewInputEndToEndTests(unittest.TestCase):
    """prepare -> run -> review-input, against the REAL default store."""

    def setUp(self):
        self.ledger = ledger_module.InMemoryRunLedger()
        self.store = install_review_store(self)

    def _drive(self, run_id):
        body = {"scenarioId": BOOTSTRAP, "runId": run_id,
                "expectedRevision": REVISION}
        prepared, code = lifecycle.prepare(
            body, caller_identity_digest="c" * 64, ledger=self.ledger,
            environ=TWIN_ENV)
        self.assertEqual(code, 200, prepared)
        result, code = lifecycle.run(
            body, caller_identity_digest="c" * 64, ledger=self.ledger,
            environ=TWIN_ENV)
        self.assertEqual(code, 200, result)
        return result

    def test_review_input_returns_the_pack_the_run_produced(self):
        result = self._drive("cert-wiring-e2e")
        self.assertEqual(result["verdict"], "PASS")

        payload, code = lifecycle.review_input(
            {"runId": "cert-wiring-e2e", "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=self.ledger)
        self.assertEqual(code, 200, payload)
        self.assertNotEqual(payload.get("reason"), "no_review_pending")
        self.assertEqual(payload["state"], "AWAITING_REVIEW")
        self.assertEqual(len(payload["messages"]),
                         result["counts"]["captured_outreach"])
        self.assertEqual([m["ordinal"] for m in payload["messages"]],
                         list(range(1, len(payload["messages"]) + 1)))
        self.assertRegex(payload["reviewSetDigest"], r"^[0-9a-f]{64}$")

    def test_the_execution_phase_is_announced_before_the_runner_is_entered(self):
        """The fact ``ambiguous`` is derived from, pinned where it is created.

        ``recovery_observation`` reads "an execution phase was announced and
        never concluded" off the row. That reading is only honest if the phase
        is written BEFORE the work it names -- otherwise a worker that died
        mid-execution leaves a row indistinguishable from one that died before
        executing, and recovery would clean up after a run that may have caused
        an effect.
        """
        seen = {}
        real = runner_module.run_scenario

        def watched(scenario_id, *, run_id, revision):
            seen["state"] = self.ledger.state(run_id)
            seen["phases"] = list(self.ledger.export()[run_id]["phases"])
            return real(scenario_id, run_id=run_id, revision=revision)

        with patch.object(runner_module, "run_scenario", watched):
            self._drive("cert-wiring-phase-first")

        self.assertEqual(seen["state"], ledger_module.RUNNING)
        self.assertEqual(seen["phases"], ["execute"])

    def test_the_run_that_produced_the_pack_is_still_a_pass_with_its_counts(self):
        result = self._drive("cert-wiring-e2e-counts")
        self.assertEqual(result["verdict"], "PASS")
        for name, want in (("captured_outreach", 1), ("fixture_audit", 1),
                           ("fixture_followup", 1), ("fixture_thread_index", 1),
                           ("replay_delta", 0), ("cleanup_residue", 0),
                           ("graph_network", 0), ("nonfixture_write", 0),
                           ("bcc", 0)):
            self.assertEqual(result["counts"][name], want, name)


# ---------------------------------------------------------------------------
# Gap 2: the ledger can be observed
# ---------------------------------------------------------------------------


class RecoveryObservationIsSharedTests(unittest.TestCase):

    def test_both_ledgers_hold_the_same_function_object(self):
        """Not "both have one" -- the SAME one. A copy agrees only until edited."""
        self.assertIs(ledger_module.InMemoryRunLedger.recovery_observation,
                      ledger_module.FirestoreRunLedger.recovery_observation)
        self.assertIs(ledger_module.InMemoryRunLedger.recovery_observation,
                      ledger_module._RunLedgerStateMachine.recovery_observation)

    def test_neither_subclass_decides_anything_about_the_observation(self):
        """Each supplies raw storage facts; the shared machine judges them."""
        for cls in (ledger_module.InMemoryRunLedger,
                    ledger_module.FirestoreRunLedger):
            with self.subTest(cls=cls.__name__):
                self.assertIn("_recovery_facts", cls.__dict__)
                self.assertNotIn("recovery_observation", cls.__dict__)


class InMemoryObservationTests(unittest.TestCase):
    """What a process-scoped ledger may and may not claim."""

    def test_an_unknown_run_has_no_observation_at_all(self):
        self.assertIsNone(
            ledger_module.InMemoryRunLedger().recovery_observation("nope"))

    def test_the_unprovable_gates_are_none_and_never_zero(self):
        ledger = ledger_module.InMemoryRunLedger()
        drive_to_claimed(ledger, "cert-wiring-mem")
        observed = ledger.recovery_observation("cert-wiring-mem")
        self.assertEqual(observed["state"], ledger_module.CLAIMED)
        for gate in ("recordAgeSeconds", "leaseExpired", "inFlight"):
            self.assertIsNone(observed[gate], gate)

    def test_recovery_names_the_missing_fact_rather_than_the_missing_answer(self):
        """"the store answered and had no age" is not "there was no answer"."""
        ledger = ledger_module.InMemoryRunLedger()
        drive_to_claimed(ledger, "cert-wiring-mem-recover")
        payload, code = lifecycle.recover(
            {"runId": "cert-wiring-mem-recover"},
            caller_identity_digest="c" * 64, ledger=ledger)
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "record_age_unprovable")
        self.assertEqual(ledger.state("cert-wiring-mem-recover"),
                         ledger_module.CLAIMED)


class DurableObservationTests(unittest.TestCase):
    """Every value comes from the STORE, and the caller cannot reach any of them."""

    def setUp(self):
        self.store = ClockedFirestore()
        self.ledger = durable_ledger(self.store)

    def test_the_age_is_measured_between_two_server_assigned_timestamps(self):
        drive_to_claimed(self.ledger, "cert-wiring-age")
        self.store.advance(1000)
        observed = self.ledger.recovery_observation("cert-wiring-age")
        self.assertEqual(observed["state"], ledger_module.CLAIMED)
        self.assertEqual(observed["recordAgeSeconds"], 1000)

    def test_a_caller_cannot_declare_its_own_run_old_enough(self):
        """A field named like the gate, planted in the row, changes nothing."""
        drive_to_claimed(self.ledger, "cert-wiring-planted")
        self.store.docs["certificationRuns/cert-wiring-planted"][
            "recordAgeSeconds"] = 99_999
        self.store.advance(30)
        observed = self.ledger.recovery_observation("cert-wiring-planted")
        self.assertEqual(observed["recordAgeSeconds"], 30)

    def test_the_lease_expires_at_the_request_ceiling_and_not_before(self):
        drive_to_claimed(self.ledger, "cert-wiring-lease")
        ceiling = ledger_module.WORKER_REQUEST_CEILING_SECONDS
        self.store.advance(ceiling - 1)
        self.assertFalse(
            self.ledger.recovery_observation("cert-wiring-lease")["leaseExpired"])
        self.store.advance(1)
        self.assertTrue(
            self.ledger.recovery_observation("cert-wiring-lease")["leaseExpired"])

    def test_in_flight_is_zero_only_once_no_request_could_still_be_writing(self):
        drive_to_claimed(self.ledger, "cert-wiring-inflight")
        ceiling = ledger_module.WORKER_REQUEST_CEILING_SECONDS
        self.store.advance(ceiling - 1)
        self.assertIsNone(
            self.ledger.recovery_observation("cert-wiring-inflight")["inFlight"],
            "a request that may still be alive was scored as quiescent")
        self.store.advance(1)
        self.assertEqual(
            self.ledger.recovery_observation("cert-wiring-inflight")["inFlight"], 0)

    def test_a_recorded_execution_phase_that_never_concluded_is_ambiguous(self):
        drive_to_claimed(self.ledger, "cert-wiring-ambiguous", running=True)
        self.store.advance(10_000)
        self.assertEqual(
            self.ledger.recovery_observation("cert-wiring-ambiguous")["ambiguous"], 1)

    def test_a_run_that_never_announced_a_phase_is_not_ambiguous(self):
        drive_to_claimed(self.ledger, "cert-wiring-notambiguous")
        self.store.advance(10_000)
        self.assertEqual(
            self.ledger.recovery_observation(
                "cert-wiring-notambiguous")["ambiguous"], 0)

    def test_an_unknown_run_has_no_observation_at_all(self):
        self.assertIsNone(self.ledger.recovery_observation("never-prepared"))

    def test_every_observation_read_carries_a_deadline(self):
        """The double refuses a deadline-less read; in production it would hang."""
        drive_to_claimed(self.ledger, "cert-wiring-deadline")
        self.assertIsNotNone(
            self.ledger.recovery_observation("cert-wiring-deadline"))

    def test_a_store_that_stamps_nothing_is_unprovable_rather_than_fresh(self):
        drive_to_claimed(self.ledger, "cert-wiring-nostamp")
        self.store.update_times.clear()
        observed = self.ledger.recovery_observation("cert-wiring-nostamp")
        self.assertEqual(observed["state"], ledger_module.CLAIMED)
        self.assertIsNone(observed["recordAgeSeconds"])
        self.assertIsNone(observed["leaseExpired"])

    def test_a_clock_that_ran_backwards_is_unprovable_rather_than_zero(self):
        drive_to_claimed(self.ledger, "cert-wiring-backwards")
        self.store.advance(-60)
        self.assertIsNone(
            self.ledger.recovery_observation(
                "cert-wiring-backwards")["recordAgeSeconds"])


class DurableRecoveryDriveTests(unittest.TestCase):
    """The point of the track: recover, driven, against the durable ledger."""

    def setUp(self):
        self.store = ClockedFirestore()
        self.ledger = durable_ledger(self.store)

    class _Clock:
        def __init__(self):
            self.now = 1_000_000.0

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.now += float(seconds)

    def _recover(self, run_id, **kwargs):
        clock = self._Clock()
        return lifecycle.recover(
            {"runId": run_id}, caller_identity_digest="c" * 64,
            ledger=self.ledger, clock=clock.time, sleeper=clock.sleep, **kwargs)

    def test_an_interrupted_claim_terminalizes_as_instrument_blocked(self):
        drive_to_claimed(self.ledger, "cert-wiring-recovered")
        self.store.advance(10_000)
        payload, code = self._recover(
            "cert-wiring-recovered", cleaner=lambda run_id: {"fixtureRows": 0})
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["verdict"], "INSTRUMENT_BLOCKED")
        self.assertNotEqual(payload["verdict"], "NOT_TESTED")
        self.assertEqual(payload["failureCode"], "recovered_after_interruption")
        self.assertFalse(payload["quarantined"])
        self.assertTrue(payload["residueProven"])
        self.assertEqual(self.ledger.verdict("cert-wiring-recovered"),
                         "INSTRUMENT_BLOCKED")

    def test_an_interrupted_execution_quarantines_instead_of_cleaning(self):
        drive_to_claimed(self.ledger, "cert-wiring-quarantine", running=True)
        self.store.advance(10_000)
        cleaned = []
        payload, code = self._recover(
            "cert-wiring-quarantine",
            cleaner=lambda run_id: cleaned.append(run_id) or {"fixtureRows": 0})
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["verdict"], "INSTRUMENT_BLOCKED")
        self.assertEqual(payload["failureCode"], "ambiguous_provider_effect")
        self.assertTrue(payload["quarantined"])
        self.assertEqual(cleaned, [], "an ambiguous effect was cleaned up")

    def test_a_record_short_of_the_threshold_is_still_refused(self):
        drive_to_claimed(self.ledger, "cert-wiring-young")
        self.store.advance(lifecycle.RECOVERY_MIN_RECORD_AGE_SECONDS - 1)
        payload, code = self._recover("cert-wiring-young")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "record_too_recent")
        self.assertEqual(self.ledger.state("cert-wiring-young"),
                         ledger_module.CLAIMED)

    def test_a_record_that_moved_between_the_two_reads_is_a_race(self):
        drive_to_claimed(self.ledger, "cert-wiring-raced")
        self.store.advance(10_000)
        reads = {"n": 0}
        real_get = _DocumentRef.get

        def moving_get(ref, *args, **kwargs):
            snapshot = real_get(ref, *args, **kwargs)
            reads["n"] += 1
            # Read 1 is recover's own state check; read 2 is the first
            # observation. The record is touched right after it, so the second
            # observation sees a record somebody else is working on.
            if reads["n"] == 2:
                self.store.update_times[
                    "certificationRuns/cert-wiring-raced"] = self.store.now()
            return snapshot

        with patch.object(_DocumentRef, "get", moving_get):
            payload, code = self._recover("cert-wiring-raced")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "recovery_raced")

    def test_the_response_carries_no_fixture_value(self):
        import json
        drive_to_claimed(self.ledger, "cert-wiring-sanitized")
        self.store.advance(10_000)
        payload, _ = self._recover(
            "cert-wiring-sanitized", cleaner=lambda run_id: {"fixtureRows": 0})
        blob = json.dumps(payload, sort_keys=True)
        for forbidden in ("@", "broker", "100 Fixture Way", "cert-uid-0001"):
            self.assertNotIn(forbidden, blob)


class RecoveryCeilingConstantTests(unittest.TestCase):

    def test_the_ceiling_is_declared_once_and_the_lifecycle_reads_it(self):
        """MUTATION. One declaration, or the two drift the day one is edited."""
        self.assertEqual(ledger_module.WORKER_REQUEST_CEILING_SECONDS, 540)
        self.assertEqual(lifecycle.SERVICE_TIMEOUT_SECONDS,
                         ledger_module.WORKER_REQUEST_CEILING_SECONDS)
        self.assertEqual(lifecycle.RECOVERY_MIN_RECORD_AGE_SECONDS,
                         ledger_module.WORKER_REQUEST_CEILING_SECONDS
                         + lifecycle.RECOVERY_AGE_MARGIN_SECONDS)


# ---------------------------------------------------------------------------
# Gap 3: recovery does NOT emit evidence records, and why
# ---------------------------------------------------------------------------
#
# The approved plan enumerates `recovery`, `quiescing` and `residue_readback`
# among its phases. They are deliberately NOT in ``evidence.ALLOWED_PHASES``,
# and the reason is a property recover actually holds today rather than a
# preference:
#
# ``project_evidence`` cannot build a record without ``scenario_id`` and
# ``revision``. ``recover`` reads exactly ONE value from its caller -- the run
# id -- so it holds neither. Taking them from the caller would let a caller
# decide what a recovered run's permanent evidence says it was. Taking them from
# the ledger would widen the allowlisted call graph of the one function whose
# entire guarantee is that its call graph is small, approved, and contains no
# execution. Neither price is worth three phase names.
#
# So recovery digests SANITIZED RECOVERY FACTS instead, and terminalizes as
# INSTRUMENT_BLOCKED -- the honest record. The two tests below are what stop
# that decision from being quietly reversed: the first pins that the phases are
# absent, the second pins the reason they are absent.


class RecoveryEvidencePhaseDecisionTests(unittest.TestCase):

    PLANNED_RECOVERY_PHASES = ("recovery", "quiescing", "residue_readback")

    def test_the_recovery_phases_are_not_evidence_phases(self):
        for phase in self.PLANNED_RECOVERY_PHASES:
            with self.subTest(phase=phase):
                self.assertNotIn(phase, ev.ALLOWED_PHASES)
                with self.assertRaises(ev.EvidenceProjectionError):
                    ev.project_evidence(
                        run_id="cert-wiring-phase", scenario_id=BOOTSTRAP,
                        revision=REVISION, outcome="instrument_blocked",
                        phase=phase)

    def test_every_shipped_phase_still_bites(self):
        """The pin above must not have been bought by loosening the check."""
        for phase in ev.ALLOWED_PHASES:
            self.assertEqual(ev.safe_phase("phase", phase), phase)
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.safe_phase("phase", "invented_later")

    def test_recover_reads_exactly_one_value_from_its_caller(self):
        """The reason the phases stay out. If this stops holding, revisit.

        A body carrying nothing but a run id must behave identically to a body
        carrying a scenario id and a revision -- because recovery reads neither.
        """
        outcomes = []
        for body in ({"runId": "cert-wiring-onlyrunid"},
                     {"runId": "cert-wiring-onlyrunid",
                      "scenarioId": "attacker-chosen",
                      "expectedRevision": "0" * 40}):
            store = ClockedFirestore()
            ledger = durable_ledger(store)
            drive_to_claimed(ledger, "cert-wiring-onlyrunid")
            store.advance(10_000)
            clock = DurableRecoveryDriveTests._Clock()
            payload, code = lifecycle.recover(
                body, caller_identity_digest="c" * 64, ledger=ledger,
                clock=clock.time, sleeper=clock.sleep,
                cleaner=lambda run_id: {"fixtureRows": 0})
            outcomes.append((payload, code))
        self.assertEqual(outcomes[0], outcomes[1])


# ---------------------------------------------------------------------------
# The refutation scenario is unchanged by any of this
# ---------------------------------------------------------------------------


class RefutationStillFailsTests(unittest.TestCase):

    def test_the_impossible_oracle_still_fails_with_zero_residue(self):
        install_review_store(self)
        record, detail = runner_module.run_scenario(
            REFUTATION, run_id="cert-wiring-refutation", revision=REVISION)
        self.assertEqual(record.outcome, "fail")
        self.assertEqual(record.failure_code, "oracle_contradicted")
        self.assertEqual(detail["observed"]["cleanup_residue"], 0)


if __name__ == "__main__":
    unittest.main()
