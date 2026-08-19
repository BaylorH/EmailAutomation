"""`/certification/recover` and `/certification/review-input`.

Two routes with opposite hazards, which is why they are tested together.

`recover` touches a run that MAY ALREADY HAVE EXECUTED. Its hazard is doing
anything at all: a recovery that runs business logic is how a "recovered" run
causes a second effect. So most of this file is about what recover must NOT do,
and the strongest of those tests do not read the implementation's behaviour at
all -- they read its call graph and poison its execution entry point.

`review-input` is the one route in the instrument that returns raw captured
text. Its hazard is being reachable. So its tests drive the REAL CLI allowlist
rather than a retyped copy, and prove the escape from the sanitization contract
is bounded to exactly this one operation.
"""

import ast
import json
import os
import re
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

import service
from email_automation.certification import input_handoff
from email_automation.certification import ledger as ledger_module
from email_automation.certification import lifecycle

REPO_ROOT = Path(__file__).resolve().parents[1]
REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
OPERATOR = "sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com"
SUB = "104729384756102938475"
AUDIENCE = "https://process-user-certification-abc123-uc.a.run.app"

TWIN_ENV = {
    "K_SERVICE": "process-user-certification",
    "K_REVISION": "process-user-certification-00001-abc",
    "SITESIFT_SOURCE_REVISION": REVISION,
    "SITESIFT_IMAGE_DIGEST": "sha256:" + "b" * 64,
    "SITESIFT_PRODUCTION_CANDIDATE_REVISION": "process-user-00042-xyz",
    "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION": "7",
    "SITESIFT_FIXTURE_CONFIG_DIGEST": "d" * 64,
    "SITESIFT_CERTIFICATION_AUDIENCE": AUDIENCE,
    "SITESIFT_CERTIFICATION_OPERATOR_EMAIL": OPERATOR,
    "SITESIFT_CERTIFICATION_OPERATOR_SUB": SUB,
}


# ---------------------------------------------------------------------------
# The observable ledger
# ---------------------------------------------------------------------------
#
# This SUBCLASSES the real ``InMemoryRunLedger`` rather than reimplementing it.
# The state machine under test has to be the shipped one -- a hand-built stub
# would let recover pass against transitions the real ledger forbids.
#
# The only thing added is the recovery observation a durable ledger can answer
# and an in-process one cannot: server-side record age, lease expiry, and the
# in-flight/ambiguous registration counts for the revoked generation.


class ObservableLedger(ledger_module.InMemoryRunLedger):

    def __init__(self, **observation):
        super().__init__()
        self.observation = dict(observation)
        self.observation_reads = 0

    def recovery_observation(self, run_id):
        self.observation_reads += 1
        return dict(self.observation)


class HangingLedger(ObservableLedger):
    """A read with no deadline HANGS; ``except Exception`` cannot catch a hang."""

    def recovery_observation(self, run_id):
        while True:
            time.sleep(0.05)


class FakeClock:
    """Advances only when something sleeps. Makes a 75s gate assertable."""

    def __init__(self, start=1_000_000.0):
        self.now = float(start)
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept += float(seconds)
        self.now += float(seconds)


def claimed_run(ledger, run_id, *, running=False):
    """Drive the REAL ledger to CLAIMED (or RUNNING) via its real transitions."""
    from email_automation.certification.models import (
        CertificationRequest,
        RunAuthorization,
    )
    from email_automation.certification import scenarios
    from email_automation.certification.canonical_json import canonical_digest

    request = CertificationRequest(
        scenario_id="campaign-one-property",
        run_id=run_id,
        expected_revision=REVISION,
    )
    authorization = RunAuthorization.create(
        scenario_id=request.scenario_id,
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
    ledger.begin_preparing(request)
    ledger.mark_prepared(request, authorization)
    ledger.claim(request, authorization)
    if running:
        ledger.mark_running(run_id, "execute")
    return request


def quiescent_observation(state=ledger_module.CLAIMED, age=10_000):
    return {"state": state, "recordAgeSeconds": age, "leaseExpired": True,
            "inFlight": 0, "ambiguous": 0}


# ---------------------------------------------------------------------------
# recover: the gates
# ---------------------------------------------------------------------------


class RecoverGateTests(unittest.TestCase):
    """Every gate refuses BEFORE anything is terminalized."""

    def _recover(self, ledger, run_id, **kwargs):
        clock = kwargs.pop("clock", None) or FakeClock()
        return lifecycle.recover(
            {"runId": run_id, "expectedRevision": REVISION},
            caller_identity_digest="c" * 64,
            ledger=ledger,
            clock=clock.time,
            sleeper=clock.sleep,
            **kwargs,
        )

    def test_an_unknown_run_is_not_invented(self):
        ledger = ObservableLedger(**quiescent_observation())
        payload, code = self._recover(ledger, "cert-recover-unknown")
        self.assertEqual(code, 404)
        self.assertEqual(payload["reason"], "unknown_run")

    def test_a_prepared_run_belongs_to_abort_not_recovery(self):
        """The two domains are disjoint by allowlist, not by convention."""
        ledger = ObservableLedger(
            **quiescent_observation(state=ledger_module.PREPARED))
        from email_automation.certification.models import CertificationRequest
        ledger.begin_preparing(CertificationRequest(
            scenario_id="campaign-one-property",
            run_id="cert-recover-prepared", expected_revision=REVISION))
        payload, code = self._recover(ledger, "cert-recover-prepared")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "not_recoverable")
        self.assertEqual(ledger.state("cert-recover-prepared"),
                         ledger_module.PREPARING)

    def test_recoverable_states_are_an_allowlist(self):
        self.assertEqual(
            set(lifecycle.RECOVERABLE_STATES),
            {ledger_module.CLAIMED, ledger_module.RUNNING},
        )

    def test_a_record_one_second_short_of_the_threshold_is_refused(self):
        """719s. An execution started at the service timeout may still be live."""
        ledger = ObservableLedger(**quiescent_observation(age=719))
        claimed_run(ledger, "cert-recover-young")
        payload, code = self._recover(ledger, "cert-recover-young")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "record_too_recent")
        self.assertEqual(ledger.state("cert-recover-young"), ledger_module.CLAIMED)

    def test_a_record_exactly_at_the_threshold_is_accepted(self):
        """720s = the 540s service timeout plus the 180s margin, exactly."""
        ledger = ObservableLedger(**quiescent_observation(age=720))
        claimed_run(ledger, "cert-recover-exact")
        payload, code = self._recover(ledger, "cert-recover-exact")
        self.assertEqual(code, 200, payload)
        self.assertEqual(ledger.state("cert-recover-exact"), ledger_module.TERMINAL)

    def test_the_threshold_is_the_service_timeout_plus_the_margin(self):
        self.assertEqual(lifecycle.RECOVERY_MIN_RECORD_AGE_SECONDS, 720)
        self.assertEqual(
            lifecycle.SERVICE_TIMEOUT_SECONDS + lifecycle.RECOVERY_AGE_MARGIN_SECONDS,
            lifecycle.RECOVERY_MIN_RECORD_AGE_SECONDS,
        )

    def test_an_unexpired_lease_is_refused(self):
        observation = quiescent_observation()
        observation["leaseExpired"] = False
        ledger = ObservableLedger(**observation)
        claimed_run(ledger, "cert-recover-leased")
        payload, code = self._recover(ledger, "cert-recover-leased")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "lease_active")
        self.assertEqual(ledger.state("cert-recover-leased"), ledger_module.CLAIMED)

    def test_a_ledger_that_cannot_be_observed_refuses_rather_than_proceeds(self):
        """The REAL default in-memory ledger cannot answer these gates.

        It is process-scoped, so it has no SERVER-ASSIGNED record age and
        therefore no provable lease or in-flight count. Recovery against it must
        refuse -- not proceed on the assumption that unmeasured means safe.

        The refusal names the missing FACT rather than a missing answer. The
        in-memory ledger now implements ``recovery_observation`` (shared with
        the durable one, so the two cannot drift), and what it reports is a real
        observation carrying ``None`` where it can prove nothing. "The store
        answered and had no age" and "there was no answer at all" need different
        operator responses; ``test_an_unanswerable_observation_refuses_rather_``
        ``than_proceeds`` below still pins the second.
        """
        ledger = ledger_module.InMemoryRunLedger()
        claimed_run(ledger, "cert-recover-blind")
        payload, code = lifecycle.recover(
            {"runId": "cert-recover-blind", "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger)
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "record_age_unprovable")
        self.assertEqual(ledger.state("cert-recover-blind"), ledger_module.CLAIMED)
        # None means UNPROVABLE, never zero and never "old enough".
        observed = ledger.recovery_observation("cert-recover-blind")
        for gate in ("recordAgeSeconds", "leaseExpired", "inFlight"):
            self.assertIsNone(observed[gate], gate)

    def test_a_hanging_readback_is_bounded_rather_than_waited_on(self):
        """A Firestore read with no deadline hangs, and no `except` catches it."""
        ledger = HangingLedger(**quiescent_observation())
        claimed_run(ledger, "cert-recover-hang")
        started = time.monotonic()
        with patch.object(lifecycle, "READBACK_DEADLINE_SECONDS", 0.4):
            payload, code = self._recover(ledger, "cert-recover-hang")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "readback_deadline_exceeded")
        self.assertLess(elapsed, 10, "the readback was not bounded")
        self.assertEqual(ledger.state("cert-recover-hang"), ledger_module.CLAIMED)

    def test_an_unanswerable_observation_refuses_rather_than_proceeds(self):
        """No observation at all is a different fact from a missing field.

        Both refuse. Neither may reach terminalization: a recovery that
        terminalizes on an unmeasured gate has measured nothing.
        """

        class SilentLedger(ObservableLedger):
            def recovery_observation(self, run_id):
                self.observation_reads += 1
                return None

        ledger = SilentLedger()
        claimed_run(ledger, "cert-recover-silent")
        payload, code = self._recover(ledger, "cert-recover-silent")
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "record_observation_unavailable")
        self.assertEqual(ledger.state("cert-recover-silent"), ledger_module.CLAIMED)

    def test_an_observation_that_vanishes_between_reads_refuses(self):
        """Each of the three reads is a gate; each must refuse on its own."""
        for vanish_at in (2, 3):
            with self.subTest(read=vanish_at):

                class VanishingLedger(ObservableLedger):
                    def recovery_observation(self, run_id):
                        self.observation_reads += 1
                        if self.observation_reads >= vanish_at:
                            return None
                        return quiescent_observation()

                ledger = VanishingLedger()
                run_id = f"cert-recover-vanish-{vanish_at}"
                claimed_run(ledger, run_id)
                payload, code = self._recover(ledger, run_id)
                self.assertEqual(code, 503)
                self.assertEqual(payload["reason"],
                                 "record_observation_unavailable")
                self.assertEqual(ledger.state(run_id), ledger_module.CLAIMED)

    def test_a_wrongly_typed_age_is_unprovable_not_coerced(self):
        """"720" is a string, not a measurement. Coercing it would skip the gate."""
        for bad_age in ("720", 720.0, True, None, -1):
            with self.subTest(age=bad_age):
                observation = quiescent_observation()
                observation["recordAgeSeconds"] = bad_age
                ledger = ObservableLedger(**observation)
                run_id = f"cert-recover-age-{type(bad_age).__name__}-{bad_age}"
                claimed_run(ledger, run_id)
                payload, code = self._recover(ledger, run_id)
                self.assertEqual(code, 503)
                self.assertEqual(payload["reason"], "record_age_unprovable")
                self.assertEqual(ledger.state(run_id), ledger_module.CLAIMED)

    def test_a_wrongly_typed_lease_is_unprovable_not_coerced(self):
        """A missing lease must never default to "expired"."""
        for bad_lease in ("expired", 0, 1, None):
            with self.subTest(lease=bad_lease):
                observation = quiescent_observation()
                observation["leaseExpired"] = bad_lease
                ledger = ObservableLedger(**observation)
                run_id = f"cert-recover-lease-{type(bad_lease).__name__}-{bad_lease}"
                claimed_run(ledger, run_id)
                payload, code = self._recover(ledger, run_id)
                self.assertEqual(code, 503)
                self.assertEqual(payload["reason"], "lease_state_unprovable")
                self.assertEqual(ledger.state(run_id), ledger_module.CLAIMED)

    def test_a_failing_readback_is_refused_rather_than_swallowed(self):
        """A store that raises is not a store that answered "quiescent"."""

        class BrokenLedger(ObservableLedger):
            def recovery_observation(self, run_id):
                raise RuntimeError("the certification store is unreachable")

        ledger = BrokenLedger()
        claimed_run(ledger, "cert-recover-broken")
        payload, code = self._recover(ledger, "cert-recover-broken")
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "readback_failed")
        self.assertEqual(ledger.state("cert-recover-broken"), ledger_module.CLAIMED)

    def test_a_second_read_that_disagrees_stops_the_recovery(self):
        """The record moved between the gate and the revoke. That is a race."""

        class MovingLedger(ObservableLedger):
            def recovery_observation(self, run_id):
                self.observation_reads += 1
                if self.observation_reads == 1:
                    return quiescent_observation()
                return quiescent_observation(state=ledger_module.RUNNING)

        ledger = MovingLedger()
        claimed_run(ledger, "cert-recover-raced")
        payload, code = self._recover(ledger, "cert-recover-raced")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "recovery_raced")
        self.assertEqual(ledger.state("cert-recover-raced"), ledger_module.CLAIMED)


# ---------------------------------------------------------------------------
# recover: quiescence
# ---------------------------------------------------------------------------


class RecoverQuiescenceTests(unittest.TestCase):

    def _recover(self, ledger, run_id, clock):
        return lifecycle.recover(
            {"runId": run_id, "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger,
            clock=clock.time, sleeper=clock.sleep)

    def test_the_gate_waits_at_least_seventy_five_seconds(self):
        """60s maximum in-flight call plus a 15s margin, before any cleanup."""
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-quiesce-wait")
        clock = FakeClock()
        payload, code = self._recover(ledger, "cert-quiesce-wait", clock)
        self.assertEqual(code, 200, payload)
        self.assertGreaterEqual(clock.slept, 75)
        self.assertGreaterEqual(payload["quiescenceWaitedSeconds"], 75)

    def test_the_gate_is_the_in_flight_deadline_plus_the_margin(self):
        self.assertEqual(lifecycle.QUIESCENCE_WAIT_SECONDS, 75)
        self.assertEqual(
            lifecycle.MAX_IN_FLIGHT_SECONDS + lifecycle.QUIESCENCE_MARGIN_SECONDS,
            lifecycle.QUIESCENCE_WAIT_SECONDS,
        )

    def test_a_still_registered_operation_blocks_terminalization(self):
        observation = quiescent_observation()
        observation["inFlight"] = 1
        ledger = ObservableLedger(**observation)
        claimed_run(ledger, "cert-quiesce-busy")
        payload, code = self._recover(ledger, "cert-quiesce-busy", FakeClock())
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "quiescence_timeout")
        self.assertEqual(ledger.state("cert-quiesce-busy"), ledger_module.CLAIMED)

    def test_an_ambiguous_operation_quarantines_and_never_cleans(self):
        observation = quiescent_observation()
        observation["ambiguous"] = 1
        ledger = ObservableLedger(**observation)
        claimed_run(ledger, "cert-quiesce-ambiguous")
        cleaned = []
        clock = FakeClock()
        payload, code = lifecycle.recover(
            {"runId": "cert-quiesce-ambiguous", "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger,
            clock=clock.time, sleeper=clock.sleep,
            cleaner=lambda run_id: cleaned.append(run_id) or {"residue": 0})
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["verdict"], "INSTRUMENT_BLOCKED")
        self.assertEqual(payload["failureCode"], "ambiguous_provider_effect")
        self.assertTrue(payload["quarantined"])
        self.assertEqual(cleaned, [], "an ambiguous effect was cleaned up")

    def test_unprovable_registrations_refuse_rather_than_assume_zero(self):
        observation = quiescent_observation()
        observation["inFlight"] = None
        ledger = ObservableLedger(**observation)
        claimed_run(ledger, "cert-quiesce-blind")
        payload, code = self._recover(ledger, "cert-quiesce-blind", FakeClock())
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], "quiescence_unprovable")


# ---------------------------------------------------------------------------
# recover: terminalization honesty
# ---------------------------------------------------------------------------


class RecoverTerminalizationTests(unittest.TestCase):

    def _recover(self, ledger, run_id, **kwargs):
        clock = FakeClock()
        return lifecycle.recover(
            {"runId": run_id, "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger,
            clock=clock.time, sleeper=clock.sleep, **kwargs)

    def test_a_recovered_run_is_never_recorded_as_not_tested(self):
        """A CLAIMED run may have executed. NOT_TESTED would be a lie."""
        for state in (ledger_module.CLAIMED, ledger_module.RUNNING):
            with self.subTest(state=state):
                ledger = ObservableLedger(**quiescent_observation(state=state))
                run_id = f"cert-terminal-{state.lower()}"
                claimed_run(ledger, run_id, running=(state == ledger_module.RUNNING))
                payload, code = self._recover(ledger, run_id)
                self.assertEqual(code, 200, payload)
                self.assertEqual(payload["verdict"], "INSTRUMENT_BLOCKED")
                self.assertNotEqual(payload["verdict"], "NOT_TESTED")
                self.assertEqual(ledger.verdict(run_id), "INSTRUMENT_BLOCKED")

    def test_recovery_can_never_produce_a_pass(self):
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-terminal-nopass")
        payload, _ = self._recover(ledger, "cert-terminal-nopass")
        self.assertNotEqual(payload["verdict"], "PASS")
        self.assertEqual(set(lifecycle.RECOVERY_VERDICTS), {"INSTRUMENT_BLOCKED"})

    def test_cleanup_runs_before_terminalization_and_is_appended(self):
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-terminal-clean")
        order = []
        original = ledger.record_terminal

        def watched(run_id, verdict, digest):
            order.append("terminalize")
            return original(run_id, verdict, digest)

        with patch.object(ledger, "record_terminal", watched):
            payload, code = self._recover(
                ledger, "cert-terminal-clean",
                cleaner=lambda run_id: order.append("cleanup") or {"fixtureRows": 0})
        self.assertEqual(code, 200, payload)
        self.assertEqual(order, ["cleanup", "terminalize"])
        self.assertEqual(payload["residue"], {"fixtureRows": 0})
        row = ledger.export()["cert-terminal-clean"]
        self.assertEqual(row["residue"], {"fixtureRows": 0})
        self.assertEqual(len(row["cleanupEvidenceDigests"]), 1)

    def test_a_second_recover_does_not_rewrite_the_terminal_record(self):
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-terminal-twice")
        first, code = self._recover(ledger, "cert-terminal-twice")
        self.assertEqual(code, 200, first)
        second, code = self._recover(ledger, "cert-terminal-twice")
        self.assertEqual(code, 409)
        self.assertEqual(second["reason"], "not_recoverable")
        self.assertEqual(ledger.verdict("cert-terminal-twice"), "INSTRUMENT_BLOCKED")

    def test_the_response_carries_no_fixture_value(self):
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-terminal-sanitized")
        payload, _ = self._recover(ledger, "cert-terminal-sanitized")
        blob = json.dumps(payload, sort_keys=True)
        for forbidden in ("@", "broker", "100 Fixture Way", "cert-uid-0001"):
            self.assertNotIn(forbidden, blob)


# ---------------------------------------------------------------------------
# recover never executes -- structurally
# ---------------------------------------------------------------------------
#
# "recover does not call execute()" is trivially true of any implementation on
# the day it is written. These tests are about the day after: they must fail for
# an edit that reintroduces execution, without anyone remembering to look.


def recover_call_graph():
    """Every call reachable from ``recover`` through lifecycle's own helpers.

    Read from the SHIPPED source, not from a copy: this is the artifact that
    runs, and a retyped call graph would only be testing itself.
    """
    source = (REPO_ROOT / "email_automation" / "certification" / "lifecycle.py")
    tree = ast.parse(source.read_text())
    functions = {node.name: node for node in tree.body
                 if isinstance(node, ast.FunctionDef)}

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return "<computed>"

    seen, calls = set(), set()

    def walk(name):
        if name in seen or name not in functions:
            return
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call):
                target = dotted(node.func)
                calls.add(target)
                walk(target)

    walk("recover")
    return calls, seen


class RecoverNeverExecutesTests(unittest.TestCase):

    # An ALLOWLIST of every call recovery is permitted to make. A denylist of
    # execution verbs would fail open for the next entry point nobody named,
    # and the thing on the other side of this check is a second real effect on
    # a run that may already have caused its first.
    ALLOWED_CALLS = frozenset({
        # lifecycle's own read/quiesce/terminalize helpers
        "_error", "_observe", "_probe", "_bounded_read", "_coerce_observation",
        "_exact_int", "_quiesce", "_recovery_digest", "_recover_under_fence",
        "default_ledger", "execution_forbidden",
        # types and sanctioned digesting
        "ReadbackTimeout", "RecordObservation", "canonical_digest",
        # injected callables the caller owns
        "clock", "sleeper", "cleaner", "reader",
        # the ledger surface recovery may touch: read, terminalize, append
        "ledger.state", "ledger.record_terminal", "ledger.append_cleanup_result",
        # bounded readback machinery
        "threading.Thread", "thread.start", "thread.join", "thread.is_alive",
        "outcome.get", "row.get", "residue_rows.items",
        # builtins
        "dict", "getattr", "int", "isinstance", "str",
    })

    def test_recovery_calls_only_what_it_is_allowed_to_call(self):
        calls, _reached = recover_call_graph()
        self.assertEqual(
            calls, self.ALLOWED_CALLS,
            "recover's call graph changed. Every call it makes is approved "
            "explicitly, because the one it must never make is an execution.",
        )

    def test_no_execution_entry_point_is_reachable_from_recovery(self):
        """Stated separately from the allowlist so the intent survives a rename."""
        calls, _reached = recover_call_graph()
        rendered = " ".join(sorted(calls))
        for forbidden in ("run_scenario", "execute", "send_outboxes",
                          "runner", "claim", "mark_running", "certification_runtime"):
            self.assertNotIn(forbidden, rendered)

    def test_the_execution_fence_is_closed_while_recovery_runs(self):
        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-fence-closed")
        observed = []
        clock = FakeClock()
        self.assertFalse(lifecycle.execution_is_forbidden())
        payload, code = lifecycle.recover(
            {"runId": "cert-fence-closed", "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger,
            clock=clock.time, sleeper=clock.sleep,
            cleaner=lambda run_id: (
                observed.append(lifecycle.execution_is_forbidden()) or {"rows": 0}))
        self.assertEqual(code, 200, payload)
        self.assertEqual(observed, [True])
        self.assertFalse(lifecycle.execution_is_forbidden(),
                         "the fence outlived the recovery that opened it")

    def test_the_fence_refuses_an_execution_attempted_inside_it(self):
        """The guard that would catch a future edit routing recovery into run()."""
        with lifecycle.execution_forbidden():
            with self.assertRaises(lifecycle.ExecutionForbidden):
                lifecycle.run({"runId": "cert-fence-run",
                               "expectedRevision": REVISION})

    def test_the_fence_does_not_leak_to_an_ordinary_run(self):
        """A process-global fence would break every legitimate run. Thread-local."""
        with lifecycle.execution_forbidden():
            leaked = []
            thread = threading.Thread(
                target=lambda: leaked.append(lifecycle.execution_is_forbidden()))
            thread.start()
            thread.join(5)
        self.assertEqual(leaked, [False])

    def test_recovery_completes_with_the_runner_poisoned(self):
        """Runtime proof, not source reading.

        The execution module is replaced with one that raises on ANY attribute
        access, and the real product send entry point is replaced with a raiser.
        A recovery that touched either would fail here rather than quietly
        cause a second effect in production.
        """

        class PoisonedRunner:
            def __getattr__(self, name):
                raise AssertionError(f"recovery reached the runner: {name}")

        def exploding_send(*args, **kwargs):
            raise AssertionError("recovery reached the product send lane")

        from email_automation import email as email_module

        ledger = ObservableLedger(**quiescent_observation())
        claimed_run(ledger, "cert-poisoned")
        clock = FakeClock()
        with patch.dict(sys.modules,
                        {"email_automation.certification.runner": PoisonedRunner()}), \
                patch.object(email_module, "send_outboxes", exploding_send):
            payload, code = lifecycle.recover(
                {"runId": "cert-poisoned", "expectedRevision": REVISION},
                caller_identity_digest="c" * 64, ledger=ledger,
                clock=clock.time, sleeper=clock.sleep)
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["verdict"], "INSTRUMENT_BLOCKED")


# ---------------------------------------------------------------------------
# recover over HTTP
# ---------------------------------------------------------------------------


class RecoverRouteTests(unittest.TestCase):

    def setUp(self):
        service.app.config["TESTING"] = True
        self.client = service.app.test_client()
        self.ledger = ObservableLedger(**quiescent_observation())
        patcher = patch.object(lifecycle, "_DEFAULT_LEDGER", self.ledger)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The route uses the real clock. The 75s gate is proven against a fake
        # clock in RecoverQuiescenceTests; here it is shortened so the HTTP
        # surface can be exercised without waiting it out.
        gate = patch.object(lifecycle, "QUIESCENCE_WAIT_SECONDS", 0)
        gate.start()
        self.addCleanup(gate.stop)

    def _decoder(self):
        return lambda token, audience: {
            "iss": "https://accounts.google.com", "aud": AUDIENCE,
            "email": OPERATOR, "email_verified": True, "sub": SUB,
            "exp": 4102444800,
        }

    def _post(self, operation, body):
        with patch.dict(os.environ, TWIN_ENV, clear=False), \
                patch.object(service, "_caller_decoder", self._decoder()):
            return self.client.post(
                f"/certification/{operation}", json=body,
                headers={"Authorization": "Bearer valid"})

    def test_recover_is_no_longer_not_implemented(self):
        claimed_run(self.ledger, "cert-route-recover")
        response = self._post("recover", {"runId": "cert-route-recover",
                                          "expectedRevision": REVISION})
        self.assertNotEqual(response.status_code, 501)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["verdict"], "INSTRUMENT_BLOCKED")

    def test_the_recover_request_surface_stays_closed(self):
        """It may never name a user, client, recipient, body, or resource."""
        claimed_run(self.ledger, "cert-route-closed")
        for extra in ({"uid": "real-user"}, {"scenarioId": "campaign-one-property"},
                      {"recipient": "someone@example.com"}, {"sheetId": "s"}):
            body = {"runId": "cert-route-closed", "expectedRevision": REVISION}
            body.update(extra)
            with self.subTest(extra=extra):
                response = self._post("recover", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason"], "invalid_request")
        self.assertEqual(self.ledger.state("cert-route-closed"),
                         ledger_module.CLAIMED)

    def test_a_wrong_revision_is_refused_before_the_lifecycle(self):
        claimed_run(self.ledger, "cert-route-revision")
        response = self._post("recover", {"runId": "cert-route-revision",
                                          "expectedRevision": "0" * 40})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "revision_mismatch")
        self.assertEqual(self.ledger.state("cert-route-revision"),
                         ledger_module.CLAIMED)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# review-input: the one route that returns raw captured text
# ---------------------------------------------------------------------------
#
# Every other lifecycle payload is sanitized BY CONSTRUCTION. This one is not,
# and pretending otherwise would be worse than the exception: the whole point of
# human naturalness review is that Baylor reads what the product actually wrote.
#
# So the escape is made explicit rather than quiet, and it is bounded on all
# four sides -- one named operation, an ordered whole set, hard size limits, and
# no agent path that can reach it.


def fixture_flavoured_messages():
    """Bodies carrying values from the REAL fixture, not invented lookalikes.

    A redaction test that redacts a string the test made up proves the regex
    compiles. Using the shipped fixture's own recipient and Firestore prefix
    proves it removes the values that would actually be in a captured message.
    """
    from email_automation.certification import fixtures as fx
    return [
        {"kind": "outreach",
         "subject": f"Following up for {fx.FIXTURE_RECIPIENT}",
         "body": f"Hi Pat, writing about 100 Fixture Way. Reply to "
                 f"{fx.FIXTURE_SENDER}. Record {fx.FIXTURE_PREFIX}/outbox/1."},
        {"kind": "reply",
         "subject": "Re: 100 Fixture Way",
         "body": "Thanks for getting back to me about the property."},
    ]


class ReviewProjectionTests(unittest.TestCase):

    def _project(self, messages, now_epoch=1_000_000):
        return input_handoff.project_review_set(
            "cert-review-run", messages, now_epoch=now_epoch)

    def test_the_projection_is_ordered_and_whole(self):
        """One array, every message, ordinals from one. It never paginates."""
        messages = fixture_flavoured_messages() * 3
        projected = self._project(messages)
        self.assertEqual(len(projected.messages), len(messages))
        self.assertEqual([m.ordinal for m in projected.messages],
                         list(range(1, len(messages) + 1)))

    def test_every_message_projects_exactly_five_fields(self):
        projected = self._project(fixture_flavoured_messages())
        for message in projected.messages:
            self.assertEqual(set(message.to_dict()),
                             {"ordinal", "kind", "bodyDigest", "subject", "body"})

    def test_an_address_from_the_real_fixture_is_removed(self):
        from email_automation.certification import fixtures as fx
        projected = self._project(fixture_flavoured_messages())
        blob = json.dumps([m.to_dict() for m in projected.messages])
        self.assertNotIn(fx.FIXTURE_RECIPIENT, blob)
        self.assertNotIn(fx.FIXTURE_SENDER, blob)
        self.assertNotIn(fx.FIXTURE_PREFIX, blob)
        # The prose survives -- redaction, not deletion of the thing under review.
        self.assertIn("100 Fixture Way", blob)

    def test_the_projection_is_checked_by_the_real_sanitizer(self):
        from email_automation.certification import evidence as ev
        projected = self._project(fixture_flavoured_messages())
        for message in projected.messages:
            ev.assert_safe_text("subject", message.subject)
            ev.assert_safe_text("body", message.body)

    def test_a_long_body_is_bounded_after_it_is_redacted(self):
        from email_automation.certification import fixtures as fx
        padding = "ordinary broker prose. " * 400
        body = f"Reply to {fx.FIXTURE_RECIPIENT}. {padding}"
        self.assertGreater(len(body), input_handoff.MAX_REVIEW_BODY_CHARS)
        projected = self._project([{"kind": "outreach", "subject": "s",
                                    "body": body}])
        projected_body = projected.messages[0].body
        self.assertLessEqual(len(projected_body),
                             input_handoff.MAX_REVIEW_BODY_CHARS)
        self.assertNotIn(fx.FIXTURE_RECIPIENT, projected_body)
        self.assertIn(input_handoff.REDACTED_ADDRESS, projected_body)

    def test_shape_is_checked_before_the_length_bound(self):
        """Truncation is not redaction.

        This is the case that tells the two orders apart. An unsafe shape sits
        PAST the length bound. Bounding first would cut it out of the visible
        text and the message would be served as checked -- checked against a
        string that no longer contained the thing wrong with it. Checking first
        refuses the whole set, which is the only honest answer.
        """
        from email_automation.certification import fixtures as fx
        padding = "ordinary broker prose. " * 400
        body = f"{padding}{fx.FIXTURE_RECIPIENT}"
        self.assertGreater(body.index(fx.FIXTURE_RECIPIENT),
                           input_handoff.MAX_REVIEW_BODY_CHARS)
        with patch.object(input_handoff, "_redact", lambda text: text):
            with self.assertRaises(input_handoff.ReviewProjectionRefused):
                self._project([{"kind": "outreach", "subject": "s", "body": body}])

    def test_a_body_whose_address_survives_redaction_refuses_the_whole_set(self):
        """Refuse, do not ship a partially redacted body."""
        with patch.object(input_handoff, "_redact", lambda text: text):
            with self.assertRaises(input_handoff.ReviewProjectionRefused):
                self._project(fixture_flavoured_messages())

    def test_too_many_messages_refuses_rather_than_truncating_the_list(self):
        messages = fixture_flavoured_messages()[:1] * (
            input_handoff.MAX_REVIEW_MESSAGES + 1)
        with self.assertRaises(input_handoff.ReviewProjectionRefused):
            self._project(messages)

    def test_an_unknown_kind_is_refused_by_allowlist(self):
        with self.assertRaises(input_handoff.ReviewProjectionRefused):
            self._project([{"kind": "invented_later", "subject": "s", "body": "b"}])

    def test_the_body_digest_is_over_the_text_that_is_shown(self):
        from email_automation.certification import evidence as ev
        projected = self._project(fixture_flavoured_messages())
        for message in projected.messages:
            self.assertEqual(message.body_digest, ev.digest_of_text(message.body))

    def test_the_set_digest_carries_no_text(self):
        projected = self._project(fixture_flavoured_messages())
        self.assertRegex(projected.set_digest, r"^[0-9a-f]{64}$")


class ReviewStoreTests(unittest.TestCase):

    def setUp(self):
        self.store = input_handoff.TransientReviewStore()

    def test_a_deposited_set_is_readable_once_deposited(self):
        self.store.deposit("cert-store-1", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        self.assertIsNotNone(self.store.get("cert-store-1", now_epoch=1_000_001))

    def test_a_second_deposit_is_refused(self):
        self.store.deposit("cert-store-2", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        with self.assertRaises(input_handoff.ReviewProjectionRefused):
            self.store.deposit("cert-store-2", fixture_flavoured_messages(),
                               now_epoch=1_000_000)

    def test_an_expired_set_is_neither_served_nor_retained(self):
        self.store.deposit("cert-store-3", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        expired_at = 1_000_000 + input_handoff.REVIEW_SET_TTL_SECONDS + 1
        self.assertIsNone(self.store.get("cert-store-3", now_epoch=expired_at))
        self.assertEqual(self.store.export_run_ids(), ())

    def test_the_ttl_is_bounded_to_one_day(self):
        self.assertEqual(input_handoff.REVIEW_SET_TTL_SECONDS, 24 * 60 * 60)

    def test_discard_is_how_cleanup_owns_the_artifact(self):
        self.store.deposit("cert-store-4", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        self.assertTrue(self.store.discard("cert-store-4"))
        self.assertIsNone(self.store.get("cert-store-4", now_epoch=1_000_001))


class ReviewInputLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.ledger = ObservableLedger(**quiescent_observation())
        self.store = input_handoff.TransientReviewStore()

    def _call(self, run_id):
        return lifecycle.review_input(
            {"runId": run_id, "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=self.ledger,
            store=self.store, now_epoch=1_000_001)

    def test_an_unknown_run_is_not_invented(self):
        payload, code = self._call("cert-review-unknown")
        self.assertEqual(code, 404)
        self.assertEqual(payload["reason"], "unknown_run")

    def test_a_run_with_nothing_awaiting_review_is_refused(self):
        claimed_run(self.ledger, "cert-review-none")
        payload, code = self._call("cert-review-none")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "no_review_pending")

    def test_the_whole_ordered_set_is_returned(self):
        claimed_run(self.ledger, "cert-review-set")
        self.store.deposit("cert-review-set", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        payload, code = self._call("cert-review-set")
        self.assertEqual(code, 200, payload)
        self.assertEqual(len(payload["messages"]), 2)
        self.assertEqual([m["ordinal"] for m in payload["messages"]], [1, 2])
        for message in payload["messages"]:
            self.assertEqual(set(message),
                             {"ordinal", "kind", "bodyDigest", "subject", "body"})

    def test_an_expired_review_set_is_not_served(self):
        claimed_run(self.ledger, "cert-review-expired")
        self.store.deposit("cert-review-expired", fixture_flavoured_messages(),
                           now_epoch=1_000_000)
        payload, code = lifecycle.review_input(
            {"runId": "cert-review-expired", "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=self.ledger,
            store=self.store,
            now_epoch=1_000_000 + input_handoff.REVIEW_SET_TTL_SECONDS + 1)
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "no_review_pending")


class ReviewInputRouteTests(unittest.TestCase):

    def setUp(self):
        service.app.config["TESTING"] = True
        self.client = service.app.test_client()
        self.ledger = ObservableLedger(**quiescent_observation())
        self.store = input_handoff.TransientReviewStore()
        for target, value in (("_DEFAULT_LEDGER", self.ledger),
                              ("_DEFAULT_REVIEW_STORE", self.store)):
            patcher = patch.object(lifecycle, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, operation, body):
        decoder = lambda token, audience: {
            "iss": "https://accounts.google.com", "aud": AUDIENCE,
            "email": OPERATOR, "email_verified": True, "sub": SUB,
            "exp": 4102444800,
        }
        with patch.dict(os.environ, TWIN_ENV, clear=False), \
                patch.object(service, "_caller_decoder", decoder):
            return self.client.post(
                f"/certification/{operation}", json=body,
                headers={"Authorization": "Bearer valid"})

    def test_review_input_is_no_longer_not_implemented(self):
        claimed_run(self.ledger, "cert-route-review")
        self.store.deposit("cert-route-review", fixture_flavoured_messages(),
                           now_epoch=int(time.time()))
        response = self._post("review-input", {"runId": "cert-route-review",
                                               "expectedRevision": REVISION})
        self.assertNotEqual(response.status_code, 501)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(response.get_json()["messages"]), 2)

    def test_the_review_input_request_surface_stays_closed(self):
        claimed_run(self.ledger, "cert-route-review-closed")
        for extra in ({"uid": "real-user"}, {"recipient": "someone@example.com"},
                      {"ordinal": "1"}, {"body": "text"}):
            body = {"runId": "cert-route-review-closed",
                    "expectedRevision": REVISION}
            body.update(extra)
            with self.subTest(extra=extra):
                response = self._post("review-input", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason"], "invalid_request")


# ---------------------------------------------------------------------------
# The escape from the sanitization contract is bounded and agent-unreachable
# ---------------------------------------------------------------------------


def real_cli():
    """Load the SHIPPED CLI. A retyped allowlist would only test itself."""
    import importlib.util
    path = REPO_ROOT / "scripts" / "certify_production.py"
    spec = importlib.util.spec_from_file_location("certify_production", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AgentCannotReachRawTextTests(unittest.TestCase):

    def test_the_real_allowlist_refuses_review_input(self):
        cli = real_cli()
        for operation in sorted(cli.HUMAN_ONLY_OPERATIONS):
            with self.subTest(operation=operation):
                with self.assertRaises(cli.CertifyRefused):
                    cli.assert_agent_may_call(operation)

    def test_the_two_real_operation_sets_do_not_overlap(self):
        cli = real_cli()
        self.assertEqual(
            cli.AGENT_ALLOWED_OPERATIONS & cli.HUMAN_ONLY_OPERATIONS, frozenset())

    def test_every_real_route_operation_is_classified_by_the_real_cli(self):
        """Driven from the shipped route table, so a new route cannot slip in
        unclassified and default to reachable."""
        cli = real_cli()
        for operation in sorted(service._CERTIFICATION_OPERATIONS):
            with self.subTest(operation=operation):
                if operation in cli.AGENT_ALLOWED_OPERATIONS:
                    cli.assert_agent_may_call(operation)
                else:
                    with self.assertRaises(cli.CertifyRefused):
                        cli.assert_agent_may_call(operation)

    def test_the_refusal_precedes_the_transport(self):
        """`call` refuses before it builds a request, not after it reads one."""
        cli = real_cli()
        attempts = []

        def record(*args, **kwargs):
            attempts.append(args)
            raise AssertionError("the CLI opened a connection for review-input")

        with patch.object(socket, "getaddrinfo", record), \
                patch.object(socket, "create_connection", record), \
                patch.object(socket.socket, "connect", record):
            with self.assertRaises(cli.CertifyRefused):
                cli.call("http://127.0.0.1:1", "review-input",
                         {"runId": "r", "expectedRevision": REVISION}, token="t")
        self.assertEqual(attempts, [])

    def test_recover_is_agent_callable_and_review_input_is_not(self):
        cli = real_cli()
        cli.assert_agent_may_call("recover")
        with self.assertRaises(cli.CertifyRefused):
            cli.assert_agent_may_call("review-input")


class SanitizationContractTests(unittest.TestCase):
    """review-input is the ONLY unsanitized payload, and it says so out loud."""

    def test_the_exception_is_named_and_is_exactly_one_operation(self):
        self.assertEqual(set(lifecycle.UNSANITIZED_OPERATIONS), {"review-input"})

    def test_the_unsanitized_operation_is_human_only_in_the_real_cli(self):
        cli = real_cli()
        self.assertTrue(
            set(lifecycle.UNSANITIZED_OPERATIONS) <= set(cli.HUMAN_ONLY_OPERATIONS))

    def test_every_other_lifecycle_payload_passes_the_real_sanitizer(self):
        from email_automation.certification import evidence as ev

        ledger = ObservableLedger(**quiescent_observation())
        store = input_handoff.TransientReviewStore()
        run_id = "cert-sanitized-run"
        environ = dict(TWIN_ENV)
        payloads = []

        prepared, _ = lifecycle.prepare(
            {"scenarioId": "campaign-one-property", "runId": run_id,
             "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger, environ=environ)
        payloads.append(prepared)
        payloads.append(lifecycle.status({"runId": run_id}, ledger=ledger)[0])

        claimed_run(ledger, "cert-sanitized-recover")
        clock = FakeClock()
        payloads.append(lifecycle.recover(
            {"runId": "cert-sanitized-recover", "expectedRevision": REVISION},
            ledger=ledger, clock=clock.time, sleeper=clock.sleep)[0])

        from email_automation.certification.models import CertificationRequest
        ledger.begin_preparing(CertificationRequest(
            scenario_id="campaign-one-property", run_id="cert-sanitized-abort",
            expected_revision=REVISION))
        payloads.append(lifecycle.abort(
            {"runId": "cert-sanitized-abort"}, ledger=ledger)[0])

        # And the one that is deliberately NOT sanitized, to prove the check
        # would have caught it.
        claimed_run(ledger, "cert-sanitized-review")
        store.deposit("cert-sanitized-review",
                      [{"kind": "outreach", "subject": "s",
                        "body": "reach me at real.person@example.com"}],
                      now_epoch=1_000_000)
        unsanitized, _ = lifecycle.review_input(
            {"runId": "cert-sanitized-review"}, ledger=ledger, store=store,
            now_epoch=1_000_001)

        # Digests are 64 hex characters and legitimately look like an opaque
        # blob, so they are excluded by their exact shape rather than by
        # loosening the check for everything else.
        digest = re.compile(r"^[0-9a-f]{64}$")
        checked = 0
        for payload in payloads:
            for key, value in payload.items():
                if isinstance(value, str) and not digest.match(value):
                    ev.assert_safe_text(f"payload.{key}", value)
                    checked += 1
        self.assertGreater(checked, 0)

        # review-input's own payload is only safe because it was REDACTED, not
        # because it was sanitized: the raw prose is still there by design.
        blob = json.dumps(unsanitized, sort_keys=True)
        self.assertNotIn("real.person@example.com", blob)
        self.assertIn("reach me at", blob)


# ---------------------------------------------------------------------------
# A tampered authorization is a finding, not a 500 and not an absence
# ---------------------------------------------------------------------------
#
# The in-memory ledger's `peek_ephemeral` only ever returns a value or None.
# The durable one raises `AuthorizationInvalid` when a stored authorization has
# been EDITED in the database -- deliberately, so a tamper and an absence do not
# look alike. `run` must be ready for that before the swap, not after it.


class TamperedAuthorizationTests(unittest.TestCase):
    """The 409 for "no prepared run" is deliberately ambiguous between "never
    prepared" and "already claimed", because telling those apart is a probe for
    which run ids exist. A 500 breaks that ambiguity in the opposite direction:
    it says this particular run id hit a path the others did not."""

    ENV = dict(TWIN_ENV)

    def _run(self, ledger, run_id):
        return lifecycle.run(
            {"scenarioId": "campaign-one-property", "runId": run_id,
             "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=ledger, environ=self.ENV)

    def test_an_absent_authorization_still_refuses_as_no_prepared_run(self):
        ledger = ledger_module.InMemoryRunLedger()
        payload, code = self._run(ledger, "cert-tamper-absent")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "no_prepared_run")

    def test_a_tampered_authorization_is_named_rather_than_crashing(self):
        from email_automation.certification.models import AuthorizationInvalid

        class TamperedLedger(ledger_module.InMemoryRunLedger):
            def peek_ephemeral(self, run_id):
                raise AuthorizationInvalid("stored authorization digest does not match")

        ledger = TamperedLedger()
        claimed_run(ledger, "cert-tamper-edited")
        payload, code = self._run(ledger, "cert-tamper-edited")
        self.assertEqual(code, 409)
        self.assertEqual(payload["reason"], "authorization_invalid")

    def test_a_tamper_and_an_absence_are_distinguishable(self):
        from email_automation.certification.models import AuthorizationInvalid

        class TamperedLedger(ledger_module.InMemoryRunLedger):
            def peek_ephemeral(self, run_id):
                raise AuthorizationInvalid("stored authorization digest does not match")

        tampered = TamperedLedger()
        claimed_run(tampered, "cert-tamper-a")
        tampered_payload, _ = self._run(tampered, "cert-tamper-a")
        absent_payload, _ = self._run(ledger_module.InMemoryRunLedger(),
                                      "cert-tamper-b")
        self.assertNotEqual(tampered_payload["reason"], absent_payload["reason"])
        # Neither answer names a run id, so neither is a probe for which exist.
        for payload in (tampered_payload, absent_payload):
            self.assertEqual(set(payload), {"status", "reason"})
            self.assertNotIn("cert-tamper", json.dumps(payload))
