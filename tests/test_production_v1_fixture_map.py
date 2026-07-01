import json
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_SAFETY_DIR = REPO_ROOT / "docs" / "release-safety"
GRADEBOOK_PATH = RELEASE_SAFETY_DIR / "feature-gradebook.json"
FIXTURE_MAP_PATH = RELEASE_SAFETY_DIR / "production-v1-fixture-map.json"

INCIDENT_ROWS = {
    ("core.name_resolution", "bad_placeholder"): "karsen_name_placeholder",
    ("core.launch_draft", "bad_placeholder"): "karsen_raw_name_message",
    ("core.launch_draft", "wrong_recipient"): "wrong_recipient_launch_guard",
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
                                self.assertIn(
                                    test_id,
                                    test_corpus,
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


if __name__ == "__main__":
    unittest.main()
