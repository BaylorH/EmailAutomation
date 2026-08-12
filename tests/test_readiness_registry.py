import copy
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from datetime import timezone
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "generate_readiness_views.py"
MODULE_NAME = "generate_readiness_views_under_test"
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "readiness-registry.json"
EVIDENCE_NOTE_PATH = (
    REPO_ROOT
    / "docs"
    / "release-safety"
    / "evidence"
    / "2026-08-11-controlled-reopen.md"
)
CURRENT_VIEW_PATH = REPO_ROOT / "docs" / "release-safety" / "current-user-readiness.md"
FULL_VIEW_PATH = REPO_ROOT / "docs" / "release-safety" / "full-quality-coverage.md"
PACKET_PATH = REPO_ROOT / "docs" / "release-safety" / "system-audit-packet.md"


def _load_module():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load validator module at {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class ReadinessValidatorBootstrapTests(unittest.TestCase):
    def test_validator_module_exists(self):
        self.assertTrue(MODULE_PATH.is_file(), "Task 1 validator module is not implemented")


@unittest.skipUnless(MODULE_PATH.is_file(), "validator module not implemented yet")
class ReadinessRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        artifact = self.repo_root / "docs" / "release-safety" / "evidence.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("sanitized evidence\n")

        self.feature_registry = {
            "features": [
                {
                    "id": "feature.beta",
                    "name": "Beta",
                    "lane": "production_v1_core",
                },
                {
                    "id": "feature.alpha",
                    "name": "Alpha",
                    "lane": "production_v1_core",
                },
            ]
        }
        self.gradebook = {
            "eventTaxonomy": {"scenario.event": {}},
            "featureScenarios": {"scenario.feature": {}},
        }
        self.fixture_map = {
            "featureFixtureMatrix": {
                "feature.alpha": {
                    "happy_path": {"status": "covered"},
                    "terminal_state": {"status": "needs_live_proof"},
                },
                "feature.beta": {
                    "happy_path": {"status": "needs_fixture"},
                },
            }
        }
        self.registry = {
            "schemaVersion": 1,
            "updatedAt": "2026-08-11T00:00:00Z",
            "releaseIdentity": {
                "backendRevision": "revision-001",
                "productionRevision": "production-001",
            },
            "rolloutGates": [
                {
                    "id": "login_view",
                    "decision": "go",
                    "scope": "Returning users may sign in and inspect state.",
                    "allows": ["login", "view"],
                    "forbids": ["campaign_launch"],
                    "guardrails": ["Keep access read-only."],
                    "evidenceIds": ["evidence.login"],
                    "blockerIds": [],
                    "nextAction": None,
                    "rollback": "Disable returning-user access.",
                    "asOf": "2026-08-11T00:00:00Z",
                    "invalidatedBy": ["release_change"],
                },
                {
                    "id": "supervised_campaign_use",
                    "decision": "ready_for_canary",
                    "scope": "One monitored campaign.",
                    "allows": ["one_monitored_launch"],
                    "forbids": ["scope_expansion", "autonomous_followups"],
                    "guardrails": ["Keep follow-ups off."],
                    "evidenceIds": ["evidence.live"],
                    "blockerIds": ["quality.canary_unrun"],
                    "nextAction": "Run the monitored canary.",
                    "rollback": "Pause the campaign and preserve evidence.",
                    "asOf": "2026-08-11T00:00:00Z",
                    "invalidatedBy": ["release_change"],
                },
                {
                    "id": "autonomous_campaign_use",
                    "decision": "hold",
                    "scope": "Unattended campaigns.",
                    "allows": [],
                    "forbids": ["autonomous_send"],
                    "guardrails": ["Keep autonomous use disabled."],
                    "evidenceIds": ["evidence.live"],
                    "blockerIds": [],
                    "nextAction": "Define separate autonomous proof.",
                    "rollback": "Keep autonomous use disabled.",
                    "asOf": "2026-08-11T00:00:00Z",
                    "invalidatedBy": ["release_change"],
                },
            ],
            "evidence": [
                {
                    "id": "evidence.login",
                    "proofLevel": "production_readback",
                    "result": "pass",
                    "claim": "Returning-user login and read-only inspection succeeded.",
                    "featureIds": ["feature.alpha"],
                    "scenarioIds": ["scenario.event"],
                    "releaseRefs": {
                        "backendRevision": "revision-001",
                        "productionRevision": "production-001",
                    },
                    "artifact": "docs/release-safety/evidence.md",
                    "observedAt": "2026-08-10T23:00:00Z",
                    "expiresAt": "2026-08-11T01:00:00Z",
                    "readbacks": ["Authenticated session state was read back."],
                    "limitations": ["No campaign mutation was exercised."],
                    "retestOn": ["runtime_change"],
                },
                {
                    "id": "evidence.live",
                    "proofLevel": "live_production",
                    "result": "pass",
                    "claim": "The monitored behavioral proof passed.",
                    "featureIds": ["feature.beta"],
                    "scenarioIds": ["scenario.feature"],
                    "releaseRefs": {
                        "backendRevision": "revision-001",
                        "productionRevision": "production-001",
                    },
                    "artifact": "docs/release-safety/evidence.md",
                    "observedAt": "2026-08-10T22:00:00Z",
                    "expiresAt": None,
                    "readbacks": ["Terminal state was read back."],
                    "limitations": ["The proof covered one monitored run."],
                    "retestOn": ["release_change"],
                },
            ],
            "qualityItems": [
                {
                    "id": "quality.canary_unrun",
                    "state": "ready_for_live",
                    "severity": "P0",
                    "featureIds": ["feature.beta"],
                    "scenarioIds": ["scenario.feature"],
                    "evidenceIds": ["evidence.live"],
                    "blocksGates": ["supervised_campaign_use"],
                    "guardrail": "One monitored campaign only.",
                    "nextProof": "Run and read back the canary.",
                    "owner": "release-operator",
                }
            ],
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def validate(self, registry=None, feature_registry=None, gradebook=None, fixture_map=None):
        return self.module.validate_registry(
            self.registry if registry is None else registry,
            self.feature_registry if feature_registry is None else feature_registry,
            self.gradebook if gradebook is None else gradebook,
            self.fixture_map if fixture_map is None else fixture_map,
            repo_root=self.repo_root,
        )

    def assert_invalid(self, registry, stable_id):
        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(registry=registry)
        message = str(caught.exception)
        self.assertIn(stable_id, message)
        self.assertNotIn(repr(registry), message)
        return message

    def add_login_failure(
        self,
        registry,
        *,
        observed_at="2026-08-10T23:30:00Z",
        feature_ids=None,
        scenario_ids=None,
        release_revision="revision-001",
        supersedes=None,
    ):
        failure = copy.deepcopy(registry["evidence"][0])
        failure.update(
            {
                "id": "evidence.login.regression",
                "result": "fail",
                "claim": "A later current-release check failed.",
                "observedAt": observed_at,
                "expiresAt": None,
                "featureIds": ["feature.alpha"] if feature_ids is None else feature_ids,
                "scenarioIds": ["scenario.event"] if scenario_ids is None else scenario_ids,
                "releaseRefs": {
                    "backendRevision": release_revision,
                    "productionRevision": "production-001",
                },
            }
        )
        if supersedes is not None:
            failure["supersedes"] = supersedes
        registry["evidence"].append(failure)

    def write_repository_inputs(self, registry=None):
        release_safety = self.repo_root / "docs" / "release-safety"
        release_safety.mkdir(parents=True, exist_ok=True)
        documents = {
            "readiness-registry.json": self.registry if registry is None else registry,
            "feature-registry.json": self.feature_registry,
            "feature-gradebook.json": self.gradebook,
            "production-v1-fixture-map.json": self.fixture_map,
        }
        for filename, document in documents.items():
            (release_safety / filename).write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n"
            )

    def install_cli_script(self):
        script = self.repo_root / "scripts" / "generate_readiness_views.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(MODULE_PATH.read_text())
        return script

    def run_cli(self, *arguments):
        script = self.repo_root / "scripts" / "generate_readiness_views.py"
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_valid_registry_builds_frozen_indexes(self):
        validated = self.validate()

        self.assertIsInstance(validated, self.module.ValidatedRegistry)
        self.assertEqual({"feature.alpha", "feature.beta"}, set(validated.feature_by_id))
        self.assertEqual(self.fixture_map["featureFixtureMatrix"], validated.fixture_matrix)
        self.assertEqual(
            {"login_view", "supervised_campaign_use", "autonomous_campaign_use"},
            set(validated.gate_ids),
        )
        self.assertEqual({"evidence.login", "evidence.live"}, set(validated.evidence_by_id))
        self.assertEqual({"quality.canary_unrun"}, set(validated.quality_by_id))
        with self.assertRaises(FrozenInstanceError):
            validated.registry = {}

    def test_top_level_schema_is_exact_and_version_is_integer_one(self):
        for label, mutation in (
            ("missing", lambda value: value.pop("releaseIdentity")),
            ("extra", lambda value: value.__setitem__("notes", [])),
            ("bool", lambda value: value.__setitem__("schemaVersion", True)),
            ("float", lambda value: value.__setitem__("schemaVersion", 1.0)),
            ("wrong", lambda value: value.__setitem__("schemaVersion", 2)),
        ):
            with self.subTest(label=label):
                candidate = copy.deepcopy(self.registry)
                mutation(candidate)
                self.assert_invalid(candidate, "registry")

    def test_fixed_enums_reject_unknown_values_and_generated_stale(self):
        cases = (
            ("rolloutGates", 0, "decision", "stale", "login_view"),
            ("rolloutGates", 0, "decision", "GO", "login_view"),
            ("evidence", 0, "proofLevel", "staging", "evidence.login"),
            ("evidence", 0, "result", "unknown", "evidence.login"),
            ("qualityItems", 0, "state", "closed", "quality.canary_unrun"),
        )
        for collection, index, field, value, stable_id in cases:
            with self.subTest(collection=collection, field=field, value=value):
                candidate = copy.deepcopy(self.registry)
                candidate[collection][index][field] = value
                self.assert_invalid(candidate, stable_id)

    def test_ids_are_unique_within_and_across_registry_namespaces(self):
        for collection in ("rolloutGates", "evidence", "qualityItems"):
            with self.subTest(collection=collection):
                candidate = copy.deepcopy(self.registry)
                candidate[collection].append(copy.deepcopy(candidate[collection][0]))
                self.assert_invalid(candidate, candidate[collection][0]["id"])

        candidate = copy.deepcopy(self.registry)
        candidate["qualityItems"][0]["id"] = "evidence.login"
        self.assert_invalid(candidate, "evidence.login")

    def test_feature_and_scenario_references_must_be_known(self):
        cases = (
            ("rolloutGates", "featureIds", "feature.unknown", "login_view"),
            ("evidence", "featureIds", "feature.unknown", "evidence.login"),
            ("qualityItems", "featureIds", "feature.unknown", "quality.canary_unrun"),
            ("rolloutGates", "scenarioIds", "scenario.unknown", "login_view"),
            ("evidence", "scenarioIds", "scenario.unknown", "evidence.login"),
            ("qualityItems", "scenarioIds", "scenario.unknown", "quality.canary_unrun"),
        )
        for collection, field, value, stable_id in cases:
            with self.subTest(collection=collection, field=field):
                candidate = copy.deepcopy(self.registry)
                candidate[collection][0][field] = [value]
                message = self.assert_invalid(candidate, stable_id)
                self.assertIn(value, message)

        bad_fixture_map = copy.deepcopy(self.fixture_map)
        bad_fixture_map["featureFixtureMatrix"]["feature.unknown"] = {}
        with self.assertRaisesRegex(self.module.RegistryError, "feature.unknown"):
            self.validate(fixture_map=bad_fixture_map)

    def test_fixture_matrix_rows_must_be_mappings(self):
        malformed = copy.deepcopy(self.fixture_map)
        malformed["featureFixtureMatrix"]["feature.alpha"] = []

        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(fixture_map=malformed)

        message = str(caught.exception)
        self.assertIn("feature.alpha", message)
        self.assertIn("fixture", message)
        self.assertNotIn(repr(malformed), message)

    def test_scenario_union_accepts_event_and_feature_scenario_keys(self):
        self.validate()

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["scenarioIds"] = ["scenario.feature"]
        candidate["qualityItems"][0]["scenarioIds"] = ["scenario.event"]
        self.validate(registry=candidate)

    def test_evidence_and_blocker_references_must_resolve(self):
        cases = (
            ("evidenceIds", "evidence.unknown", "login_view"),
            ("blockerIds", "quality.unknown", "supervised_campaign_use"),
        )
        for field, value, stable_id in cases:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                gate_index = 0 if field == "evidenceIds" else 1
                candidate["rolloutGates"][gate_index][field] = [value]
                message = self.assert_invalid(candidate, stable_id)
                self.assertIn(value, message)

        candidate = copy.deepcopy(self.registry)
        candidate["qualityItems"][0]["evidenceIds"] = ["evidence.unknown"]
        message = self.assert_invalid(candidate, "quality.canary_unrun")
        self.assertIn("evidence.unknown", message)

    def test_stable_id_syntax_is_enforced_before_values_can_be_echoed(self):
        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["id"] = "unsafe/id"
        candidate["rolloutGates"][0]["evidenceIds"] = ["unsafe/id"]
        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(registry=candidate)
        self.assertIn("stable ID syntax", str(caught.exception))
        self.assertNotIn("unsafe/id", str(caught.exception))

        candidate = copy.deepcopy(self.registry)
        candidate["rolloutGates"][0]["evidenceIds"] = ["person@example.com"]
        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(registry=candidate)
        self.assertIn("stable ID syntax", str(caught.exception))
        self.assertNotIn("person@example.com", str(caught.exception))

        for collection, field in (
            ("evidence", "featureIds"),
            ("qualityItems", "evidenceIds"),
        ):
            with self.subTest(collection=collection, field=field):
                candidate = copy.deepcopy(self.registry)
                candidate[collection][0][field] = ["person@example.com"]
                with self.assertRaises(self.module.RegistryError) as caught:
                    self.validate(registry=candidate)
                self.assertIn("stable ID syntax", str(caught.exception))
                self.assertNotIn("person@example.com", str(caught.exception))

    def test_quality_items_block_only_their_explicit_gates(self):
        nonblocking = copy.deepcopy(self.registry["qualityItems"][0])
        nonblocking.update({"id": "quality.p0_nonblocking", "blocksGates": []})
        candidate = copy.deepcopy(self.registry)
        candidate["qualityItems"].append(nonblocking)
        self.validate(registry=candidate)

        candidate = copy.deepcopy(self.registry)
        candidate["qualityItems"][0]["blocksGates"] = []
        self.assert_invalid(candidate, "supervised_campaign_use")

        candidate = copy.deepcopy(self.registry)
        candidate["qualityItems"][0]["blocksGates"] = ["login_view"]
        self.assert_invalid(candidate, "quality.canary_unrun")

    def test_evidence_artifacts_are_existing_repo_relative_files(self):
        missing = copy.deepcopy(self.registry)
        missing["evidence"][0]["artifact"] = "docs/release-safety/missing.md"
        self.assert_invalid(missing, "evidence.login")

        absolute = copy.deepcopy(self.registry)
        absolute["evidence"][0]["artifact"] = str(
            self.repo_root / "docs" / "release-safety" / "evidence.md"
        )
        self.assert_invalid(absolute, "evidence.login")

        with tempfile.NamedTemporaryFile() as outside:
            escaped = copy.deepcopy(self.registry)
            escaped["evidence"][0]["artifact"] = os.path.relpath(
                outside.name, self.repo_root
            )
            self.assert_invalid(escaped, "evidence.login")

    def test_privacy_and_secret_material_is_rejected_without_echoing_values(self):
        cases = (
            ("notes", "contact person@example.com", "person@example.com"),
            ("notes", "/Users/private/evidence.md", "/Users/private/evidence.md"),
            ("notes", "file:///tmp/evidence.md", "file:///tmp/evidence.md"),
            ("accessToken", "super-secret-token-value", "super-secret-token-value"),
            ("apiKeyValue", "super-secret-key-value", "super-secret-key-value"),
            ("retestToken", "secret-retest-value", "secret-retest-value"),
            ("rawMessage", "full broker message text", "full broker message text"),
            ("rawMessagePayload", "full broker payload", "full broker payload"),
            ("subject", "raw provider subject", "raw provider subject"),
        )
        for field, value, sensitive_value in cases:
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                candidate["evidence"][0][field] = value
                message = self.assert_invalid(candidate, "evidence.login")
                self.assertNotIn(sensitive_value, message)

    def test_common_credential_signatures_are_rejected_without_echo(self):
        credentials = (
            "sk-" + "a" * 40,
            "ghp_" + "A" * 36,
            "AIza" + "A" * 35,
            "AKIA" + "A" * 16,
            "xoxb-" + "1" * 12 + "-" + "A" * 24,
            "eyJhbGciOiJIUzI1NiJ9." + "e" * 20 + "." + "s" * 24,
            "Bearer " + "b" * 32,
            "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
        )
        for credential in credentials:
            with self.subTest(prefix=credential[:12]):
                candidate = copy.deepcopy(self.registry)
                candidate["evidence"][0]["limitations"] = [
                    f"Synthetic credential-shape probe: {credential}"
                ]
                message = self.assert_invalid(candidate, "evidence.login")
                self.assertNotIn(credential, message)

    def test_credential_shaped_unknown_reference_is_rejected_before_echo(self):
        credential = "sk-" + "z" * 40
        candidate = copy.deepcopy(self.registry)
        candidate["rolloutGates"][0]["evidenceIds"] = [credential]

        message = self.assert_invalid(candidate, "login_view")
        self.assertNotIn(credential, message)

        fixture_credential = "AKIA" + "Z" * 16
        fixture_map = copy.deepcopy(self.fixture_map)
        fixture_map["featureFixtureMatrix"][fixture_credential] = {}
        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(fixture_map=fixture_map)
        self.assertNotIn(fixture_credential, str(caught.exception))

    def test_ordinary_token_and_secret_words_remain_allowed(self):
        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["limitations"] = [
            "The token and secret words describe review categories, not credentials."
        ]
        ordinary_quality = copy.deepcopy(candidate["qualityItems"][0])
        ordinary_quality.update(
            {"id": "quality.token-secret-review", "blocksGates": []}
        )
        candidate["qualityItems"].append(ordinary_quality)

        self.validate(registry=candidate)

    def test_sensitive_stable_id_is_rejected_without_echoing_it(self):
        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["id"] = "person@example.com"

        with self.assertRaises(self.module.RegistryError) as caught:
            self.validate(registry=candidate)

        self.assertNotIn("person@example.com", str(caught.exception))

    def test_go_requires_passing_unexpired_evidence_and_zero_blockers(self):
        mutations = (
            lambda value: value["rolloutGates"][0].__setitem__("evidenceIds", []),
            lambda value: value["evidence"][0].__setitem__("result", "partial"),
            lambda value: value["evidence"][0].__setitem__(
                "expiresAt", "2026-08-10T23:59:59Z"
            ),
            lambda value: value["rolloutGates"][0].__setitem__(
                "blockerIds", ["quality.canary_unrun"]
            ),
        )
        for mutation in mutations:
            candidate = copy.deepcopy(self.registry)
            mutation(candidate)
            self.assert_invalid(candidate, "login_view")

    def test_every_evidence_item_requires_explicit_claim_scope_and_proof_details(self):
        for field in (
            "claim",
            "featureIds",
            "scenarioIds",
            "readbacks",
            "limitations",
            "retestOn",
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                candidate["evidence"][0].pop(field)
                self.assert_invalid(candidate, "evidence.login")

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["claim"] = ""
        self.assert_invalid(candidate, "evidence.login")

    def test_live_evidence_requires_current_release_refs_and_nonempty_proof_details(self):
        for evidence_index in (0, 1):
            stable_id = self.registry["evidence"][evidence_index]["id"]
            for field, empty_value in (
                ("releaseRefs", {}),
                ("readbacks", []),
                ("limitations", []),
                ("retestOn", []),
            ):
                with self.subTest(evidence=stable_id, field=field):
                    candidate = copy.deepcopy(self.registry)
                    candidate["evidence"][evidence_index][field] = empty_value
                    self.assert_invalid(candidate, stable_id)

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][1]["releaseRefs"] = {"unknownRelease": "revision-001"}
        self.assert_invalid(candidate, "evidence.live")

    def test_production_evidence_binds_to_every_release_identity_key(self):
        for evidence_index in (0, 1):
            with self.subTest(evidence=self.registry["evidence"][evidence_index]["id"]):
                candidate = copy.deepcopy(self.registry)
                candidate["evidence"][evidence_index]["releaseRefs"].pop(
                    "productionRevision"
                )
                self.assert_invalid(
                    candidate, self.registry["evidence"][evidence_index]["id"]
                )

        base = self.validate()
        partial_identity = copy.deepcopy(self.registry)
        partial_identity["evidence"][0]["releaseRefs"].pop("productionRevision")
        legacy_validated = self.module.ValidatedRegistry(
            registry=partial_identity,
            feature_by_id=base.feature_by_id,
            fixture_matrix=base.fixture_matrix,
            gate_ids=base.gate_ids,
            evidence_by_id={
                item["id"]: item for item in partial_identity["evidence"]
            },
            quality_by_id=base.quality_by_id,
        )

        decisions = self.module.effective_gate_decisions(
            legacy_validated,
            at=self.module.parse_utc("2026-08-11T00:00:00Z"),
        )
        self.assertEqual("stale", decisions["login_view"])

    def test_control_plane_readback_may_use_explicit_empty_scope_but_live_evidence_may_not(self):
        control_plane = copy.deepcopy(self.registry)
        control_plane["evidence"][0]["featureIds"] = []
        control_plane["evidence"][0]["scenarioIds"] = []
        self.validate(registry=control_plane)

        for field in ("featureIds", "scenarioIds"):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                candidate["evidence"][1][field] = []
                self.assert_invalid(candidate, "evidence.live")

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["featureIds"] = []
        self.assert_invalid(candidate, "evidence.login")

    def test_nonproduction_evidence_may_have_empty_proof_detail_lists(self):
        candidate = copy.deepcopy(self.registry)
        evidence = candidate["evidence"][1]
        evidence["proofLevel"] = "deterministic_test"
        evidence.pop("releaseRefs")
        evidence["readbacks"] = []
        evidence["limitations"] = []
        evidence["retestOn"] = []

        self.validate(registry=candidate)

    def test_historical_or_old_release_evidence_cannot_support_go(self):
        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["proofLevel"] = "historical"
        self.assert_invalid(candidate, "login_view")

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["releaseRefs"] = {
            "backendRevision": "revision-000",
            "productionRevision": "production-001",
        }
        self.assert_invalid(candidate, "login_view")

    def test_same_feature_same_scenario_newer_current_failure_invalidates_go(self):
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate)

        self.assert_invalid(candidate, "login_view")

    def test_equal_time_pass_and_fail_invalidates_go(self):
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate, observed_at="2026-08-10T23:00:00Z")

        self.assert_invalid(candidate, "login_view")

    def test_effective_decision_stales_on_equal_time_pass_and_fail(self):
        base = self.validate()
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate, observed_at="2026-08-10T23:00:00Z")
        authored = copy.deepcopy(candidate)
        validated = self.module.ValidatedRegistry(
            registry=candidate,
            feature_by_id=base.feature_by_id,
            fixture_matrix=base.fixture_matrix,
            gate_ids=base.gate_ids,
            evidence_by_id={item["id"]: item for item in candidate["evidence"]},
            quality_by_id=base.quality_by_id,
        )

        decisions = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )

        self.assertEqual("stale", decisions["login_view"])
        self.assertEqual(authored, candidate)
        self.assertEqual(authored, validated.registry)

    def test_same_feature_different_scenario_failure_does_not_regress_go(self):
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(
            candidate,
            feature_ids=["feature.alpha"],
            scenario_ids=["scenario.feature"],
        )

        try:
            validated = self.validate(registry=candidate)
        except self.module.RegistryError as exc:
            self.fail(f"different scenarios must stay independent: {exc}")
        decisions = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )
        self.assertEqual("go", decisions["login_view"])

    def test_empty_scope_readbacks_regress_only_with_explicit_supersedes(self):
        unrelated = copy.deepcopy(self.registry)
        unrelated["evidence"][0]["featureIds"] = []
        unrelated["evidence"][0]["scenarioIds"] = []
        self.add_login_failure(unrelated, feature_ids=[], scenario_ids=[])
        try:
            self.validate(registry=unrelated)
        except self.module.RegistryError as exc:
            self.fail(f"unrelated control-plane readbacks must stay independent: {exc}")

        superseding = copy.deepcopy(self.registry)
        superseding["evidence"][0]["featureIds"] = []
        superseding["evidence"][0]["scenarioIds"] = []
        self.add_login_failure(
            superseding,
            feature_ids=[],
            scenario_ids=[],
            supersedes=["evidence.login"],
        )
        self.assert_invalid(superseding, "login_view")

    def test_supersedes_references_must_resolve(self):
        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][1]["supersedes"] = ["evidence.unknown"]

        message = self.assert_invalid(candidate, "evidence.live")
        self.assertIn("evidence.unknown", message)

    def test_disjoint_or_old_release_failure_does_not_regress_go(self):
        disjoint = copy.deepcopy(self.registry)
        self.add_login_failure(
            disjoint,
            feature_ids=["feature.beta"],
            scenario_ids=["scenario.feature"],
        )
        self.validate(registry=disjoint)

        old_release = copy.deepcopy(self.registry)
        self.add_login_failure(old_release, release_revision="revision-000")
        self.validate(registry=old_release)

    def test_effective_decision_stales_when_a_new_current_failure_becomes_observed(self):
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate, observed_at="2026-08-11T00:30:00Z")
        authored = copy.deepcopy(candidate)
        validated = self.validate(registry=candidate)

        before_failure = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T00:29:59Z")
        )
        after_failure = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T00:30:00Z")
        )

        self.assertEqual("go", before_failure["login_view"])
        self.assertEqual("stale", after_failure["login_view"])
        self.assertEqual(authored, candidate)
        self.assertEqual(authored, validated.registry)

    def test_ready_for_canary_requires_explicit_operating_contract(self):
        for field, empty_value in (
            ("scope", ""),
            ("forbids", []),
            ("nextAction", ""),
            ("blockerIds", []),
            ("rollback", ""),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.registry)
                candidate["rolloutGates"][1][field] = empty_value
                self.assert_invalid(candidate, "supervised_campaign_use")

    def test_every_gate_requires_complete_authoritative_contract(self):
        required_fields = (
            "scope",
            "allows",
            "forbids",
            "guardrails",
            "rollback",
            "asOf",
            "nextAction",
            "invalidatedBy",
            "evidenceIds",
            "blockerIds",
        )
        for gate_index, gate in enumerate(self.registry["rolloutGates"]):
            gate_id = gate["id"]
            for field in required_fields:
                with self.subTest(gate=gate_id, missing=field):
                    candidate = copy.deepcopy(self.registry)
                    candidate["rolloutGates"][gate_index].pop(field)
                    self.assert_invalid(candidate, gate_id)

            for field, empty_value in (
                ("scope", ""),
                ("allows", None),
                ("forbids", []),
                ("guardrails", []),
                ("rollback", ""),
                ("asOf", ""),
                ("invalidatedBy", []),
                ("evidenceIds", None),
                ("blockerIds", None),
            ):
                with self.subTest(gate=gate_id, empty=field):
                    candidate = copy.deepcopy(self.registry)
                    candidate["rolloutGates"][gate_index][field] = empty_value
                    self.assert_invalid(candidate, gate_id)

            if gate["decision"] == "go":
                candidate = copy.deepcopy(self.registry)
                candidate["rolloutGates"][gate_index]["nextAction"] = ""
                self.assert_invalid(candidate, gate_id)
            else:
                for empty_value in (None, ""):
                    with self.subTest(gate=gate_id, empty="nextAction"):
                        candidate = copy.deepcopy(self.registry)
                        candidate["rolloutGates"][gate_index][
                            "nextAction"
                        ] = empty_value
                        self.assert_invalid(candidate, gate_id)

    def test_every_gate_requires_timestamped_invalidation_provenance(self):
        for field, mutation in (
            ("asOf", lambda gate: gate.pop("asOf")),
            ("asOf", lambda gate: gate.__setitem__("asOf", "2026-08-11T00:00:00+00:00")),
            ("invalidatedBy", lambda gate: gate.pop("invalidatedBy")),
            ("invalidatedBy", lambda gate: gate.__setitem__("invalidatedBy", [])),
            (
                "invalidatedBy",
                lambda gate: gate.__setitem__("invalidatedBy", ["unsafe/reference"]),
            ),
        ):
            with self.subTest(field=field, mutation=mutation.__code__.co_firstlineno):
                candidate = copy.deepcopy(self.registry)
                mutation(candidate["rolloutGates"][0])
                self.assert_invalid(candidate, "login_view")

    def test_quality_items_require_owner_and_traceable_provenance(self):
        for value in (None, ""):
            with self.subTest(owner=value):
                candidate = copy.deepcopy(self.registry)
                if value is None:
                    candidate["qualityItems"][0].pop("owner")
                else:
                    candidate["qualityItems"][0]["owner"] = value
                self.assert_invalid(candidate, "quality.canary_unrun")

        no_provenance = copy.deepcopy(self.registry)
        no_provenance["qualityItems"][0]["evidenceIds"] = []
        self.assert_invalid(no_provenance, "quality.canary_unrun")

        legacy_provenance = copy.deepcopy(no_provenance)
        legacy_provenance["qualityItems"][0]["legacyRefs"] = ["FDR-010"]
        self.validate(registry=legacy_provenance)

        unsafe_legacy_ref = copy.deepcopy(no_provenance)
        unsafe_legacy_ref["qualityItems"][0]["legacyRefs"] = ["unsafe/reference"]
        self.assert_invalid(unsafe_legacy_ref, "quality.canary_unrun")

    def test_every_quality_item_requires_complete_authoritative_contract(self):
        required_fields = (
            "featureIds",
            "scenarioIds",
            "evidenceIds",
            "blocksGates",
            "severity",
            "guardrail",
            "nextProof",
            "owner",
        )
        for field in required_fields:
            with self.subTest(missing=field):
                candidate = copy.deepcopy(self.registry)
                candidate["qualityItems"][0].pop(field)
                self.assert_invalid(candidate, "quality.canary_unrun")

        for field, invalid_value in (
            ("featureIds", None),
            ("scenarioIds", None),
            ("evidenceIds", None),
            ("blocksGates", None),
            ("severity", "P3"),
            ("guardrail", ""),
            ("nextProof", ""),
            ("owner", ""),
        ):
            with self.subTest(invalid=field):
                candidate = copy.deepcopy(self.registry)
                candidate["qualityItems"][0][field] = invalid_value
                self.assert_invalid(candidate, "quality.canary_unrun")

    def test_quality_items_use_only_the_approved_severity_enum(self):
        try:
            self.validate()
        except self.module.RegistryError as exc:
            self.fail(f"approved severity must validate: {exc}")

        legacy = copy.deepcopy(self.registry)
        quality = legacy["qualityItems"][0]
        quality["priority"] = quality.pop("severity")
        self.assert_invalid(legacy, "quality.canary_unrun")

    def test_quality_blockers_require_explicit_nonempty_scope(self):
        for field in ("featureIds", "scenarioIds"):
            for mutation in ("delete", "empty"):
                with self.subTest(field=field, mutation=mutation):
                    candidate = copy.deepcopy(self.registry)
                    quality = candidate["qualityItems"][0]
                    if mutation == "delete":
                        quality.pop(field)
                    else:
                        quality[field] = []
                    self.assert_invalid(candidate, "quality.canary_unrun")

    def test_parse_utc_is_strict_and_evidence_intervals_are_ordered(self):
        parsed = self.module.parse_utc("2026-08-11T00:00:00Z")
        self.assertEqual(timezone.utc, parsed.tzinfo)
        self.assertEqual("2026-08-11T00:00:00+00:00", parsed.isoformat())

        for value in (
            "2026-08-11T00:00:00+00:00",
            "2026-08-11 00:00:00Z",
            "2026-08-11T00:00Z",
            "2026-08-11T00:00:00z",
            "not-a-date",
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(self.module.RegistryError):
                    self.module.parse_utc(value)

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["observedAt"] = "2026-08-10T23:00:00+00:00"
        self.assert_invalid(candidate, "evidence.login")

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["expiresAt"] = "2026-08-10T22:59:59Z"
        self.assert_invalid(candidate, "evidence.login")

    def test_effective_decisions_only_stale_expired_go_without_mutating_authored_json(self):
        authored = copy.deepcopy(self.registry)
        validated = self.validate()

        before_expiry = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T00:59:59Z")
        )
        after_expiry = self.module.effective_gate_decisions(
            validated, at=self.module.parse_utc("2026-08-11T01:00:01Z")
        )

        self.assertEqual("go", before_expiry["login_view"])
        self.assertEqual("stale", after_expiry["login_view"])
        self.assertEqual(
            "ready_for_canary", after_expiry["supervised_campaign_use"]
        )
        self.assertEqual("hold", after_expiry["autonomous_campaign_use"])
        self.assertEqual(authored, self.registry)
        self.assertEqual(authored, validated.registry)

    def test_current_view_states_exact_capability_boundary(self):
        self.assertTrue(
            hasattr(self.module, "render_current_readiness"),
            "Task 2 current-readiness renderer is not implemented",
        )
        rendered = self.module.render_current_readiness(
            self.validate(), at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )

        self.assertIn("Login / view | GO", rendered)
        self.assertIn("Supervised campaign use | READY FOR CANARY", rendered)
        self.assertIn("Autonomous campaign use | HOLD", rendered)
        self.assertIn("follow-ups off", rendered)
        self.assertIn("quality.canary_unrun", rendered)
        self.assertIn("Run the monitored canary.", rendered)
        self.assertIn("Pause the campaign and preserve evidence.", rendered)
        self.assertIn("2026-08-10T23:00:00Z", rendered)
        self.assertIn("expires 2026-08-11T01:00:00Z", rendered)

    def test_current_view_shows_gate_decision_provenance(self):
        rendered = self.module.render_current_readiness(
            self.validate(), at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )

        self.assertEqual(3, rendered.count("- Decision as of: `2026-08-11T00:00:00Z`"))
        self.assertEqual(3, rendered.count("- Invalidated by: `release_change`"))

    def test_current_view_marks_expired_go_evidence_stale(self):
        rendered = self.module.render_current_readiness(
            self.validate(), at=self.module.parse_utc("2026-08-11T01:00:01Z")
        )

        self.assertIn("Login / view | STALE", rendered)
        self.assertNotIn("Login / view | GO", rendered)

    def test_current_view_names_failure_that_regresses_supporting_pass(self):
        base = self.validate()
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate, observed_at="2026-08-10T23:30:00Z")
        validated = self.module.ValidatedRegistry(
            registry=candidate,
            feature_by_id=base.feature_by_id,
            fixture_matrix=base.fixture_matrix,
            gate_ids=base.gate_ids,
            evidence_by_id={item["id"]: item for item in candidate["evidence"]},
            quality_by_id=base.quality_by_id,
        )

        rendered = self.module.render_current_readiness(
            validated, at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )
        evidence_line = next(
            line for line in rendered.splitlines() if "`evidence.login` —" in line
        )

        self.assertIn("Login / view | STALE", rendered)
        self.assertIn("pass / regressed;", evidence_line)
        self.assertIn("invalidated by `evidence.login.regression`", evidence_line)
        self.assertIn("same-release overlapping failure", evidence_line)
        self.assertNotIn("pass / current;", evidence_line)

    def test_full_view_separates_mapped_fixtures_from_live_proof(self):
        self.assertTrue(
            hasattr(self.module, "render_full_quality_coverage"),
            "Task 2 full-coverage renderer is not implemented",
        )
        rendered = self.module.render_full_quality_coverage(
            self.validate(), at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )

        self.assertIn("Mapped fixtures", rendered)
        self.assertIn("Live/source evidence", rendered)
        self.assertIn(
            "Mapped fixtures are deterministic coverage, not proof of live production behavior.",
            rendered,
        )
        self.assertNotIn("Mapped fixtures = proven live", rendered)
        alpha_row = next(
            line for line in rendered.splitlines() if line.startswith("| feature.alpha |")
        )
        beta_row = next(
            line for line in rendered.splitlines() if line.startswith("| feature.beta |")
        )
        self.assertIn("| 1: happy_path |", alpha_row)
        self.assertIn("| 0 |", beta_row)

    def test_full_view_is_deterministic_core_only_and_sorted(self):
        feature_registry = copy.deepcopy(self.feature_registry)
        feature_registry["features"].append(
            {"id": "feature.support", "name": "Support", "lane": "recovery_support"}
        )
        validated = self.validate(feature_registry=feature_registry)
        authored = copy.deepcopy(validated.registry)
        at = self.module.parse_utc("2026-08-11T00:00:00Z")

        first = self.module.render_full_quality_coverage(validated, at=at)
        second = self.module.render_full_quality_coverage(validated, at=at)

        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertLess(first.index("| feature.alpha |"), first.index("| feature.beta |"))
        self.assertNotIn("feature.support", first)
        self.assertEqual(authored, validated.registry)

    def test_full_view_uses_scoped_evidence_precedence(self):
        feature_registry = copy.deepcopy(self.feature_registry)
        fixture_map = copy.deepcopy(self.fixture_map)
        for suffix in ("gamma", "delta", "epsilon"):
            feature_registry["features"].append(
                {
                    "id": f"feature.{suffix}",
                    "name": suffix.title(),
                    "lane": "production_v1_core",
                }
            )
            fixture_map["featureFixtureMatrix"][f"feature.{suffix}"] = {}

        registry = copy.deepcopy(self.registry)
        failed = copy.deepcopy(registry["evidence"][1])
        failed.update(
            {
                "id": "evidence.beta.fail",
                "result": "fail",
                "claim": "The scoped beta check failed.",
                "observedAt": "2026-08-10T22:30:00Z",
            }
        )
        partial = copy.deepcopy(registry["evidence"][1])
        partial.update(
            {
                "id": "evidence.gamma.partial",
                "result": "partial",
                "claim": "The scoped gamma check was partial.",
                "featureIds": ["feature.gamma"],
            }
        )
        source_only = copy.deepcopy(registry["evidence"][1])
        source_only.update(
            {
                "id": "evidence.delta.source",
                "proofLevel": "source_review",
                "result": "pass",
                "claim": "The scoped delta source review passed.",
                "featureIds": ["feature.delta"],
            }
        )
        source_only.pop("releaseRefs")
        registry["evidence"].extend([failed, partial, source_only])
        validated = self.validate(
            registry=registry,
            feature_registry=feature_registry,
            fixture_map=fixture_map,
        )

        rendered = self.module.render_full_quality_coverage(
            validated, at=self.module.parse_utc("2026-08-11T00:00:00Z")
        )
        rows = {
            feature_id: next(
                line
                for line in rendered.splitlines()
                if line.startswith(f"| {feature_id} |")
            )
            for feature_id in (
                "feature.alpha",
                "feature.beta",
                "feature.gamma",
                "feature.delta",
                "feature.epsilon",
            )
        }

        self.assertIn("| PASS —", rows["feature.alpha"])
        self.assertIn("| FAIL —", rows["feature.beta"])
        self.assertIn("evidence.beta.fail", rows["feature.beta"])
        self.assertIn("scenario.feature", rows["feature.beta"])
        self.assertNotIn("evidence.beta.fail", rows["feature.alpha"])
        self.assertIn("| PARTIAL —", rows["feature.gamma"])
        self.assertIn("| DETERMINISTIC / SOURCE ONLY —", rows["feature.delta"])
        self.assertIn("| UNPROVEN |", rows["feature.epsilon"])

    def test_full_view_sorts_multiple_quality_items_by_stable_id(self):
        registry = copy.deepcopy(self.registry)
        earlier = copy.deepcopy(registry["qualityItems"][0])
        earlier.update(
            {
                "id": "quality.alpha-review",
                "blocksGates": [],
            }
        )
        registry["qualityItems"].append(earlier)

        rendered = self.module.render_full_quality_coverage(
            self.validate(registry=registry),
            at=self.module.parse_utc("2026-08-11T00:00:00Z"),
        )
        beta_row = next(
            line for line in rendered.splitlines() if line.startswith("| feature.beta |")
        )

        self.assertLess(
            beta_row.index("quality.alpha-review"),
            beta_row.index("quality.canary_unrun"),
        )

    def test_render_outputs_loads_temp_repository_without_writing(self):
        self.write_repository_inputs()
        at = self.module.parse_utc("2026-08-11T00:00:00Z")

        outputs = self.module.render_outputs(self.repo_root, at=at)

        relative_paths = {path.relative_to(self.repo_root.resolve()) for path in outputs}
        self.assertEqual(
            {
                Path("docs/release-safety/current-user-readiness.md"),
                Path("docs/release-safety/full-quality-coverage.md"),
            },
            relative_paths,
        )
        self.assertFalse(
            (self.repo_root / "docs/release-safety/current-user-readiness.md").exists()
        )
        self.assertFalse(
            (self.repo_root / "docs/release-safety/full-quality-coverage.md").exists()
        )

    def test_cli_explicit_at_writes_and_check_reports_drift_without_writing(self):
        self.write_repository_inputs()
        self.install_cli_script()
        at_args = ("--at", "2026-08-11T00:00:00Z")

        written = self.run_cli(*at_args)
        self.assertEqual(0, written.returncode, written.stderr)
        current_path = self.repo_root / "docs/release-safety/current-user-readiness.md"
        full_path = self.repo_root / "docs/release-safety/full-quality-coverage.md"
        self.assertTrue(current_path.is_file())
        self.assertTrue(full_path.is_file())

        clean_snapshot = {current_path: current_path.read_bytes(), full_path: full_path.read_bytes()}
        clean_check = self.run_cli("--check", *at_args)
        self.assertEqual(0, clean_check.returncode, clean_check.stderr)
        self.assertEqual(clean_snapshot, {path: path.read_bytes() for path in clean_snapshot})

        current_path.write_text("drift\n")
        drift_snapshot = {current_path: current_path.read_bytes(), full_path: full_path.read_bytes()}
        drift_check = self.run_cli("--check", *at_args)

        self.assertEqual(2, drift_check.returncode)
        output = drift_check.stdout + drift_check.stderr
        self.assertIn("docs/release-safety/current-user-readiness.md", output)
        self.assertNotIn(str(self.repo_root), output)
        self.assertEqual(drift_snapshot, {path: path.read_bytes() for path in drift_snapshot})
        self.assertEqual([], list(current_path.parent.glob(".*readiness*.tmp")))

    def test_cli_default_uses_registry_updated_at(self):
        self.write_repository_inputs()
        self.install_cli_script()

        written = self.run_cli()

        self.assertEqual(0, written.returncode, written.stderr)
        for filename in (
            "current-user-readiness.md",
            "full-quality-coverage.md",
        ):
            rendered = (
                self.repo_root / "docs" / "release-safety" / filename
            ).read_text()
            self.assertIn("As of `2026-08-11T00:00:00Z`.", rendered)

    def test_cli_delayed_default_check_is_clean_and_writes_nothing(self):
        self.write_repository_inputs()
        self.install_cli_script()
        written = self.run_cli()
        self.assertEqual(0, written.returncode, written.stderr)
        paths = (
            self.repo_root / "docs/release-safety/current-user-readiness.md",
            self.repo_root / "docs/release-safety/full-quality-coverage.md",
        )
        snapshot = {path: path.read_bytes() for path in paths}
        time.sleep(1.1)

        checked = self.run_cli("--check")

        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertEqual(snapshot, {path: path.read_bytes() for path in paths})

    def test_cli_malformed_fixture_row_fails_safely_without_writing(self):
        self.fixture_map["featureFixtureMatrix"]["feature.alpha"] = []
        self.write_repository_inputs()
        self.install_cli_script()

        result = self.run_cli("--check")

        self.assertEqual(2, result.returncode)
        output = result.stdout + result.stderr
        self.assertIn("feature.alpha", output)
        self.assertIn("fixture", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn(str(self.repo_root), output)
        self.assertFalse(
            (self.repo_root / "docs/release-safety/current-user-readiness.md").exists()
        )
        self.assertFalse(
            (self.repo_root / "docs/release-safety/full-quality-coverage.md").exists()
        )

    def test_cli_rolls_back_both_outputs_when_second_replace_fails(self):
        release_safety = self.repo_root / "docs" / "release-safety"
        first = release_safety / "current-user-readiness.md"
        second = release_safety / "full-quality-coverage.md"
        first.write_bytes(b"original current\n")
        second.write_bytes(b"original full\n")
        outputs = {first: "new current\n", second: "new full\n"}
        real_replace = os.replace
        injected = {"failed": False}

        def fail_second_new_output(source, target):
            source_path = Path(source)
            target_path = Path(target)
            if source_path.name.endswith(".tmp") and target_path == second:
                injected["failed"] = True
                raise OSError("injected failure at /Users/private/second-target")
            return real_replace(source, target)

        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "render_outputs", return_value=outputs),
            mock.patch.object(
                self.module.os, "replace", side_effect=fail_second_new_output
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = self.module.main([])

        self.assertTrue(injected["failed"])
        self.assertEqual(2, exit_code)
        self.assertEqual(b"original current\n", first.read_bytes())
        self.assertEqual(b"original full\n", second.read_bytes())
        message = stderr.getvalue()
        self.assertIn("readiness_outputs", message)
        self.assertNotIn("/Users/private", message)
        self.assertNotIn("Traceback", message)
        residue = [
            path.name
            for path in release_safety.iterdir()
            if path.name.startswith(".current-user-readiness.md.")
            or path.name.startswith(".full-quality-coverage.md.")
        ]
        self.assertEqual([], residue)

    def test_cli_preserves_recovery_backups_when_replace_and_rollback_fail(self):
        release_safety = self.repo_root / "docs" / "release-safety"
        first = release_safety / "current-user-readiness.md"
        second = release_safety / "full-quality-coverage.md"
        originals = {b"original current\n", b"original full\n"}
        first.write_bytes(b"original current\n")
        second.write_bytes(b"original full\n")
        outputs = {first: "new current\n", second: "new full\n"}
        real_replace = os.replace
        injected = {"replace": False, "rollback": False}

        def fail_second_output_and_first_rollback(source, target):
            source_path = Path(source)
            target_path = Path(target)
            if source_path.name.endswith(".tmp") and target_path == second:
                injected["replace"] = True
                raise OSError("injected replace failure at /Users/private/second")
            if source_path.name.endswith(".bak") and target_path == first:
                injected["rollback"] = True
                raise OSError("injected rollback failure at /Users/private/first")
            return real_replace(source, target)

        stderr = io.StringIO()
        with (
            mock.patch.object(self.module, "render_outputs", return_value=outputs),
            mock.patch.object(
                self.module.os,
                "replace",
                side_effect=fail_second_output_and_first_rollback,
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = self.module.main([])

        self.assertEqual({"replace": True, "rollback": True}, injected)
        self.assertEqual(2, exit_code)
        backups = sorted(release_safety.glob(".*.bak"))
        self.assertTrue(backups, "rollback failure must preserve recovery backups")
        surviving_bytes = {first.read_bytes(), second.read_bytes()}
        surviving_bytes.update(path.read_bytes() for path in backups)
        self.assertTrue(originals.issubset(surviving_bytes))
        self.assertEqual([], sorted(release_safety.glob(".*.tmp")))
        message = stderr.getvalue()
        self.assertIn("readiness_outputs: rollback failed", message)
        self.assertNotIn("/Users/private", message)
        self.assertNotIn(str(self.repo_root), message)
        self.assertNotIn("Traceback", message)

    def test_cli_validation_error_is_stable_and_writes_nothing(self):
        registry = copy.deepcopy(self.registry)
        registry["evidence"][0]["claim"] = "contact person@example.com"
        self.write_repository_inputs(registry)
        self.install_cli_script()

        result = self.run_cli("--at", "2026-08-11T00:00:00Z")

        self.assertEqual(2, result.returncode)
        output = result.stdout + result.stderr
        self.assertIn("evidence.login", output)
        self.assertNotIn("person@example.com", output)
        self.assertFalse(
            (self.repo_root / "docs/release-safety/current-user-readiness.md").exists()
        )
        self.assertFalse(
            (self.repo_root / "docs/release-safety/full-quality-coverage.md").exists()
        )


class CommittedReadinessArtifactsTests(unittest.TestCase):
    maxDiff = None

    def load_registry(self):
        self.assertTrue(REGISTRY_PATH.is_file(), "committed readiness registry is missing")
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def read_artifact(self, path):
        self.assertTrue(path.is_file(), f"committed artifact is missing: {path.name}")
        return path.read_text(encoding="utf-8")

    def test_system_audit_packet_routes_current_clearance_to_readiness_views(self):
        packet = self.read_artifact(PACKET_PATH)
        clearance_heading = "## Current capability clearance"
        evidence_heading = "## Evidence Required Before Normal Users Return"

        self.assertIn(clearance_heading, packet)
        self.assertLess(packet.index(clearance_heading), packet.index(evidence_heading))
        clearance = packet[
            packet.index(clearance_heading) : packet.index(evidence_heading)
        ]
        for required_text in (
            "[readiness-registry.json](readiness-registry.json)",
            "[current-user-readiness.md](current-user-readiness.md)",
            "[full-quality-coverage.md](full-quality-coverage.md)",
            "authoritative for current capability clearance",
            "This packet remains the test-selection contract",
            "`login_view = go`",
            "`supervised_campaign_use = go`",
            "`autonomous_campaign_use = hold`",
            "Historical language in this packet is not a blanket hold",
            "Mapped fixtures and P0/P1 labels do not automatically equal live proof or a rollout block.",
            "Priority alone never blocks a rollout gate; only an explicit blocksGates link does.",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, clearance)

        normalized_clearance = " ".join(clearance.split())
        for required_text in (
            "one deliberately admitted, continuously monitored, one-row, "
            "one-property existing campaign for one user at a time",
            "controls remain Closed/Closed and the client remains paused until admission",
            "autonomous follow-ups remain the sole named readiness blocker",
        ):
            with self.subTest(boundary_text=required_text):
                self.assertIn(required_text, normalized_clearance)

    def test_evidence_note_records_the_current_control_and_finish_line_boundary(self):
        note = self.read_artifact(EVIDENCE_NOTE_PATH)
        for required_text in (
            "## Current control readback",
            "Login and view remained available",
            "Controls read back Closed/Closed",
            "client remained paused outside deliberate admission",
            "no send-capable residue remained",
            "## Finish-line certification",
            "### Copied-party reply-all",
            "### Ambiguous mixed-property PDF",
            "### Thirteen-message correction and ordering flow",
            "Autonomous follow-ups are the sole remaining named readiness blocker",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, note)

    def test_committed_gate_decisions_match_authoritative_boundary(self):
        registry = self.load_registry()
        decisions = {
            gate["id"]: gate["decision"] for gate in registry["rolloutGates"]
        }

        self.assertEqual(
            {
                "login_view": "go",
                "supervised_campaign_use": "go",
                "autonomous_campaign_use": "hold",
            },
            decisions,
        )

    def test_finish_line_certification_keeps_the_exact_return_boundary(self):
        registry = self.load_registry()
        gates = {gate["id"]: gate for gate in registry["rolloutGates"]}

        login = gates["login_view"]
        self.assertEqual("go", login["decision"])
        self.assertEqual(["login", "view_existing_state"], login["allows"])

        supervised = gates["supervised_campaign_use"]
        self.assertEqual("go", supervised["decision"])
        self.assertEqual(
            "One deliberately admitted, continuously monitored, one-row, "
            "one-property existing campaign for one user at a time, with "
            "follow-ups off.",
            supervised["scope"],
        )
        self.assertEqual(
            ["one_existing_row_monitored_campaign"], supervised["allows"]
        )
        for forbidden in (
            "autonomous_followups",
            "broad_campaign_creation",
            "cross_tenant_use",
            "multirow_campaign",
            "simultaneous_campaigns",
            "uncertain_send_recovery",
            "unattended_recovery",
            "unattended_use",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, supervised["forbids"])
        self.assertIn("new_campaign_launch", supervised["forbids"])
        self.assertIn("Controls stay Closed/Closed", " ".join(supervised["guardrails"]))
        self.assertIn("client stays paused until admission", " ".join(supervised["guardrails"]))
        self.assertNotIn("launch", " ".join(supervised["guardrails"]).lower())

        autonomous = gates["autonomous_campaign_use"]
        self.assertEqual("hold", autonomous["decision"])
        self.assertEqual(
            ["autonomous-followups-current-live-gap"], autonomous["blockerIds"]
        )
        self.assertIn("autonomous_followups", autonomous["forbids"])
        self.assertIn("unattended_campaigns", autonomous["forbids"])

    def test_committed_gates_have_exact_decision_provenance(self):
        registry = self.load_registry()
        provenance = {
            gate["id"]: (gate.get("asOf"), gate.get("invalidatedBy"))
            for gate in registry["rolloutGates"]
        }

        self.assertEqual(
            {
                "login_view": (
                    "2026-08-12T04:05:30Z",
                    [
                        "backend_release_change",
                        "production_revision_change",
                        "runtime_allowlist_change",
                        "campaign_control_change",
                        "new_operational_residue",
                        "evidence_expiry",
                    ],
                ),
                "supervised_campaign_use": (
                    "2026-08-12T04:05:30Z",
                    [
                        "backend_release_change",
                        "production_revision_change",
                        "runtime_allowlist_change",
                        "campaign_control_change",
                        "queue_or_send_cap_change",
                        "new_operational_residue",
                        "evidence_expiry",
                    ],
                ),
                "autonomous_campaign_use": (
                    "2026-08-12T04:05:30Z",
                    [
                        "backend_release_change",
                        "production_revision_change",
                        "runtime_allowlist_change",
                        "campaign_control_change",
                        "queue_or_send_cap_change",
                        "new_failure_or_regression",
                    ],
                ),
            },
            provenance,
        )

    def test_committed_evidence_stays_within_the_nine_bounded_claims(self):
        registry = self.load_registry()
        evidence_scope = {
            item["id"]: (set(item["featureIds"]), set(item["scenarioIds"]))
            for item in registry["evidence"]
        }

        self.assertEqual(
            {
                "returning-workspace-containment-readback": (set(), set()),
                "m27-ten-row-launch-integrity-live": (
                    {
                        "core.launch_draft",
                        "core.name_resolution",
                        "core.outbox_send",
                        "core.scheduler_scope",
                        "core.upload_mapping",
                    },
                    {"launch_with_variable_mapping"},
                ),
                "m27-simple-extraction-close-live": (
                    {
                        "core.event_classifier",
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.property_extraction",
                        "core.sheet_update",
                    },
                    {"broker_available_full_specs"},
                ),
                "m27-unavailable-terminalization-live": (
                    {
                        "core.event_classifier",
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.sheet_update",
                    },
                    {"broker_property_unavailable"},
                ),
                "m27-returning-user-canary-live": (
                    {
                        "core.event_classifier",
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.property_extraction",
                        "core.sheet_update",
                    },
                    {
                        "broker_available_full_specs",
                        "broker_available_partial_specs",
                    },
                ),
                "finish-line-reply-all-cc-live": (
                    {
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.outbox_send",
                        "core.property_extraction",
                        "core.reply_all_cc",
                        "core.sheet_update",
                    },
                    {"reply_all_cc_context"},
                ),
                "finish-line-reply-all-cc-carry-forward": (
                    {
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.outbox_send",
                        "core.property_extraction",
                        "core.reply_all_cc",
                        "core.sheet_update",
                    },
                    {"reply_all_cc_context"},
                ),
                "finish-line-ambiguous-pdf-live": (
                    {
                        "core.event_classifier",
                        "core.inbox_matching",
                        "core.property_extraction",
                        "core.sheet_update",
                    },
                    {
                        "broker_attachment_or_link_only",
                        "broker_available_partial_specs",
                    },
                ),
                "finish-line-long-multiturn-live": (
                    {
                        "core.event_classifier",
                        "core.inbox_auto_reply",
                        "core.inbox_matching",
                        "core.manual_reply",
                        "core.outbox_send",
                        "core.property_extraction",
                        "core.sheet_update",
                    },
                    {
                        "broker_available_partial_specs",
                        "dashboard_action_resolution",
                        "manual_user_continuation",
                    },
                ),
            },
            evidence_scope,
        )

    def test_finish_line_evidence_records_exact_sanitized_live_readbacks(self):
        registry = self.load_registry()
        evidence = {item["id"]: item for item in registry["evidence"]}
        current_release_refs = {
            "backendCommit": "62a7d59e434881e0a230395523b3e6df86dec1f6",
            "productionRevision": "process-user-00097-yus",
        }
        case_one_release_refs = {
            "backendCommit": "c6dbe4a27140268a0840476c9cf70ff1c72ed7bc",
            "productionRevision": "process-user-00094-lib",
        }

        copied_party = evidence["finish-line-reply-all-cc-live"]
        self.assertEqual("live_production", copied_party["proofLevel"])
        self.assertEqual("pass", copied_party["result"])
        self.assertEqual(case_one_release_refs, copied_party["releaseRefs"])
        self.assertNotEqual(
            "2026-08-12T04:05:30Z", copied_party["observedAt"]
        )
        copied_text = " ".join(
            [copied_party["claim"], *copied_party["readbacks"], *copied_party["limitations"]]
        )
        for required_text in (
            "canonical To",
            "one safe copied Cc",
            "Bcc was empty",
            "52,400",
            "14.80",
            "3.95",
            "81,875.00",
            "same-row formula",
            "terminal",
            "zero scoped residue",
        ):
            with self.subTest(proof="reply-all", required_text=required_text):
                self.assertIn(required_text, copied_text)

        ambiguous_pdf = evidence["finish-line-ambiguous-pdf-live"]
        self.assertEqual(current_release_refs, ambiguous_pdf["releaseRefs"])
        pdf_text = " ".join(
            [ambiguous_pdf["claim"], *ambiguous_pdf["readbacks"], *ambiguous_pdf["limitations"]]
        )
        for required_text in (
            "exactly one review action",
            "zero automatic sends",
            "zero counter delta",
            "zero fact",
            "zero asset",
            "zero Sheet change",
            "duplicate-action hard stop",
            "62a7d59",
            "clean retry",
            "deployed candidate",
        ):
            with self.subTest(proof="pdf", required_text=required_text):
                self.assertIn(required_text, pdf_text)

        long_turn = evidence["finish-line-long-multiturn-live"]
        self.assertEqual(current_release_refs, long_turn["releaseRefs"])
        long_text = " ".join(
            [long_turn["claim"], *long_turn["readbacks"], *long_turn["limitations"]]
        )
        for required_text in (
            "13-message",
            "correction precedence",
            "chronological ordering",
            "call-request pause",
            "Dashboard continuation",
            "only the exact missing fields",
            "terminal close",
            "idempotent replay",
            "40,800",
            "15.10",
            "3.75",
            "64,090.00",
        ):
            with self.subTest(proof="long-turn", required_text=required_text):
                self.assertIn(required_text, long_text)

        carry_forward = evidence["finish-line-reply-all-cc-carry-forward"]
        self.assertEqual("source_review", carry_forward["proofLevel"])
        self.assertEqual("pass", carry_forward["result"])
        self.assertEqual(current_release_refs, carry_forward["releaseRefs"])
        carry_text = " ".join(
            [carry_forward["claim"], *carry_forward["readbacks"], *carry_forward["limitations"]]
        )
        for required_text in (
            "attachment-only",
            "did not touch",
            "reply-all",
            "recipient",
            "send",
            "Sheet",
            "not a second live send",
        ):
            with self.subTest(proof="carry-forward", required_text=required_text):
                self.assertIn(required_text, carry_text)

    def test_current_release_control_readback_proves_serving_health_and_settlement(self):
        registry = self.load_registry()
        evidence = {item["id"]: item for item in registry["evidence"]}
        control = evidence["returning-workspace-containment-readback"]
        text = " ".join([control["claim"], *control["readbacks"], *control["limitations"]])
        for required_text in (
            "sole 100 percent serving traffic",
            "healthy",
            "7/7",
            "queue drained",
            "zero scoped residue",
            "zero application errors",
            "zero 5xx responses",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, text)

    def test_finish_line_proofs_close_only_the_live_quality_gaps_they_exercised(self):
        registry = self.load_registry()
        quality = {item["id"]: item for item in registry["qualityItems"]}
        expected_provenance = {
            "reply-all-cc-multiparty-live-gap": "finish-line-reply-all-cc-carry-forward",
            "pdf-multi-suite-ambiguity": "finish-line-ambiguous-pdf-live",
            "hard-repeat-ask-rejection-gap": "finish-line-long-multiturn-live",
            "long-multiturn-ordering-gap": "finish-line-long-multiturn-live",
        }
        for quality_id, evidence_id in expected_provenance.items():
            with self.subTest(quality_id=quality_id):
                item = quality[quality_id]
                self.assertEqual("proven_live", item["state"])
                self.assertEqual([], item["blocksGates"])
                self.assertIn(evidence_id, item["evidenceIds"])

        followups = quality["autonomous-followups-current-live-gap"]
        self.assertEqual("ready_for_live", followups["state"])
        self.assertEqual(["autonomous_campaign_use"], followups["blocksGates"])

        natural_voice = quality["natural-voice-variety"]
        self.assertEqual("open", natural_voice["state"])
        self.assertEqual([], natural_voice["blocksGates"])
        self.assertIn("finish-line-long-multiturn-live", natural_voice["evidenceIds"])

    def test_prior_returning_user_canary_is_retained_without_driving_current_gates(self):
        registry = self.load_registry()
        gates = {gate["id"]: gate for gate in registry["rolloutGates"]}
        quality = {item["id"]: item for item in registry["qualityItems"]}
        evidence = {item["id"]: item for item in registry["evidence"]}

        for gate in gates.values():
            with self.subTest(gate_id=gate["id"]):
                self.assertNotIn("m27-returning-user-canary-live", gate["evidenceIds"])

        canary_gap = quality["returning-user-canary-unrun"]
        self.assertEqual("proven_live", canary_gap["state"])
        self.assertEqual([], canary_gap["blocksGates"])
        self.assertEqual(
            [
                "m27-ten-row-launch-integrity-live",
                "m27-returning-user-canary-live",
            ],
            canary_gap["evidenceIds"],
        )

        natural_voice = quality["natural-voice-variety"]
        self.assertEqual("open", natural_voice["state"])
        self.assertEqual([], natural_voice["blocksGates"])
        self.assertIn("m27-returning-user-canary-live", natural_voice["evidenceIds"])

        repeat_ask = quality["hard-repeat-ask-rejection-gap"]
        self.assertEqual("proven_live", repeat_ask["state"])
        self.assertIn("m27-returning-user-canary-live", repeat_ask["evidenceIds"])
        self.assertIn("finish-line-long-multiturn-live", repeat_ask["evidenceIds"])

        canary = evidence["m27-returning-user-canary-live"]
        self.assertEqual("2026-08-11T17:28:58Z", canary["observedAt"])
        self.assertEqual(
            "process-user-00092-som",
            canary["releaseRefs"]["productionRevision"],
        )
        proof_text = " ".join(
            [canary["claim"], *canary["readbacks"], *canary["limitations"]]
        )
        for required_text in (
            "20/20",
            "21,600",
            "17.25",
            "4.10",
            "38,430.00",
            "G8*(H8+I8)/12",
            "47,900",
            "15.35",
            "3.85",
            "76,640.00",
            "G9*(H9+I9)/12",
            "correction",
            "operating expenses",
            "natural voice",
            "follow-ups",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, proof_text)

    def test_supervised_go_does_not_clear_followups_or_autonomous_use(self):
        registry = self.load_registry()
        gates = {gate["id"]: gate for gate in registry["rolloutGates"]}
        supervised = gates["supervised_campaign_use"]
        autonomous = gates["autonomous_campaign_use"]

        self.assertIn("autonomous_followups", supervised["forbids"])
        self.assertIn("autonomous_followups", autonomous["forbids"])
        self.assertIn(
            "autonomous-followups-current-live-gap", autonomous["blockerIds"]
        )
        self.assertNotIn(
            "autonomous-followups-current-live-gap", supervised["blockerIds"]
        )

    def test_quality_items_have_exact_gate_blocking_links(self):
        registry = self.load_registry()
        blocks = {
            item["id"]: set(item["blocksGates"])
            for item in registry["qualityItems"]
        }

        self.assertEqual(
            {
                "returning-user-canary-unrun": set(),
                "autonomous-followups-current-live-gap": {
                    "autonomous_campaign_use"
                },
                "reply-all-cc-multiparty-live-gap": set(),
                "pdf-multi-suite-ambiguity": set(),
                "hard-repeat-ask-rejection-gap": set(),
                "natural-voice-variety": set(),
                "long-multiturn-ordering-gap": set(),
                "account-b-historical-cleanup": set(),
            },
            blocks,
        )

    def test_committed_quality_items_have_owner_and_provenance(self):
        registry = self.load_registry()
        quality_by_id = {item["id"]: item for item in registry["qualityItems"]}
        self.assertEqual(
            {
                "returning-user-canary-unrun": "release-operator",
                "autonomous-followups-current-live-gap": "messaging-runtime",
                "reply-all-cc-multiparty-live-gap": "reply-routing",
                "pdf-multi-suite-ambiguity": "extraction",
                "hard-repeat-ask-rejection-gap": "response-generation",
                "natural-voice-variety": "quality-evaluation",
                "long-multiturn-ordering-gap": "event-ordering",
                "account-b-historical-cleanup": "operations",
            },
            {item_id: item.get("owner") for item_id, item in quality_by_id.items()},
        )
        legacy_backed = {
            "reply-all-cc-multiparty-live-gap",
            "pdf-multi-suite-ambiguity",
            "natural-voice-variety",
            "long-multiturn-ordering-gap",
        }
        for item_id, item in quality_by_id.items():
            with self.subTest(item_id=item_id):
                self.assertTrue(item.get("evidenceIds") or item.get("legacyRefs"))
                if item_id in legacy_backed:
                    self.assertEqual(["FDR-010", "FDR-011"], item.get("legacyRefs"))

    def test_full_view_contains_every_core_feature_without_claim_broadening(self):
        registry = self.load_registry()
        full_view = self.read_artifact(FULL_VIEW_PATH)
        feature_registry = json.loads(
            (REPO_ROOT / "docs" / "release-safety" / "feature-registry.json").read_text(
                encoding="utf-8"
            )
        )
        core_ids = {
            feature["id"]
            for feature in feature_registry["features"]
            if feature.get("lane") == "production_v1_core"
        }
        rows = {
            line.split("|", 2)[1].strip()
            for line in full_view.splitlines()
            if line.startswith("| core.")
        }

        self.assertEqual(16, len(core_ids))
        self.assertEqual(core_ids, rows)
        followups_row = next(
            line
            for line in full_view.splitlines()
            if line.startswith("| core.followups |")
        )
        self.assertIn("UNPROVEN", followups_row)
        self.assertNotIn("m27-ten-row-launch-integrity-live", followups_row)
        self.assertEqual(9, len(registry["evidence"]))

    def test_committed_artifacts_are_sanitized(self):
        payloads = {
            path.name: self.read_artifact(path)
            for path in (
                REGISTRY_PATH,
                EVIDENCE_NOTE_PATH,
                CURRENT_VIEW_PATH,
                FULL_VIEW_PATH,
            )
        }
        forbidden_patterns = {
            "email_address": re.compile(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE
            ),
            "local_user_path": re.compile(r"/Users/"),
            "file_uri": re.compile(r"file://", re.IGNORECASE),
            "message_id_header": re.compile(r"\bmessage-id\s*:", re.IGNORECASE),
            "uid": re.compile(r"\buid\b", re.IGNORECASE),
            "raw_message_material": re.compile(
                r"\braw\s+(?:body|message)\b", re.IGNORECASE
            ),
            "project_id": re.compile(r"\bproject[- ]id\b", re.IGNORECASE),
            "revision_url": re.compile(r"https?://", re.IGNORECASE),
            "image_digest": re.compile(r"\bsha256:[0-9a-f]+\b", re.IGNORECASE),
            "temporary_path": re.compile(
                r"/(?:tmp|private/var|var/folders)/", re.IGNORECASE
            ),
            "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            "github_token": re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b"),
            "aws_key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
            "bearer_secret": re.compile(
                r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE
            ),
        }

        for filename, payload in payloads.items():
            for label, pattern in forbidden_patterns.items():
                with self.subTest(filename=filename, label=label):
                    self.assertIsNone(pattern.search(payload))

    def test_committed_views_are_clean_at_default_and_snapshot_time(self):
        commands = (
            [sys.executable, str(MODULE_PATH), "--check"],
            [
                sys.executable,
                str(MODULE_PATH),
                "--check",
                "--at",
                "2026-08-12T04:05:30Z",
            ],
        )
        for command in commands:
            with self.subTest(command=command[2:]):
                result = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
