import copy
import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / "docs" / "release-safety" / "production-clearance-state.json"

EXPECTED_TRANSITIONS = {
    "BLOCKED": ["CODE_GREEN", "OFF_SAFE"],
    "CODE_GREEN": ["DEPLOYED_DARK", "BLOCKED"],
    "DEPLOYED_DARK": ["BROWSER_PASS", "BLOCKED"],
    "BROWSER_PASS": ["PROD_CLEARED", "BLOCKED"],
    "PROD_CLEARED": ["BLOCKED"],
    "OFF_SAFE": ["BLOCKED"],
}

EXPECTED_FEATURE_LANES = {
    "campaign.start": "core",
    "campaign.identity_context": "core",
    "campaign.mailbox_preflight": "core",
    "campaign.delivery_exact_once": "core",
    "campaign.extraction": "core",
    "campaign.attachments": "core",
    "campaign.partial_completion": "core",
    "campaign.unavailable": "core",
    "campaign.nonviable": "core",
    "campaign.alternate_property": "core",
    "campaign.questions": "core",
    "campaign.call_action": "core",
    "campaign.tour_action": "core",
    "campaign.contact_ooo": "core",
    "campaign.wrong_contact": "core",
    "campaign.optout": "core",
    "campaign.property_issue": "core",
    "campaign.followups": "core",
    "campaign.operator_actions": "core",
    "campaign.stop": "core",
    "campaign.health_recovery": "core",
    "campaign.firestore_integrity": "core",
    "campaign.sheet_formulas": "core",
    "campaign.completion": "core",
    "tour.scheduler": "advanced",
    "tour.invite": "advanced",
    "results.workspace": "advanced",
    "results.artifacts": "advanced",
}


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_reject_duplicate_keys)


class ProductionClearanceStateTests(unittest.TestCase):
    def setUp(self):
        self.state = _load_json(STATE_PATH)

    def _assert_statuses_are_declared(self, state):
        declared_statuses = set(state["statusModel"])
        self.assertIn(state["status"], declared_statuses)
        for milestone in state["milestones"]:
            self.assertIn(
                milestone["status"],
                declared_statuses,
                f"milestone {milestone['id']} uses an undeclared status",
            )

    def test_has_one_authoritative_control_path(self):
        self.assertEqual(
            "docs/superpowers/plans/2026-08-06-browser-first-production-clearance.md",
            self.state.get("authoritativePlan"),
        )
        self.assertEqual(
            "docs/release-safety/feature-gradebook.json",
            self.state.get("canonicalRubric"),
        )
        self.assertEqual("browser_only", self.state["executionPolicy"]["productActions"])

        authoritative_plan = REPO_ROOT / self.state["authoritativePlan"]
        canonical_rubric = REPO_ROOT / self.state["canonicalRubric"]
        self.assertTrue(authoritative_plan.is_file())
        self.assertTrue(canonical_rubric.is_file())

    def test_milestones_are_r0_through_r7_once_and_in_order(self):
        milestone_ids = [row["id"] for row in self.state["milestones"]]
        self.assertEqual([f"R{index}" for index in range(8)], milestone_ids)
        self.assertEqual(len(milestone_ids), len(set(milestone_ids)))

    def test_status_transitions_are_exact_and_fail_closed(self):
        self.assertEqual(list(EXPECTED_TRANSITIONS), self.state["statusModel"])
        self.assertEqual(EXPECTED_TRANSITIONS, self.state["allowedTransitions"])
        self._assert_statuses_are_declared(self.state)

        for status, destinations in self.state["allowedTransitions"].items():
            with self.subTest(status=status):
                if status != "BLOCKED":
                    self.assertIn("BLOCKED", destinations)
                self.assertEqual(len(destinations), len(set(destinations)))

    def test_invalid_status_mutations_fail_the_status_contract(self):
        invalid_top_level = copy.deepcopy(self.state)
        invalid_top_level["status"] = "NOT_A_REAL_STATUS"
        invalid_milestone = copy.deepcopy(self.state)
        invalid_milestone["milestones"][0]["status"] = "NOT_A_REAL_STATUS"

        for name, mutated_state in (
            ("top_level", invalid_top_level),
            ("milestone", invalid_milestone),
        ):
            with self.subTest(case=name):
                with self.assertRaises(AssertionError):
                    self._assert_statuses_are_declared(mutated_state)

    def test_feature_stamps_cover_each_required_lane_exactly_once(self):
        stamps = self.state["featureStamps"]
        feature_ids = [row["featureId"] for row in stamps]
        self.assertEqual(len(feature_ids), len(set(feature_ids)))
        self.assertEqual(
            EXPECTED_FEATURE_LANES,
            {row["featureId"]: row["lane"] for row in stamps},
        )
        for stamp in stamps:
            with self.subTest(feature=stamp["featureId"]):
                self.assertIn(stamp["status"], self.state["statusModel"])

    def test_records_passing_browser_preflight_without_side_effects(self):
        preflight = self.state["browserPreflight"]
        self.assertEqual("none", preflight["sideEffects"])
        self.assertTrue(preflight["checkedAt"])
        for control in (
            "dedicatedBrowserControl",
            "authenticatedDashboard",
            "parallelChromeControl",
        ):
            with self.subTest(control=control):
                self.assertEqual("PASS", preflight[control]["status"])
                self.assertTrue(preflight[control]["evidence"])

    def test_canonical_rubric_repeats_the_executable_clearance_policy(self):
        rubric = _load_json(REPO_ROOT / self.state["canonicalRubric"])
        policy = rubric["clearanceStatusModel"]
        self.assertEqual(self.state["statusModel"], policy["statuses"])
        self.assertEqual(self.state["allowedTransitions"], policy["allowedTransitions"])
        self.assertEqual("BLOCKED", policy["invalidationTarget"])

    def test_core_call_and_tour_actions_map_to_state_stamps(self):
        rubric = _load_json(REPO_ROOT / self.state["canonicalRubric"])
        action_entries = rubric["releaseSuites"]["production_v1_base_campaign"][
            "coreActionEntries"
        ]
        self.assertEqual(
            {"core.call_action", "core.broker_tour_action"},
            {row["id"] for row in action_entries},
        )
        self.assertEqual(
            {"campaign.call_action", "campaign.tour_action"},
            {row["featureStampId"] for row in action_entries},
        )
        self.assertTrue(all(row["lane"] == "core" for row in action_entries))
        self.assertEqual(
            {
                "tour.planner_preview",
                "tour.route_timing",
                "tour.invite_queue",
            },
            set(
                rubric["browserExecutionPolicy"][
                    "advancedTourFeatureIds"
                ]
            ),
        )

    def test_gradebook_policies_make_browser_freshness_and_checkpoints_executable(self):
        rubric = _load_json(REPO_ROOT / self.state["canonicalRubric"])
        self.assertTrue(
            {
                "browserExecutionPolicy",
                "historicalSeedPolicy",
                "clearanceStatusModel",
                "checkpointPolicy",
            }.issubset(rubric)
        )
        self.assertEqual(
            "browser_only",
            rubric["browserExecutionPolicy"]["productActions"],
        )
        self.assertEqual(
            "prohibited",
            rubric["historicalSeedPolicy"]["exactBodyReuse"],
        )
        self.assertEqual(
            self.state["checkpointLedger"],
            rubric["checkpointPolicy"]["ledger"],
        )
        self.assertTrue(rubric["checkpointPolicy"]["appendOnly"])

    def test_state_points_to_each_contract_executable(self):
        self.assertEqual(
            {
                "stateTest": "tests/test_production_clearance_state.py",
                "browserVariantFixture": (
                    "tests/fixtures/production_browser_conversation_variants.json"
                ),
                "browserVariantSelector": (
                    "scripts/select_production_browser_variant.py"
                ),
                "evidencePiiScanner": "scripts/scan_clearance_evidence_pii.py",
            },
            self.state["contractExecutables"],
        )


if __name__ == "__main__":
    unittest.main()
