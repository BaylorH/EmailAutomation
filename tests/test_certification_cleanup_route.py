"""`/certification/cleanup`: terminal-only repair that may never rewrite a verdict.

`cleanup` was a route with no handler -- listed in the operation table, so it
passed the fence, the revision binding, the schema and the auth, and then fell
through to 501. It was also in NEITHER CLI operation set, which meant the only
thing stopping an agent from calling it was the accident that its name was
unknown. An operation nobody classified is not an operation nobody may call.

Three hazards, and this file is organised around them.

**It must never change a verdict.** A verdict somebody may already have acted on
is not rewritable. `append_cleanup_result` is documented as appending exactly
because of that, and the guarantee here is structural rather than reviewed:
`record_terminal` is not in cleanup's call graph at all, so there is no line it
could reach that writes one.

**It must never execute.** Same hazard as recovery, and the same three guards --
the thread-local execution fence, an AST call-graph allowlist over the SHIPPED
`lifecycle.py`, and runtime poisoning of the runner module. Cleanup gets its own
approved call set; recover's is not widened to cover it, and a test pins that
the two remain separate.

**It must not be quietly agent-reachable.** So the classification is driven from
the REAL route table and the REAL CLI, and a route added later cannot default to
reachable by being unclassified.
"""

import ast
import json
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

import service
from email_automation.certification import input_handoff
from email_automation.certification import ledger as ledger_module
from email_automation.certification import lifecycle
from email_automation.certification.canonical_json import canonical_digest
from email_automation.certification.models import (
    CertificationRequest,
    RunAuthorization,
)
from email_automation.certification import scenarios

from tests.test_certification_recovery_routes import (
    RecoverNeverExecutesTests,
    real_cli,
    recover_call_graph,
)
from tests.test_certification_wiring import ClockedFirestore, durable_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_SOURCE = REPO_ROOT / "email_automation" / "certification" / "lifecycle.py"

REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
BOOTSTRAP = "campaign-one-property"
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


def _authorization(run_id):
    return RunAuthorization.create(
        scenario_id=BOOTSTRAP,
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


def drive_to(ledger, run_id, state, *, verdict="PASS"):
    """Drive ANY ledger to one state through its REAL transitions.

    A hand-built row would let cleanup pass against a state the shipped state
    machine cannot actually produce.
    """
    request = CertificationRequest(
        scenario_id=BOOTSTRAP, run_id=run_id, expected_revision=REVISION)
    ledger.begin_preparing(request)
    if state == ledger_module.PREPARING:
        return
    authorization = _authorization(run_id)
    ledger.mark_prepared(request, authorization)
    if state == ledger_module.PREPARED:
        return
    ledger.claim(request, authorization)
    if state == ledger_module.CLAIMED:
        return
    ledger.mark_running(run_id, "execute")
    if state == ledger_module.RUNNING:
        return
    ledger.record_terminal(run_id, verdict, canonical_digest({"run": run_id}))


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class CleanupContractTests(unittest.TestCase):

    def setUp(self):
        self.ledger = ledger_module.InMemoryRunLedger()
        self.store = input_handoff.TransientReviewStore()

    def _cleanup(self, run_id, **kwargs):
        return lifecycle.cleanup(
            {"runId": run_id, "expectedRevision": REVISION},
            caller_identity_digest="c" * 64, ledger=self.ledger,
            store=self.store, **kwargs)

    def test_an_unknown_run_is_not_invented(self):
        payload, code = self._cleanup("cert-cleanup-unknown")
        self.assertEqual(code, 404)
        self.assertEqual(payload["reason"], "unknown_run")

    def test_a_run_that_is_not_terminal_belongs_to_abort_or_recovery(self):
        """The three repair domains are disjoint by allowlist, not convention.

        abort proves a run did not execute; recover handles the one that may
        have; cleanup is what is left once a verdict exists. Cleaning a live run
        would delete the fixture out from under an execution in flight.
        """
        for state in (ledger_module.PREPARING, ledger_module.PREPARED,
                      ledger_module.CLAIMED, ledger_module.RUNNING):
            with self.subTest(state=state):
                ledger = ledger_module.InMemoryRunLedger()
                run_id = f"cert-cleanup-{state.lower()}"
                drive_to(ledger, run_id, state)
                payload, code = lifecycle.cleanup(
                    {"runId": run_id}, ledger=ledger, store=self.store,
                    cleaner=lambda _run_id: {"fixtureRows": 0})
                self.assertEqual(code, 409)
                self.assertEqual(payload["reason"], "not_cleanable")
                self.assertEqual(ledger.state(run_id), state)

    def test_the_cleanable_states_are_an_allowlist_of_exactly_terminal(self):
        """MUTATION. Widen this and a live run becomes cleanable."""
        self.assertEqual(set(lifecycle.CLEANABLE_STATES),
                         {ledger_module.TERMINAL})

    def test_no_verdict_is_ever_rewritten(self):
        for verdict in ledger_module.ALLOWED_VERDICTS:
            with self.subTest(verdict=verdict):
                ledger = ledger_module.InMemoryRunLedger()
                run_id = f"cert-cleanup-verdict-{verdict.lower()}"
                drive_to(ledger, run_id, ledger_module.TERMINAL, verdict=verdict)
                before = dict(ledger.export()[run_id])
                payload, code = lifecycle.cleanup(
                    {"runId": run_id}, ledger=ledger, store=self.store,
                    cleaner=lambda _run_id: {"fixtureRows": 0})
                self.assertEqual(code, 200, payload)
                after = ledger.export()[run_id]
                self.assertEqual(after["verdict"], before["verdict"])
                self.assertEqual(after["evidenceDigest"], before["evidenceDigest"])
                self.assertEqual(payload["verdict"], verdict)

    def test_proven_residue_is_appended_as_repair_evidence(self):
        run_id = "cert-cleanup-proven"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload, code = self._cleanup(
            run_id, cleaner=lambda _run_id: {"fixtureRows": 0, "sheetRows": 2})
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["residueProven"])
        self.assertEqual(payload["residue"], {"fixtureRows": 0, "sheetRows": 2})
        row = self.ledger.export()[run_id]
        self.assertEqual(row["residue"], {"fixtureRows": 0, "sheetRows": 2})
        self.assertEqual(row["cleanupEvidenceDigests"], [payload["cleanupDigest"]])

    def test_an_unprovable_residue_is_reported_false_and_never_a_zero(self):
        """No cleaner, no measurement. An empty residue map written anyway would
        read as a proven zero -- which is the one thing a cleanup readback is
        for."""
        run_id = "cert-cleanup-unproven"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload, code = self._cleanup(run_id)
        self.assertEqual(code, 200, payload)
        self.assertFalse(payload["residueProven"])
        self.assertEqual(payload["residue"], {})
        row = self.ledger.export()[run_id]
        self.assertEqual(row["cleanupEvidenceDigests"], [])
        self.assertIsNone(row["residue"])

    def test_an_unprovable_cleanup_never_overwrites_a_proven_one(self):
        """The failure this ordering exists to prevent: a later unmeasured
        cleanup blanking the residue an earlier measured one recorded."""
        run_id = "cert-cleanup-overwrite"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        self._cleanup(run_id, cleaner=lambda _run_id: {"fixtureRows": 3})
        self._cleanup(run_id)
        row = self.ledger.export()[run_id]
        self.assertEqual(row["residue"], {"fixtureRows": 3})
        self.assertEqual(len(row["cleanupEvidenceDigests"]), 1)

    def test_repair_may_repeat_and_still_only_appends(self):
        run_id = "cert-cleanup-twice"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        first, code = self._cleanup(run_id, cleaner=lambda _r: {"fixtureRows": 2})
        self.assertEqual(code, 200, first)
        second, code = self._cleanup(run_id, cleaner=lambda _r: {"fixtureRows": 0})
        self.assertEqual(code, 200, second)
        row = self.ledger.export()[run_id]
        self.assertEqual(len(row["cleanupEvidenceDigests"]), 2)
        self.assertEqual(row["verdict"], "PASS")
        self.assertEqual(row["residue"], {"fixtureRows": 0})

    def test_the_review_pack_is_discarded_because_cleanup_owns_it(self):
        """`review_input`'s contract already says so: the artifact expires
        within a day and cleanup owns it. It is the one artifact holding raw
        captured prose, and a resolved run has no reason to keep it."""
        run_id = "cert-cleanup-pack"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        self.store.deposit(run_id, [{"kind": "outreach", "subject": "s",
                                     "body": "ordinary prose"}],
                           now_epoch=1_000_000)
        payload, code = self._cleanup(run_id)
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["reviewPackDiscarded"])
        self.assertIsNone(self.store.get(run_id, now_epoch=1_000_001))

    def test_a_run_with_no_pack_reports_false_rather_than_claiming_a_discard(self):
        run_id = "cert-cleanup-nopack"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload, _ = self._cleanup(run_id)
        self.assertFalse(payload["reviewPackDiscarded"])

    def test_the_response_carries_no_fixture_value(self):
        run_id = "cert-cleanup-sanitized"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload, _ = self._cleanup(run_id, cleaner=lambda _r: {"fixtureRows": 0})
        blob = json.dumps(payload, sort_keys=True)
        for forbidden in ("@", "broker", "100 Fixture Way", "cert-uid-0001"):
            self.assertNotIn(forbidden, blob)

    def test_the_digest_is_over_sanitized_cleanup_facts_only(self):
        run_id = "cert-cleanup-digest"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload, _ = self._cleanup(run_id, cleaner=lambda _r: {"fixtureRows": 1})
        self.assertRegex(payload["cleanupDigest"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            payload["cleanupDigest"],
            canonical_digest({"cleanedRun": run_id,
                              "residue": {"fixtureRows": 1},
                              "reviewPackDiscarded": False}))

    def test_a_ledger_that_cannot_be_resolved_refuses_by_name(self):
        with patch.object(lifecycle, "_DEFAULT_LEDGER", None):
            payload, code = lifecycle.cleanup({"runId": "cert-cleanup-noledger"})
        self.assertEqual(code, 503)
        self.assertEqual(payload["reason"], lifecycle.LEDGER_UNAVAILABLE_REASON)


# ---------------------------------------------------------------------------
# cleanup never executes -- structurally
# ---------------------------------------------------------------------------


def cleanup_call_graph():
    """Every call reachable from ``cleanup`` through lifecycle's own helpers.

    Read from the SHIPPED source, like recovery's: this is the artifact that
    runs, and a retyped call graph would only be testing itself.
    """
    tree = ast.parse(LIFECYCLE_SOURCE.read_text())
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

    walk("cleanup")
    return calls, seen


class CleanupNeverExecutesTests(unittest.TestCase):

    # cleanup's OWN allowlist. Deliberately not recover's widened to fit both:
    # recover may read an observation and terminalize, cleanup may append and
    # discard, and merging the sets would silently grant each the other's
    # authority. `ledger.record_terminal` appears in neither this set nor the
    # implementation, which is what makes "cleanup never rewrites a verdict"
    # structural rather than reviewed.
    ALLOWED_CALLS = frozenset({
        # lifecycle's own helpers
        "_error", "_cleanup_under_fence", "_cleanup_digest",
        "_ledger_or_refusal", "default_ledger", "default_review_store",
        "execution_forbidden", "LedgerUnavailable",
        # sanctioned digesting
        "canonical_digest",
        # the injected cleaner the caller owns
        "cleaner",
        # the ledger surface cleanup may touch: read state, read verdict,
        # append repair evidence. Nothing that writes a verdict.
        "ledger.state", "ledger.verdict", "ledger.append_cleanup_result",
        # the transient review pack cleanup owns
        "store.discard",
        # builtins (``getattr`` is the fence reading its own thread-local)
        "bool", "dict", "getattr", "int", "residue_rows.items", "str",
    })

    def test_cleanup_calls_only_what_it_is_allowed_to_call(self):
        calls, _reached = cleanup_call_graph()
        self.assertEqual(
            calls, self.ALLOWED_CALLS,
            "cleanup's call graph changed. Every call it makes is approved "
            "explicitly, because two of the ones it must never make are an "
            "execution and a verdict write.",
        )

    def test_no_execution_entry_point_is_reachable_from_cleanup(self):
        """Stated separately from the allowlist so the intent survives a rename."""
        calls, _reached = cleanup_call_graph()
        rendered = " ".join(sorted(calls))
        for forbidden in ("run_scenario", "execute", "send_outboxes", "runner",
                          "claim", "mark_running", "certification_runtime"):
            self.assertNotIn(forbidden, rendered)

    def test_no_verdict_writing_entry_point_is_reachable_from_cleanup(self):
        calls, _reached = cleanup_call_graph()
        rendered = " ".join(sorted(calls))
        for forbidden in ("record_terminal", "begin_preparing", "mark_prepared"):
            self.assertNotIn(forbidden, rendered)

    def test_the_two_allowlists_are_separate_sets(self):
        """Cleanup was added with its own approved calls, never by widening
        recover's -- which would have handed recovery a discard and cleanup a
        terminalization."""
        self.assertNotEqual(self.ALLOWED_CALLS,
                            RecoverNeverExecutesTests.ALLOWED_CALLS)
        self.assertIn("ledger.record_terminal",
                      RecoverNeverExecutesTests.ALLOWED_CALLS)
        self.assertNotIn("ledger.record_terminal", self.ALLOWED_CALLS)
        self.assertIn("store.discard", self.ALLOWED_CALLS)
        self.assertNotIn("store.discard",
                         RecoverNeverExecutesTests.ALLOWED_CALLS)

    def test_adding_cleanup_did_not_widen_recovers_graph(self):
        recover_calls, _ = recover_call_graph()
        self.assertEqual(recover_calls, RecoverNeverExecutesTests.ALLOWED_CALLS)

    def test_the_execution_fence_is_closed_while_cleanup_runs(self):
        ledger = ledger_module.InMemoryRunLedger()
        drive_to(ledger, "cert-cleanup-fence", ledger_module.TERMINAL)
        observed = []
        self.assertFalse(lifecycle.execution_is_forbidden())
        payload, code = lifecycle.cleanup(
            {"runId": "cert-cleanup-fence"}, ledger=ledger,
            store=input_handoff.TransientReviewStore(),
            cleaner=lambda _run_id: (
                observed.append(lifecycle.execution_is_forbidden()) or {"rows": 0}))
        self.assertEqual(code, 200, payload)
        self.assertEqual(observed, [True])
        self.assertFalse(lifecycle.execution_is_forbidden(),
                         "the fence outlived the cleanup that opened it")

    def test_cleanup_completes_with_the_runner_poisoned(self):
        """Runtime proof, not source reading.

        The execution module is replaced with one that raises on ANY attribute
        access, and the real product send entry point is replaced with a raiser.
        A cleanup that touched either would fail here rather than quietly cause
        an effect on a run whose verdict is already recorded.
        """

        class PoisonedRunner:
            def __getattr__(self, name):
                raise AssertionError(f"cleanup reached the runner: {name}")

        def exploding_send(*args, **kwargs):
            raise AssertionError("cleanup reached the product send lane")

        from email_automation import email as email_module

        ledger = ledger_module.InMemoryRunLedger()
        drive_to(ledger, "cert-cleanup-poisoned", ledger_module.TERMINAL)
        with patch.dict(sys.modules,
                        {"email_automation.certification.runner": PoisonedRunner()}), \
                patch.object(email_module, "send_outboxes", exploding_send):
            payload, code = lifecycle.cleanup(
                {"runId": "cert-cleanup-poisoned"}, ledger=ledger,
                store=input_handoff.TransientReviewStore(),
                cleaner=lambda _run_id: {"fixtureRows": 0})
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["verdict"], "PASS")


# ---------------------------------------------------------------------------
# cleanup over HTTP, against the durable ledger
# ---------------------------------------------------------------------------


class CleanupRouteTests(unittest.TestCase):

    def setUp(self):
        service.app.config["TESTING"] = True
        self.client = service.app.test_client()
        self.store = ClockedFirestore()
        self.ledger = durable_ledger(self.store)
        self.review = input_handoff.TransientReviewStore()
        for target, value in (("_DEFAULT_LEDGER", self.ledger),
                              ("_DEFAULT_REVIEW_STORE", self.review)):
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

    def test_cleanup_is_no_longer_not_implemented(self):
        run_id = "cert-cleanup-route"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        response = self._post("cleanup", {"runId": run_id,
                                          "expectedRevision": REVISION})
        self.assertNotEqual(response.status_code, 501)
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["state"], "TERMINAL")

    def test_the_route_reports_residue_it_cannot_prove_as_false(self):
        """Honest by construction, and permanently so for this surface: the
        fixture store is an in-process double built per run, so a later request
        has nothing to read a residue count from. A fabricated zero here would
        be the strongest-looking evidence in the instrument and mean nothing."""
        run_id = "cert-cleanup-route-residue"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        payload = self._post("cleanup", {"runId": run_id,
                                         "expectedRevision": REVISION}).get_json()
        self.assertFalse(payload["residueProven"])
        self.assertEqual(payload["residue"], {})
        row = self.store.docs[f"certificationRuns/{run_id}"]
        self.assertEqual(row["cleanupEvidenceDigests"], [])
        self.assertIsNone(row["residue"])

    def test_the_stored_verdict_is_untouched_by_the_route(self):
        run_id = "cert-cleanup-route-verdict"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL, verdict="FAIL")
        before = dict(self.store.docs[f"certificationRuns/{run_id}"])
        self._post("cleanup", {"runId": run_id, "expectedRevision": REVISION})
        after = self.store.docs[f"certificationRuns/{run_id}"]
        self.assertEqual(after["verdict"], before["verdict"])
        self.assertEqual(after["evidenceDigest"], before["evidenceDigest"])
        self.assertEqual(after["state"], "TERMINAL")

    def test_a_live_run_is_refused_over_the_route(self):
        run_id = "cert-cleanup-route-live"
        drive_to(self.ledger, run_id, ledger_module.CLAIMED)
        response = self._post("cleanup", {"runId": run_id,
                                          "expectedRevision": REVISION})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "not_cleanable")
        self.assertEqual(self.ledger.state(run_id), ledger_module.CLAIMED)

    def test_an_unknown_run_is_refused_over_the_route(self):
        response = self._post("cleanup", {"runId": "cert-cleanup-route-unknown",
                                          "expectedRevision": REVISION})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["reason"], "unknown_run")

    def test_the_cleanup_request_surface_stays_closed(self):
        """It may never name a user, client, recipient, body, or resource."""
        run_id = "cert-cleanup-route-closed"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        for extra in ({"uid": "real-user"}, {"scenarioId": BOOTSTRAP},
                      {"recipient": "someone@example.com"}, {"sheetId": "s"},
                      {"verdict": "PASS"}, {"residue": "0"}):
            body = {"runId": run_id, "expectedRevision": REVISION}
            body.update(extra)
            with self.subTest(extra=extra):
                response = self._post("cleanup", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason"], "invalid_request")

    def test_a_wrong_revision_is_refused_before_the_lifecycle(self):
        run_id = "cert-cleanup-route-revision"
        drive_to(self.ledger, run_id, ledger_module.TERMINAL)
        response = self._post("cleanup", {"runId": run_id,
                                          "expectedRevision": "0" * 40})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "revision_mismatch")

    def test_the_route_still_refuses_the_operation_nobody_implemented(self):
        """`review` remains a deliberate 501, so "not implemented" is still a
        real answer here and cleanup's 200 is not a blanket change."""
        response = self._post("review", {
            "runId": "cert-cleanup-route-review", "expectedRevision": REVISION,
            "reviewSetDigest": "a" * 64, "rubricVersion": "1", "reviews": []})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(response.get_json()["reason"], "not_implemented")


# ---------------------------------------------------------------------------
# Who may call it
# ---------------------------------------------------------------------------


class CleanupIsNotAgentReachableTests(unittest.TestCase):
    """Cleanup mutates fixture state, appends durable repair evidence, and
    destroys the human review pack. None of that belongs on the smallest
    possible agent-safe surface, and nothing in the agent's own flow --
    prepare, run, status, abort, recover -- needs it."""

    def test_cleanup_is_classified_and_the_classification_is_human_only(self):
        cli = real_cli()
        self.assertIn("cleanup", cli.HUMAN_ONLY_OPERATIONS)
        self.assertNotIn("cleanup", cli.AGENT_ALLOWED_OPERATIONS)
        with self.assertRaises(cli.CertifyRefused):
            cli.assert_agent_may_call("cleanup")

    def test_the_refusal_states_the_reason_rather_than_pleading_ignorance(self):
        """It used to raise "unknown certification operation", which is a
        refusal by accident: the day somebody classified it, the accident would
        have become whatever they typed."""
        cli = real_cli()
        with self.assertRaises(cli.CertifyRefused) as caught:
            cli.assert_agent_may_call("cleanup")
        message = str(caught.exception)
        self.assertNotIn("unknown certification operation", message)
        self.assertIn("cleanup", message)

    def test_every_human_only_operation_states_why(self):
        """A human-only set with no reason attached is a list somebody will
        shorten. Reasons are required, and the two structures must agree."""
        cli = real_cli()
        self.assertEqual(set(cli.HUMAN_ONLY_REASONS), set(cli.HUMAN_ONLY_OPERATIONS))
        for operation, reason in cli.HUMAN_ONLY_REASONS.items():
            with self.subTest(operation=operation):
                self.assertTrue(reason.strip(), operation)

    def test_every_real_route_operation_is_classified_by_the_real_cli(self):
        """Driven from the SHIPPED route table plus the one route the table
        does not carry, so a route added later cannot default to reachable."""
        cli = real_cli()
        classified = cli.AGENT_ALLOWED_OPERATIONS | cli.HUMAN_ONLY_OPERATIONS
        for operation in sorted(set(service._CERTIFICATION_OPERATIONS) | {"review"}):
            with self.subTest(operation=operation):
                self.assertIn(operation, classified,
                              f"{operation} is a route nobody classified")
                if operation in cli.AGENT_ALLOWED_OPERATIONS:
                    cli.assert_agent_may_call(operation)
                else:
                    with self.assertRaises(cli.CertifyRefused):
                        cli.assert_agent_may_call(operation)

    def test_the_two_real_operation_sets_do_not_overlap(self):
        cli = real_cli()
        self.assertEqual(
            cli.AGENT_ALLOWED_OPERATIONS & cli.HUMAN_ONLY_OPERATIONS, frozenset())

    def test_every_handler_the_route_dispatches_is_classified(self):
        """The handler table is what actually runs code, so it is driven too."""
        cli = real_cli()
        classified = cli.AGENT_ALLOWED_OPERATIONS | cli.HUMAN_ONLY_OPERATIONS
        for operation in sorted(service._CERTIFICATION_HANDLERS):
            self.assertIn(operation, classified)

    def test_the_refusal_precedes_the_transport(self):
        """`call` refuses before it builds a request, not after it reads one."""
        cli = real_cli()
        attempts = []

        def record(*args, **kwargs):
            attempts.append(args)
            raise AssertionError("the CLI opened a connection for cleanup")

        with patch.object(socket, "getaddrinfo", record), \
                patch.object(socket, "create_connection", record), \
                patch.object(socket.socket, "connect", record):
            with self.assertRaises(cli.CertifyRefused):
                cli.call("http://127.0.0.1:1", "cleanup",
                         {"runId": "r", "expectedRevision": REVISION}, token="t")
        self.assertEqual(attempts, [])


if __name__ == "__main__":
    unittest.main()
