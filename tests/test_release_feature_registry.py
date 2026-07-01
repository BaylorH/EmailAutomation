import json
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-registry.json"
RUBRICS_PATH = REPO_ROOT / "docs" / "release-safety" / "adversarial-rubrics.json"
OUTBOUND_INVENTORY_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "outbound-send-surface-inventory.json"
)

REQUIRED_FEATURE_IDS = {
    "core.upload_mapping",
    "core.name_resolution",
    "core.launch_draft",
    "core.outbox_send",
    "core.reply_all_cc",
    "core.manual_reply",
    "core.inbox_matching",
    "core.inbox_auto_reply",
    "core.event_classifier",
    "core.property_extraction",
    "core.sheet_update",
    "core.followups",
    "core.stop_cancel_dismiss",
    "core.signature_identity",
    "core.health_recovery",
    "admin.usage_readonly",
    "core.scheduler_scope",
    "results.launcher",
    "results.summary_pdf",
    "results.packet_pdf",
    "results.map_geocode",
    "results.saved_rebuild",
    "tour.planner_preview",
    "tour.route_timing",
    "tour.invite_queue",
    "tour.reply_handling",
    "tour.alternate_time",
    "infra.shared_entitlements",
    "infra.cloud_scheduler",
    "infra.cloud_tasks",
    "infra.firestore_lane_rules",
    "infra.observability",
}

VALID_LANES = {
    "production_v1_core",
    "production_v1_admin",
    "dev_results",
    "dev_tour",
    "later_firebase_native",
}

VALID_RELEASE_STATUSES = {
    "prod_required",
    "prod_guarded",
    "dev_only",
    "later",
}

VALID_SEND_RISKS = {
    "none",
    "read_only",
    "queues_email",
    "sends_email_user_click",
    "sends_email_autonomous",
    "recovery_send",
}

REQUIRED_RUBRIC_IDS = {
    "placeholder_name_resolution",
    "core_vs_tour_language_isolation",
    "recipient_reply_all_preservation",
    "manual_intervention_duplicate_send",
    "ai_failure_visibility",
    "dashboard_state_truth",
    "entitlement_bypass",
    "prompt_taxonomy_drift",
    "signature_identity_boundary",
    "data_write_artifact_parity",
    "blocked_contact_optout",
}

SEND_RISKS_REQUIRING_FIXTURE_COVERAGE = {
    "queues_email",
    "sends_email_user_click",
    "sends_email_autonomous",
    "recovery_send",
}

SEND_RISK_BASE_FIXTURE_CATEGORIES = {
    "happy_path",
    "operator_visible_failure",
}

RUBRIC_FIXTURE_REQUIREMENTS = {
    "placeholder_name_resolution": {"bad_placeholder"},
    "recipient_reply_all_preservation": {"wrong_recipient"},
    "manual_intervention_duplicate_send": {"manual_continuation", "duplicate_retry"},
    "ai_failure_visibility": {"operator_visible_failure"},
    "dashboard_state_truth": {"terminal_state", "operator_visible_failure"},
    "entitlement_bypass": {"wrong_recipient", "operator_visible_failure"},
    "blocked_contact_optout": {"wrong_recipient", "operator_visible_failure"},
}

FIXTURE_CATEGORY_ALIASES = {
    "happy_path": ("happy_path", "baylor_only"),
    "terminal_state": (
        "terminal",
        "cancelled",
        "decline",
        "no_followup",
        "closed",
        "unavailable",
    ),
    "bad_placeholder": ("bad_placeholder", "placeholder", "blank_name", "missing_name"),
    "wrong_recipient": (
        "wrong_recipient",
        "recipient_mismatch",
        "cc_preservation",
        "reply_all",
        "blocked_cc",
        "normal_user",
        "scope_denied",
        "jill_rejected",
    ),
    "manual_continuation": (
        "manual_continuation",
        "manual_sent",
        "manual_user",
    ),
    "duplicate_retry": ("duplicate", "retry", "idempotent", "partial_success"),
    "operator_visible_failure": (
        "operator_visible",
        "visible",
        "failure",
        "failed",
        "denied",
        "block",
        "dead_letter",
    ),
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


class ReleaseFeatureRegistryTests(unittest.TestCase):
    def test_registry_and_rubric_files_exist(self):
        self.assertTrue(
            REGISTRY_PATH.exists(),
            "docs/release-safety/feature-registry.json must map every production/dev feature before release.",
        )
        self.assertTrue(
            RUBRICS_PATH.exists(),
            "docs/release-safety/adversarial-rubrics.json must define the adversarial test categories.",
        )

    def test_registry_has_required_features_and_unique_ids(self):
        registry = _read_json(REGISTRY_PATH)
        features = registry.get("features", [])
        feature_ids = [feature.get("id") for feature in features]

        self.assertEqual(1, registry.get("schemaVersion"))
        self.assertEqual(len(feature_ids), len(set(feature_ids)), "Feature ids must be unique.")
        self.assertTrue(
            REQUIRED_FEATURE_IDS.issubset(set(feature_ids)),
            "Feature registry is missing launch-critical feature ids.",
        )

    def test_each_feature_has_release_safety_contract(self):
        registry = _read_json(REGISTRY_PATH)
        rubric_ids = {rubric["id"] for rubric in _read_json(RUBRICS_PATH).get("rubrics", [])}

        for feature in registry.get("features", []):
            with self.subTest(feature=feature.get("id")):
                self.assertIn(feature.get("lane"), VALID_LANES)
                self.assertIn(feature.get("releaseStatus"), VALID_RELEASE_STATUSES)
                self.assertIn(feature.get("sendRisk"), VALID_SEND_RISKS)
                self.assertTrue(feature.get("name"))
                self.assertTrue(feature.get("ownerModules"))
                self.assertTrue(feature.get("uiSurfaces"))
                self.assertTrue(feature.get("testFixtures"))
                self.assertTrue(feature.get("manualScreenshotRubrics"))
                self.assertTrue(feature.get("codeRabbitChecks"))
                self.assertIn("productionGate", feature)
                self.assertTrue(
                    set(feature.get("adversarialRubrics", [])).issubset(rubric_ids),
                    "Feature references an unknown adversarial rubric.",
                )

                if feature.get("sendRisk") in SEND_RISKS_REQUIRING_FIXTURE_COVERAGE:
                    required_categories = set(SEND_RISK_BASE_FIXTURE_CATEGORIES)
                    for rubric_id in feature.get("adversarialRubrics", []):
                        required_categories.update(RUBRIC_FIXTURE_REQUIREMENTS.get(rubric_id, set()))
                    fixture_text = " ".join(feature.get("testFixtures", [])).lower()
                    for category in required_categories:
                        aliases = FIXTURE_CATEGORY_ALIASES[category]
                        self.assertTrue(
                            any(alias in fixture_text for alias in aliases),
                            f"{feature.get('id')} send-risk fixtures must cover {category}.",
                        )

    def test_feature_dependencies_resolve_to_known_feature_ids(self):
        registry = _read_json(REGISTRY_PATH)
        features = registry.get("features", [])
        feature_ids = {feature.get("id") for feature in features}

        for feature in registry.get("features", []):
            for dependency in feature.get("dependencies", []):
                with self.subTest(feature=feature.get("id"), dependency=dependency):
                    self.assertIn(
                        dependency,
                        feature_ids,
                        "Feature dependency must reference another registry feature id.",
                    )

        graph = {feature.get("id"): feature.get("dependencies", []) for feature in features}
        visited = set()
        visiting = []

        def visit(feature_id):
            if feature_id in visited:
                return
            if feature_id in visiting:
                cycle = " -> ".join([*visiting, feature_id])
                self.fail(f"Feature dependencies must be acyclic; found {cycle}.")
            visiting.append(feature_id)
            for dependency in graph.get(feature_id, []):
                visit(dependency)
            visiting.pop()
            visited.add(feature_id)

        for feature_id in feature_ids:
            visit(feature_id)

    def test_processing_send_surface_is_not_misclassified_as_read_only(self):
        registry = _read_json(REGISTRY_PATH)
        features = {feature.get("id"): feature for feature in registry.get("features", [])}
        outbound = _read_json(OUTBOUND_INVENTORY_PATH)
        processing_surface = next(
            (
                surface
                for surface in outbound.get("sendSurfaces", [])
                if surface.get("path") == "email_automation/processing.py"
            ),
            None,
        )
        if processing_surface is None:
            self.fail("Outbound inventory must include email_automation/processing.py.")

        feature = features.get("core.inbox_auto_reply")
        self.assertIsNotNone(feature)
        self.assertEqual("inbox_auto_reply", processing_surface.get("lane"))
        self.assertIn("email_automation/processing.py", feature.get("ownerModules", {}).get("backend", []))
        self.assertEqual("sends_email_autonomous", feature.get("sendRisk"))
        self.assertTrue(feature.get("productionGate", {}).get("requiredBeforePush"))

    def test_current_scheduler_scope_is_prod_required(self):
        registry = _read_json(REGISTRY_PATH)
        features = {feature.get("id"): feature for feature in registry.get("features", [])}
        scheduler = features.get("core.scheduler_scope")

        self.assertIsNotNone(scheduler)
        self.assertEqual("production_v1_core", scheduler.get("lane"))
        self.assertEqual("prod_required", scheduler.get("releaseStatus"))
        self.assertEqual("sends_email_autonomous", scheduler.get("sendRisk"))
        self.assertTrue(scheduler.get("productionGate", {}).get("requiredBeforePush"))
        owner_modules = scheduler.get("ownerModules", {})
        self.assertIn("main.py", owner_modules.get("backend", []))
        self.assertIn("app.py", owner_modules.get("backend", []))
        self.assertIn(".github/workflows/email.yml", owner_modules.get("githubActions", []))

    def test_dev_features_are_not_normal_user_enabled(self):
        registry = _read_json(REGISTRY_PATH)

        for feature in registry.get("features", []):
            if feature.get("lane") in {"dev_results", "dev_tour", "later_firebase_native"}:
                with self.subTest(feature=feature.get("id")):
                    self.assertFalse(feature.get("normalUserAccess"))
                    self.assertNotEqual("prod_required", feature.get("releaseStatus"))
                    gate = feature.get("productionGate", {})
                    self.assertFalse(gate.get("requiredBeforePush"))
                    self.assertTrue(gate.get("stopConditions"))

    def test_rubrics_cover_named_failure_classes(self):
        rubrics = _read_json(RUBRICS_PATH).get("rubrics", [])
        rubric_ids = {rubric.get("id") for rubric in rubrics}

        self.assertTrue(
            REQUIRED_RUBRIC_IDS.issubset(rubric_ids),
            "Rubric pack must cover the known Karsen/Jill failure classes.",
        )

        for rubric in rubrics:
            with self.subTest(rubric=rubric.get("id")):
                self.assertTrue(rubric.get("goal"))
                self.assertTrue(rubric.get("breakAttempts"))
                self.assertTrue(rubric.get("evidence"))
                self.assertTrue(rubric.get("stopConditions"))


if __name__ == "__main__":
    unittest.main()
