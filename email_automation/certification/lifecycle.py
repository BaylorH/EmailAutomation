"""prepare → run → status → review-input → abort/recover, over the run ledger.

The HTTP routes are a thin shell over this module. Keeping the lifecycle out of
the request context is what lets the cases that actually matter -- a reused run
id, a run claimed twice, an incomplete deployment identity -- be exercised
directly, instead of through status codes that flatten them into "400".

Every function returns ``(payload, http_status)``. Payloads are SANITIZED by
construction: states, verdicts, phases, counts and digests. No recipient, body,
subject, sheet id, fixture alias, or exception text, because a route response is
the easiest place for a fixture value to escape the fixture.

``review_input`` is the ONE exception, named in ``UNSANITIZED_OPERATIONS``. It
returns redacted but otherwise raw captured subjects and bodies, because a human
naturalness verdict cannot be reached from a digest. It is bounded, ordered,
transient, and unreachable from any agent path; nothing else in this module
relaxes to accommodate it.

``recover`` is the counterpart hazard. It handles the CLAIMED/RUNNING run whose
worker vanished, and it NEVER EXECUTES: its whole body runs inside the execution
fence below, and its call graph is approved by allowlist in the tests.

One deliberate limitation, stated rather than hidden: the default ledger is
in-memory and therefore process-scoped. It enforces the full state machine but
does not survive a restart, so it is correct for a single-instance twin
(containerConcurrency 1, maxScale 1) and is NOT yet the durable Firestore
ledger the plan ultimately requires. The state machine lives in ledger.py so
both implementations agree by construction.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from email_automation.certification import ledger as ledger_module
from email_automation.certification import scenarios
from email_automation.certification.canonical_json import canonical_digest
from email_automation.certification import input_handoff
from email_automation.certification.input_handoff import SealedInput
from email_automation.certification.models import (
    AuthorizationInvalid,
    CertificationRequest,
    RunAuthorization,
)

Response = Tuple[Dict[str, Any], int]

# How long a prepared authorization stays usable. Fixed rather than configurable:
# a caller-chosen expiry is a caller-chosen security property.
AUTHORIZATION_TTL_SECONDS = 3600

# Only this launch class may be driven without a human launching the
# product runtime. Anything else stops before any provider can be reached.
AGENT_SAFE_LAUNCH_CLASS = "agent_safe"

# Every one is a deployment fact. None may be defaulted -- a missing candidate
# revision or fixture-secret version quietly filled in would produce an
# authorization, and therefore a stamp, bound to something nobody deployed.
REQUIRED_IDENTITY_ENV = (
    "SITESIFT_SOURCE_REVISION",
    "SITESIFT_IMAGE_DIGEST",
    "K_SERVICE",
    "K_REVISION",
    "SITESIFT_PRODUCTION_CANDIDATE_REVISION",
    "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION",
    "SITESIFT_FIXTURE_CONFIG_DIGEST",
)

# NOT an environment fact. The caller identity is whatever the VERIFIED token
# said, passed in explicitly -- an env-configured caller digest would let a
# deployment assert who called it, which is exactly what verification exists to
# stop.

_DEFAULT_LEDGER = ledger_module.InMemoryRunLedger()

# Transient, process-scoped, and never persisted. See input_handoff for why the
# raw review text may not go anywhere else.
_DEFAULT_REVIEW_STORE = input_handoff.TransientReviewStore()

# The ONE operation whose payload is not sanitized, named here so the exception
# is a declared fact instead of an omission somebody discovers later. Human
# naturalness review cannot be done against digests; everything else can, and
# does. This set is asserted against the CLI's human-only operations by test, so
# an unsanitized route can never become agent-callable without failing.
UNSANITIZED_OPERATIONS = frozenset({"review-input"})


def default_ledger() -> ledger_module.InMemoryRunLedger:
    return _DEFAULT_LEDGER


def default_review_store() -> input_handoff.TransientReviewStore:
    return _DEFAULT_REVIEW_STORE


def _error(reason: str, status: int) -> Response:
    return {"status": "error", "reason": reason}, status


def _identity(environ: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """Every required deployment fact, or None. Never a partial identity."""
    resolved = {}
    for key in REQUIRED_IDENTITY_ENV:
        value = (environ.get(key) or "").strip()
        if not value:
            return None
        resolved[key] = value
    return resolved


def _expires_at(now_epoch: int) -> str:
    """Exactly YYYY-MM-DDTHH:MM:SSZ -- the only encoding the digest accepts."""
    from datetime import datetime, timedelta, timezone
    moment = datetime.fromtimestamp(now_epoch, tz=timezone.utc) + timedelta(
        seconds=AUTHORIZATION_TTL_SECONDS
    )
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_input_for(scenario: Mapping[str, Any]) -> SealedInput:
    """Build the run's canonical input from the IN-IMAGE registry.

    `backend_registry_v1`: the input is derived from bytes the image ships, so
    there is no caller-supplied payload to validate and nothing to seal that a
    request could have influenced. Logical aliases only -- concrete identities
    are resolved later, from the bound fixture secret.
    """
    return SealedInput.seal({
        "scenarioId": scenario["scenarioId"],
        "capabilityId": scenario["capabilityId"],
        "logicalFixtureKey": scenario["logicalFixtureKey"],
        "oracleProjectionKey": scenario["oracleProjectionKey"],
        "inputProducerKind": scenario["inputProducerKind"],
    })


def prepare(body: Mapping[str, Any], *, caller_identity_digest: str,
            ledger=None, environ: Optional[Mapping[str, str]] = None,
            now_epoch: Optional[int] = None) -> Response:
    ledger = ledger if ledger is not None else default_ledger()
    environ = environ if environ is not None else os.environ
    identity = _identity(environ)
    if identity is None:
        return _error("instrument_unavailable", 503)

    try:
        scenario = scenarios.get(body["scenarioId"])
    except KeyError:
        # Refused BEFORE the ledger is touched, so an unapproved scenario cannot
        # consume a run id.
        return _error("unknown_scenario", 404)

    # Defence in depth, and not redundant: the CLI refuses this before calling,
    # but the CLI is one caller of a route that executes real product code. A
    # non-agent-safe scenario needs a human-launched runtime, so preparing one
    # here would create an authorization nobody can legitimately spend.
    if scenario.get("launchClass") != AGENT_SAFE_LAUNCH_CLASS:
        return _error("user_runtime_launch_required", 409)

    request = CertificationRequest(
        scenario_id=body["scenarioId"],
        run_id=body["runId"],
        expected_revision=body["expectedRevision"],
    )

    sealed = _canonical_input_for(scenario)
    try:
        authorization = RunAuthorization.create(
            scenario_id=request.scenario_id,
            run_id=request.run_id,
            source_revision=identity["SITESIFT_SOURCE_REVISION"],
            image_digest=identity["SITESIFT_IMAGE_DIGEST"],
            certification_service=identity["K_SERVICE"],
            certification_revision=identity["K_REVISION"],
            production_candidate_revision=identity[
                "SITESIFT_PRODUCTION_CANDIDATE_REVISION"],
            caller_identity_digest=caller_identity_digest,
            fixture_config_secret_version=identity[
                "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION"],
            fixture_config_digest=identity["SITESIFT_FIXTURE_CONFIG_DIGEST"],
            scenario_registry_digest=scenarios.registry_digest(),
            launch_class=scenario["launchClass"],
            input_producer_kind=scenario["inputProducerKind"],
            canonical_input_digest=sealed.digest,
            input_producer_artifact_digest=canonical_digest(
                {"registryDigest": scenarios.registry_digest()}),
            authorization_expires_at=_expires_at(
                now_epoch if now_epoch is not None else _now_epoch()),
        )
    except AuthorizationInvalid:
        return _error("authorization_invalid", 503)

    try:
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, authorization)
    except ledger_module.LedgerStateError:
        return _error("run_id_unavailable", 409)
    except AuthorizationInvalid:
        return _error("authorization_invalid", 409)

    return {
        "status": "ok",
        "state": "PREPARED",
        "runId": request.run_id,
        "scenarioId": request.scenario_id,
        "authorizationDigest": authorization.authorization_digest,
        "canonicalInputDigest": sealed.digest,
        "scenarioRegistryDigest": authorization.scenario_registry_digest,
        "launchClass": authorization.launch_class,
        "expiresAt": authorization.authorization_expires_at,
    }, 200


class ExecutionForbidden(RuntimeError):
    """Business logic was reached from a lane that may never execute."""


# Thread-local, not process-global: a global fence would refuse every
# legitimate concurrent run, and a fence that breaks ordinary operation gets
# removed by the next person who needs the instrument to work.
_execution_fence = threading.local()


@contextmanager
def execution_forbidden():
    """Close the fence for this thread. Restores whatever it replaced."""
    previous = getattr(_execution_fence, "forbidden", False)
    _execution_fence.forbidden = True
    try:
        yield
    finally:
        _execution_fence.forbidden = previous


def execution_is_forbidden() -> bool:
    return bool(getattr(_execution_fence, "forbidden", False))


def run(body: Mapping[str, Any], *, caller_identity_digest: str = "",
        ledger=None, environ: Optional[Mapping[str, str]] = None) -> Response:
    # Checked BEFORE the runner is even imported. Recovery runs its whole body
    # inside the fence, so an edit that routes recovery through here raises
    # instead of quietly causing a second effect on a run that may already have
    # caused its first.
    if execution_is_forbidden():
        raise ExecutionForbidden(
            "execution was attempted inside a lane that may never execute")

    from email_automation.certification import runner as runner_module

    ledger = ledger if ledger is not None else default_ledger()
    environ = environ if environ is not None else os.environ
    identity = _identity(environ)
    if identity is None:
        return _error("instrument_unavailable", 503)

    run_id = body["runId"]
    try:
        authorization = ledger.peek_ephemeral(run_id)
    except AuthorizationInvalid:
        # A durable ledger raises here when a STORED authorization has been
        # edited. That is a finding and gets its own reason: collapsing it into
        # the ordinary 409 would hide a tamper, and letting it escape would
        # return a 500 -- the one answer that tells a caller this run id hit a
        # code path the others did not.
        return _error("authorization_invalid", 409)
    if authorization is None:
        # Covers "never prepared" and "already claimed" alike, and deliberately
        # does not distinguish them to a caller: both mean this request may not
        # execute, and telling them apart is a probe for which run ids exist.
        return _error("no_prepared_run", 409)

    # Use the scenario the CALLER named, not the one on file. Defaulting to the
    # stored value would make the request's scenarioId decorative, and the
    # binding check inside claim() would then be comparing a value to itself.
    request = CertificationRequest(
        scenario_id=body.get("scenarioId") or authorization.scenario_id,
        run_id=run_id,
        expected_revision=body["expectedRevision"],
    )

    try:
        claimed = ledger.claim(request, authorization)
    except ledger_module.LedgerStateError:
        return _error("no_prepared_run", 409)
    except AuthorizationInvalid:
        return _error("authorization_invalid", 409)

    # Announce the phase BEFORE the work it names, and durably. This is the
    # ``CLAIMED → RUNNING(phase)`` step the state machine has always enforced
    # and nothing ever took: without it, a worker that vanishes mid-execution
    # leaves a row indistinguishable from one that vanished before executing,
    # and recovery has no way to tell "may have caused an effect" from "provably
    # did not". Written first, so the row is never behind reality.
    ledger.mark_running(run_id, "execute")

    record, _detail = runner_module.run_scenario(
        claimed.scenario_id, run_id=run_id, revision=claimed.source_revision
    )
    verdict = {"pass": "PASS", "fail": "FAIL",
               "instrument_blocked": "INSTRUMENT_BLOCKED"}.get(
        record.outcome, "FAIL")
    ledger.record_terminal(run_id, verdict, record.canonical_digest())

    return {
        "status": "ok",
        "state": "TERMINAL",
        "runId": run_id,
        "scenarioId": claimed.scenario_id,
        "verdict": verdict,
        "phase": record.phase,
        "failureCode": record.failure_code or "",
        "counts": dict(record.counts),
        "evidenceDigest": record.canonical_digest(),
        "authorizationDigest": claimed.authorization_digest,
    }, 200


def status(body: Mapping[str, Any], *, caller_identity_digest: str = "",
           ledger=None, environ: Optional[Mapping[str, str]] = None) -> Response:
    ledger = ledger if ledger is not None else default_ledger()
    run_id = body["runId"]
    state = ledger.state(run_id)
    if state is None:
        return _error("unknown_run", 404)
    return {"status": "ok", "state": state, "runId": run_id,
            "verdict": ledger.verdict(run_id) or ""}, 200


def review_input(body: Mapping[str, Any], *, caller_identity_digest: str = "",
                 ledger=None, environ: Optional[Mapping[str, str]] = None,
                 store=None, now_epoch: Optional[int] = None) -> Response:
    """The bounded ordered projection of every captured message for one run.

    THE ONE UNSANITIZED PAYLOAD IN THE INSTRUMENT. It returns subjects and
    bodies, because a naturalness verdict cannot be reached from a digest.

    Everything about it is narrowed to make that survivable. The text is
    redacted by shape and re-verified with the real evidence sanitizer before
    any length bound applies; the pack is whole and ordered or it is refused;
    the artifact expires within a day and cleanup owns it; and no agent path can
    reach the route -- ``scripts/certify_production.py`` refuses the operation
    before it builds a request, which is a capability the CLI does not have
    rather than a guard it chooses not to use.
    """
    ledger = ledger if ledger is not None else default_ledger()
    store = store if store is not None else default_review_store()
    run_id = body["runId"]

    if ledger.state(run_id) is None:
        return _error("unknown_run", 404)

    moment = now_epoch if now_epoch is not None else _now_epoch()
    review_set = store.get(run_id, now_epoch=moment)
    if review_set is None:
        # Covers "nothing captured", "already reviewed", and "expired" alike.
        # None of them is a state in which raw text may be served.
        return _error("no_review_pending", 409)

    return {"status": "ok", "state": "AWAITING_REVIEW", "runId": run_id,
            "reviewSetDigest": review_set.set_digest,
            "expiresAtEpoch": review_set.expires_at_epoch,
            "messages": [message.to_dict() for message in review_set.messages]}, 200


def abort(body: Mapping[str, Any], *, caller_identity_digest: str = "",
          ledger=None, environ: Optional[Mapping[str, str]] = None) -> Response:
    """Terminalize a run proven not to have executed.

    Allowed only from PREPARING/PREPARED. A CLAIMED run may have run already, so
    aborting it would record "did not execute" over an execution that happened;
    that case is recovery's, not abort's.
    """
    ledger = ledger if ledger is not None else default_ledger()
    run_id = body["runId"]
    state = ledger.state(run_id)
    if state is None:
        return _error("unknown_run", 404)
    if state not in (ledger_module.PREPARING, ledger_module.PREPARED):
        return _error("not_abortable", 409)
    ledger.record_terminal(run_id, "NOT_TESTED", canonical_digest({"aborted": run_id}))
    return {"status": "ok", "state": "TERMINAL", "runId": run_id,
            "verdict": "NOT_TESTED"}, 200


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------
#
# ``abort`` terminalizes a run PROVEN not to have executed and explicitly
# refuses CLAIMED. Recovery's domain is exactly what abort refuses: the
# CLAIMED/RUNNING run that may or may not have executed, whose worker is gone.
#
# The single property that matters here is that recovery NEVER EXECUTES. A
# recovery that runs business logic is how a "recovered" run causes a second
# effect -- the exact failure the whole ledger exists to prevent. So recover
# reads back, quiesces, cleans, and terminalizes, and nothing else.
#
# It also terminalizes HONESTLY. ``NOT_TESTED`` means "did not execute"; a
# claimed run may have executed, so recording NOT_TESTED over it would write a
# falsehood into the permanent record. The only verdict recovery may write is
# INSTRUMENT_BLOCKED, which says what is true: the instrument lost track, and
# the capability was not certified.

# 540s Cloud Run request timeout. A record younger than this may still have an
# execution in flight, because the execution itself has not yet been killed.
#
# READ from the ledger rather than restated here. The same number is what makes
# a lease provably dead inside ``recovery_observation``, and a second copy of it
# would agree with the first only until somebody edited one of them.
SERVICE_TIMEOUT_SECONDS = ledger_module.WORKER_REQUEST_CEILING_SECONDS
RECOVERY_AGE_MARGIN_SECONDS = 180
RECOVERY_MIN_RECORD_AGE_SECONDS = SERVICE_TIMEOUT_SECONDS + RECOVERY_AGE_MARGIN_SECONDS

# Every certification provider wrapper carries a 60s maximum client deadline.
# After revoking the generation, a conclusive old-generation call still has that
# long to land, so quiescence waits it out plus a margin before anything is
# cleaned or terminalized.
MAX_IN_FLIGHT_SECONDS = 60
QUIESCENCE_MARGIN_SECONDS = 15
QUIESCENCE_WAIT_SECONDS = MAX_IN_FLIGHT_SECONDS + QUIESCENCE_MARGIN_SECONDS
QUIESCENCE_POLL_SECONDS = 5

# A Firestore read with NO DEADLINE hangs rather than errors, and no `except`
# clause can catch a hang. Every readback here is run under this deadline in a
# daemon thread, so an unresponsive store becomes a refusal instead of a request
# that never returns.
READBACK_DEADLINE_SECONDS = 20.0

# ALLOWLIST. A denylist would fail open for any state added later, and the thing
# on the other side of this check is a permanent terminal record.
RECOVERABLE_STATES = (ledger_module.CLAIMED, ledger_module.RUNNING)

# The only verdicts recovery may write. Not a style choice: PASS would stamp a
# capability nobody observed, and NOT_TESTED would deny an execution that may
# have happened.
RECOVERY_VERDICTS = ("INSTRUMENT_BLOCKED",)


class ReadbackTimeout(RuntimeError):
    """A readback exceeded its deadline. Distinct from a readback that failed."""


@dataclass(frozen=True)
class RecordObservation:
    """What a durable ledger can say about an interrupted run.

    Every field is Optional and ``None`` means UNPROVABLE, never "zero" or
    "old enough". An in-process ledger cannot answer any of them, and recovery
    refuses rather than assuming.
    """

    state: Optional[str]
    record_age_seconds: Optional[int]
    lease_expired: Optional[bool]
    in_flight: Optional[int]
    ambiguous: Optional[int]


def _bounded_read(reader: Callable[[str], Any], run_id: str) -> Any:
    """Run one readback under an explicit deadline, in a daemon thread."""
    outcome: Dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = reader(run_id)
        except BaseException as exc:      # noqa: BLE001 - re-raised in this thread
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(READBACK_DEADLINE_SECONDS)
    if thread.is_alive():
        raise ReadbackTimeout("readback exceeded its deadline")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _probe(ledger: Any, run_id: str) -> Any:
    """The raw observation mapping, or None when the ledger cannot observe."""
    reader = getattr(ledger, "recovery_observation", None)
    if reader is None:
        return None
    return _bounded_read(reader, run_id)


def _exact_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _coerce_observation(raw: Any) -> Optional[RecordObservation]:
    """Strict types only. A value of the wrong type is unprovable, not coerced.

    ``None`` means the ledger could not be observed AT ALL, which is a different
    fact from an observation with a missing field, and both are refusals.
    """
    if not isinstance(raw, Mapping):
        return None
    row = raw
    state = row.get("state")
    lease = row.get("leaseExpired")
    return RecordObservation(
        state=state if isinstance(state, str) and state else None,
        record_age_seconds=_exact_int(row.get("recordAgeSeconds")),
        lease_expired=lease if isinstance(lease, bool) else None,
        in_flight=_exact_int(row.get("inFlight")),
        ambiguous=_exact_int(row.get("ambiguous")),
    )


def _observe(ledger: Any, run_id: str) -> Tuple[Optional[RecordObservation],
                                                Optional[Response]]:
    """(observation, None) or (None, refusal). Never a partial observation."""
    try:
        raw = _probe(ledger, run_id)
    except ReadbackTimeout:
        return None, _error("readback_deadline_exceeded", 503)
    except Exception:      # noqa: BLE001 - an unreadable record is not a recoverable one
        return None, _error("readback_failed", 503)
    return _coerce_observation(raw), None


def _quiesce(clock: Callable[[], float], sleeper: Callable[[float], None],
             revoked_at: float) -> float:
    """Wait out the maximum in-flight call plus its margin. Returns the wait.

    Bounded rather than ``while True``: a clock that does not advance would
    otherwise hang the request forever, and the caller has to be able to tell
    "the gate completed" from "the gate never could".
    """
    attempts = 0
    max_attempts = int(QUIESCENCE_WAIT_SECONDS / QUIESCENCE_POLL_SECONDS) + 3
    while attempts < max_attempts:
        waited = clock() - revoked_at
        remaining = QUIESCENCE_WAIT_SECONDS - waited
        if remaining <= 0:
            return waited
        attempts += 1
        sleeper(remaining if remaining < QUIESCENCE_POLL_SECONDS
                else QUIESCENCE_POLL_SECONDS)
    return clock() - revoked_at


def _recovery_digest(run_id: str, failure_code: str, waited: float,
                     residue: Mapping[str, int]) -> str:
    """Sanitized recovery facts only: no fixture value can reach this preimage."""
    residue_rows = dict(residue)
    return canonical_digest({
        "recoveredRun": run_id,
        "failureCode": failure_code,
        "quiescenceWaitedSeconds": int(waited),
        "residue": {str(k): int(v) for k, v in residue_rows.items()},
    })


def recover(body: Mapping[str, Any], *, caller_identity_digest: str = "",
            ledger=None, environ: Optional[Mapping[str, str]] = None,
            clock: Optional[Callable[[], float]] = None,
            sleeper: Optional[Callable[[float], None]] = None,
            cleaner: Optional[Callable[[str], Mapping[str, int]]] = None) -> Response:
    """Read back, quiesce, clean, terminalize. Never execute.

    The whole body runs inside the execution fence. That is the structural half
    of the guarantee: recovery imports nothing that can run product code, and if
    a later edit routes it into one anyway, ``run`` raises rather than causing a
    second effect on a run that may already have caused its first.
    """
    with execution_forbidden():
        return _recover_under_fence(
            body, caller_identity_digest=caller_identity_digest, ledger=ledger,
            environ=environ, clock=clock, sleeper=sleeper, cleaner=cleaner)


def _recover_under_fence(
        body: Mapping[str, Any], *, caller_identity_digest: str = "",
        ledger=None, environ: Optional[Mapping[str, str]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        cleaner: Optional[Callable[[str], Mapping[str, int]]] = None) -> Response:
    ledger = ledger if ledger is not None else default_ledger()
    clock = clock if clock is not None else _now_float
    sleeper = sleeper if sleeper is not None else _sleep
    run_id = body["runId"]

    state = ledger.state(run_id)
    if state is None:
        return _error("unknown_run", 404)
    if state not in RECOVERABLE_STATES:
        # PREPARING/PREPARED belong to abort, which can prove no execution
        # happened. TERMINAL is already resolved and is never rewritten.
        return _error("not_recoverable", 409)

    first, refusal = _observe(ledger, run_id)
    if refusal is not None:
        return refusal
    if first is None:
        # No observation at all. Naming it separately from a missing FIELD
        # matters: "the store answered and had no age" and "there was no answer"
        # need different operator responses, and neither may proceed.
        return _error("record_observation_unavailable", 503)
    if first.record_age_seconds is None:
        return _error("record_age_unprovable", 503)
    if first.record_age_seconds < RECOVERY_MIN_RECORD_AGE_SECONDS:
        # Younger than the service timeout plus its margin: an execution
        # started by the lost worker may still be running right now.
        return _error("record_too_recent", 409)
    if first.lease_expired is None:
        return _error("lease_state_unprovable", 503)
    if not first.lease_expired:
        return _error("lease_active", 409)

    # The second read is what makes the first one a decision rather than a
    # guess: a record that moved between them is being worked on by someone.
    second, refusal = _observe(ledger, run_id)
    if refusal is not None:
        return refusal
    if second is None:
        return _error("record_observation_unavailable", 503)
    if (second.state != first.state
            or second.lease_expired != first.lease_expired
            or second.record_age_seconds is None
            or second.record_age_seconds < first.record_age_seconds):
        return _error("recovery_raced", 409)

    revoked_at = clock()
    waited = _quiesce(clock, sleeper, revoked_at)
    if waited < QUIESCENCE_WAIT_SECONDS:
        # The gate did not complete. Nothing below this line may run: cleanup
        # and terminalization both assume no old-generation call can still land.
        return _error("quiescence_incomplete", 503)

    settled, refusal = _observe(ledger, run_id)
    if refusal is not None:
        return refusal
    if settled is None:
        return _error("record_observation_unavailable", 503)
    if settled.ambiguous is None or settled.in_flight is None:
        return _error("quiescence_unprovable", 503)

    if settled.ambiguous:
        # An ambiguous operation may have committed at the provider after the
        # client gave up. Cleaning would destroy the only evidence that could
        # ever resolve it, so the resources stay quarantined and the run
        # terminalizes as blocked rather than as a verdict.
        digest = _recovery_digest(run_id, "ambiguous_provider_effect", waited, {})
        ledger.record_terminal(run_id, "INSTRUMENT_BLOCKED", digest)
        return {"status": "ok", "state": "TERMINAL", "runId": run_id,
                "verdict": "INSTRUMENT_BLOCKED",
                "failureCode": "ambiguous_provider_effect",
                "quarantined": True, "quiescenceWaitedSeconds": int(waited),
                "residueProven": False, "residue": {},
                "recoveryDigest": digest}, 200

    if settled.in_flight:
        # Not quiescent. Terminalizing now would race a live worker's write.
        return _error("quiescence_timeout", 409)

    residue: Dict[str, int] = {}
    residue_proven = False
    if cleaner is not None:
        residue_rows = dict(cleaner(run_id))
        residue = {str(k): int(v) for k, v in residue_rows.items()}
        residue_proven = True

    digest = _recovery_digest(run_id, "recovered_after_interruption", waited, residue)
    ledger.record_terminal(run_id, "INSTRUMENT_BLOCKED", digest)
    if residue_proven:
        ledger.append_cleanup_result(run_id, digest, residue)

    return {"status": "ok", "state": "TERMINAL", "runId": run_id,
            "verdict": "INSTRUMENT_BLOCKED",
            "failureCode": "recovered_after_interruption",
            "quarantined": False, "quiescenceWaitedSeconds": int(waited),
            "residueProven": residue_proven, "residue": residue,
            "recoveryDigest": digest}, 200


def _now_epoch() -> int:
    import time
    return int(time.time())


def _now_float() -> float:
    import time
    return time.time()


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)
