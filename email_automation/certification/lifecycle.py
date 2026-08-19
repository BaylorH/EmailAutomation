"""prepare → run → status → abort, driven through the permanent run ledger.

The HTTP routes are a thin shell over this module. Keeping the lifecycle out of
the request context is what lets the cases that actually matter -- a reused run
id, a run claimed twice, an incomplete deployment identity -- be exercised
directly, instead of through status codes that flatten them into "400".

Every function returns ``(payload, http_status)``. Payloads are SANITIZED by
construction: states, verdicts, phases, counts and digests. No recipient, body,
subject, sheet id, fixture alias, or exception text, because a route response is
the easiest place for a fixture value to escape the fixture.

One deliberate limitation, stated rather than hidden: the default ledger is
in-memory and therefore process-scoped. It enforces the full state machine but
does not survive a restart, so it is correct for a single-instance twin
(containerConcurrency 1, maxScale 1) and is NOT yet the durable Firestore
ledger the plan ultimately requires. The state machine lives in ledger.py so
both implementations agree by construction.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional, Tuple

from email_automation.certification import ledger as ledger_module
from email_automation.certification import scenarios
from email_automation.certification.canonical_json import canonical_digest
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


def default_ledger() -> ledger_module.InMemoryRunLedger:
    return _DEFAULT_LEDGER


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


def run(body: Mapping[str, Any], *, caller_identity_digest: str = "",
        ledger=None, environ: Optional[Mapping[str, str]] = None) -> Response:
    from email_automation.certification import runner as runner_module

    ledger = ledger if ledger is not None else default_ledger()
    environ = environ if environ is not None else os.environ
    identity = _identity(environ)
    if identity is None:
        return _error("instrument_unavailable", 503)

    run_id = body["runId"]
    authorization = ledger.peek_ephemeral(run_id)
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


def _now_epoch() -> int:
    import time
    return int(time.time())
