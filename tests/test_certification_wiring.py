"""The wiring that turns two structurally-correct routes into two that run.

``/certification/review-input`` and ``/certification/recover`` were both
implemented, tested, and INERT: nothing in the instrument produced the review
pack the first serves, and no shipped ledger could answer the observation the
second gates on. A route that is structurally correct and never exercised is
exactly the failure this project already hit once -- three phases proved
properties by test, and the first actual run found a real ordering bug.

So this module does not assert that the wiring exists. It DRIVES it:

* the real bootstrap scenario through the real runner, and then asks the real
  ``review_input`` for the pack that run produced.

Every value under test comes from the shipped registry, the shipped runner and
the shipped lifecycle. Nothing here makes a provider call.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.certification import input_handoff
from email_automation.certification import ledger as ledger_module
from email_automation.certification import lifecycle
from email_automation.certification import runner as runner_module

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
