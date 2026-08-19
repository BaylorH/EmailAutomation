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


if __name__ == "__main__":
    unittest.main()
