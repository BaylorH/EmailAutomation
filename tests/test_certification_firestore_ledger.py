"""The DURABLE certification run ledger, and what a transaction has to buy.

``InMemoryRunLedger`` enforces the whole state machine, but it is process-scoped:
every property that matters across a restart or across two instances is, for it,
untestable by construction. ``FirestoreRunLedger`` is the same machine over a
store that outlives the process, and this module is where the across-process
half is actually exercised:

* a run id consumed permanently, seen by a SECOND ledger instance;
* a terminal record that a retry converges on and a rewrite cannot touch;
* forward-only transitions;
* the one-use authorization consumed in the SAME commit that advances to
  CLAIMED, so there is never a moment where the claim is visible and the
  authorization is still spendable;
* durable rows that hold states, phases, counts and digests and nothing else.

WHAT THE DOUBLE IN THIS FILE IS. ``FakeFirestore`` is an in-process store shaped
like the Firestore API the ledger calls, driven by the REAL
``google.cloud.firestore.transactional`` decorator. So the decorator's retry
loop, its ``Aborted``-on-conflict retry, and its rollback-on-exception path are
the library's own code, not a reimplementation. The store contributes
optimistic concurrency: a commit whose read set moved raises the real
``google.api_core.exceptions.Aborted``.

WHAT IT CANNOT PROVE. It is not Firestore. It does not prove Firestore's own
serialization or contention behaviour, its 500-write / 10MiB commit limits, its
field type coercion, or that a deadline is honoured by the gRPC layer. What it
CAN prove about deadlines is narrower and stated as such: the double refuses a
read that carries no deadline at all, because in production such a read HANGS
rather than errors, and a hang is not catchable by the ``except Exception`` that
wraps these call sites. Every claim below is scoped to that.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from google.api_core import exceptions as gcp_exceptions

from email_automation.certification import ledger as lg
from email_automation.certification import models as m


# ---------------------------------------------------------------------------
# The double
# ---------------------------------------------------------------------------


class DeadlinelessRead(AssertionError):
    """A read with no deadline. In production this HANGS; here it is loud.

    Modelled as an error rather than a slow path on purpose: a hang cannot be
    caught by the broad ``except Exception`` that wraps every call site in this
    codebase, so it would surface as a stuck process, not as a failing test.
    """


class NonTransactionalWrite(AssertionError):
    """A durable write made outside a transaction."""


class _Snapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data
        self.exists = data is not None

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
            raise DeadlinelessRead(f"read of {self._path} carried no deadline")
        self._store.reads.append(self._path)
        version, data = self._store.snapshot(self._path)
        if transaction is not None:
            transaction._note_read(self._path, version)
            self._store.fire_read_hook(self._path)
        return _Snapshot(self, data)

    # Writes outside a transaction are refused: every durable mutation this
    # ledger makes has to be part of a commit, and a helper that quietly wrote
    # directly would defeat every atomicity property below.
    def set(self, *_args, **_kwargs):
        raise NonTransactionalWrite(f"{self._path} written outside a transaction")

    update = delete = create = set


class _CollectionRef:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _DocumentRef(self._store, f"{self._name}/{doc_id}")

    def stream(self, timeout=None, **kwargs):
        if timeout is None:
            raise DeadlinelessRead(f"stream of {self._name} carried no deadline")
        prefix = f"{self._name}/"
        for path in sorted(self._store.docs):
            if path.startswith(prefix):
                yield _Snapshot(_DocumentRef(self._store, path), self._store.docs[path])


class _Transaction:
    """Duck-typed to what the real ``_Transactional`` driver actually touches."""

    def __init__(self, store, max_attempts=5, read_only=False):
        self._store = store
        self._max_attempts = max_attempts
        self._read_only = read_only
        self._id = None
        self._reads = {}
        self._writes = []

    @property
    def in_progress(self):
        return self._id is not None

    def _clean_up(self):
        self._id = None
        self._reads = {}
        self._writes = []

    def _begin(self, retry_id=None):
        self._store.begins += 1
        self._id = b"tx-%d" % self._store.begins

    def _rollback(self):
        self._store.rollbacks += 1
        self._clean_up()

    def _commit(self):
        if not self.in_progress:
            raise ValueError("cannot commit a transaction that has not begun")
        self._store.commit(self._reads, self._writes)
        self._clean_up()
        return []

    def _note_read(self, path, version):
        self._reads.setdefault(path, version)

    def set(self, ref, data, merge=False):
        self._writes.append(("set", ref._path, copy.deepcopy(data)))

    def delete(self, ref):
        self._writes.append(("delete", ref._path, None))


class FakeFirestore:
    """A store shaped like the Firestore surface the ledger calls."""

    def __init__(self):
        self.docs = {}
        self.versions = {}
        self.reads = []
        self.commits = 0
        self.begins = 0
        self.rollbacks = 0
        self.aborts = 0
        self._read_hook = None
        self._read_hook_after = 0
        self.history = []

    # -- store ------------------------------------------------------------

    def snapshot(self, path):
        return self.versions.get(path, 0), self.docs.get(path)

    def commit(self, reads, writes):
        for path, version in reads.items():
            if self.versions.get(path, 0) != version:
                self.aborts += 1
                raise gcp_exceptions.Aborted(f"read set moved under {path}")
        for kind, path, data in writes:
            if kind == "set":
                self.docs[path] = copy.deepcopy(data)
            else:
                self.docs.pop(path, None)
            self.versions[path] = self.versions.get(path, 0) + 1
        self.commits += 1
        self.history.append(copy.deepcopy(self.docs))

    # -- hooks used to interleave two callers -----------------------------

    def arm_after_reads(self, hook, after=1):
        """Fire ``hook`` once, after the ``after``-th transactional read.

        Where the hook fires decides WHICH refusal a losing caller hits, and
        both matter: fire between the two reads and the loser finds the
        authorization already consumed; fire after both and the loser's commit
        aborts on a moved read set and the retry finds the run already CLAIMED.
        """
        self._read_hook = hook
        self._read_hook_after = after

    def fire_read_hook(self, path):
        if self._read_hook is None:
            return
        self._read_hook_after -= 1
        if self._read_hook_after > 0:
            return
        hook, self._read_hook = self._read_hook, None
        hook(path)

    # -- client surface ---------------------------------------------------

    def collection(self, name):
        return _CollectionRef(self, name)

    def transaction(self, **kwargs):
        return _Transaction(self, **kwargs)


# ---------------------------------------------------------------------------
# Shared fixtures. The authorization is the one the existing suite already pins.
# ---------------------------------------------------------------------------

VALID_AUTHORIZATION = {
    "scenario_id": "campaign-one-property",
    "run_id": "cert-auth-0001",
    "source_revision": "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9",
    "image_digest": "sha256:" + "b" * 64,
    "certification_service": "process-user-certification",
    "certification_revision": "process-user-certification-00001-abc",
    "production_candidate_revision": "process-user-00042-xyz",
    "caller_identity_digest": "c" * 64,
    "fixture_config_secret_version": "7",
    "fixture_config_digest": "d" * 64,
    "scenario_registry_digest": "e" * 64,
    "launch_class": "agent_safe",
    "input_producer_kind": "backend_registry_v1",
    "canonical_input_digest": "f" * 64,
    "input_producer_artifact_digest": "0" * 64,
    "authorization_expires_at": "2026-08-19T00:00:00Z",
}


class LedgerCase(unittest.TestCase):
    """Everything below drives BOTH ledgers off the same helpers."""

    def setUp(self):
        self.store = FakeFirestore()

    def firestore_ledger(self, **kwargs):
        return lg.FirestoreRunLedger(self.store, **kwargs)

    def request(self, run_id="cert-auth-0001", scenario_id="campaign-one-property"):
        return m.CertificationRequest(
            scenario_id=scenario_id,
            run_id=run_id,
            expected_revision=VALID_AUTHORIZATION["source_revision"],
        )

    def auth(self, **overrides):
        return m.RunAuthorization.create(**{**VALID_AUTHORIZATION, **overrides})


class FirestoreLedgerWalksTheMachineTests(LedgerCase):
    """The whole machine, over a store, with every read carrying a deadline."""

    def test_the_happy_path_walks_the_whole_machine(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        self.assertEqual(ledger.state(request.run_id), "PREPARING")
        ledger.mark_prepared(request, self.auth())
        self.assertEqual(ledger.state(request.run_id), "PREPARED")
        ledger.claim(request, self.auth())
        self.assertEqual(ledger.state(request.run_id), "CLAIMED")
        for phase in ("fixture_open", "seed", "execute", "replay", "cleanup"):
            ledger.mark_running(request.run_id, phase)
        self.assertTrue(ledger.record_terminal(request.run_id, "PASS", "d" * 64))
        self.assertEqual(ledger.state(request.run_id), "TERMINAL")
        self.assertEqual(ledger.verdict(request.run_id), "PASS")

    def test_an_unknown_run_has_no_state_rather_than_a_guessed_one(self):
        self.assertIsNone(self.firestore_ledger().state("never-seen"))

    def test_every_read_carries_an_explicit_deadline(self):
        """A deadline-less Firestore read HANGS, and a hang is not catchable.

        The double refuses one outright so the omission is a failing test rather
        than a stuck process.
        """
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        ledger.mark_running(request.run_id, "execute")
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        ledger.append_cleanup_result(request.run_id, "e" * 64, {"residue": 0})
        ledger.state(request.run_id)
        ledger.verdict(request.run_id)
        ledger.peek_ephemeral(request.run_id)
        ledger.export()
        self.assertTrue(self.store.reads)

    def test_a_ledger_without_a_usable_deadline_is_refused_at_construction(self):
        """Mutation evidence for the deadline pin: each bad value is refused."""
        for bad in (None, 0, -1, "20"):
            with self.assertRaises(ValueError):
                self.firestore_ledger(deadline_seconds=bad)

    def test_a_ledger_without_an_explicit_client_is_refused(self):
        """No ambient reach. Ten modules import ``clients._fs`` BY VALUE and
        patch their own copy; a ledger that resolved one canonical global would
        silently disagree with whichever binding its caller had patched. So the
        client is a constructor argument and there is no fallback to resolve."""
        with self.assertRaises(ValueError):
            lg.FirestoreRunLedger(None)


class RunIdsAreSingleUseAcrossInstancesTests(LedgerCase):
    """The property the in-memory ledger structurally cannot demonstrate.

    ``InMemoryRunLedger`` keeps its rows in one process, so "a run id is
    consumed permanently" only ever means "for as long as this process lives".
    Two ledger instances over one store is the smallest honest simulation of two
    processes, and it is the only place the word "permanently" can be checked.
    """

    def _terminalized(self, ledger, request):
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)

    def test_a_second_instance_refuses_a_run_id_the_first_one_used(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        first.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            second.begin_preparing(request)

    def test_a_terminalized_run_id_is_still_consumed_for_a_second_instance(self):
        """Terminal is not a release. A finished run's id is the cheapest way to
        make one authorization cover a second execution, so it stays spent."""
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._terminalized(first, request)
        with self.assertRaises(lg.LedgerStateError):
            second.begin_preparing(self.request())

    def test_a_second_instance_reads_the_state_the_first_one_wrote(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        first.begin_preparing(request)
        self.assertEqual(second.state(request.run_id), "PREPARING")
        first.mark_prepared(request, self.auth())
        self.assertEqual(second.state(request.run_id), "PREPARED")
        self.assertIsNotNone(second.peek_ephemeral(request.run_id))
        first.claim(request, self.auth())
        self.assertEqual(second.state(request.run_id), "CLAIMED")
        self.assertIsNone(second.peek_ephemeral(request.run_id))

    def test_a_run_prepared_by_one_instance_is_claimable_by_the_other_exactly_once(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        first.begin_preparing(request)
        first.mark_prepared(request, self.auth())
        claimed = second.claim(request, self.auth())
        self.assertEqual(claimed.run_id, request.run_id)
        with self.assertRaises(lg.LedgerStateError):
            first.claim(request, self.auth())


class TransitionsAreMonotonicTests(LedgerCase):
    """Forward only. A backwards step is how a claimed run runs twice."""

    def test_state_never_moves_backwards(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.mark_prepared(request, self.auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.begin_preparing(request)

    def test_a_run_cannot_be_claimed_before_it_is_prepared(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(request, self.auth())

    def test_only_a_claimed_run_may_run(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            ledger.mark_running(request.run_id, "execute")

    def test_a_terminal_run_accepts_no_further_phase(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        with self.assertRaises(lg.LedgerStateError):
            ledger.mark_running(request.run_id, "execute")

    def test_an_unknown_run_is_not_transitionable(self):
        ledger = self.firestore_ledger()
        with self.assertRaises(lg.LedgerStateError):
            ledger.mark_running("never-seen", "execute")
        with self.assertRaises(lg.LedgerStateError):
            ledger.record_terminal("never-seen", "PASS", "d" * 64)
        with self.assertRaises(lg.LedgerStateError):
            ledger.append_cleanup_result("never-seen", "e" * 64, {"residue": 0})


class TheAuthorizationIsRevalidatedAtTheBoundaryTests(LedgerCase):
    """A durable store makes the stored authorization editable between the
    prepare and the claim, so the claim rechecks rather than trusting."""

    def test_a_claim_whose_authorization_mismatches_the_request_is_refused(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        with self.assertRaises(m.AuthorizationInvalid):
            ledger.claim(request, self.auth(scenario_id="some-other-scenario"))
        self.assertEqual(ledger.state(request.run_id), "PREPARED")
        self.assertIsNotNone(ledger.peek_ephemeral(request.run_id))

    def test_a_claim_against_a_different_authorization_than_prepared_is_refused(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(request, self.auth(fixture_config_secret_version="8"))

    def test_a_wholesale_substituted_pair_is_caught_by_the_prepared_binding(self):
        """Swapping BOTH request and authorization keeps them consistent with
        each other, so the request binding cannot see it. What refuses it is
        that the run was PREPARED under a different authorization."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(
                self.request(scenario_id="some-other-scenario"),
                self.auth(scenario_id="some-other-scenario"),
            )

    def test_a_stored_authorization_edited_in_the_database_is_refused(self):
        """The case only a DURABLE ledger has: the record sat in a database
        between prepare and claim and a field was changed without recomputing
        the digest. It fails on the way back in, and does NOT read as
        'never prepared' -- those are different facts."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        path = f"{lg.DEFAULT_AUTHORIZATIONS_COLLECTION}/{request.run_id}"
        self.store.docs[path]["source_revision"] = "0" * 40
        with self.assertRaises(m.AuthorizationInvalid):
            ledger.claim(request, self.auth())
        self.assertEqual(ledger.state(request.run_id), "PREPARED")

    def test_a_refused_step_commits_nothing(self):
        """A transaction that raises rolls back, so a refusal leaves no partial
        row behind -- there is no 'half prepared' state to recover from."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        commits_before = self.store.commits
        with self.assertRaises(m.AuthorizationInvalid):
            ledger.claim(request, self.auth(scenario_id="some-other-scenario"))
        self.assertEqual(self.store.commits, commits_before)
        self.assertGreater(self.store.rollbacks, 0)


class ADurableWriteNeverSilentlyPersistsNothingTests(LedgerCase):
    """A missing row reaching the write path must be LOUD.

    ``claim`` and ``record_terminal`` both read a row that may not exist and then
    write it. The shared transition rules refuse a missing row first, so the
    write is unreachable with one -- but "unreachable today" is not a property.
    Every call site in this codebase is wrapped in a broad ``except Exception``,
    so a write that quietly did nothing would surface as a clean early return
    and the caller would believe a terminal record was persisted when none was.
    On the terminal and claim paths that is indistinguishable from success,
    which is the exact failure a durable ledger exists to remove.
    """

    def test_writing_a_missing_row_is_refused_rather_than_skipped(self):
        ledger = self.firestore_ledger()
        transaction = self.store.transaction()
        with self.assertRaises(lg.LedgerStateError) as caught:
            ledger._write_row(transaction, "cert-auth-0001", None)
        self.assertIn("cert-auth-0001", str(caught.exception))
        self.assertEqual(transaction._writes, [], "a refused write buffered a write")

    def test_a_row_carrying_an_unallowed_key_is_refused_at_the_write(self):
        """The sanitization guard runs on the way OUT, immediately before the
        write, so no future edit anywhere upstream can add a durable field
        without this refusing it."""
        ledger = self.firestore_ledger()
        transaction = self.store.transaction()
        row = dict(ledger._new_row(self.request()))
        row["recipient"] = "broker@fixture.example.com"
        with self.assertRaises(lg.LedgerStateError) as caught:
            ledger._write_row(transaction, "cert-auth-0001", row)
        self.assertIn("recipient", str(caught.exception))
        self.assertEqual(transaction._writes, [])


class TerminalRecordingIsIdempotentTests(LedgerCase):
    """A retried write must converge. A rewrite must not be possible.

    Durable storage is what makes the distinction matter: the retry arrives from
    a different process, minutes later, possibly after someone has already read
    the result and acted on it. "Converge" and "overwrite" look identical from
    the caller and are opposite facts.
    """

    def _claimed(self, ledger, request):
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())

    def test_the_first_write_reports_true_and_an_identical_repeat_reports_false(self):
        ledger, request = self.firestore_ledger(), self.request()
        self._claimed(ledger, request)
        self.assertTrue(ledger.record_terminal(request.run_id, "FAIL", "d" * 64))
        self.assertFalse(ledger.record_terminal(request.run_id, "FAIL", "d" * 64))
        self.assertEqual(ledger.verdict(request.run_id), "FAIL")

    def test_a_repeat_from_a_second_instance_also_converges(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._claimed(first, request)
        self.assertTrue(first.record_terminal(request.run_id, "FAIL", "d" * 64))
        self.assertFalse(second.record_terminal(request.run_id, "FAIL", "d" * 64))

    def test_a_repeat_carrying_a_different_verdict_is_refused(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._claimed(first, request)
        first.record_terminal(request.run_id, "FAIL", "d" * 64)
        with self.assertRaises(lg.LedgerStateError):
            second.record_terminal(request.run_id, "PASS", "d" * 64)
        self.assertEqual(first.verdict(request.run_id), "FAIL")

    def test_a_repeat_carrying_a_different_evidence_digest_is_refused(self):
        """Same verdict, different evidence. Accepting it would leave a stamp
        pointing at evidence that is not the evidence the verdict came from."""
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._claimed(first, request)
        first.record_terminal(request.run_id, "PASS", "d" * 64)
        with self.assertRaises(lg.LedgerStateError):
            second.record_terminal(request.run_id, "PASS", "a" * 64)
        row = self.store.docs[f"{lg.DEFAULT_RUNS_COLLECTION}/{request.run_id}"]
        self.assertEqual(row["evidenceDigest"], "d" * 64)

    def test_a_converging_repeat_writes_nothing_at_all(self):
        """Not merely "changes nothing". A repeat that re-set the row would bump
        it under any concurrent reader and could resurrect a consumed
        authorization, so the convergent path commits no write."""
        ledger, request = self.firestore_ledger(), self.request()
        self._claimed(ledger, request)
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        path = f"{lg.DEFAULT_RUNS_COLLECTION}/{request.run_id}"
        version_before = self.store.versions[path]
        self.assertFalse(ledger.record_terminal(request.run_id, "PASS", "d" * 64))
        self.assertEqual(self.store.versions[path], version_before)

    def test_an_unknown_verdict_is_refused_before_a_transaction_is_opened(self):
        ledger, request = self.firestore_ledger(), self.request()
        self._claimed(ledger, request)
        begins_before = self.store.begins
        with self.assertRaises(lg.LedgerStateError):
            ledger.record_terminal(request.run_id, "MOSTLY_PASS", "d" * 64)
        self.assertEqual(self.store.begins, begins_before)

    def test_every_allowed_verdict_is_accepted(self):
        for index, verdict in enumerate(lg.ALLOWED_VERDICTS):
            with self.subTest(verdict=verdict):
                store = FakeFirestore()
                self.store = store
                ledger = self.firestore_ledger()
                request = self.request(run_id=f"cert-auth-000{index}")
                ledger.begin_preparing(request)
                self.assertTrue(
                    ledger.record_terminal(request.run_id, verdict, "d" * 64)
                )

    def test_terminalizing_consumes_the_authorization_in_the_same_commit(self):
        """A run that terminalizes from PREPARED -- an abort -- must not leave a
        spendable authorization behind for a caller who still holds the run id."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        commits_before = self.store.commits
        ledger.record_terminal(request.run_id, "NOT_TESTED", "d" * 64)
        self.assertEqual(self.store.commits, commits_before + 1)
        self.assertIsNone(ledger.peek_ephemeral(request.run_id))


class CleanupEvidenceAppendsTests(LedgerCase):
    """Repair evidence is additive and terminal-only. It never moves a verdict."""

    def _terminal(self, ledger, request, verdict="FAIL"):
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        ledger.record_terminal(request.run_id, verdict, "d" * 64)

    def test_cleanup_evidence_appends_without_changing_the_verdict(self):
        first, second = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._terminal(first, request)
        self.assertTrue(second.append_cleanup_result(request.run_id, "e" * 64, {"residue": 0}))
        self.assertTrue(first.append_cleanup_result(request.run_id, "f" * 64, {"residue": 2}))
        row = first.export()[request.run_id]
        self.assertEqual(row["verdict"], "FAIL")
        self.assertEqual(row["cleanupEvidenceDigests"], ["e" * 64, "f" * 64])
        self.assertEqual(row["residue"], {"residue": 2})

    def test_cleanup_evidence_is_refused_before_the_run_is_terminal(self):
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            ledger.append_cleanup_result(request.run_id, "e" * 64, {"residue": 0})


class _LeakyClaimLedger(lg.FirestoreRunLedger):
    """The bug the atomic claim exists to prevent, written out on purpose.

    Advances to CLAIMED in one transaction and consumes the authorization in a
    second. It is a NEGATIVE CONTROL: the window test below must fail against
    this and pass against the real one, otherwise the test is agreeing with
    itself rather than constraining anything.
    """

    def claim(self, request, authorization):
        def advance(transaction):
            row = self._read_row(request.run_id, transaction)
            prepared = self._read_authorization(request.run_id, transaction)
            claimed = self._apply_claim(row, request, authorization, prepared)
            self._write_row(transaction, request.run_id, row)
            return claimed

        claimed = self._in_transaction(advance)

        def consume(transaction):
            transaction.delete(self._authorization_ref(request.run_id))

        self._in_transaction(consume)
        return claimed


class TheClaimConsumesTheAuthorizationInOneTransactionTests(LedgerCase):
    """The property the plan names: no consumed-without-claim window.

    In memory the advance and the delete happen inside one lock. Over a store
    they have to happen inside one COMMIT. If there is any moment where the
    claim is already visible and the one-use records are still there, a second
    caller can spend the same authorization -- and the whole point of a one-use
    authorization is that it covers exactly one execution.
    """

    RUN_PATH = f"{lg.DEFAULT_RUNS_COLLECTION}/cert-auth-0001"
    AUTH_PATH = f"{lg.DEFAULT_AUTHORIZATIONS_COLLECTION}/cert-auth-0001"

    def _prepared(self, ledger, request):
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())

    def _observable_states(self):
        """Every state an outside reader could have seen, in order.

        The double makes uncommitted writes invisible, so the sequence of
        post-commit snapshots IS the sequence of observable states.
        """
        return [
            ((docs.get(self.RUN_PATH) or {}).get("state"), self.AUTH_PATH in docs)
            for docs in self.store.history
        ]

    def test_the_claim_is_exactly_one_commit(self):
        ledger, request = self.firestore_ledger(), self.request()
        self._prepared(ledger, request)
        commits_before = self.store.commits
        ledger.claim(request, self.auth())
        self.assertEqual(self.store.commits, commits_before + 1)

    def test_no_observable_state_shows_a_claim_beside_a_spendable_authorization(self):
        ledger, request = self.firestore_ledger(), self.request()
        self._prepared(ledger, request)
        ledger.claim(request, self.auth())
        self.assertIn(("PREPARED", True), self._observable_states())
        self.assertIn(("CLAIMED", False), self._observable_states())
        self.assertNotIn(("CLAIMED", True), self._observable_states())

    def test_the_window_test_fails_against_a_claim_split_across_transactions(self):
        """Negative control. Splitting the delete out opens exactly the window,
        which proves the assertion above is constraining the implementation and
        not merely agreeing with it."""
        ledger, request = _LeakyClaimLedger(self.store), self.request()
        self._prepared(ledger, request)
        ledger.claim(request, self.auth())
        self.assertIn(("CLAIMED", True), self._observable_states())

    def test_a_second_caller_can_spend_the_authorization_through_that_window(self):
        """And the window is not cosmetic: while it is open, a second caller
        holding the same authorization reads it straight back out."""
        leaky, request = _LeakyClaimLedger(self.store), self.request()
        honest = self.firestore_ledger()
        self._prepared(leaky, request)

        seen = {}

        def peek_between_the_two_transactions(_path):
            seen["authorization"] = honest.peek_ephemeral(request.run_id)
            seen["state"] = honest.state(request.run_id)

        # Fires after the leaky claim's first transaction has committed, which
        # is the only moment that exists at all in the atomic implementation.
        original_in_transaction = leaky._in_transaction
        calls = []

        def counting_in_transaction(body):
            result = original_in_transaction(body)
            calls.append(body)
            if len(calls) == 1:
                peek_between_the_two_transactions(None)
            return result

        leaky._in_transaction = counting_in_transaction
        leaky.claim(request, self.auth())

        self.assertEqual(seen["state"], "CLAIMED")
        self.assertIsNotNone(
            seen["authorization"],
            "the split claim left a spendable authorization beside a visible claim",
        )

    def test_two_callers_racing_a_claim_between_the_reads_produce_one_winner(self):
        """The competitor commits while the loser is mid-transaction, before the
        loser has read the authorization. The loser finds it already consumed."""
        loser, winner = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._prepared(loser, request)

        outcome = {}

        def competing_claim(_path):
            outcome["winner"] = winner.claim(request, self.auth())

        self.store.arm_after_reads(competing_claim, after=1)
        with self.assertRaises(lg.LedgerStateError):
            loser.claim(request, self.auth())

        self.assertIsNotNone(outcome["winner"])
        self.assertEqual(loser.state(request.run_id), "CLAIMED")
        self.assertIsNone(loser.peek_ephemeral(request.run_id))
        self.assertNotIn(("CLAIMED", True), self._observable_states())

    def test_two_callers_racing_a_claim_after_the_reads_produce_one_winner(self):
        """The competitor commits after the loser has read BOTH documents, so
        the loser's own reads looked consistent. Its commit aborts on the moved
        read set, the driver retries, and the retry re-reads CLAIMED and
        refuses -- rather than overwriting the winner with a stale row."""
        loser, winner = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()
        self._prepared(loser, request)

        outcome = {}

        def competing_claim(_path):
            outcome["winner"] = winner.claim(request, self.auth())

        self.store.arm_after_reads(competing_claim, after=2)
        aborts_before = self.store.aborts
        with self.assertRaises(lg.LedgerStateError):
            loser.claim(request, self.auth())

        self.assertIsNotNone(outcome["winner"])
        self.assertGreater(
            self.store.aborts, aborts_before, "the losing commit did not abort"
        )
        self.assertEqual(loser.state(request.run_id), "CLAIMED")
        self.assertNotIn(("CLAIMED", True), self._observable_states())

    def test_two_callers_racing_begin_preparing_produce_one_winner(self):
        """Single-use has to survive the race too, or two callers both believe
        they own a fresh run id."""
        loser, winner = self.firestore_ledger(), self.firestore_ledger()
        request = self.request()

        def competing_prepare(_path):
            winner.begin_preparing(request)

        self.store.arm_after_reads(competing_prepare, after=1)
        with self.assertRaises(lg.LedgerStateError):
            loser.begin_preparing(request)
        self.assertEqual(loser.state(request.run_id), "PREPARING")


class DurableRowsStaySanitizedTests(LedgerCase):
    """What a permanent row is allowed to hold, checked against a REAL run.

    A hand-built row would only test the test. So this drives the actual
    lifecycle -- the real scenario registry, the real runner, the real evidence
    digest -- into a FirestoreRunLedger and then reads what physically landed in
    the store.

    The stake is specific to durability. A fixture value written to a durable
    ledger outlives both the fixture it came from and the cleanup that erases
    it, which is exactly the residue certification exists to prove absent. An
    in-memory ledger cannot fail this way for long enough to matter; this one
    can, permanently.
    """

    REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
    SCENARIO_ID = "campaign-one-property"
    RUN_ID = "cert-firestore-sanitized-0001"

    def _env(self):
        return {
            "SITESIFT_SOURCE_REVISION": self.REVISION,
            "SITESIFT_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "K_SERVICE": "process-user-certification",
            "K_REVISION": "process-user-certification-00001-abc",
            "SITESIFT_PRODUCTION_CANDIDATE_REVISION": "process-user-00042-xyz",
            "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION": "7",
            "SITESIFT_FIXTURE_CONFIG_DIGEST": "d" * 64,
        }

    def _drive_a_real_run(self):
        from email_automation.certification import lifecycle as lc

        ledger = self.firestore_ledger()
        body = {
            "scenarioId": self.SCENARIO_ID,
            "runId": self.RUN_ID,
            "expectedRevision": self.REVISION,
        }
        prepared, prepared_code = lc.prepare(
            body, caller_identity_digest="c" * 64, ledger=ledger, environ=self._env()
        )
        self.assertEqual(prepared_code, 200, prepared)
        ran, ran_code = lc.run(
            {"runId": self.RUN_ID, "expectedRevision": self.REVISION},
            ledger=ledger,
            environ=self._env(),
        )
        self.assertEqual(ran_code, 200, ran)
        return ledger, prepared, ran

    def _forbidden_values(self):
        """Every fixture identity the run could have leaked, from the REAL
        fixture module and the REAL registry entry -- not a guessed list."""
        from email_automation.certification import fixtures as fx
        from email_automation.certification import scenarios

        scenario = scenarios.get(self.SCENARIO_ID)
        return [
            fx.FIXTURE_UID,
            fx.FIXTURE_CLIENT,
            fx.FIXTURE_SHEET,
            fx.FIXTURE_RECIPIENT,
            fx.FIXTURE_SENDER,
            fx.FIXTURE_PREFIX,
            scenario["logicalFixtureKey"],
            scenario["oracleProjectionKey"],
        ]

    def test_a_real_run_persists_only_allowlisted_keys(self):
        ledger, _prepared, _ran = self._drive_a_real_run()
        ledger.append_cleanup_result(self.RUN_ID, "e" * 64, {"nonfixture_write": 0})
        row = ledger.export()[self.RUN_ID]
        self.assertEqual(sorted(row), sorted(lg.DURABLE_ROW_KEYS))
        self.assertEqual(row["state"], "TERMINAL")
        self.assertRegex(row["evidenceDigest"], r"^[0-9a-f]{64}$")

    def test_a_real_run_leaves_no_fixture_identity_in_the_store(self):
        ledger, _prepared, _ran = self._drive_a_real_run()
        ledger.append_cleanup_result(self.RUN_ID, "e" * 64, {"nonfixture_write": 0})
        blob = json.dumps(self.store.docs, sort_keys=True, default=str)
        for forbidden in self._forbidden_values():
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached the ledger")
        for forbidden in ("@", "Hi Pat", "100 Fixture Way", "Traceback"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached the ledger")

    def test_no_committed_snapshot_ever_held_a_fixture_identity(self):
        """Scanning only the final store misses a value that was written and
        then overwritten -- and a durable row that briefly held a recipient was
        still readable, replicated, and backed up while it did.

        Found the hard way: a mutation that seeded a fixture recipient into the
        row's residue field passed the end-state scan, because the cleanup step
        overwrote that field before the scan ran.
        """
        ledger, _prepared, _ran = self._drive_a_real_run()
        ledger.append_cleanup_result(self.RUN_ID, "e" * 64, {"nonfixture_write": 0})
        self.assertTrue(self.store.history, "no committed snapshot was recorded")
        forbidden = self._forbidden_values() + ["@", "Hi Pat", "100 Fixture Way",
                                                "Traceback"]
        for index, snapshot in enumerate(self.store.history):
            blob = json.dumps(snapshot, sort_keys=True, default=str)
            for value in forbidden:
                self.assertNotIn(
                    value, blob, f"{value!r} was readable in commit {index}"
                )

    def test_the_residue_scan_would_catch_a_fixture_identity(self):
        """Negative control for the scan itself. A test that only ever looks at
        clean rows cannot tell a working scan from a scan that matches nothing.
        """
        blob = json.dumps({"row": {"recipient": self._forbidden_values()[3]}})
        found = [v for v in self._forbidden_values() if v in blob]
        self.assertTrue(found, "the residue scan matches nothing at all")

    def test_the_allowlist_is_exactly_the_shape_the_machine_creates(self):
        """The allowlist and the row builder must not drift. If a new field is
        added to a row without being allowlisted the write refuses; if it is
        allowlisted without being created, this catches the dead key."""
        row = lg.InMemoryRunLedger()._new_row(self.request())
        self.assertEqual(set(row), set(lg.DURABLE_ROW_KEYS))

    def test_the_in_memory_ledger_holds_the_same_allowlisted_keys(self):
        """The sanitization guard lives on the durable path, but the two ledgers
        must persist the same shape or the swap changes what a row means."""
        ledger, request = lg.InMemoryRunLedger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        ledger.claim(request, self.auth())
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        for row in ledger.export().values():
            self.assertLessEqual(set(row), set(lg.DURABLE_ROW_KEYS))

    def test_the_ephemeral_record_holds_only_authorization_scalars(self):
        """The one-use half is short-lived but it is still written down. Its
        keys are exactly the authorization's own fields and its digest -- there
        is no sealed body, recipient, or oracle riding along."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self.auth())
        stored = self.store.docs[
            f"{lg.DEFAULT_AUTHORIZATIONS_COLLECTION}/{request.run_id}"
        ]
        self.assertEqual(
            set(stored), set(m.AUTHORIZATION_FIELDS) | {"authorization_digest"}
        )

    def test_a_refusal_message_names_states_and_never_a_value(self):
        """Sanitized refusals matter more here than in memory: these strings are
        what a route returns and what an operator pastes into a ticket."""
        ledger, request = self.firestore_ledger(), self.request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError) as caught:
            ledger.claim(request, self.auth())
        message = str(caught.exception)
        self.assertIn("PREPARED", message)
        for forbidden in self._forbidden_values():
            self.assertNotIn(forbidden, message)


class OneStateMachineDrivesBothLedgersTests(LedgerCase):
    """The claim the module docstring makes, turned into something that fails.

    "Both implementations agree by construction rather than by review" is only
    true if there is nothing to review -- if a second copy of the transition
    table does not exist to drift. These tests are what stops a future backend
    from restating it and passing because its own copy happens to match today.
    """

    LEDGER_SOURCE = Path(lg.__file__).read_text(encoding="utf-8")

    def test_both_ledgers_inherit_the_same_machine(self):
        self.assertTrue(issubclass(lg.InMemoryRunLedger, lg._RunLedgerStateMachine))
        self.assertTrue(issubclass(lg.FirestoreRunLedger, lg._RunLedgerStateMachine))

    def test_the_transition_methods_are_the_identical_function_objects(self):
        """Not "equivalent". The same object, so there is no second body that
        could be edited on one side only."""
        for name in (
            "_new_row",
            "_refuse_reuse",
            "_require_row",
            "_require",
            "_advance",
            "_validate_verdict",
            "_apply_prepared",
            "_apply_claim",
            "_apply_running",
            "_apply_terminal",
            "_apply_cleanup",
        ):
            with self.subTest(method=name):
                mine = getattr(lg.InMemoryRunLedger, name)
                theirs = getattr(lg.FirestoreRunLedger, name)
                self.assertIs(
                    getattr(mine, "__func__", mine), getattr(theirs, "__func__", theirs)
                )

    def test_the_transition_table_is_declared_once_and_read_once(self):
        """A source-level pin. Two declarations is exactly the failure the
        docstring names, and it would otherwise be invisible to every
        behavioural test as long as the copies agreed."""
        occurrences = self.LEDGER_SOURCE.count("_ALLOWED_TRANSITIONS")
        self.assertEqual(
            occurrences,
            2,
            "_ALLOWED_TRANSITIONS should appear exactly twice: declared once and "
            f"read once in _advance; found {occurrences}",
        )
        for state in ("PREPARING", "PREPARED", "CLAIMED", "RUNNING", "TERMINAL"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.LEDGER_SOURCE.count(f'"{state}"'),
                    1,
                    f"the literal {state!r} is spelled out more than once; state "
                    "names are constants, not strings each backend retypes",
                )

    def test_the_state_names_are_the_exact_strings_that_get_persisted(self):
        """These are on disk once the ledger is durable. Renaming one silently
        orphans every row already written under the old name."""
        self.assertEqual(lg.PREPARING, "PREPARING")
        self.assertEqual(lg.PREPARED, "PREPARED")
        self.assertEqual(lg.CLAIMED, "CLAIMED")
        self.assertEqual(lg.RUNNING, "RUNNING")
        self.assertEqual(lg.TERMINAL, "TERMINAL")

    def test_the_table_is_the_machine_the_plan_describes(self):
        self.assertEqual(
            {state: sorted(nexts) for state, nexts in lg._ALLOWED_TRANSITIONS.items()},
            {
                "PREPARING": ["PREPARED", "TERMINAL"],
                "PREPARED": ["CLAIMED", "TERMINAL"],
                "CLAIMED": ["RUNNING", "TERMINAL"],
                "RUNNING": ["RUNNING", "TERMINAL"],
                "TERMINAL": [],
            },
        )

    def test_editing_the_one_table_changes_BOTH_ledgers(self):
        """The direct proof, and the mutation evidence for the table pin at
        once: narrow the single table and both implementations refuse the same
        step. If either kept its own copy, only one of them would move."""
        narrowed = dict(lg._ALLOWED_TRANSITIONS)
        narrowed["PREPARED"] = frozenset({lg.TERMINAL})

        def try_claim(ledger):
            request = self.request(run_id="cert-shared-table-0001")
            ledger.begin_preparing(request)
            ledger.mark_prepared(request, self.auth(run_id=request.run_id))
            try:
                ledger.claim(request, self.auth(run_id=request.run_id))
            except lg.LedgerStateError as error:
                return str(error)
            return None

        self.assertIsNone(try_claim(lg.InMemoryRunLedger()))
        self.assertIsNone(try_claim(self.firestore_ledger()))

        original = lg._ALLOWED_TRANSITIONS
        lg._ALLOWED_TRANSITIONS = narrowed
        try:
            self.store = FakeFirestore()
            in_memory_refusal = try_claim(lg.InMemoryRunLedger())
            firestore_refusal = try_claim(self.firestore_ledger())
        finally:
            lg._ALLOWED_TRANSITIONS = original

        self.assertEqual(in_memory_refusal, "refused transition PREPARED -> CLAIMED")
        self.assertEqual(firestore_refusal, in_memory_refusal)


class TheTwoLedgersAgreeStepForStepTests(LedgerCase):
    """Same scripts, both backends, identical outcomes.

    The interesting half is the refusals: it is easy for two ledgers to walk the
    happy path alike and disagree about which hostile sequence is refused, and
    the refusals are the entire safety story.
    """

    SCRIPTS = {
        "happy path": [
            ("begin_preparing",),
            ("mark_prepared",),
            ("claim",),
            ("mark_running", "fixture_open"),
            ("mark_running", "execute"),
            ("record_terminal", "PASS", "d" * 64),
            ("append_cleanup_result", "e" * 64, {"nonfixture_write": 0}),
        ],
        "claim before prepare": [("begin_preparing",), ("claim",)],
        "re-prepare after claim": [
            ("begin_preparing",), ("mark_prepared",), ("claim",), ("mark_prepared",),
        ],
        "reuse a spent run id": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "PASS", "d" * 64), ("begin_preparing",),
        ],
        "claim twice": [
            ("begin_preparing",), ("mark_prepared",), ("claim",), ("claim",),
        ],
        "run without claiming": [("begin_preparing",), ("mark_running", "execute")],
        "run after terminal": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "PASS", "d" * 64), ("mark_running", "execute"),
        ],
        "terminal twice, identical": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "FAIL", "d" * 64),
            ("record_terminal", "FAIL", "d" * 64),
        ],
        "terminal twice, different verdict": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "FAIL", "d" * 64),
            ("record_terminal", "PASS", "d" * 64),
        ],
        "terminal twice, different evidence": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "FAIL", "d" * 64),
            ("record_terminal", "FAIL", "a" * 64),
        ],
        "unknown verdict": [
            ("begin_preparing",), ("mark_prepared",), ("claim",),
            ("record_terminal", "MOSTLY_PASS", "d" * 64),
        ],
        "cleanup before terminal": [
            ("begin_preparing",),
            ("append_cleanup_result", "e" * 64, {"nonfixture_write": 0}),
        ],
        "abort from prepared": [
            ("begin_preparing",), ("mark_prepared",),
            ("record_terminal", "NOT_TESTED", "d" * 64),
        ],
        "abort from preparing": [
            ("begin_preparing",), ("record_terminal", "NOT_TESTED", "d" * 64),
        ],
        "claim with a mismatched authorization": [
            ("begin_preparing",), ("mark_prepared",), ("claim_other_scenario",),
        ],
        "prepare with a mismatched authorization": [
            ("begin_preparing",), ("mark_prepared_other_scenario",),
        ],
        "transition an unknown run": [("mark_running", "execute")],
    }

    def _drive(self, ledger, script):
        request = self.request()
        trace = []
        for step in script:
            op, args = step[0], step[1:]
            try:
                if op == "begin_preparing":
                    result = ledger.begin_preparing(request)
                elif op == "mark_prepared":
                    result = ledger.mark_prepared(request, self.auth())
                elif op == "mark_prepared_other_scenario":
                    result = ledger.mark_prepared(
                        request, self.auth(scenario_id="some-other-scenario")
                    )
                elif op == "claim":
                    result = ledger.claim(request, self.auth())
                elif op == "claim_other_scenario":
                    result = ledger.claim(
                        request, self.auth(scenario_id="some-other-scenario")
                    )
                elif op == "mark_running":
                    result = ledger.mark_running(request.run_id, *args)
                elif op == "record_terminal":
                    result = ledger.record_terminal(request.run_id, *args)
                elif op == "append_cleanup_result":
                    result = ledger.append_cleanup_result(request.run_id, *args)
                else:  # pragma: no cover - a typo in the script, not a behaviour
                    raise AssertionError(f"unknown script op {op}")
            except Exception as error:  # noqa: BLE001 - the outcome IS the value
                trace.append((op, type(error).__name__, str(error)))
            else:
                if isinstance(result, m.RunAuthorization):
                    result = result.authorization_digest
                trace.append((op, "ok", result))
        trace.append(("final", "state", ledger.state(request.run_id)))
        trace.append(("final", "verdict", ledger.verdict(request.run_id)))
        trace.append(
            ("final", "ephemeral", ledger.peek_ephemeral(request.run_id) is not None)
        )
        return trace

    def test_every_script_produces_the_identical_trace_on_both_ledgers(self):
        for name, script in self.SCRIPTS.items():
            with self.subTest(script=name):
                self.store = FakeFirestore()
                self.assertEqual(
                    self._drive(lg.InMemoryRunLedger(), script),
                    self._drive(self.firestore_ledger(), script),
                )

    # The only scripts that are supposed to run clean through. Named rather
    # than counted, so adding a script that silently stops refusing shows up
    # here instead of being absorbed by a threshold.
    SCRIPTS_WITH_NO_REFUSAL = frozenset({
        "happy path",
        "terminal twice, identical",
        "abort from prepared",
        "abort from preparing",
    })

    def test_the_scripts_actually_exercise_refusals(self):
        """A parity suite made only of happy paths would pass against a ledger
        that refused nothing at all, so the refusals are pinned by name."""
        clean = set()
        for name, script in self.SCRIPTS.items():
            self.store = FakeFirestore()
            trace = self._drive(self.firestore_ledger(), script)
            if not any(
                outcome in ("LedgerStateError", "AuthorizationInvalid")
                for _op, outcome, _detail in trace
            ):
                clean.add(name)
        self.assertEqual(clean, self.SCRIPTS_WITH_NO_REFUSAL)


if __name__ == "__main__":
    unittest.main()
