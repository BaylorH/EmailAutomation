"""The permanent sanitized certification run ledger.

This is the only thing standing between "a run happened" and "a run happened
exactly once, under an authorization somebody approved". Everything else in the
program can be retried; the ledger is what makes retrying safe.

Three properties carry the weight.

**Monotonic.** ``PREPARING → PREPARED → CLAIMED → RUNNING(phase) → TERMINAL``
only ever moves forward. A backwards transition is how a claimed run gets
re-prepared and executed twice, so it is refused rather than tolerated.

**Single use.** A run ID is consumed permanently the first time it is seen -
including by a run that already terminalized. Reuse is the cheapest possible way
to make one authorization cover two executions.

**Sanitized.** Durable rows hold states, phases, counts and digests. No
recipient, body, subject, sheet id, or exception text. A fixture value written
here would outlive the fixture it belongs to and the cleanup that erases it -
which is precisely the residue certification exists to prove absent.

The ephemeral half is deliberately separate. The authorization and sealed input
are one-use records that the claim CONSUMES in the same step that moves the
ledger to ``CLAIMED``: if they outlived the claim there would be a window where
a second caller could still use them, and "no consumed-without-claim window" is
the property the plan names.

``InMemoryRunLedger`` is the reference implementation and the one most tests
drive. ``FirestoreRunLedger`` implements the same surface over a store that
outlives the process, one transaction per step. Neither owns the state machine:
``_RunLedgerStateMachine`` below does, and both inherit it unchanged, so the two
agree by construction rather than by review. What differs between them is only
where a row is read from and what makes the write atomic - one lock, or one
transaction.
"""

from __future__ import annotations

import hmac
import threading
from typing import Any, Callable, Dict, Mapping, Optional

# ``AuthorizationInvalid`` is deliberately NOT imported. Nothing here raises it:
# the authorization revalidates ITSELF, in ``RunAuthorization.verify`` and
# ``assert_matches_request``, and the exception travels through this module to
# the caller. An import kept "for the exception this module raises" would be a
# standing hint that the refusal lives here, and it does not.
from email_automation.certification.models import (
    CertificationRequest,
    RunAuthorization,
)

PREPARING = "PREPARING"
PREPARED = "PREPARED"
CLAIMED = "CLAIMED"
RUNNING = "RUNNING"
TERMINAL = "TERMINAL"

# Which states may follow which. Absent key == no successor.
#
# Declared ONCE. Every implementation reads this exact mapping through
# ``_RunLedgerStateMachine._advance``; a backend that restated it would agree
# with this one only for as long as somebody kept checking.
_ALLOWED_TRANSITIONS: Mapping[str, frozenset] = {
    PREPARING: frozenset({PREPARED, TERMINAL}),
    PREPARED: frozenset({CLAIMED, TERMINAL}),
    CLAIMED: frozenset({RUNNING, TERMINAL}),
    RUNNING: frozenset({RUNNING, TERMINAL}),
    TERMINAL: frozenset(),
}

ALLOWED_VERDICTS = ("PASS", "FAIL", "INSTRUMENT_BLOCKED", "NOT_TESTED")

# Every key a durable row may carry, and nothing else. States, phases, counts
# and digests. A row key outside this set is how a recipient, a subject, a sheet
# id, a fixture alias, or an exception string reaches permanent storage - where
# it would outlive both the fixture it came from and the cleanup that erases it.
DURABLE_ROW_KEYS = frozenset({
    "state",
    "scenarioId",
    "sourceRevision",
    "phases",
    "verdict",
    "evidenceDigest",
    "authorizationDigest",
    "cleanupEvidenceDigests",
    "residue",
})


class LedgerStateError(RuntimeError):
    """A refused transition. Names the states, never a fixture value."""


def assert_row_is_sanitized(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """Refuse a durable row carrying anything outside ``DURABLE_ROW_KEYS``.

    Checked on the way OUT, immediately before the write, rather than trusted
    from whoever built the row: the point is that no future edit anywhere can
    add a field to a durable row without this refusing it first.
    """
    extra = sorted(set(row) - DURABLE_ROW_KEYS)
    if extra:
        raise LedgerStateError(
            "durable ledger row carries key(s) outside the sanitized set: "
            + ", ".join(extra)
        )
    return row


class _RunLedgerStateMachine:
    """The transitions themselves, over a plain row ``dict``.

    Storage-free on purpose. Every rule about which state may follow which,
    which authorization a claim will accept, and when a terminal record is a
    convergent retry rather than a rewrite lives here and ONLY here, so an
    in-memory ledger and a transactional one cannot drift apart. A subclass
    supplies the row and makes the write atomic; it decides nothing.
    """

    # -- guards -------------------------------------------------------------

    @staticmethod
    def _new_row(request: CertificationRequest) -> Dict[str, Any]:
        return {
            "state": PREPARING,
            "scenarioId": request.scenario_id,
            "sourceRevision": request.expected_revision,
            "phases": [],
            "verdict": None,
            "evidenceDigest": None,
            "authorizationDigest": None,
            "cleanupEvidenceDigests": [],
            "residue": None,
        }

    @staticmethod
    def _refuse_reuse(run_id: str, row: Optional[Mapping[str, Any]]) -> None:
        if row is not None:
            raise LedgerStateError(
                f"run {run_id} was already used; run ids are single-use"
            )

    @staticmethod
    def _require_row(run_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if row is None:
            raise LedgerStateError(f"run {run_id} has no ledger record")
        return row

    @classmethod
    def _require(
        cls, run_id: str, row: Optional[Dict[str, Any]], expected: str
    ) -> Dict[str, Any]:
        row = cls._require_row(run_id, row)
        if row["state"] != expected:
            raise LedgerStateError(
                f"run {run_id} is {row['state']}; this transition requires {expected}"
            )
        return row

    @staticmethod
    def _advance(row: Dict[str, Any], target: str) -> None:
        current = row["state"]
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise LedgerStateError(f"refused transition {current} -> {target}")
        row["state"] = target

    @staticmethod
    def _validate_verdict(verdict: str) -> None:
        if verdict not in ALLOWED_VERDICTS:
            raise LedgerStateError(
                f"verdict must be one of {', '.join(ALLOWED_VERDICTS)}"
            )

    # -- one step each ------------------------------------------------------

    @classmethod
    def _apply_prepared(
        cls,
        row: Optional[Dict[str, Any]],
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> Dict[str, Any]:
        row = cls._require(request.run_id, row, PREPARING)
        authorization.verify()
        authorization.assert_matches_request(request)
        cls._advance(row, PREPARED)
        row["authorizationDigest"] = authorization.authorization_digest
        return row

    @classmethod
    def _apply_claim(
        cls,
        row: Optional[Dict[str, Any]],
        request: CertificationRequest,
        authorization: RunAuthorization,
        prepared: Optional[RunAuthorization],
    ) -> RunAuthorization:
        """PREPARED → CLAIMED, and hand back the record being consumed.

        The authorization is revalidated here rather than trusted from the
        prepare: a record can be edited between the two, and the claim is the
        last point before business logic runs.
        """
        row = cls._require(request.run_id, row, PREPARED)
        authorization.verify()
        authorization.assert_matches_request(request)

        if prepared is None:
            raise LedgerStateError(
                f"run {request.run_id} has no one-use authorization to consume"
            )
        if not hmac.compare_digest(
            prepared.authorization_digest, authorization.authorization_digest
        ):
            raise LedgerStateError(
                f"run {request.run_id} was prepared under a different authorization"
            )

        cls._advance(row, CLAIMED)
        return prepared

    @classmethod
    def _apply_running(
        cls, row: Optional[Dict[str, Any]], run_id: str, phase: str
    ) -> Dict[str, Any]:
        row = cls._require_row(run_id, row)
        if row["state"] not in (CLAIMED, RUNNING):
            raise LedgerStateError(
                f"run {run_id} is {row['state']}; only a claimed run may run"
            )
        cls._advance(row, RUNNING)
        row["phases"].append(phase)
        return row

    @classmethod
    def _apply_terminal(
        cls,
        row: Optional[Dict[str, Any]],
        run_id: str,
        verdict: str,
        evidence_digest: str,
    ) -> bool:
        """True on the first write, False on an identical repeat.

        Idempotent because a retried write must converge, but NOT permissive: a
        repeat carrying a different verdict or a different evidence digest is
        refused, because that is not a retry -- it is a rewrite of a result
        somebody may already have acted on.
        """
        row = cls._require_row(run_id, row)
        if row["state"] == TERMINAL:
            if row["verdict"] != verdict or row["evidenceDigest"] != evidence_digest:
                raise LedgerStateError(
                    f"run {run_id} already terminalized as {row['verdict']}; "
                    "a terminal record is never rewritten"
                )
            return False
        cls._advance(row, TERMINAL)
        row["verdict"] = verdict
        row["evidenceDigest"] = evidence_digest
        return True

    @classmethod
    def _apply_cleanup(
        cls,
        row: Optional[Dict[str, Any]],
        run_id: str,
        evidence_digest: str,
        residue: Mapping[str, int],
    ) -> Dict[str, Any]:
        """Terminal-only repair evidence. Appends; never alters the verdict."""
        row = cls._require(run_id, row, TERMINAL)
        row["cleanupEvidenceDigests"].append(evidence_digest)
        row["residue"] = {str(k): int(v) for k, v in dict(residue).items()}
        return row


class InMemoryRunLedger(_RunLedgerStateMachine):
    """Reference ledger. Same transitions a transactional store must enforce.

    Process-scoped: correct for a single-instance twin, and structurally unable
    to demonstrate anything that spans a restart. Every step happens under one
    lock, which is the in-memory analogue of one transaction.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._ephemeral: Dict[str, RunAuthorization] = {}

    # -- reads --------------------------------------------------------------

    def state(self, run_id: str) -> Optional[str]:
        """None for an unknown run. Not a guess, and not a default."""
        with self._lock:
            row = self._rows.get(run_id)
            return row["state"] if row else None

    def verdict(self, run_id: str) -> Optional[str]:
        with self._lock:
            row = self._rows.get(run_id)
            return row.get("verdict") if row else None

    def peek_ephemeral(self, run_id: str) -> Optional[RunAuthorization]:
        with self._lock:
            return self._ephemeral.get(run_id)

    def export(self) -> Dict[str, Any]:
        """The durable rows, as they would be persisted."""
        with self._lock:
            return {run_id: dict(row) for run_id, row in self._rows.items()}

    # -- lifecycle ----------------------------------------------------------

    def begin_preparing(self, request: CertificationRequest) -> None:
        """Claim the run ID permanently, before anything else exists.

        Recorded FIRST and for a never-used ID, so a failure anywhere later
        leaves a recoverable record rather than an untracked side effect.
        """
        with self._lock:
            self._refuse_reuse(request.run_id, self._rows.get(request.run_id))
            self._rows[request.run_id] = self._new_row(request)

    def mark_prepared(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> None:
        with self._lock:
            self._apply_prepared(
                self._rows.get(request.run_id), request, authorization
            )
            self._ephemeral[request.run_id] = authorization

    def claim(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> RunAuthorization:
        """PREPARED → CLAIMED, consuming the one-use records in the same step."""
        with self._lock:
            prepared = self._apply_claim(
                self._rows.get(request.run_id),
                request,
                authorization,
                self._ephemeral.get(request.run_id),
            )
            # Consumed in the same critical section that advances the state, so
            # there is no window in which a claim is visible and the one-use
            # records are still usable.
            del self._ephemeral[request.run_id]
            return prepared

    def mark_running(self, run_id: str, phase: str) -> None:
        with self._lock:
            self._apply_running(self._rows.get(run_id), run_id, phase)

    def record_terminal(self, run_id: str, verdict: str, evidence_digest: str) -> bool:
        """Terminalize. Returns True on the first write, False on a repeat."""
        self._validate_verdict(verdict)
        with self._lock:
            wrote = self._apply_terminal(
                self._rows.get(run_id), run_id, verdict, evidence_digest
            )
            if wrote:
                self._ephemeral.pop(run_id, None)
            return wrote

    def append_cleanup_result(
        self,
        run_id: str,
        evidence_digest: str,
        residue: Mapping[str, int],
    ) -> bool:
        """Terminal-only repair evidence. Appends; never alters the verdict."""
        with self._lock:
            self._apply_cleanup(
                self._rows.get(run_id), run_id, evidence_digest, residue
            )
            return True


# ---------------------------------------------------------------------------
# The durable ledger
# ---------------------------------------------------------------------------

# Durable and ephemeral live in SEPARATE collections rather than one document
# with an embedded authorization. They have different lifetimes -- the run row is
# permanent, the authorization is destroyed at claim -- and a retention rule that
# has to reach inside a document to delete part of it is a retention rule nobody
# can audit at a glance.
DEFAULT_RUNS_COLLECTION = "certificationRuns"
DEFAULT_AUTHORIZATIONS_COLLECTION = "certificationRunAuthorizations"

# Every read carries this. A Firestore read with no deadline HANGS rather than
# raising, and a hang is not catchable by the broad ``except Exception`` that
# wraps these call sites -- it surfaces as a stuck instance, not an error.
DEFAULT_LEDGER_DEADLINE_SECONDS = 20.0

# The transaction's own bounded retry. Firestore aborts a transaction whose read
# set moved; the driver retries it, and each retry re-reads, so a retry that
# arrives after a competing claim sees CLAIMED and refuses instead of racing.
DEFAULT_TRANSACTION_ATTEMPTS = 5


class FirestoreRunLedger(_RunLedgerStateMachine):
    """The same machine, over a store that outlives the process.

    Every step is one transaction: read the row (and, where the step consumes
    it, the one-use authorization), run the SHARED transition logic over the
    plain dict, and write the result. Nothing about which state may follow which
    is decided here.

    The Firestore client is a constructor argument with NO ambient fallback.
    ``clients._fs`` is imported by value into ten modules, each of which patches
    its own copy; a ledger that resolved one canonical global would silently
    disagree with whichever binding its caller had patched, which is the trap
    ``firestore_for`` exists to avoid. Making the client explicit means there is
    no binding to resolve ambiguously.
    """

    def __init__(
        self,
        client: Any,
        *,
        runs_collection: str = DEFAULT_RUNS_COLLECTION,
        authorizations_collection: str = DEFAULT_AUTHORIZATIONS_COLLECTION,
        deadline_seconds: float = DEFAULT_LEDGER_DEADLINE_SECONDS,
        max_attempts: int = DEFAULT_TRANSACTION_ATTEMPTS,
    ) -> None:
        if client is None:
            raise ValueError(
                "FirestoreRunLedger requires an explicit Firestore client; there "
                "is deliberately no ambient fallback to resolve"
            )
        # bool first: it is an int subclass, and `True` must not pass as a
        # deadline. A non-number, zero, or a negative would each leave a read
        # without an enforceable bound.
        if isinstance(deadline_seconds, bool) or not isinstance(
            deadline_seconds, (int, float)
        ):
            raise ValueError(
                "deadline_seconds must be a number of seconds; a read with no "
                "usable deadline hangs rather than failing"
            )
        if not deadline_seconds > 0:
            raise ValueError("deadline_seconds must be greater than zero")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self._client = client
        self._runs_collection = runs_collection
        self._authorizations_collection = authorizations_collection
        self._deadline = float(deadline_seconds)
        self._max_attempts = max_attempts

    # -- references ---------------------------------------------------------

    def _run_ref(self, run_id: str) -> Any:
        return self._client.collection(self._runs_collection).document(run_id)

    def _authorization_ref(self, run_id: str) -> Any:
        return self._client.collection(
            self._authorizations_collection
        ).document(run_id)

    # -- reads --------------------------------------------------------------

    def _read_row(self, run_id: str, transaction: Any = None) -> Optional[Dict[str, Any]]:
        snapshot = self._run_ref(run_id).get(
            transaction=transaction, timeout=self._deadline
        )
        if not snapshot.exists:
            return None
        return dict(snapshot.to_dict() or {})

    def _read_authorization(
        self, run_id: str, transaction: Any = None
    ) -> Optional[RunAuthorization]:
        """Rebuild the one-use record, revalidating it on the way in.

        ``from_stored`` recomputes the digest from the stored scalars, so a
        record edited in the database without a matching digest raises here
        rather than being handed to a claim. That is deliberately NOT flattened
        to ``None``: "the authorization was tampered with" and "this run was
        never prepared" are different facts and must not look alike.
        """
        snapshot = self._authorization_ref(run_id).get(
            transaction=transaction, timeout=self._deadline
        )
        if not snapshot.exists:
            return None
        return RunAuthorization.from_stored(snapshot.to_dict() or {})

    def state(self, run_id: str) -> Optional[str]:
        """None for an unknown run. Not a guess, and not a default."""
        row = self._read_row(run_id)
        return row["state"] if row else None

    def verdict(self, run_id: str) -> Optional[str]:
        row = self._read_row(run_id)
        return row.get("verdict") if row else None

    def peek_ephemeral(self, run_id: str) -> Optional[RunAuthorization]:
        return self._read_authorization(run_id)

    def export(self) -> Dict[str, Any]:
        """The durable rows, as they are persisted."""
        stream = self._client.collection(self._runs_collection).stream(
            timeout=self._deadline
        )
        return {snapshot.id: dict(snapshot.to_dict() or {}) for snapshot in stream}

    # -- transactions -------------------------------------------------------

    def _in_transaction(self, body: Callable[[Any], Any]) -> Any:
        """Run ``body`` inside one Firestore transaction, bounded and retried.

        The driver is the library's own ``transactional``: it begins the
        transaction, calls ``body``, commits, retries the whole thing on an
        ``Aborted`` (a read set that moved under it), and rolls back on any
        exception. A retry re-runs ``body`` from its reads, so a step that lost
        a race re-reads the moved state and refuses on the shared transition
        rules instead of overwriting the winner.
        """
        from google.cloud.firestore import transactional

        transaction = self._client.transaction(max_attempts=self._max_attempts)
        return transactional(body)(transaction)

    def _write_row(
        self, transaction: Any, run_id: str, row: Optional[Mapping[str, Any]]
    ) -> None:
        """Persist one durable row, refusing a missing one by name.

        A row read from the store is ``Optional`` and the shared transition
        rules refuse a missing one before this is reached -- so today the
        ``None`` branch is unreachable. It is written anyway, because every
        call site above is wrapped in a broad ``except Exception``: a write that
        blew up on ``None``, or worse quietly wrote nothing, would surface as a
        clean early return and the caller would believe a terminal record had
        been persisted when none was. On the claim and terminal paths that is
        indistinguishable from success, which is precisely the failure a durable
        ledger exists to remove. So it refuses, loudly, naming the run.
        """
        if row is None:
            raise LedgerStateError(
                f"run {run_id} has no ledger row to persist; a durable write "
                "with no row would record nothing while appearing to succeed"
            )
        transaction.set(self._run_ref(run_id), dict(assert_row_is_sanitized(row)))

    # -- lifecycle ----------------------------------------------------------

    def begin_preparing(self, request: CertificationRequest) -> None:
        """Claim the run ID permanently, before anything else exists.

        Read and create in ONE transaction: two callers preparing the same id
        concurrently must not both see "unused".
        """
        run_id = request.run_id

        def body(transaction: Any) -> None:
            self._refuse_reuse(run_id, self._read_row(run_id, transaction))
            self._write_row(transaction, run_id, self._new_row(request))

        self._in_transaction(body)

    def mark_prepared(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> None:
        run_id = request.run_id

        def body(transaction: Any) -> None:
            row = self._apply_prepared(
                self._read_row(run_id, transaction), request, authorization
            )
            self._write_row(transaction, run_id, row)
            transaction.set(
                self._authorization_ref(run_id), dict(authorization.to_stored())
            )

        self._in_transaction(body)

    def claim(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> RunAuthorization:
        """PREPARED → CLAIMED, consuming the one-use record in the SAME commit.

        The advance and the delete are two writes in one transaction, so they
        land together or not at all. Splitting them would open exactly the
        window the plan forbids: a claim already visible while the
        authorization it spent is still there for a second caller to spend.
        """
        run_id = request.run_id

        def body(transaction: Any) -> RunAuthorization:
            # Both reads first: Firestore requires every read in a transaction
            # to precede every write in it.
            row = self._read_row(run_id, transaction)
            prepared = self._read_authorization(run_id, transaction)
            claimed = self._apply_claim(row, request, authorization, prepared)
            self._write_row(transaction, run_id, row)
            transaction.delete(self._authorization_ref(run_id))
            return claimed

        return self._in_transaction(body)

    def mark_running(self, run_id: str, phase: str) -> None:
        def body(transaction: Any) -> None:
            row = self._apply_running(
                self._read_row(run_id, transaction), run_id, phase
            )
            self._write_row(transaction, run_id, row)

        self._in_transaction(body)

    def record_terminal(self, run_id: str, verdict: str, evidence_digest: str) -> bool:
        """Terminalize. True on the first write, False on an identical repeat.

        The verdict is validated BEFORE the transaction opens: an unknown
        verdict is a caller error, not a state conflict, and there is no reason
        to spend a transaction discovering that.
        """
        self._validate_verdict(verdict)

        def body(transaction: Any) -> bool:
            row = self._read_row(run_id, transaction)
            wrote = self._apply_terminal(row, run_id, verdict, evidence_digest)
            if not wrote:
                # A convergent repeat writes nothing at all, so a retry cannot
                # bump the row and cannot resurrect a consumed authorization.
                return False
            self._write_row(transaction, run_id, row)
            transaction.delete(self._authorization_ref(run_id))
            return True

        return self._in_transaction(body)

    def append_cleanup_result(
        self,
        run_id: str,
        evidence_digest: str,
        residue: Mapping[str, int],
    ) -> bool:
        """Terminal-only repair evidence. Appends; never alters the verdict."""

        def body(transaction: Any) -> bool:
            row = self._apply_cleanup(
                self._read_row(run_id, transaction), run_id, evidence_digest, residue
            )
            self._write_row(transaction, run_id, row)
            return True

        return self._in_transaction(body)
