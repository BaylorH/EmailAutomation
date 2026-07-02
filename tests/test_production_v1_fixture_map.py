import json
import re
import unittest
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SAFETY_DIR = REPO_ROOT / "docs" / "release-safety"
GRADEBOOK_PATH = RELEASE_SAFETY_DIR / "feature-gradebook.json"
FIXTURE_MAP_PATH = RELEASE_SAFETY_DIR / "production-v1-fixture-map.json"

INCIDENT_ROWS = {
    ("core.name_resolution", "bad_placeholder"): "karsen_name_placeholder",
    ("core.launch_draft", "bad_placeholder"): "karsen_raw_name_message",
    ("core.launch_draft", "wrong_recipient"): "wrong_recipient_launch_recipient_mismatch",
    ("core.outbox_send", "wrong_recipient"): "wrong_recipient_safety_block",
    ("core.outbox_send", "duplicate_retry"): "graph_accepted_index_missing",
    ("core.reply_all_cc", "wrong_recipient"): "reply_all_cc_preservation",
    ("core.inbox_auto_reply", "terminal_state"): "no_tour_scheduling_core_lane",
    ("core.event_classifier", "terminal_state"): "tour_unavailable_property_stays_viable",
    ("core.followups", "manual_continuation"): "manual_sent_items_suppresses_followup",
    ("core.health_recovery", "operator_visible_failure"): "dead_letter_visible_before_retry",
    ("core.scheduler_scope", "wrong_recipient"): "normal_users_scope_denied",
    ("core.signature_identity", "wrong_recipient"): "jill_mohr_signature_leakage",
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


class ProductionV1FixtureMapTests(unittest.TestCase):
    def test_production_v1_fixture_map_exists(self):
        self.assertTrue(
            FIXTURE_MAP_PATH.exists(),
            "Production V1 needs an executable fixture map that links gradebook cells to concrete tests or explicit gaps.",
        )

    def test_fixture_map_covers_every_production_feature_and_fixture_class(self):
        gradebook = _read_json(GRADEBOOK_PATH)
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        suite = gradebook["releaseSuites"]["production_v1_base_campaign"]
        required_features = set(suite["featureIds"])
        required_fixture_classes = set(suite["requiredFixtureClasses"])
        valid_statuses = set(fixture_map["statusLegend"])
        matrix = fixture_map["featureFixtureMatrix"]

        self.assertEqual(1, fixture_map.get("schemaVersion"))
        self.assertEqual("production_v1_base_campaign", fixture_map.get("sourceSuite"))
        self.assertEqual(required_features, set(matrix))
        self.assertEqual(
            required_fixture_classes,
            set(fixture_map["fixtureClasses"]),
            "Fixture map must inherit the full Production V1 fixture class set.",
        )

        for feature_id, fixture_cells in matrix.items():
            with self.subTest(feature=feature_id):
                self.assertEqual(
                    required_fixture_classes,
                    set(fixture_cells),
                    f"{feature_id} must name every fixture class, including uncovered gaps.",
                )
                for fixture_class, cell in fixture_cells.items():
                    with self.subTest(feature=feature_id, fixture_class=fixture_class):
                        self.assertIn(cell.get("status"), valid_statuses)
                        self.assertTrue(cell.get("eventClasses"))
                        self.assertTrue(cell.get("combinationPlaybooks"))
                        self.assertTrue(cell.get("statePermutations"))
                        self.assertTrue(cell.get("evidence"))
                        if cell["status"] == "covered":
                            self.assertTrue(cell.get("testFiles"))
                            self.assertTrue(cell.get("testIds"))
                            for test_file in cell["testFiles"]:
                                self.assertTrue(
                                    (REPO_ROOT / test_file).exists(),
                                    f"{feature_id}/{fixture_class} references missing test file {test_file}.",
                                )
                            test_corpus = "\n".join(
                                (REPO_ROOT / test_file).read_text(errors="ignore")
                                for test_file in cell["testFiles"]
                            )
                            for test_id in cell["testIds"]:
                                self.assertRegex(
                                    test_corpus,
                                    rf"def\s+{re.escape(test_id)}\s*\(",
                                    f"{feature_id}/{fixture_class} references stale or descriptive testId {test_id}.",
                                )
                        else:
                            self.assertTrue(cell.get("gapReason"))
                            self.assertTrue(cell.get("nextProof"))

    def test_fixture_map_covers_required_events_and_combinations(self):
        gradebook = _read_json(GRADEBOOK_PATH)
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        suite = gradebook["releaseSuites"]["production_v1_base_campaign"]
        cells = [
            cell
            for feature_cells in fixture_map["featureFixtureMatrix"].values()
            for cell in feature_cells.values()
        ]
        covered_events = {event for cell in cells for event in cell["eventClasses"]}
        covered_combinations = {
            combination for cell in cells for combination in cell["combinationPlaybooks"]
        }
        covered_states = {state for cell in cells for state in cell["statePermutations"]}

        self.assertTrue(set(suite["requiredEventClasses"]).issubset(covered_events))
        self.assertTrue(set(suite["requiredCombinationPlaybooks"]).issubset(covered_combinations))
        self.assertTrue(set(suite["requiredStatePermutations"]).issubset(covered_states))

    def test_summary_counts_match_fixture_cells(self):
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        cells = [
            cell
            for feature_cells in fixture_map["featureFixtureMatrix"].values()
            for cell in feature_cells.values()
        ]
        counts = Counter(cell["status"] for cell in cells)

        self.assertEqual(dict(counts), fixture_map["summary"])

    def test_known_incident_rows_are_named_and_not_generic(self):
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        matrix = fixture_map["featureFixtureMatrix"]
        for (feature_id, fixture_class), incident_id in INCIDENT_ROWS.items():
            with self.subTest(feature=feature_id, fixture_class=fixture_class):
                cell = matrix[feature_id][fixture_class]
                self.assertEqual(
                    incident_id,
                    cell.get("incidentId"),
                    "Known incident-class rows must be named so proof is not a generic happy-path replay.",
                )
                self.assertTrue(cell.get("nextProof") or cell.get("testIds"))

    def test_manual_dashboard_reply_has_dedicated_send_safety_fixtures(self):
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        manual_reply = fixture_map["featureFixtureMatrix"]["core.manual_reply"]
        required_cells = {
            "happy_path",
            "bad_placeholder",
            "manual_continuation",
            "duplicate_retry",
            "operator_visible_failure",
        }

        for fixture_class in required_cells:
            with self.subTest(fixture_class=fixture_class):
                cell = manual_reply[fixture_class]
                self.assertEqual("covered", cell["status"])
                self.assertIn("dashboard_action_resolution", cell["eventClasses"])
                self.assertIn("manual_reply_before_retry", cell["combinationPlaybooks"])
                self.assertTrue(cell.get("testIds"))

    def test_inbox_auto_reply_has_retry_and_visibility_fixtures(self):
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        auto_reply = fixture_map["featureFixtureMatrix"]["core.inbox_auto_reply"]
        required_cells = {
            "manual_continuation",
            "duplicate_retry",
            "operator_visible_failure",
        }

        for fixture_class in required_cells:
            with self.subTest(fixture_class=fixture_class):
                cell = auto_reply[fixture_class]
                self.assertEqual("covered", cell["status"])
                self.assertIn("broker_reply", cell["eventClasses"])
                self.assertIn("manual_reply_before_retry", cell["combinationPlaybooks"])
                self.assertTrue(cell.get("testIds"))


if __name__ == "__main__":
    unittest.main()
