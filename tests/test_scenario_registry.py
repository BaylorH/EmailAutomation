import json
import os
import subprocess
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
SEC_01_EVIDENCE_RELATIVE_PATH = (
    "docs/release-safety/sec-01-l2-emulator-evidence-2026-07-24.md"
)
SEC_01_ADAPTER_TEST_PATH = "tests/test_sec_l2_emulator_adapter.py"
SEC_01_RULES_TEST_PATH = (
    "email-admin-ui:tests/firestore-rules/firestore.rules.test.js"
)
RUNNABLE_SOURCE_COMMIT = "b60c31f6b1ae59c6ef3ac6944ef9094a8c55e34a"
ADAPTER_IMPLEMENTATION_COMMIT = "7b2f6aa539c440ebcda25d24ebef20e4c7389d3b"
EMAIL_ADMIN_UI_COMMIT = "d98740b9eab03bf0ef971b26349318d25e1956b5"


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
        self.assertEqual(levels["L2"]["availability"], "environment_required")
        self.assertEqual(
            levels["L2"]["requiredEnvironment"],
            ["SITESIFT_ADMIN_UI_ROOT"],
        )
        self.assertEqual(levels["L2"]["scenarioIds"], ["SEC-01"])

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
                    if relative_path.startswith("email-admin-ui:"):
                        self.assertEqual(scenario["id"], "SEC-01")
                        self.assertEqual(relative_path, SEC_01_RULES_TEST_PATH)
                        continue
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

    def test_sec_01_records_cross_repository_tests_and_commit_metadata(self):
        scenario = next(
            item for item in self.registry["scenarios"] if item["id"] == "SEC-01"
        )
        evidence = self.registry["latestExecutionEvidence"]
        latest = scenario["latestResult"]

        self.assertEqual(
            scenario["testPaths"],
            [SEC_01_ADAPTER_TEST_PATH, SEC_01_RULES_TEST_PATH],
        )
        self.assertEqual(latest["status"], "passed")
        self.assertEqual(latest["level"], "L2")
        self.assertEqual(latest["sourceCommit"], RUNNABLE_SOURCE_COMMIT)
        self.assertEqual(evidence["sourceCommit"], RUNNABLE_SOURCE_COMMIT)
        self.assertEqual(
            latest["dependencyCommits"],
            {"emailAdminUi": EMAIL_ADMIN_UI_COMMIT},
        )
        self.assertLessEqual(len(latest["summary"]), 300)
        for boundary in ("SEC-02", "SEC-03", "deployment", "server-writer"):
            self.assertIn(boundary, latest["summary"])

    def test_latest_execution_evidence_is_scenario_scoped_and_reproducible(self):
        evidence = self.registry["latestExecutionEvidence"]

        self.assertEqual(evidence["artifact"], SEC_01_EVIDENCE_RELATIVE_PATH)
        self.assertEqual(evidence["scenarioIds"], ["SEC-01"])
        self.assertEqual(evidence["sourceCommit"], RUNNABLE_SOURCE_COMMIT)
        self.assertEqual(
            evidence["dependencyCommits"],
            {"emailAdminUi": EMAIL_ADMIN_UI_COMMIT},
        )
        self.assertEqual(evidence["command"], self.registry["levels"]["L2"]["command"])
        self.assertEqual(evidence["status"], "passed")
        self.assertGreater(evidence["testsRun"], 0)
        self.assertEqual(evidence["failures"], 0)
        self.assertEqual(evidence["errors"], 0)
        self.assertEqual(evidence["skipped"], 0)
        self.assertGreater(evidence["durationMs"], 0)

        evidence_text = (REPO_ROOT / evidence["artifact"]).read_text(encoding="utf-8")
        for expected_text in (
            evidence["sourceCommit"],
            ADAPTER_IMPLEMENTATION_COMMIT,
            EMAIL_ADMIN_UI_COMMIT,
            f"{evidence['command']}",
            f"tests={evidence['testsRun']}",
            f"duration_ms={evidence['durationMs']}",
            "SEC-01",
            "Gate 2 remains unauthorized",
        ):
            self.assertIn(expected_text, evidence_text)

    def test_latest_evidence_source_commit_contains_runnable_l2_profile(self):
        evidence = self.registry["latestExecutionEvidence"]
        source_commit = evidence["sourceCommit"]
        completed = subprocess.run(
            [
                "git",
                "show",
                f"{source_commit}:docs/release-safety/scenario-registry.json",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0)
        source_registry = json.loads(completed.stdout)
        source_l2 = source_registry["levels"]["L2"]
        self.assertEqual(source_l2["availability"], "environment_required")
        self.assertEqual(
            source_l2["requiredEnvironment"],
            ["SITESIFT_ADMIN_UI_ROOT"],
        )
        self.assertEqual(source_l2["scenarioIds"], ["SEC-01"])
        self.assertEqual(
            source_l2["command"],
            "./scripts/run_test_level.sh --level L2",
        )

    def test_each_scenario_points_to_its_own_existing_evidence(self):
        for scenario in self.registry["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                evidence_path = REPO_ROOT / scenario["evidenceArtifact"]
                self.assertTrue(evidence_path.is_file())
                if scenario["id"] == "SEC-01":
                    self.assertEqual(
                        scenario["evidenceArtifact"],
                        SEC_01_EVIDENCE_RELATIVE_PATH,
                    )
                else:
                    self.assertEqual(
                        scenario["evidenceArtifact"],
                        EVIDENCE_RELATIVE_PATH,
                    )


if __name__ == "__main__":
    unittest.main()
