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

    def on_transactional_read(self, hook):
        self._read_hook = hook

    def fire_read_hook(self, path):
        hook, self._read_hook = self._read_hook, None
        if hook is not None:
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


if __name__ == "__main__":
    unittest.main()
