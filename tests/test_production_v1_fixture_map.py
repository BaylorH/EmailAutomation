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
                            self.assertTrue(
                                cell.get("provesBehavior"),
                                f"{feature_id}/{fixture_class} is 'covered' but does not state "
                                f"provesBehavior - a covered cell must name what its test proves "
                                f"about THIS feature under THIS fixture class (borrowed/assert-nothing "
                                f"greens cannot honestly produce this).",
                            )
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

    def test_covered_cells_state_distinct_proves_behavior(self):
        """A covered cell must make a DISTINCT claim about what its test proves.

        Two covered cells sharing an identical provesBehavior means one test is
        being reused to satisfy an unrelated cell (a borrowed green). A test that
        genuinely proves a specific feature x fixture-class cannot honestly carry
        the same claim as a different cell.
        """
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        seen: dict[str, str] = {}
        for feature_id, cells in fixture_map["featureFixtureMatrix"].items():
            for fixture_class, cell in cells.items():
                if cell.get("status") != "covered":
                    continue
                proves = " ".join((cell.get("provesBehavior") or "").split()).lower()
                with self.subTest(feature=feature_id, fixture_class=fixture_class):
                    self.assertTrue(
                        proves,
                        f"{feature_id}/{fixture_class} covered cell has no provesBehavior.",
                    )
                    self.assertNotIn(
                        proves,
                        seen,
                        f"{feature_id}/{fixture_class} shares an identical provesBehavior "
                        f"with {seen.get(proves)} - a borrowed/assert-nothing green.",
                    )
                seen[proves] = f"{feature_id}/{fixture_class}"

    def test_gap_cells_state_distinct_gap_reasons(self):
        """Templated boilerplate gap reasons are banned.

        Each uncovered cell must name its own specific missing proof; identical
        gapReasons signal copy-pasted placeholders that hide what is actually
        unproven.
        """
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        seen: dict[str, str] = {}
        for feature_id, cells in fixture_map["featureFixtureMatrix"].items():
            for fixture_class, cell in cells.items():
                if cell.get("status") == "covered":
                    continue
                reason = " ".join((cell.get("gapReason") or "").split()).lower()
                with self.subTest(feature=feature_id, fixture_class=fixture_class):
                    self.assertTrue(
                        reason,
                        f"{feature_id}/{fixture_class} gap cell has no gapReason.",
                    )
                    self.assertNotIn(
                        reason,
                        seen,
                        f"{feature_id}/{fixture_class} shares an identical gapReason with "
                        f"{seen.get(reason)} - templated boilerplate is banned.",
                    )
                seen[reason] = f"{feature_id}/{fixture_class}"

    def test_stress_matrix_covers_every_feature_and_stress_class(self):
        """The additive stress lane widens coverage into adversarial operating
        conditions (429/throttle, concurrency, malformed data, partial-batch
        failure, retry storm, near-miss negatives) WITHOUT weakening the base
        seven-class send-risk contract. Every Production V1 feature must name
        every stress class - covered with a real test + provesBehavior, or an
        honestly-named gap. Distinct provesBehavior / gapReason are enforced so a
        stress cell cannot be a borrowed green or templated boilerplate.
        """
        gradebook = _read_json(GRADEBOOK_PATH)
        fixture_map = _read_json(FIXTURE_MAP_PATH)
        suite = gradebook["releaseSuites"]["production_v1_base_campaign"]
        required_features = set(suite["featureIds"])
        valid_statuses = set(fixture_map["statusLegend"])
        stress_classes = fixture_map.get("stressFixtureClasses")
        matrix = fixture_map.get("featureStressMatrix")

        self.assertTrue(stress_classes, "Fixture map must declare stressFixtureClasses.")
        self.assertTrue(matrix, "Fixture map must declare featureStressMatrix.")
        self.assertEqual(
            required_features,
            set(matrix),
            "Every Production V1 feature must appear in the stress matrix.",
        )

        proves_seen: dict[str, str] = {}
        gap_seen: dict[str, str] = {}
        counts: Counter = Counter()
        for feature_id, cells in matrix.items():
            with self.subTest(feature=feature_id):
                self.assertEqual(
                    set(stress_classes),
                    set(cells),
                    f"{feature_id} must name every stress class, including uncovered gaps.",
                )
            for stress_class, cell in cells.items():
                with self.subTest(feature=feature_id, stress_class=stress_class):
                    self.assertIn(cell.get("status"), valid_statuses)
                    self.assertTrue(cell.get("stressScenario"))
                    counts[cell["status"]] += 1
                    if cell["status"] == "covered":
                        self.assertTrue(cell.get("testFiles"))
                        self.assertTrue(cell.get("testIds"))
                        proves = " ".join((cell.get("provesBehavior") or "").split()).lower()
                        self.assertTrue(
                            proves,
                            f"{feature_id}/{stress_class} covered stress cell needs provesBehavior.",
                        )
                        self.assertNotIn(
                            proves,
                            proves_seen,
                            f"{feature_id}/{stress_class} shares provesBehavior with "
                            f"{proves_seen.get(proves)} - borrowed stress green.",
                        )
                        proves_seen[proves] = f"{feature_id}/{stress_class}"
                        test_corpus = "\n".join(
                            (REPO_ROOT / test_file).read_text(errors="ignore")
                            for test_file in cell["testFiles"]
                        )
                        for test_id in cell["testIds"]:
                            self.assertRegex(
                                test_corpus,
                                rf"def\s+{re.escape(test_id)}\s*\(",
                                f"{feature_id}/{stress_class} references missing testId {test_id}.",
                            )
                    else:
                        self.assertTrue(cell.get("nextProof"))
                        reason = " ".join((cell.get("gapReason") or "").split()).lower()
                        self.assertTrue(reason, f"{feature_id}/{stress_class} gap needs gapReason.")
                        self.assertNotIn(
                            reason,
                            gap_seen,
                            f"{feature_id}/{stress_class} shares gapReason with "
                            f"{gap_seen.get(reason)} - templated stress boilerplate.",
                        )
                        gap_seen[reason] = f"{feature_id}/{stress_class}"

        self.assertEqual(
            dict(counts),
            fixture_map.get("stressSummary"),
            "stressSummary must match the stress-matrix cell counts.",
        )

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
            "terminal_state",
            "bad_placeholder",
            "wrong_recipient",
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
            "happy_path",
            "manual_continuation",
            "duplicate_retry",
            "operator_visible_failure",
        }

        for fixture_class in required_cells:
            with self.subTest(fixture_class=fixture_class):
                cell = auto_reply[fixture_class]
                self.assertEqual("covered", cell["status"])
                self.assertIn("broker_reply", cell["eventClasses"])
                if fixture_class != "happy_path":
                    self.assertIn("manual_reply_before_retry", cell["combinationPlaybooks"])
                self.assertTrue(cell.get("testIds"))


if __name__ == "__main__":
    unittest.main()
