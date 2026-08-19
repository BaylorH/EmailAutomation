"""The refutation scenario: proving the instrument can return a real FAIL.

Everything before this proved the instrument can produce a PASS. An instrument
that cannot fail is not an instrument - it is a rubber stamp with a run id.

Three properties are under test here, and they are deliberately separable:

1. **The oracle comes from the image, never from the caller.** The registry
   names an ``oracleProjectionKey``; the expectation behind that key ships in
   ``certification.fixtures``. There is no parameter anywhere on the run path
   that could carry an oracle, because a caller who could supply the oracle
   could declare any run a pass.

2. **Every declared effect of the refutation scenario is OBSERVED.** An
   unobserved forbidden effect must report ``instrument_blocked`` - the
   correct-but-incomplete answer - and never be silently scored as zero.

3. **A failing run still cleans up.** A run that reports FAIL and leaves the
   fixture behind is a worse bug than the failure it reports.

Every fact in this module is read from the REAL in-image scenario registry via
``certification.scenarios``. Nothing here hand-builds a scenario dict: a test
that constructs its own copy of a value that exists in a shipped artifact is
only testing itself, which is exactly how 91 scenarios once went unguarded.
"""

from __future__ import annotations

import inspect
import unittest

from email_automation.certification import evidence as ev
from email_automation.certification import fixtures as fx
from email_automation.certification import runner as rn
from email_automation.certification import scenarios


VALID_REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
BOOTSTRAP_SCENARIO_ID = "campaign-one-property"
REFUTATION_SCENARIO_ID = "campaign-one-property-impossible-oracle"

# lifecycle.run's mapping, restated here on purpose. The runner reports what it
# MEASURED; deciding whether that measurement agrees with the registry's
# expectedVerdict is an instrument-level judgement, and it belongs in a test
# rather than inside the thing being judged.
OUTCOME_TO_VERDICT = {
    "pass": "PASS",
    "fail": "FAIL",
    "instrument_blocked": "INSTRUMENT_BLOCKED",
    "aborted": "FAIL",
}


def registry_scenario(scenario_id):
    """The scenario as the IMAGE ships it. Never a local reconstruction."""
    return scenarios.get(scenario_id)


class OracleProjectionTests(unittest.TestCase):
    """Where the expectation a run is judged against comes from.

    The refutation scenario differs from the bootstrap scenario in exactly one
    field - ``oracleProjectionKey``. That is the whole experiment: same product,
    same fixture, same lane, different declared expectation. If the oracle could
    arrive from the request, the experiment would prove nothing, because the
    caller would be grading its own exam.
    """

    def test_the_two_integrity_scenarios_share_a_fixture_and_differ_by_oracle(self):
        bootstrap = registry_scenario(BOOTSTRAP_SCENARIO_ID)
        refutation = registry_scenario(REFUTATION_SCENARIO_ID)
        self.assertEqual(
            bootstrap["logicalFixtureKey"], refutation["logicalFixtureKey"],
            "the refutation scenario must reuse the bootstrap fixture, not seed a new one",
        )
        self.assertNotEqual(
            bootstrap["oracleProjectionKey"], refutation["oracleProjectionKey"],
            "the oracle key is the ONLY thing that may differentiate them",
        )
        self.assertEqual(refutation["expectedVerdict"], "FAIL")
        self.assertEqual(refutation["scenarioClass"], "refutation")
        self.assertEqual(refutation["launchClass"], "agent_safe")
        self.assertIs(refutation["capabilityStamp"], False)

    def test_every_registry_scenario_names_an_oracle_projection_key(self):
        for scenario in scenarios.all_scenarios():
            with self.subTest(scenario=scenario["scenarioId"]):
                key = scenario.get("oracleProjectionKey")
                self.assertIsInstance(key, str)
                self.assertTrue(key.startswith("oracle/"))

    def test_every_runnable_scenario_resolves_an_in_image_oracle(self):
        """A lane this runner can drive must have an oracle the image ships.

        Driven off ``runner.LANES`` rather than a list of ids typed here, so
        wiring a new lane without shipping its oracle fails this test instead of
        producing a confident verdict against no expectation at all.
        """
        runnable = [
            scenario for scenario in scenarios.all_scenarios()
            if rn.LANES.get(scenario["logicalFixtureKey"]) == rn.BOOTSTRAP_LANE
        ]
        self.assertGreaterEqual(len(runnable), 2, "bootstrap and refutation must both be runnable")
        for scenario in runnable:
            with self.subTest(scenario=scenario["scenarioId"]):
                oracle = fx.oracle_for(scenario)
                self.assertEqual(oracle.key, scenario["oracleProjectionKey"])
                self.assertTrue(oracle.expectations, "an empty oracle expects nothing and proves nothing")

    def test_an_unregistered_oracle_key_is_refused_never_improvised(self):
        """Allow-list. An unknown key is a refusal, not an empty expectation."""
        unknown = dict(registry_scenario(REFUTATION_SCENARIO_ID))
        unknown["oracleProjectionKey"] = "oracle/certification-integrity/not-shipped"
        with self.assertRaises(fx.OracleNotRegistered):
            fx.oracle_for(unknown)

    def test_a_scenario_with_no_oracle_key_at_all_is_refused(self):
        blank = dict(registry_scenario(REFUTATION_SCENARIO_ID))
        for value in ("", None, 7, "  "):
            with self.subTest(value=repr(value)):
                blank["oracleProjectionKey"] = value
                with self.assertRaises(fx.OracleNotRegistered):
                    fx.oracle_for(blank)

    def test_the_oracle_is_never_taken_from_the_caller(self):
        """Two independent proofs, because either one alone is weak.

        The signature proof says no oracle can be passed to a run. The inline
        proof says an oracle body smuggled into the scenario mapping is ignored
        in favour of the shipped one.
        """
        self.assertEqual(
            [name for name in inspect.signature(rn.run_scenario).parameters],
            ["scenario_id", "run_id", "revision"],
            "run_scenario grew a parameter; an oracle must never be one of them",
        )
        shipped = fx.oracle_for(registry_scenario(REFUTATION_SCENARIO_ID))
        smuggled = dict(registry_scenario(REFUTATION_SCENARIO_ID))
        smuggled["oracle"] = {"captured_outreach": 999}
        smuggled["oracleProjection"] = {"expectations": {"captured_outreach": 999}}
        smuggled["expectations"] = {"captured_outreach": 999}
        self.assertEqual(dict(fx.oracle_for(smuggled).expectations),
                         dict(shipped.expectations))

    def test_the_impossible_oracle_exceeds_the_fixtures_structural_ceiling(self):
        """Impossible, not merely unmet.

        The ceiling is MEASURED from the seeded fixture - one queued outbox item
        addressed to one recipient can produce at most one captured outreach -
        so the refutation oracle's expectation is unsatisfiable by construction
        rather than unsatisfied by accident.
        """
        refutation = registry_scenario(REFUTATION_SCENARIO_ID)
        fixture = fx.prepare(refutation["logicalFixtureKey"])
        ceilings = fx.fixture_ceilings(fixture)
        oracle = fx.oracle_for(refutation)
        exceeded = oracle.exceeds_fixture_ceiling(ceilings)
        self.assertTrue(
            exceeded,
            f"the refutation oracle {dict(oracle.expectations)} is satisfiable "
            f"under ceilings {ceilings}, so it is not impossible",
        )

    def test_the_bootstrap_oracle_is_satisfiable_under_the_same_ceilings(self):
        """The control. If BOTH oracles were impossible, the ceiling test would
        pass for a reason that has nothing to do with the refutation."""
        bootstrap = registry_scenario(BOOTSTRAP_SCENARIO_ID)
        fixture = fx.prepare(bootstrap["logicalFixtureKey"])
        ceilings = fx.fixture_ceilings(fixture)
        oracle = fx.oracle_for(bootstrap)
        self.assertEqual(oracle.exceeds_fixture_ceiling(ceilings), [])

    def test_the_ceiling_is_measured_from_the_seed_not_hardcoded(self):
        """Mutate the fixture, and the ceiling must move with it."""
        fixture = fx.prepare(BOOTSTRAP_FIXTURE_KEY := registry_scenario(
            BOOTSTRAP_SCENARIO_ID)["logicalFixtureKey"])
        before = fx.fixture_ceilings(fixture)["captured_outreach"]
        fixture.firestore.data[f"{fixture.prefix}/outbox/outbox-2"] = {
            "assignedEmails": ["a@fixture.example.com", "b@fixture.example.com"],
        }
        after = fx.fixture_ceilings(fixture)["captured_outreach"]
        self.assertEqual(after, before + 2,
                         "the ceiling ignored a seeded outbox item, so it is a constant")


class _FakeDrivePublication:
    """A drive publication transport that records what it was asked to publish."""

    def __init__(self, captured=(), real_permission_calls=0):
        self.captured = list(captured)
        self.real_permission_calls = real_permission_calls


class _OpaqueDrivePublication:
    """A transport whose effects cannot be enumerated at all.

    Not a contrived case: ``ProviderBackedDrivePublication`` and
    ``AmbientDrivePublication`` both look like this. Neither can be asked what
    it published, so neither can support a claim of zero.
    """


class _RuntimeStub:
    def __init__(self, drive_publication):
        self.drive_publication = drive_publication


class PublicDrivePermissionObserverTests(unittest.TestCase):
    """The least reversible effect in the system, MEASURED rather than assumed.

    A public Drive link, once created, may be crawled, cached, or reshared;
    deleting the permission afterwards does not undo any of that. So the
    refutation scenario forbids it at zero, and a forbidden effect that nothing
    measures is not a zero - it is an absence, and it has to report as one.
    """

    def _observe(self, transport):
        return rn.observe_public_drive_permission(_RuntimeStub(transport))

    def test_the_certification_transport_measures_zero(self):
        """Measured off the REAL certification runtime, not a stand-in."""
        from email_automation import automation_runtime as ar
        fixture = fx.prepare(registry_scenario(REFUTATION_SCENARIO_ID)["logicalFixtureKey"])
        runtime = ar.certification_runtime(
            run_id="cert-observer-drive-0001",
            scope=REFUTATION_SCENARIO_ID,
            firestore=fixture.firestore,
            sheets=fixture.sheets,
            firestore_prefix=fixture.prefix,
            sheet_ids=fixture.sheet_ids,
            readable_paths=fixture.readable_paths,
        )
        self.assertEqual(rn.observe_public_drive_permission(runtime), 0)

    def test_a_transport_that_cannot_be_enumerated_is_unmeasurable_not_zero(self):
        self.assertIsNone(self._observe(_OpaqueDrivePublication()))

    def test_a_runtime_without_a_publication_transport_is_unmeasurable(self):
        """Resolution goes through the PRODUCT's own drive_publication_for, so a
        runtime that would fall back to ambient production reports unmeasurable
        rather than confidently reporting zero."""
        self.assertIsNone(self._observe(None))

    def test_a_real_provider_permission_call_is_counted(self):
        self.assertEqual(self._observe(_FakeDrivePublication(real_permission_calls=2)), 2)

    def test_a_public_grant_is_counted_even_when_only_captured(self):
        for permission in (
            {"type": "anyone", "role": "reader"},
            {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
            {"type": "domain", "role": "reader", "domain": "example.com"},
        ):
            with self.subTest(permission=permission):
                self.assertEqual(
                    self._observe(_FakeDrivePublication([("file-1", permission)])), 1
                )

    def test_an_unrecognised_grant_shape_counts_as_public(self):
        """Allow-list, not deny-list. A shape this instrument does not recognise
        is scored as the irreversible effect, because scoring it clean would let
        a novel public grant type through as a zero."""
        for permission in ({}, {"role": "reader"}, {"type": "user"},
                           {"type": "user", "emailAddress": "   "},
                           {"type": "anyoneWithLink"}, "anyone"):
            with self.subTest(permission=permission):
                self.assertEqual(
                    self._observe(_FakeDrivePublication([("file-1", permission)])), 1
                )

    def test_an_explicitly_addressed_private_grant_is_not_public(self):
        self.assertEqual(
            self._observe(_FakeDrivePublication(
                [("file-1", {"type": "user", "role": "writer",
                             "emailAddress": "person@fixture.example.com"})]
            )),
            0,
        )

    def test_the_private_grant_allowlist_is_load_bearing(self):
        """Mutate the pin and confirm it bites.

        A constant nothing consults is decoration. Widening the allow-list must
        change the score of the exact same captured permission.
        """
        from unittest.mock import patch
        transport = _FakeDrivePublication([("file-1", {"type": "anyone", "role": "reader"})])
        self.assertEqual(self._observe(transport), 1)
        with patch.object(rn, "PRIVATE_GRANT_TYPES", ("user", "group", "anyone")):
            # "anyone" now allow-listed, but the address requirement still bites
            self.assertEqual(self._observe(transport), 1)
            addressed = _FakeDrivePublication(
                [("file-1", {"type": "anyone", "emailAddress": "x@fixture.example.com"})]
            )
            self.assertEqual(self._observe(addressed), 0)


class CapabilityStampObserverTests(unittest.TestCase):
    """A stamp is a durable claim that a capability was certified in production.

    Both certification-integrity scenarios declare ``capabilityStamp: false``,
    and the refutation scenario forbids the effect at zero. Reading that zero off
    the registry alone would be assuming it; the observer counts both the
    entitlement the registry grants AND any stamp-shaped write the run made.
    """

    def test_a_scenario_that_may_not_stamp_contributes_no_entitlement(self):
        for scenario_id in (BOOTSTRAP_SCENARIO_ID, REFUTATION_SCENARIO_ID):
            with self.subTest(scenario=scenario_id):
                self.assertEqual(
                    rn.stamp_entitlement(registry_scenario(scenario_id)), 0
                )

    def test_a_stamp_bearing_scenario_does_contribute_one(self):
        """Driven off real stamp-bearing registry entries, so the entitlement is
        read from the artifact rather than agreed with itself."""
        stamping = [s for s in scenarios.all_scenarios() if s["capabilityStamp"]]
        self.assertTrue(stamping, "the registry ships no stamp-bearing scenario")
        for scenario in stamping[:3]:
            with self.subTest(scenario=scenario["scenarioId"]):
                self.assertEqual(rn.stamp_entitlement(scenario), 1)

    def test_a_stamp_shaped_write_is_counted(self):
        writes = [
            ("set", "users/cert-uid-0001/msgIndex/m1", {"threadId": "t"}, False),
            ("set", "capabilities/spreadsheet-admission",
             {"productionVerdict": "PASS"}, False),
        ]
        self.assertEqual(rn.observe_capability_stamp(writes, stamp_entitlement=0), 1)

    def test_an_ordinary_fixture_write_is_not_a_stamp(self):
        writes = [("set", "users/cert-uid-0001/actionAudit/audit-1", {"status": "sent"}, False)]
        self.assertEqual(rn.observe_capability_stamp(writes, stamp_entitlement=0), 0)

    def test_the_stamp_marker_pin_is_load_bearing(self):
        from unittest.mock import patch
        writes = [("set", "capabilities/x", {"productionVerdict": "PASS"}, False)]
        self.assertEqual(rn.observe_capability_stamp(writes, stamp_entitlement=0), 1)
        with patch.object(rn, "STAMP_MARKERS", ("somethingElseEntirely",)):
            self.assertEqual(rn.observe_capability_stamp(writes, stamp_entitlement=0), 0)

    def test_entitlement_and_observed_writes_both_reach_the_count(self):
        writes = [("set", "capabilities/x", {"productionVerdict": "PASS"}, False)]
        self.assertEqual(rn.observe_capability_stamp(writes, stamp_entitlement=1), 2)


class ObserverCompletenessTests(unittest.TestCase):
    """Every effect the registry declares must have an observer behind it.

    ``instrument_blocked`` is the honest answer for a scenario nothing measured,
    and it must stay reachable. But a scenario the instrument is claimed to
    cover cannot rest there: "never exercised" reading as "exercised and clean"
    is the failure this whole program exists to prevent.
    """

    def _run(self, scenario_id, run_id):
        return rn.run_scenario(scenario_id, run_id=run_id, revision=VALID_REVISION)

    def test_every_declared_effect_of_the_refutation_scenario_is_observed(self):
        scenario = registry_scenario(REFUTATION_SCENARIO_ID)
        _record, detail = self._run(REFUTATION_SCENARIO_ID, "cert-refute-observed-0001")
        declared = set(scenario["requiredEffects"]) | set(scenario["forbiddenEffects"])
        self.assertTrue(declared)
        missing = sorted(declared - set(detail["observed"]))
        self.assertEqual(missing, [], f"unobserved declared effects: {missing}")
        self.assertEqual(detail["unmeasured"], [])

    def test_every_declared_effect_of_the_bootstrap_scenario_is_observed(self):
        scenario = registry_scenario(BOOTSTRAP_SCENARIO_ID)
        _record, detail = self._run(BOOTSTRAP_SCENARIO_ID, "cert-boot-observed-0001")
        declared = set(scenario["requiredEffects"]) | set(scenario["forbiddenEffects"])
        self.assertEqual(sorted(declared - set(detail["observed"])), [])
        self.assertEqual(detail["unmeasured"], [])


if __name__ == "__main__":
    unittest.main()
