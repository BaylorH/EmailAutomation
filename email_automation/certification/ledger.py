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

``InMemoryRunLedger`` is the reference implementation and the one tests drive.
A Firestore-backed ledger implements the same surface with the same transitions
under a transaction; the state machine lives here so both agree by construction
rather than by review.
"""

from __future__ import annotations

import hmac
import threading
from typing import Any, Dict, List, Mapping, Optional

from email_automation.certification.models import (
    AuthorizationInvalid,
    CertificationRequest,
    RunAuthorization,
)

PREPARING = "PREPARING"
PREPARED = "PREPARED"
CLAIMED = "CLAIMED"
RUNNING = "RUNNING"
TERMINAL = "TERMINAL"

# Which states may follow which. Absent key == no successor.
_ALLOWED_TRANSITIONS: Mapping[str, frozenset] = {
    PREPARING: frozenset({PREPARED, TERMINAL}),
    PREPARED: frozenset({CLAIMED, TERMINAL}),
    CLAIMED: frozenset({RUNNING, TERMINAL}),
    RUNNING: frozenset({RUNNING, TERMINAL}),
    TERMINAL: frozenset(),
}

ALLOWED_VERDICTS = ("PASS", "FAIL", "INSTRUMENT_BLOCKED", "NOT_TESTED")


class LedgerStateError(RuntimeError):
    """A refused transition. Names the states, never a fixture value."""


class InMemoryRunLedger:
    """Reference ledger. Same transitions a transactional store must enforce."""

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

    # -- internals ----------------------------------------------------------

    def _require(self, run_id: str, expected: str) -> Dict[str, Any]:
        row = self._rows.get(run_id)
        if row is None:
            raise LedgerStateError(f"run {run_id} has no ledger record")
        if row["state"] != expected:
            raise LedgerStateError(
                f"run {run_id} is {row['state']}; this transition requires {expected}"
            )
        return row

    def _advance(self, row: Dict[str, Any], target: str) -> None:
        current = row["state"]
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise LedgerStateError(f"refused transition {current} -> {target}")
        row["state"] = target

    # -- lifecycle ----------------------------------------------------------

    def begin_preparing(self, request: CertificationRequest) -> None:
        """Claim the run ID permanently, before anything else exists.

        Recorded FIRST and for a never-used ID, so a failure anywhere later
        leaves a recoverable record rather than an untracked side effect.
        """
        with self._lock:
            if request.run_id in self._rows:
                raise LedgerStateError(
                    f"run {request.run_id} was already used; run ids are single-use"
                )
            self._rows[request.run_id] = {
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

    def mark_prepared(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> None:
        with self._lock:
            row = self._require(request.run_id, PREPARING)
            authorization.verify()
            authorization.assert_matches_request(request)
            self._advance(row, PREPARED)
            row["authorizationDigest"] = authorization.authorization_digest
            self._ephemeral[request.run_id] = authorization

    def claim(
        self,
        request: CertificationRequest,
        authorization: RunAuthorization,
    ) -> RunAuthorization:
        """PREPARED → CLAIMED, consuming the one-use records in the same step.

        The authorization is revalidated here rather than trusted from the
        prepare: a record can be edited between the two, and the claim is the
        last point before business logic runs.
        """
        with self._lock:
            row = self._require(request.run_id, PREPARED)
            authorization.verify()
            authorization.assert_matches_request(request)

            prepared = self._ephemeral.get(request.run_id)
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

            self._advance(row, CLAIMED)
            # Consumed in the same critical section that advances the state, so
            # there is no window in which a claim is visible and the one-use
            # records are still usable.
            del self._ephemeral[request.run_id]
            return prepared

    def mark_running(self, run_id: str, phase: str) -> None:
        with self._lock:
            row = self._rows.get(run_id)
            if row is None:
                raise LedgerStateError(f"run {run_id} has no ledger record")
            if row["state"] not in (CLAIMED, RUNNING):
                raise LedgerStateError(
                    f"run {run_id} is {row['state']}; only a claimed run may run"
                )
            self._advance(row, RUNNING)
            row["phases"].append(phase)

    def record_terminal(self, run_id: str, verdict: str, evidence_digest: str) -> bool:
        """Terminalize. Returns True on the first write, False on a repeat.

        Idempotent because a retried write must converge, but NOT permissive: a
        repeat carrying a different verdict is refused, because that is not a
        retry -- it is a rewrite of a result somebody may already have acted on.
        """
        if verdict not in ALLOWED_VERDICTS:
            raise LedgerStateError(f"verdict must be one of {', '.join(ALLOWED_VERDICTS)}")
        with self._lock:
            row = self._rows.get(run_id)
            if row is None:
                raise LedgerStateError(f"run {run_id} has no ledger record")
            if row["state"] == TERMINAL:
                if row["verdict"] != verdict or row["evidenceDigest"] != evidence_digest:
                    raise LedgerStateError(
                        f"run {run_id} already terminalized as {row['verdict']}; "
                        "a terminal record is never rewritten"
                    )
                return False
            self._advance(row, TERMINAL)
            row["verdict"] = verdict
            row["evidenceDigest"] = evidence_digest
            self._ephemeral.pop(run_id, None)
            return True

    def append_cleanup_result(
        self,
        run_id: str,
        evidence_digest: str,
        residue: Mapping[str, int],
    ) -> bool:
        """Terminal-only repair evidence. Appends; never alters the verdict."""
        with self._lock:
            row = self._require(run_id, TERMINAL)
            row["cleanupEvidenceDigests"].append(evidence_digest)
            row["residue"] = {str(k): int(v) for k, v in dict(residue).items()}
            return True
