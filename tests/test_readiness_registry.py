import copy
import importlib.util
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "generate_readiness_views.py"
MODULE_NAME = "generate_readiness_views_under_test"


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
                {"id": "feature.alpha", "name": "Alpha"},
                {"id": "feature.beta", "name": "Beta"},
            ]
        }
        self.gradebook = {
            "eventTaxonomy": {"scenario.event": {}},
            "featureScenarios": {"scenario.feature": {}},
        }
        self.fixture_map = {
            "featureFixtureMatrix": {
                "feature.alpha": {"happy_path": {}},
                "feature.beta": {"happy_path": {}},
            }
        }
        self.registry = {
            "schemaVersion": 1,
            "updatedAt": "2026-08-11T00:00:00Z",
            "releaseIdentity": {"backendRevision": "revision-001"},
            "rolloutGates": [
                {
                    "id": "login_view",
                    "decision": "go",
                    "scope": "Returning users may sign in and inspect state.",
                    "allows": ["login", "view"],
                    "forbids": ["campaign_launch"],
                    "evidenceIds": ["evidence.login"],
                    "blockerIds": [],
                    "nextAction": None,
                    "rollback": "Disable returning-user access.",
                },
                {
                    "id": "supervised_canary",
                    "decision": "ready_for_canary",
                    "scope": "One monitored campaign.",
                    "allows": ["one_monitored_launch"],
                    "forbids": ["scope_expansion"],
                    "evidenceIds": ["evidence.live"],
                    "blockerIds": ["quality.canary_unrun"],
                    "nextAction": "Run the monitored canary.",
                    "rollback": "Pause the campaign and preserve evidence.",
                },
                {
                    "id": "autonomous_use",
                    "decision": "hold",
                    "scope": "Unattended campaigns.",
                    "allows": [],
                    "forbids": ["autonomous_send"],
                    "evidenceIds": ["evidence.live"],
                    "blockerIds": [],
                    "nextAction": "Define separate autonomous proof.",
                    "rollback": "Keep autonomous use disabled.",
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
                    "releaseRefs": {"backendRevision": "revision-001"},
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
                    "releaseRefs": {"backendRevision": "revision-001"},
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
                    "priority": "P0",
                    "featureIds": ["feature.beta"],
                    "scenarioIds": ["scenario.feature"],
                    "evidenceIds": ["evidence.live"],
                    "blocksGates": ["supervised_canary"],
                    "guardrail": "One monitored campaign only.",
                    "nextProof": "Run and read back the canary.",
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
                "releaseRefs": {"backendRevision": release_revision},
            }
        )
        registry["evidence"].append(failure)

    def test_valid_registry_builds_frozen_indexes(self):
        validated = self.validate()

        self.assertIsInstance(validated, self.module.ValidatedRegistry)
        self.assertEqual({"feature.alpha", "feature.beta"}, set(validated.feature_by_id))
        self.assertEqual(self.fixture_map["featureFixtureMatrix"], validated.fixture_matrix)
        self.assertEqual(
            {"login_view", "supervised_canary", "autonomous_use"},
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

    def test_scenario_union_accepts_event_and_feature_scenario_keys(self):
        self.validate()

        candidate = copy.deepcopy(self.registry)
        candidate["evidence"][0]["scenarioIds"] = ["scenario.feature"]
        candidate["qualityItems"][0]["scenarioIds"] = ["scenario.event"]
        self.validate(registry=candidate)

    def test_evidence_and_blocker_references_must_resolve(self):
        cases = (
            ("evidenceIds", "evidence.unknown", "login_view"),
            ("blockerIds", "quality.unknown", "supervised_canary"),
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
        self.assert_invalid(candidate, "supervised_canary")

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
        candidate["evidence"][0]["releaseRefs"] = {"backendRevision": "revision-000"}
        self.assert_invalid(candidate, "login_view")

    def test_newer_current_failure_with_overlapping_scope_invalidates_go(self):
        candidate = copy.deepcopy(self.registry)
        self.add_login_failure(candidate)

        self.assert_invalid(candidate, "login_view")

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
                self.assert_invalid(candidate, "supervised_canary")

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
        self.assertEqual("ready_for_canary", after_expiry["supervised_canary"])
        self.assertEqual("hold", after_expiry["autonomous_use"])
        self.assertEqual(authored, self.registry)
        self.assertEqual(authored, validated.registry)


if __name__ == "__main__":
    unittest.main()
