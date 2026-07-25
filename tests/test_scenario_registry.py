import json
import os
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "scenario-registry.json"
)

FAMILY_COUNTS = {
    "ACC": 5,
    "QUE": 4,
    "OUT": 8,
    "IN": 8,
    "CLM": 10,
    "SHT": 5,
    "RSP": 3,
    "FUP": 3,
    "NOT": 2,
    "REC": 2,
    "SEC": 3,
    "OBS": 2,
    "DEP": 4,
}
EXPECTED_SCENARIO_IDS = {
    f"{family}-{number:02d}"
    for family, count in FAMILY_COUNTS.items()
    for number in range(1, count + 1)
}
VALID_LEVELS = {"L1", "L2", "L3", "L4"}
VALID_OWNERS = {"backend", "frontend", "platform", "shared"}
VALID_DATA_CLASSIFICATIONS = {
    "synthetic_local",
    "synthetic_emulator",
    "dedicated_sandbox",
    "controlled_test_accounts",
}
VALID_RESULT_STATUSES = {
    "passed",
    "partial",
    "gap",
    "unavailable",
    "not_run",
}
EVIDENCE_RELATIVE_PATH = (
    "docs/release-safety/credential-free-l1-baseline-2026-07-24.md"
)


class TestScenarioRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_registry_is_the_authoritative_pre_gate_2_source(self):
        self.assertEqual(self.registry["schemaVersion"], 1)
        self.assertTrue(self.registry["authoritativeScenarioSource"])
        self.assertFalse(self.registry["gate2Authorized"])
        self.assertIn("Gate 2", self.registry["authorizationBoundary"])

    def test_all_test_levels_have_explicit_commands_and_availability(self):
        levels = self.registry["levels"]
        self.assertEqual(set(levels), VALID_LEVELS)

        for level, profile in levels.items():
            with self.subTest(level=level):
                self.assertEqual(
                    profile["command"],
                    f"./scripts/run_test_level.sh --level {level}",
                )
                self.assertIn(
                    profile["availability"],
                    {"always", "environment_required", "unconfigured"},
                )
                self.assertIsInstance(profile["requiredEnvironment"], list)
                self.assertIn(
                    profile["dataClassification"],
                    VALID_DATA_CLASSIFICATIONS,
                )

        self.assertEqual(levels["L1"]["availability"], "always")
        self.assertEqual(levels["L1"]["requiredEnvironment"], [])
        self.assertTrue(levels["L1"]["requiredPythonModules"])
        self.assertEqual(
            levels["L1"]["selection"],
            {"startDirectory": "tests", "pattern": "test*.py"},
        )

    def test_canonical_command_bootstraps_the_pinned_environment(self):
        wrapper = REPO_ROOT / "scripts" / "run_test_level.sh"

        self.assertTrue(wrapper.is_file())
        self.assertTrue(os.access(wrapper, os.X_OK))
        wrapper_text = wrapper.read_text(encoding="utf-8")
        self.assertIn("uv run", wrapper_text)
        self.assertIn("requirements.lock", wrapper_text)
        self.assertIn("--isolated", wrapper_text)

    def test_registry_contains_exactly_the_approved_scenarios(self):
        scenarios = self.registry["scenarios"]
        scenario_ids = [scenario["id"] for scenario in scenarios]

        self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
        self.assertEqual(set(scenario_ids), EXPECTED_SCENARIO_IDS)
        self.assertEqual(
            {scenario["family"] for scenario in scenarios},
            set(FAMILY_COUNTS),
        )

    def test_each_scenario_has_owner_levels_data_and_required_result(self):
        for scenario in self.registry["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                self.assertEqual(scenario["family"], scenario["id"].split("-")[0])
                self.assertIn(scenario["owner"], VALID_OWNERS)
                self.assertTrue(scenario["executionLevels"])
                self.assertLessEqual(
                    set(scenario["executionLevels"]),
                    VALID_LEVELS,
                )
                self.assertTrue(scenario["requiredResult"].strip())
                self.assertTrue(scenario["dataClassification"])
                self.assertLessEqual(
                    set(scenario["dataClassification"]),
                    VALID_DATA_CLASSIFICATIONS,
                )

    def test_each_scenario_has_exact_paths_or_an_explicit_gap(self):
        for scenario in self.registry["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                test_paths = scenario["testPaths"]
                explicit_gap = scenario["explicitGap"]

                self.assertIsInstance(test_paths, list)
                if not test_paths:
                    self.assertIsInstance(explicit_gap, str)
                    self.assertTrue(explicit_gap.strip())

                for relative_path in test_paths:
                    self.assertTrue(relative_path.startswith("tests/"))
                    self.assertTrue(
                        (REPO_ROOT / relative_path).is_file(),
                        f"{scenario['id']} references missing test {relative_path}",
                    )

    def test_latest_result_and_evidence_are_explicit(self):
        for scenario in self.registry["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                latest = scenario["latestResult"]
                self.assertIn(latest["status"], VALID_RESULT_STATUSES)
                self.assertRegex(latest["date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertTrue(latest["summary"].strip())

                if latest["level"] is not None:
                    self.assertIn(latest["level"], scenario["executionLevels"])

                evidence_artifact = scenario["evidenceArtifact"]
                if evidence_artifact is None:
                    self.assertTrue(scenario["explicitGap"])
                else:
                    self.assertTrue(
                        (REPO_ROOT / evidence_artifact).is_file(),
                        f"{scenario['id']} references missing evidence "
                        f"{evidence_artifact}",
                    )

    def test_every_latest_result_points_to_the_dated_execution_record(self):
        evidence = self.registry["latestExecutionEvidence"]

        self.assertEqual(evidence["artifact"], EVIDENCE_RELATIVE_PATH)
        self.assertRegex(evidence["sourceCommit"], r"^[0-9a-f]{40}$")
        self.assertEqual(evidence["command"], self.registry["levels"]["L1"]["command"])
        self.assertEqual(evidence["status"], "passed")
        self.assertGreater(evidence["testsRun"], 0)
        self.assertEqual(evidence["failures"], 0)
        self.assertEqual(evidence["errors"], 0)
        self.assertEqual(evidence["skipped"], 0)

        evidence_text = (REPO_ROOT / evidence["artifact"]).read_text(encoding="utf-8")
        for expected_text in (
            evidence["sourceCommit"],
            evidence["command"],
            f"tests={evidence['testsRun']}",
            "Gate 2 remains unauthorized",
        ):
            self.assertIn(expected_text, evidence_text)

        for scenario in self.registry["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                self.assertEqual(scenario["evidenceArtifact"], evidence["artifact"])


if __name__ == "__main__":
    unittest.main()
