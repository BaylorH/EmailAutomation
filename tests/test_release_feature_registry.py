import json
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-registry.json"
RUBRICS_PATH = REPO_ROOT / "docs" / "release-safety" / "adversarial-rubrics.json"
GRADEBOOK_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-gradebook.json"
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
    "terminal_state",
    "bad_placeholder",
    "wrong_recipient",
    "manual_continuation",
    "duplicate_retry",
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

REQUIRED_GRADEBOOK_DIMENSIONS = {
    "eventTaxonomy",
    "eventVariantCatalog",
    "triggerVariationAxes",
    "combinationPlaybooks",
    "combinationStressDecks",
    "statePermutations",
    "actorAndAccountAxes",
    "evidenceRequirements",
    "fixtureClasses",
    "gradingRoles",
    "freshnessRules",
    "releaseSuites",
    "featureInteractionMatrix",
    "featureScenarios",
}

REQUIRED_EVENT_CLASSES = {
    "launch_with_variable_mapping",
    "broker_available_full_specs",
    "broker_available_partial_specs",
    "broker_attachment_or_link_only",
    "broker_property_unavailable",
    "broker_property_non_viable",
    "broker_wrong_contact",
    "broker_opt_out",
    "broker_confidential_question",
    "broker_tour_available",
    "broker_tour_unavailable",
    "broker_alternate_tour_time",
    "broker_new_property_referral",
    "manual_user_continuation",
    "reply_all_cc_context",
    "followup_due",
    "retry_after_uncertain_send",
    "sheet_row_moved",
    "token_or_graph_failure",
    "dashboard_action_resolution",
}

REQUIRED_TRIGGER_VARIATION_AXES = {
    "phrasing",
    "thread_shape",
    "sender_identity",
    "recipient_shape",
    "attachment_shape",
    "data_quality",
    "timing",
    "campaign_state",
    "operator_action",
    "entitlement_scope",
}

REQUIRED_COMBINATION_PLAYBOOKS = {
    "partial_specs_plus_pdf_plus_followup",
    "confidential_question_plus_partial_specs",
    "wrong_contact_plus_new_property",
    "tour_unavailable_but_property_viable",
    "manual_reply_before_retry",
    "reply_all_cc_plus_blocked_contact",
    "row_move_during_pending_action",
    "same_broker_multiple_properties",
    "subject_drift_split_thread",
    "opt_out_after_prior_interest",
    "graph_accepted_but_index_missing",
}

REQUIRED_COMBINATION_STRESS_DECKS = {
    "karsen_launch_placeholder_and_tour_leak",
    "jill_nonviable_vs_unavailable",
    "reply_all_with_redirect_and_blocked_contact",
    "same_broker_multi_property_split_subject",
    "attachment_only_with_ai_failure",
    "stop_cancel_during_claim",
    "confidential_question_with_partial_specs",
    "health_visibility_after_hidden_failure",
}

REQUIRED_STATE_PERMUTATIONS = {
    "not_started",
    "outbox_queued",
    "live_waiting",
    "action_needed",
    "paused_manual_review",
    "stopped",
    "completed",
    "archived",
    "dead_letter_visible",
    "retry_reconciled",
}

REQUIRED_GRADING_ROLES = {
    "broker_operator",
    "tenant_rep",
    "backend_safety_reviewer",
    "frontend_ux_reviewer",
    "privacy_reviewer",
    "support_recovery_reviewer",
}

REQUIRED_RELEASE_SUITES = {
    "production_v1_base_campaign",
    "results_dev_artifact_workspace",
    "tour_dev_scheduling_lifecycle",
    "firebase_native_worker_migration",
}

SHEET_EVIDENCE_FEATURE_IDS = {
    "core.property_extraction",
    "core.sheet_update",
    "results.summary_pdf",
    "results.packet_pdf",
    "results.saved_rebuild",
}

FEATURE_SPECIFIC_NEGATIVE_CONTROL_KEYWORDS = {
    "core.event_classifier": ("tour unavailable", "non-viable"),
    "core.property_extraction": ("invent", "source"),
    "core.sheet_update": ("wrong row",),
    "core.stop_cancel_dismiss": ("stopped", "autonomous"),
    "admin.usage_readonly": ("admin", "non-admin"),
    "results.summary_pdf": ("blank", "source"),
    "results.packet_pdf": ("invent", "source"),
    "results.map_geocode": ("geocode", "manual review"),
    "tour.reply_handling": ("tour unavailable", "non-viable"),
    "tour.alternate_time": ("date", "route"),
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
        self.assertTrue(
            GRADEBOOK_PATH.exists(),
            "docs/release-safety/feature-gradebook.json must define broad event, variation, combination, and grading coverage.",
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

    def test_feature_gradebook_forces_broad_event_and_combination_coverage(self):
        registry = _read_json(REGISTRY_PATH)
        gradebook = _read_json(GRADEBOOK_PATH)
        feature_ids = {feature.get("id") for feature in registry.get("features", [])}

        self.assertEqual(1, gradebook.get("schemaVersion"))
        self.assertTrue(
            REQUIRED_GRADEBOOK_DIMENSIONS.issubset(gradebook),
            "The gradebook must cover taxonomy, variations, combinations, states, actors, evidence, roles, and feature scenarios.",
        )
        self.assertTrue(
            REQUIRED_EVENT_CLASSES.issubset(set(gradebook["eventTaxonomy"])),
            "Event taxonomy must include the real-world broker/operator events that have broken or could break SiteSift.",
        )
        event_variants = gradebook["eventVariantCatalog"]
        self.assertEqual(
            set(gradebook["eventTaxonomy"]),
            set(event_variants),
            "Every event taxonomy entry must have concrete trigger variants and near-misses.",
        )
        for event_id, variant in event_variants.items():
            with self.subTest(event_variant=event_id):
                for key in ("sampleTriggers", "nearMisses", "expectedSignals", "stopIf"):
                    self.assertTrue(
                        variant.get(key),
                        f"{event_id} must define {key} so release tests are not rigid canned cases.",
                    )
                self.assertGreaterEqual(
                    len(variant["sampleTriggers"]),
                    3,
                    f"{event_id} needs at least three fresh trigger phrasings.",
                )
                self.assertGreaterEqual(
                    len(variant["nearMisses"]),
                    2,
                    f"{event_id} needs near-misses so classifiers prove boundaries, not just positives.",
                )
                self.assertGreaterEqual(
                    len(variant["expectedSignals"]),
                    2,
                    f"{event_id} needs source-of-truth signals for grading.",
                )
                self.assertGreaterEqual(
                    len(variant["stopIf"]),
                    2,
                    f"{event_id} needs hard stop conditions for production-readiness grading.",
                )
                combined_text = " ".join(
                    [*variant["sampleTriggers"], *variant["nearMisses"], *variant["expectedSignals"], *variant["stopIf"]]
                ).lower()
                self.assertNotIn(
                    "todo",
                    combined_text,
                    "Variant catalog must contain usable concrete cases, not TODO placeholders.",
                )
        self.assertTrue(
            REQUIRED_TRIGGER_VARIATION_AXES.issubset(set(gradebook["triggerVariationAxes"])),
            "Trigger variation axes must force fresh wording/account/thread/data variations instead of canned replays.",
        )
        self.assertTrue(
            REQUIRED_COMBINATION_PLAYBOOKS.issubset(set(gradebook["combinationPlaybooks"])),
            "Combination playbooks must cover multi-event collisions like manual replies before retry and tour-unavailable-but-viable.",
        )
        stress_decks = gradebook["combinationStressDecks"]
        self.assertTrue(
            REQUIRED_COMBINATION_STRESS_DECKS.issubset(set(stress_decks)),
            "Stress decks must include the known Karsen/Jill failure clusters and cross-feature collisions.",
        )
        all_playbooks = set(gradebook["combinationPlaybooks"])
        all_events = set(gradebook["eventTaxonomy"])
        for deck_id, deck in stress_decks.items():
            with self.subTest(stress_deck=deck_id):
                self.assertTrue(deck.get("playbooks"))
                self.assertTrue(deck.get("eventClasses"))
                self.assertTrue(deck.get("variantsToCross"))
                self.assertTrue(deck.get("mustProve"))
                self.assertGreaterEqual(
                    len(deck["playbooks"]),
                    3,
                    "Each stress deck must cross at least three combination playbooks.",
                )
                self.assertGreaterEqual(
                    len(deck["eventClasses"]),
                    3,
                    "Each stress deck must collide at least three event classes.",
                )
                self.assertGreaterEqual(
                    len(deck["variantsToCross"]),
                    3,
                    "Each stress deck must name multiple concrete variant axes to cross.",
                )
                self.assertGreaterEqual(
                    len(deck["mustProve"]),
                    3,
                    "Each stress deck must name source-of-truth proof obligations.",
                )
                self.assertTrue(set(deck["playbooks"]).issubset(all_playbooks))
                self.assertTrue(set(deck["eventClasses"]).issubset(all_events))
        self.assertTrue(
            REQUIRED_STATE_PERMUTATIONS.issubset(set(gradebook["statePermutations"])),
            "State permutations must cover lifecycle states from queued through visible recovery.",
        )
        self.assertTrue(
            REQUIRED_GRADING_ROLES.issubset(set(gradebook["gradingRoles"])),
            "Grading roles must force reviewers to judge safety, UX, privacy, and support/recovery separately.",
        )
        self.assertEqual(
            SEND_RISK_BASE_FIXTURE_CATEGORIES,
            set(gradebook["fixtureClasses"]),
            "The gradebook must name the mandatory adversarial fixture classes used by release suites.",
        )
        self.assertTrue(
            REQUIRED_RELEASE_SUITES.issubset(set(gradebook["releaseSuites"])),
            "Release suites must force full Product V1, Results, Tour, and Firebase-native proof tracks instead of isolated spot checks.",
        )

        feature_scenarios = gradebook["featureScenarios"]
        self.assertEqual(
            feature_ids,
            set(feature_scenarios),
            "Every feature in feature-registry.json must have an explicit gradebook scenario entry.",
        )
        used_variation_axes = set()

        for feature_id, scenario in feature_scenarios.items():
            with self.subTest(feature=feature_id):
                for key in (
                    "eventClasses",
                    "variationAxes",
                    "combinationPlaybooks",
                    "statePermutations",
                    "negativeControls",
                    "evidenceRequired",
                    "gradingRoles",
                    "passBar",
                ):
                    self.assertTrue(scenario.get(key), f"{feature_id} is missing gradebook key {key}.")
                self.assertTrue(
                    set(scenario["eventClasses"]).issubset(set(gradebook["eventTaxonomy"])),
                    f"{feature_id} references unknown event classes.",
                )
                self.assertTrue(
                    set(scenario["variationAxes"]).issubset(set(gradebook["triggerVariationAxes"])),
                    f"{feature_id} references unknown variation axes.",
                )
                self.assertTrue(
                    set(scenario["combinationPlaybooks"]).issubset(set(gradebook["combinationPlaybooks"])),
                    f"{feature_id} references unknown combination playbooks.",
                )
                self.assertTrue(
                    set(scenario["statePermutations"]).issubset(set(gradebook["statePermutations"])),
                    f"{feature_id} references unknown state permutations.",
                )
                self.assertTrue(
                    set(scenario["gradingRoles"]).issubset(set(gradebook["gradingRoles"])),
                    f"{feature_id} references unknown grading roles.",
                )
                used_variation_axes.update(scenario["variationAxes"])
                if "broker_attachment_or_link_only" in scenario["eventClasses"]:
                    self.assertIn(
                        "attachment_shape",
                        scenario["variationAxes"],
                        f"{feature_id} handles attachment/link events and must vary attachment shape.",
                    )
                if feature_id in SHEET_EVIDENCE_FEATURE_IDS:
                    self.assertIn(
                        "sheet",
                        scenario["evidenceRequired"],
                        f"{feature_id} must prove Sheet row/cell/provenance readback.",
                    )
                if feature_id in FEATURE_SPECIFIC_NEGATIVE_CONTROL_KEYWORDS:
                    negative_text = " ".join(scenario["negativeControls"]).lower()
                    for keyword in FEATURE_SPECIFIC_NEGATIVE_CONTROL_KEYWORDS[feature_id]:
                        self.assertIn(
                            keyword,
                            negative_text,
                            f"{feature_id} negative controls must include {keyword}.",
                        )

        self.assertEqual(
            set(gradebook["triggerVariationAxes"]),
            used_variation_axes,
            "Every trigger variation axis must be used by at least one feature scenario.",
        )

        suite_feature_ids = {
            feature_id
            for suite in gradebook["releaseSuites"].values()
            for feature_id in suite.get("featureIds", [])
        }
        self.assertEqual(
            feature_ids,
            suite_feature_ids,
            "Every feature must belong to at least one release suite.",
        )

        interaction_feature_ids = {
            feature_id
            for interaction in gradebook["featureInteractionMatrix"].values()
            for feature_id in interaction.get("features", [])
        }
        self.assertEqual(
            feature_ids,
            interaction_feature_ids,
            "Every feature must appear in at least one cross-feature interaction group.",
        )

        for suite_id, suite in gradebook["releaseSuites"].items():
            with self.subTest(release_suite=suite_id):
                self.assertTrue(suite.get("featureIds"))
                self.assertTrue(set(suite["featureIds"]).issubset(feature_ids))
                self.assertTrue(suite.get("requiredEventClasses"))
                self.assertTrue(suite.get("requiredCombinationPlaybooks"))
                self.assertTrue(suite.get("requiredStatePermutations"))
                self.assertTrue(suite.get("requiredNegativeControls"))
                self.assertEqual(
                    SEND_RISK_BASE_FIXTURE_CATEGORIES,
                    set(suite.get("requiredFixtureClasses", [])),
                    f"{suite_id} must require the full adversarial fixture class set before it can pass.",
                )
                self.assertTrue(suite.get("proofArtifacts"))

        for feature in registry.get("features", []):
            if feature.get("sendRisk") in SEND_RISKS_REQUIRING_FIXTURE_COVERAGE:
                scenario = feature_scenarios[feature["id"]]
                with self.subTest(send_risk_feature=feature["id"]):
                    self.assertGreaterEqual(
                        len(scenario["eventClasses"]),
                        3,
                        "Send-risk features need multiple event classes, not a single happy path.",
                    )
                    self.assertGreaterEqual(
                        len(scenario["variationAxes"]),
                        5,
                        "Send-risk features need broad variation axes to catch fresh real-world phrasings.",
                    )
                    self.assertGreaterEqual(
                        len(scenario["combinationPlaybooks"]),
                        2,
                        "Send-risk features need combination/collision playbooks.",
                    )
                    self.assertIn(
                        "manual_reply_before_retry",
                        scenario["combinationPlaybooks"],
                        "Every send-risk feature must consider manual user continuation before retry/autonomous send.",
                    )
                    self.assertIn(
                        "wrong_recipient",
                        " ".join(scenario["negativeControls"]).lower(),
                        "Every send-risk feature must include a wrong-recipient negative control.",
                    )


if __name__ == "__main__":
    unittest.main()
