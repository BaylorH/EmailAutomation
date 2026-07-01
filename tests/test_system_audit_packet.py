import json
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-registry.json"
GRADEBOOK_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-gradebook.json"
MATRIX_PATH = REPO_ROOT / "docs" / "release-safety" / "system-audit-matrix.json"
PACKET_PATH = REPO_ROOT / "docs" / "release-safety" / "system-audit-packet.md"
CODERABBIT_CONTRACT_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "coderabbit-review-contract.md"
)

SEND_RISKS_REQUIRING_SYSTEM_AUDIT = {
    "queues_email",
    "sends_email_user_click",
    "sends_email_autonomous",
    "recovery_send",
}

REQUIRED_ENTRY_FIELDS = {
    "frontendSurfaces",
    "backendSurfaces",
    "firestoreWrites",
    "emailBehavior",
    "userVisibleEvidence",
    "sourceOfTruthReadbacks",
    "codeRabbitQuestions",
    "gradebookScenario",
    "adversarialFixtureClasses",
}

REQUIRED_FIXTURE_CLASSES = {
    "happy_path",
    "terminal_state",
    "bad_placeholder",
    "wrong_recipient",
    "manual_continuation",
    "duplicate_retry",
    "operator_visible_failure",
}

REQUIRED_REVIEW_ORDER = {
    "frontend_surface",
    "backend_surface",
    "firestore_state",
    "email_graph_state",
    "sheet_results_state",
    "coderabbit_review",
}

REQUIRED_INVARIANT_IDS = {
    "normal_users_cannot_trigger_tour",
    "raw_placeholders_never_reach_outbox",
    "every_send_has_visible_audit",
    "retry_checks_sent_items_and_manual_continuation",
    "frontend_gates_match_backend_entitlements",
    "reply_all_cc_context_is_preserved",
}

QUESTION_KEYWORDS = {
    "send",
    "recipient",
    "placeholder",
    "failure",
    "entitlement",
    "normal user",
    "tour",
    "duplicate",
    "audit",
    "cc",
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open() as handle:
        return json.load(handle)


class SystemAuditPacketTests(unittest.TestCase):
    def test_system_audit_packet_files_exist(self):
        self.assertTrue(
            MATRIX_PATH.exists(),
            "docs/release-safety/system-audit-matrix.json must tie frontend, backend, Firestore, email, and evidence together.",
        )
        self.assertTrue(
            GRADEBOOK_PATH.exists(),
            "docs/release-safety/feature-gradebook.json must force broad event, variation, combination, and role coverage.",
        )
        self.assertTrue(
            PACKET_PATH.exists(),
            "docs/release-safety/system-audit-packet.md must explain the cross-repo release review protocol.",
        )
        self.assertTrue(
            CODERABBIT_CONTRACT_PATH.exists(),
            "docs/release-safety/coderabbit-review-contract.md must keep CodeRabbit aligned with the system audit packet.",
        )

    def test_matrix_covers_every_send_risk_feature(self):
        registry = _read_json(REGISTRY_PATH)
        matrix = _read_json(MATRIX_PATH)
        registry_features = {feature["id"]: feature for feature in registry["features"]}
        matrix_features = matrix.get("features", {})

        self.assertEqual(1, matrix.get("schemaVersion"))
        self.assertEqual(
            set(matrix_features) - set(registry_features),
            set(),
            "System audit matrix cannot reference unknown feature ids.",
        )

        required_feature_ids = {
            feature["id"]
            for feature in registry["features"]
            if feature.get("sendRisk") in SEND_RISKS_REQUIRING_SYSTEM_AUDIT
        }
        self.assertTrue(
            required_feature_ids.issubset(set(matrix_features)),
            "Every send-risk feature must have a cross-repo audit entry.",
        )

    def test_send_risk_entries_name_full_product_evidence_contract(self):
        registry = _read_json(REGISTRY_PATH)
        matrix = _read_json(MATRIX_PATH)
        send_risk_ids = {
            feature["id"]
            for feature in registry["features"]
            if feature.get("sendRisk") in SEND_RISKS_REQUIRING_SYSTEM_AUDIT
        }

        for feature_id in send_risk_ids:
            with self.subTest(feature=feature_id):
                entry = matrix["features"][feature_id]
                self.assertTrue(
                    REQUIRED_ENTRY_FIELDS.issubset(entry),
                    "Send-risk entries must name frontend/backend/state/email/evidence review surfaces.",
                )
                for field in REQUIRED_ENTRY_FIELDS:
                    self.assertTrue(entry[field], f"{field} cannot be empty for {feature_id}.")
                self.assertEqual(
                    REQUIRED_FIXTURE_CLASSES,
                    set(entry["adversarialFixtureClasses"]),
                    "Every send-risk system audit entry needs the full fixture class set.",
                )
                question_text = " ".join(entry["codeRabbitQuestions"]).lower()
                self.assertTrue(
                    any(keyword in question_text for keyword in QUESTION_KEYWORDS),
                    "CodeRabbit questions must be specific to email/send/operator safety.",
                )

    def test_review_protocol_and_invariants_cover_known_incident_classes(self):
        matrix = _read_json(MATRIX_PATH)
        review_order = set(matrix.get("reviewProtocol", {}).get("reviewOrder", []))
        invariant_ids = {
            invariant.get("id") for invariant in matrix.get("crossRepoInvariants", [])
        }

        self.assertTrue(
            REQUIRED_REVIEW_ORDER.issubset(review_order),
            "Review protocol must force frontend, backend, Firestore, email, Sheet/results, and CodeRabbit review.",
        )
        self.assertTrue(
            REQUIRED_INVARIANT_IDS.issubset(invariant_ids),
            "Cross-repo invariants must cover the Karsen/Tyneesia/Jill/Baylor regression classes.",
        )

        packet = PACKET_PATH.read_text()
        for phrase in (
            "one SiteSift product",
            "Normal users stay on Production V1",
            "CodeRabbit",
            "No live-user email or data mutation",
        ):
            self.assertIn(phrase, packet)

    def test_coderabbit_contract_names_cross_repo_source_of_truth(self):
        contract = CODERABBIT_CONTRACT_PATH.read_text()
        section_start = contract.index("## Feature Registry Contract")
        section_end = contract.index("\n## ", section_start + 1)
        feature_registry_section = contract[section_start:section_end]
        for phrase in (
            "AGENTS.md",
            "feature-registry.json",
            "feature-gradebook.json",
            "adversarial-rubrics.json",
            "outbound-send-surface-inventory.json",
            "system-audit-matrix.json",
        ):
            self.assertIn(
                phrase,
                feature_registry_section,
                "CodeRabbit Feature Registry Contract must include every release-safety source-of-truth artifact.",
            )

        minimum_evidence_section = contract[contract.index("## Minimum Evidence Before Merge") :]
        for phrase in (
            "feature-gradebook",
            "fixture classes",
            "evidence",
            "human grading roles",
        ):
            self.assertIn(
                phrase,
                minimum_evidence_section,
                "CodeRabbit minimum merge evidence must repeat the gradebook evidence and grading-role requirements.",
            )

        packet_minimum_evidence = PACKET_PATH.read_text()[PACKET_PATH.read_text().index("The minimum evidence set is:") :]
        for phrase in ("fixture classes", "evidence", "human grading roles"):
            self.assertIn(
                phrase,
                packet_minimum_evidence,
                "System audit packet minimum evidence must stay aligned with CodeRabbit's gradebook contract.",
            )

    def test_matrix_send_risk_entries_have_gradebook_scenarios(self):
        registry = _read_json(REGISTRY_PATH)
        gradebook = _read_json(GRADEBOOK_PATH)
        matrix = _read_json(MATRIX_PATH)
        send_risk_ids = {
            feature["id"]
            for feature in registry["features"]
            if feature.get("sendRisk") in SEND_RISKS_REQUIRING_SYSTEM_AUDIT
        }

        for feature_id in send_risk_ids:
            with self.subTest(feature=feature_id):
                self.assertIn(feature_id, gradebook["featureScenarios"])
                scenario = gradebook["featureScenarios"][feature_id]
                entry = matrix["features"][feature_id]
                self.assertTrue(scenario.get("eventClasses"))
                self.assertTrue(scenario.get("variationAxes"))
                self.assertTrue(scenario.get("combinationPlaybooks"))
                self.assertTrue(scenario.get("statePermutations"))
                self.assertTrue(scenario.get("negativeControls"))
                self.assertEqual(
                    feature_id,
                    entry.get("gradebookScenario"),
                    "System audit matrix entries must link directly to their feature-gradebook scenario.",
                )
