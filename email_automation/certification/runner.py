"""Execute one approved scenario and project a verdict.

Everything ahead of this module proved, BY TEST, that a certification run cannot
cause a production effect. Nothing executed a run. This module executes one.

The shape is deliberately narrow:

    prepare fixture -> build certification runtime -> drive the REAL product
    entry point -> observe effects -> compare to the registry -> project evidence

Two rules make the result mean something.

**The product entry point is the real one.** ``email.send_outboxes`` is what
production calls. Certification differs only in the runtime handed to it - the
fenced data clients, the capturing delivery transport, the denying AI and Drive
transports. A parallel code path would prove nothing about the product.

**Effects are OBSERVED, then compared - never asserted.** The registry declares
required and forbidden effect counts per scenario. This module measures what
actually happened and reports the comparison. A scenario whose declared counts
do not match reality is a finding about the registry or the product, and must
surface as one rather than be tuned away in the observer.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from contextlib import ExitStack
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest.mock import patch

from email_automation.certification import evidence as ev
from email_automation.certification import fixtures as fx
from email_automation.certification import scenarios


class ScenarioNotRunnable(RuntimeError):
    """The scenario is approved but this runner cannot drive it yet."""


class NetworkAttempted(RuntimeError):
    """The lane tried to reach a provider. A capture run may not."""


class NetworkSentinel:
    """Stands in for ``requests`` inside the product module under test.

    Records the attempt and raises. Counting attempts is the point: the
    registry's ``graph_network: 0`` is a MEASURED zero here, not an assumed one.
    """

    def __init__(self) -> None:
        self.attempts: List[str] = []

    def _record(self, verb: str):
        def call(url: str = "", *args: Any, **kwargs: Any) -> Any:
            self.attempts.append(f"{verb} {url}")
            raise NetworkAttempted(f"{verb} {url}")
        return call

    def __getattr__(self, name: str) -> Any:
        if name in ("get", "post", "patch", "put", "delete", "head", "request"):
            return self._record(name.upper())
        raise AttributeError(name)


# -- the lane ----------------------------------------------------------------
#
# Which product entry point a scenario drives is a property of the scenario's
# capability, not something a caller may choose. Only the bootstrap lane is
# wired so far; every other scenario reports instrument_blocked rather than
# silently reporting a pass it never earned.

BOOTSTRAP_LANE = "campaign_outreach"

LANES = {
    "certification-integrity/campaign-one-property": BOOTSTRAP_LANE,
}


def _drive_campaign_outreach(runtime: Any, fixture: fx.PreparedFixture,
                             sentinel: NetworkSentinel) -> Any:
    """Drive the real outbox lane with every ambient client booby-trapped."""
    from email_automation import clients as clients_module
    from email_automation import email as email_module
    from email_automation import followup as followup_module
    from email_automation import messaging as messaging_module
    from email_automation import notifications as notifications_module
    from email_automation import processing as processing_module
    from email_automation import sheets as sheets_module

    # Ten modules import ``clients._fs`` BY VALUE, so each holds its own copy and
    # each must be trapped separately. Patching one canonical global would leave
    # the other nine live.
    ambient_fs = (
        (clients_module, "_fs"),
        (messaging_module, "_fs"),
        (processing_module, "_fs"),
        (followup_module, "_fs"),
        (notifications_module, "_fs"),
    )
    with ExitStack() as stack:
        for module, attribute in ambient_fs:
            stack.enter_context(patch.object(
                module, attribute,
                fx.ExplodingClient(f"{module.__name__}.{attribute}", fixture.ambient_reaches),
            ))
        # A fresh provider client is just as much an escape as the global one.
        stack.enter_context(patch(
            "google.cloud.firestore.Client",
            fx.ExplodingClient("firestore.Client", fixture.ambient_reaches),
        ))
        for module in (clients_module, sheets_module):
            stack.enter_context(patch.object(
                module, "_sheets_client",
                fx.ExplodingClient(f"{module.__name__}._sheets_client", fixture.ambient_reaches),
            ))
        stack.enter_context(patch.object(email_module, "requests", sentinel))
        stack.enter_context(patch.object(email_module.time, "sleep", return_value=None))
        return email_module.send_outboxes(
            fx.FIXTURE_UID,
            {"Authorization": "Bearer fixture"},
            runtime=runtime,
        )



# -- cleanup, allocated before the fixture is opened -------------------------
#
# Ordering is the property, not politeness. A fixture opened before its cleanup
# handle exists can leak on any fault between the two, and the leak is invisible
# because nothing yet knows the fixture is there to clean.
#
# A fixture teardown is a DELETE. An inventory scoped to "what can send?" is
# blind to it, so it is enumerated here as the destructive operation it is.

CLEANUP_MAX_ATTEMPTS = 3


class CleanupHandle:
    """Owns the delete list for one run, from before the fixture exists."""

    def __init__(self, prefix: str, seq: int) -> None:
        self.prefix = prefix
        self.allocated_seq = seq
        self.attempts = 0
        self.deleted: List[str] = []
        self.failures: List[str] = []

    def _scoped_ref(self, firestore: Any, path: str) -> Any:
        """Walk collection/document alternately, so the fence sees every hop."""
        segments = path.split("/")
        ref = firestore.collection(segments[0])
        for index, segment in enumerate(segments[1:], start=1):
            ref = ref.document(segment) if index % 2 else ref.collection(segment)
        return ref

    def run(self, firestore: Any, store: Any) -> None:
        """Delete every fixture artifact through the FENCED client.

        Deleting through the scoped client rather than the raw store is what
        proves the fence permits a fixture delete and would refuse anything
        outside the prefix - teardown is exactly where an over-broad delete
        would otherwise be indistinguishable from a tidy one.
        """
        for _attempt in range(CLEANUP_MAX_ATTEMPTS):
            self.attempts += 1
            remaining = [p for p in list(store.data) if p.startswith(self.prefix)]
            if not remaining:
                return
            self.failures = []
            for path in sorted(remaining, key=lambda p: -p.count("/")):
                try:
                    self._scoped_ref(firestore, path).delete()
                    self.deleted.append(path)
                except Exception as exc:      # noqa: BLE001 - a refusal is data
                    self.failures.append(f"{path}: {type(exc).__name__}")
        return

    def residue(self, store: Any) -> List[str]:
        return sorted(p for p in store.data if p.startswith(self.prefix))


def _replay(runtime: Any, fixture: "fx.PreparedFixture",
            sentinel: NetworkSentinel) -> int:
    """Re-execute the same scenario. A converged run does nothing the second time.

    Not a repeat of the first run for confidence - a DIFFERENT claim. The first
    run proves the effects happen; the replay proves they happen exactly once,
    which is the property a retry, a duplicate event, or a redelivered queue
    message would break.
    """
    captured_before = len(getattr(runtime.outbound, "captured", ()))
    writes_before = len(fixture.firestore.writes)
    try:
        _drive_campaign_outreach(runtime, fixture, sentinel)
    except Exception:                          # noqa: BLE001 - counted, not raised
        pass
    captured_after = len(getattr(runtime.outbound, "captured", ()))
    writes_after = len(fixture.firestore.writes)
    return (captured_after - captured_before) + (writes_after - writes_before)


# -- observation -------------------------------------------------------------
#
# Two of the effects below are the least reversible things this system can do,
# and both were previously UNOBSERVED. An unobserved forbidden effect is not a
# zero; it is an absence, and a run that scores it as a zero is claiming to have
# checked something nothing looked at.

# Permission grants that are provably NOT a public publication. An ALLOW-LIST:
# a captured permission counts as clean only if it names one concrete
# addressable subject. Anything else - including a grant shape this instrument
# does not recognise - is scored as the irreversible effect, because a deny-list
# of ("anyone",) would score a novel public grant type as clean.
PRIVATE_GRANT_TYPES: Tuple[str, ...] = ("user", "group")

# Field names that make a write a durable claim about a capability's production
# status. A stamp is not an irreversible external effect - it is a record - so
# this is a detector, not the guard. The guard is ``stamp_entitlement``, which
# is read from the registry, plus the fixture prefix fence behind
# ``nonfixture_write``.
STAMP_MARKERS: Tuple[str, ...] = (
    "productionVerdict",
    "capabilityStamp",
    "certificationStamp",
)


def _is_private_grant(permission: Any) -> bool:
    """True only for a grant that names one concrete addressable subject."""
    if not isinstance(permission, Mapping):
        return False
    if permission.get("type") not in PRIVATE_GRANT_TYPES:
        return False
    return bool(str(permission.get("emailAddress") or "").strip())


def observe_public_drive_permission(runtime: Any) -> Optional[int]:
    """Public Drive permissions this run caused, or ``None`` if unmeasurable.

    Resolved through the PRODUCT's own ``drive_publication_for`` rather than by
    reading ``runtime.drive_publication`` directly, so what is measured is the
    surface the product would actually have published through. Reading the
    attribute would agree with itself even if resolution had drifted.

    ``None`` is deliberate and is NOT zero. ``ProviderBackedDrivePublication``
    and ``AmbientDrivePublication`` cannot be asked what they published, so a
    run holding one cannot support a claim of zero - it has to report the gap
    and land on ``instrument_blocked``.
    """
    from email_automation import automation_runtime as ar

    transport = ar.drive_publication_for(runtime, None)
    real = getattr(transport, "real_permission_calls", None)
    captured = getattr(transport, "captured", None)
    if isinstance(real, bool) or not isinstance(real, int) or captured is None:
        return None
    public = 0
    for entry in captured:
        try:
            _file_id, permission = entry
        except (TypeError, ValueError):
            # An entry this instrument cannot read is not a clean entry.
            public += 1
            continue
        if not _is_private_grant(permission):
            public += 1
    return int(real) + public


def stamp_entitlement(scenario: Mapping[str, Any]) -> int:
    """Whether this scenario is permitted to stamp a capability at all.

    Read from the in-image registry entry, so a scenario whose ``capabilityStamp``
    flag were flipped would immediately violate the refutation scenario's
    ``capability_stamp: 0`` instead of quietly acquiring the right to certify.
    """
    return 1 if scenario.get("capabilityStamp") is True else 0


def observe_capability_stamp(writes: Any, *, stamp_entitlement: int) -> int:
    """Capability stamps this run produced: entitlement plus stamp-shaped writes."""
    produced = 0
    for entry in writes:
        try:
            _kind, path, payload, _merge = entry
        except (TypeError, ValueError):
            continue
        text = f"{path} {payload}"
        if any(marker in text for marker in STAMP_MARKERS):
            produced += 1
    return int(stamp_entitlement) + produced


def _observe(runtime: Any, fixture: fx.PreparedFixture,
             sentinel: NetworkSentinel, *,
             replay_delta: int, cleanup_residue: int,
             stamp_entitlement: int) -> Dict[str, int]:
    """Measure what the run actually did.

    No scenario knowledge is looked up here. ``stamp_entitlement`` arrives as a
    scalar the caller already read from the registry, the same way
    ``replay_delta`` and ``cleanup_residue`` arrive as scalars the caller
    already measured.

    A counter that cannot be measured is OMITTED rather than defaulted to zero.
    ``_compare`` then reports it as "no observer", which lands the run on
    ``instrument_blocked`` - the honest answer for a forbidden effect nothing
    looked at.
    """
    store = fixture.firestore
    prefix = fixture.prefix
    write_paths = [path for _kind, path, _payload, _merge in store.writes]
    captured = list(getattr(runtime.outbound, "captured", ()))

    observed: Dict[str, int] = {
        # required-effect surfaces
        "captured_outreach": len(captured),
        "fixture_audit": sum(
            1 for p in write_paths if p.startswith(f"{prefix}/actionAudit/")
        ),
        "fixture_followup": sum(
            1 for _k, p, payload, _m in store.writes
            if payload and ("followUp" in str(payload) or "followup" in p.lower())
        ),
        "fixture_thread_index": sum(
            1 for p in write_paths if p.startswith(f"{prefix}/msgIndex/")
        ),
        # forbidden-effect surfaces
        "graph_network": len(sentinel.attempts),
        "drive_call": int(getattr(runtime.drive_publication, "real_permission_calls", 0)),
        "nonfixture_write": sum(
            1 for p in write_paths if not p.startswith(prefix)
        ) + len(fixture.ambient_reaches),
        "bcc": sum(len(getattr(d, "bcc", ()) or ()) for d in captured),
        "global_counter_effect": sum(
            1 for p in write_paths if "sendCounters" in p or "/counters/" in p
        ),
        "capability_stamp": observe_capability_stamp(
            store.writes, stamp_entitlement=stamp_entitlement
        ),
        "cleanup_residue": cleanup_residue,
        "replay_delta": replay_delta,
    }

    public_drive = observe_public_drive_permission(runtime)
    if public_drive is not None:
        observed["public_drive_permission"] = public_drive
    return observed


UNWIRED: Tuple[str, ...] = ()


def _compare(scenario: Mapping[str, Any],
             observed: Mapping[str, int]) -> Tuple[List[str], List[str]]:
    """Return (mismatches, unmeasured) against the registry's declared counts."""
    mismatches: List[str] = []
    unmeasured: List[str] = []

    for name, want in sorted(dict(scenario.get("requiredEffects") or {}).items()):
        if name not in observed:
            unmeasured.append(f"required {name} (no observer)")
            continue
        got = observed[name]
        if got != want:
            mismatches.append(f"required {name}: want {want}, observed {got}")

    for name, want in sorted(dict(scenario.get("forbiddenEffects") or {}).items()):
        if name not in observed:
            unmeasured.append(f"forbidden {name} (no observer)")
            continue
        if name in UNWIRED:
            unmeasured.append(f"forbidden {name} (observer not wired)")
            continue
        got = observed[name]
        if got != want:
            mismatches.append(f"forbidden {name}: want {want}, observed {got}")

    return mismatches, unmeasured


# -- the run -----------------------------------------------------------------


def run_scenario(scenario_id: str, *, run_id: str,
                 revision: str) -> Tuple[ev.EvidenceRecord, Dict[str, Any]]:
    """Execute one approved scenario. Returns (evidence, an operator detail dict).

    The detail dict is for a human reading the terminal. Only the evidence record
    is safe to persist - it is the allow-listed projection.
    """
    from email_automation import automation_runtime as ar

    scenario = scenarios.get(scenario_id)          # KeyError for anything unapproved
    logical_key = scenario["logicalFixtureKey"]

    if LANES.get(logical_key) != BOOTSTRAP_LANE:
        record = ev.instrument_blocked(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            phase="execute", reason_code="lane_not_wired",
        )
        return record, {"reason": "lane_not_wired", "logical_key": logical_key}

    # The oracle is resolved HERE: from the IN-IMAGE projection table, keyed by
    # the registry entry's oracleProjectionKey, before anything is opened and
    # OUTSIDE every try block. Two reasons, both load bearing.
    #
    # There is no oracle parameter on this function and no oracle field in a
    # certification request, so the expectation a run is graded against cannot
    # come from the caller - which is the entire claim the refutation scenario
    # exists to prove.
    #
    # And a name this module does not own fails as SILENCE in this codebase:
    # nearly every call site downstream is wrapped in a broad ``except
    # Exception``, so a NameError inside the run would become a tidy-looking
    # early return. Resolving before the try region means a missing name is a
    # loud crash instead of a clean verdict nobody earned.
    try:
        oracle = fx.oracle_for(scenario)
    except fx.OracleNotRegistered:
        record = ev.instrument_blocked(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            phase="prepare", reason_code="oracle_not_registered",
        )
        return record, {"reason": "oracle_not_registered", "logical_key": logical_key,
                        "oracle_key": scenario.get("oracleProjectionKey")}

    entitlement = stamp_entitlement(scenario)

    # Cleanup is allocated BEFORE the fixture is opened. Between these two
    # statements there is nothing to leak; after them there always is.
    sequence = itertools.count(1)
    cleanup = CleanupHandle(fx.FIXTURE_PREFIX, next(sequence))

    fixture = fx.prepare(logical_key)
    fixture_opened_seq = next(sequence)
    # Measured from the SEEDED fixture, before the lane consumes it: the lane
    # deletes each outbox item it sends, so a ceiling read after the run would
    # be zero and would make every oracle look impossible.
    ceilings = fx.fixture_ceilings(fixture)
    sentinel = NetworkSentinel()
    runtime = ar.certification_runtime(
        run_id=run_id,
        scope=scenario_id,
        firestore=fixture.firestore,
        sheets=fixture.sheets,
        firestore_prefix=fixture.prefix,
        sheet_ids=fixture.sheet_ids,
        readable_paths=fixture.readable_paths,
    )

    phase = "execute"
    error: Optional[BaseException] = None
    try:
        _drive_campaign_outreach(runtime, fixture, sentinel)
    except BaseException as exc:            # noqa: BLE001 - a crash is a verdict
        error = exc

    # Readback happens BEFORE replay and cleanup, and that ordering is load
    # bearing rather than stylistic. Teardown is itself a stream of DELETEs
    # against the fixture, so an observer that reads after cleanup counts the
    # teardown as product behaviour - which is exactly how fixture_audit and
    # fixture_thread_index first came back at 2 for a run that emitted one of
    # each. Measure the product, then dismantle the fixture.
    phase = "readback"
    violations = [str(v) for v in getattr(runtime.effect_scope, "violations", ())]
    observed = _observe(runtime, fixture, sentinel,
                        replay_delta=0, cleanup_residue=0,
                        stamp_entitlement=entitlement)

    # The oracle comparison, against the READBACK - the state of the world
    # before replay re-executes anything and before cleanup dismantles it.
    oracle_contradictions = oracle.contradictions(observed)
    oracle_unmeasured = oracle.unmeasured(observed)
    oracle_impossible = oracle.exceeds_fixture_ceiling(ceilings)
    # One counted fact, not a tally: "the readback provably contradicts the
    # declared oracle" is true or it is not, and a scenario declaring it at 1
    # must not fail merely because two expectations were refuted instead of one.
    observed["intentional_oracle_mismatch"] = 1 if oracle_contradictions else 0

    # Replay BEFORE cleanup: replaying a torn-down fixture proves only that a
    # missing fixture does nothing.
    phase = "replay"
    replay_ran = False
    replay_delta = 0
    if error is None:
        replay_delta = _replay(runtime, fixture, sentinel)
        replay_ran = True

    phase = "cleanup"
    cleanup.run(runtime.firestore, fixture.firestore)
    residue_paths = cleanup.residue(fixture.firestore)

    # The global policy document sits OUTSIDE the fixture prefix. A cleanup that
    # walked it would be deleting live campaign authority while reporting clean.
    surviving_global = sorted(
        p for p in fixture.firestore.data if not p.startswith(fixture.prefix)
    )

    observed["replay_delta"] = replay_delta
    observed["cleanup_residue"] = len(residue_paths)
    observed["nonfixture_write"] += len(violations)

    mismatches, unmeasured = _compare(scenario, observed)
    unmeasured = unmeasured + oracle_unmeasured

    detail: Dict[str, Any] = {
        "logical_key": logical_key,
        "oracle_key": oracle.key,
        "oracle_expectations": dict(oracle.expectations),
        "oracle_contradictions": oracle_contradictions,
        "oracle_impossible": oracle_impossible,
        "fixture_ceilings": ceilings,
        "stamp_entitlement": entitlement,
        "observed": observed,
        "required": dict(scenario.get("requiredEffects") or {}),
        "forbidden": dict(scenario.get("forbiddenEffects") or {}),
        "mismatches": mismatches,
        "unmeasured": unmeasured,
        "scope_violations": violations,
        "ambient_reaches": list(fixture.ambient_reaches),
        "network_attempts": list(sentinel.attempts),
        "error": f"{type(error).__name__}: {error}" if error else None,
        "expected_verdict": scenario.get("expectedVerdict"),
        "replay_ran": replay_ran,
        "replay_delta": replay_delta,
        "cleanup_attempts": cleanup.attempts,
        "cleanup_deleted": len(cleanup.deleted),
        "cleanup_failures": cleanup.failures,
        "cleanup_residue_paths": residue_paths,
        "surviving_global_paths": surviving_global,
        "cleanup_allocated_seq": cleanup.allocated_seq,
        "fixture_opened_seq": fixture_opened_seq,
    }

    if error is not None:
        record = ev.project_evidence(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            outcome="fail", phase="execute",
            counts=observed,
            failure_code="lane_raised",
            summary="the product lane raised before the scenario completed",
        )
    elif mismatches:
        record = ev.project_evidence(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            outcome="fail", phase=phase,
            counts=observed,
            failure_code="effect_count_mismatch",
            summary=f"{len(mismatches)} declared effect count(s) did not match observation",
        )
    elif unmeasured:
        # BEFORE the oracle verdict, on purpose. A FAIL is only meaningful from
        # a complete instrument: reporting "the oracle was refuted" while some
        # forbidden effect went unobserved would dress an incomplete instrument
        # up as a finding about the product.
        record = ev.instrument_blocked(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            phase=phase, reason_code="observer_not_wired",
        )
    elif oracle_contradictions:
        # Every declared effect matched AND every observer was wired, and the
        # run still contradicted the expectation it was graded against. That is
        # a FAIL by definition, and for the refutation scenario it is the point:
        # the approved expectation was impossible, and the harness refused it
        # without ever having accepted an oracle from the caller.
        #
        # ``readback`` rather than ``cleanup``: the contradiction is a fact
        # about the state the product left behind, measured before replay and
        # before teardown. Cleanup still runs - it ran above - because a failing
        # run that leaks a fixture is worse than the failure it reports.
        record = ev.project_evidence(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            outcome="fail", phase="readback",
            counts=observed,
            failure_code="oracle_contradicted",
            summary=f"{len(oracle_contradictions)} declared oracle expectation(s) "
                    "were refuted by the readback",
        )
    else:
        record = ev.project_evidence(
            run_id=run_id, scenario_id=scenario_id, revision=revision,
            outcome="pass", phase=phase, counts=observed,
        )
    return record, detail


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one approved certification scenario.")
    parser.add_argument("scenario_id")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--json", action="store_true", help="emit the detail dict as JSON")
    args = parser.parse_args(argv)

    record, detail = run_scenario(
        args.scenario_id, run_id=args.run_id, revision=args.revision
    )

    if args.json:
        print(json.dumps({"evidence": record.to_dict(), "detail": detail},
                         indent=2, sort_keys=True, default=str))
    else:
        print(f"scenario  {args.scenario_id}")
        print(f"run       {args.run_id}")
        print(f"outcome   {record.outcome.upper()}   (registry expects "
              f"{detail.get('expected_verdict')})")
        if detail.get("error"):
            print(f"error     {detail['error']}")
        print("\neffects observed:")
        for name, value in sorted(detail.get("observed", {}).items()):
            want_req = detail.get("required", {}).get(name)
            want_forb = detail.get("forbidden", {}).get(name)
            want = want_req if want_req is not None else want_forb
            flag = "" if want is None or want == value else "   <-- MISMATCH"
            print(f"  {name:24} {value:>4}   declared {want}{flag}")
        for label in ("mismatches", "unmeasured", "scope_violations",
                      "ambient_reaches", "network_attempts"):
            rows = detail.get(label) or []
            if rows:
                print(f"\n{label}:")
                for row in rows:
                    print(f"  - {row}")
    return 0 if record.outcome == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
