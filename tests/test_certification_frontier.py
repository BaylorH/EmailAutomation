"""Deterministic ranked-frontier contract for production automation certification.

Task 0 of the production automation certification plan. This module validates the
approved planning manifest *by itself* and pins the deterministic ranker that
selects exactly one active capability plus at most one independent blocker.

The not-yet-created in-image runtime registry is deliberately NOT required here;
Task 1 adds canonicalization plus the runtime registry and then extends this same
module to require exact manifest<->runtime parity before either task is jointly
GREEN.
"""

from pathlib import Path
import copy
import json
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_PATH = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-08-17-production-automation-certification-scenarios.json"
)
CERTIFICATION_DOCS = REPO_ROOT / "docs" / "release-safety" / "production-certification"
FRONTIER_PATH = CERTIFICATION_DOCS / "frontier.json"
IDENTITY_SCHEMA_PATH = CERTIFICATION_DOCS / "identity.schema.json"
STAMPS_README_PATH = CERTIFICATION_DOCS / "stamps" / "README.md"
RANKER_PATH = REPO_ROOT / "scripts" / "rank_certification_frontier.py"

BACKEND_SOURCE_ANCHOR = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
FRONTEND_BASELINE_SOURCE_ANCHOR = "2ad02ee2b9bfad9d331d50dbfe341742159404b2"

EXPECTED_CAPABILITY_ORDER = [
    "spreadsheet-admission",
    "authoritative-field-contract",
    "initial-outreach-quality",
    "thread-property-binding",
    "text-extraction-sheet-integrity",
    "property-decision",
    "natural-reply-closure",
    "pdf-and-link-understanding",
    "operator-actions",
    "followup-and-stop-controls",
    "retry-reorder-recovery",
    "whole-scrub",
]

EXPECTED_CAPABILITY_SCENARIO_COUNT = 91
EXPECTED_TOTAL_SCENARIO_COUNT = 93  # 91 capability + bootstrap + one refutation

BOOTSTRAP_CAPABILITY_ID = "certification-integrity"
VALID_VERDICTS = ["PASS", "FAIL", "INSTRUMENT_BLOCKED", "NOT_TESTED"]

REQUIRED_SCENARIO_FIELDS = (
    "scenarioId",
    "capabilityId",
    "logicalFixtureKey",
    "oracleProjectionKey",
    "expectedVerdict",
    "capabilityStamp",
    "inputProducerKind",
    "launchClass",
    "modelRepeatCount",
    "requiresHumanReview",
    "naturalnessRubricVersion",
    "requiredEffects",
    "forbiddenEffects",
)

# A logical alias may never carry a concrete resource identity. These substrings
# mark a value that leaked a real Sheet id, Drive id, mailbox, or URL.
CONCRETE_IDENTIFIER_MARKERS = ("@", "://", "docs.google", "drive.google", "sitesiftai")


def load_manifest():
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def run_ranker(*args):
    """Invoke the ranker as a black box and return (returncode, stdout, stderr)."""
    completed = subprocess.run(
        [sys.executable, "-B", str(RANKER_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return completed.returncode, completed.stdout, completed.stderr


def known_identity(**overrides):
    """A fully-known identity. Every field must be present or the ranker fails closed."""
    identity = {
        "fullGitSha": BACKEND_SOURCE_ANCHOR,
        "imageDigest": "sha256:" + "a" * 64,
        "configDigest": "b" * 64,
        "scenarioRegistryDigest": "c" * 64,
        "promptDigest": "d" * 64,
        "requestedModel": "gpt-5.2",
        "resolvedModel": "gpt-5.2",
        "modelFingerprint": "fp_" + "e" * 16,
        "dependencyDigest": "f" * 64,
        "fixtureConfigSecretVersion": 1,
        "crossRepoAnchors": {
            "backend": BACKEND_SOURCE_ANCHOR,
            "frontend": FRONTEND_BASELINE_SOURCE_ANCHOR,
        },
    }
    identity.update(overrides)
    return identity


class ApprovedManifestContractTests(unittest.TestCase):
    """The approved planning manifest is pinned exactly, on its own."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()

    def test_manifest_exists_and_declares_schema_version_one(self):
        self.assertTrue(MANIFEST_PATH.is_file(), f"missing manifest: {MANIFEST_PATH}")
        self.assertEqual(self.manifest["schemaVersion"], 1)
        self.assertEqual(
            self.manifest["mission"], "spreadsheet-to-correct-property-decision"
        )

    def test_source_anchors_are_exact(self):
        self.assertEqual(self.manifest["backendSourceAnchor"], BACKEND_SOURCE_ANCHOR)
        self.assertEqual(
            self.manifest["frontendBaselineSourceAnchor"],
            FRONTEND_BASELINE_SOURCE_ANCHOR,
        )

    def test_frontend_certification_anchor_is_null_and_blocks_spreadsheet_admission(self):
        self.assertIsNone(
            self.manifest["frontendCertificationSourceAnchor"],
            "prebuild anchor must be null until a reviewed coordinated adapter successor",
        )
        rule = self.manifest["frontendCertificationSourceAnchorRule"]
        self.assertIn("spreadsheet-admission", rule)

    def test_capabilities_are_the_twelve_expected_ids_in_order(self):
        ids = [capability["id"] for capability in self.manifest["capabilities"]]
        self.assertEqual(ids, EXPECTED_CAPABILITY_ORDER)

    def test_every_capability_starts_not_tested(self):
        start = self.manifest["allCapabilitiesStartProductionVerdict"]
        self.assertEqual(start, "NOT_TESTED")
        for capability in self.manifest["capabilities"]:
            self.assertEqual(
                capability["productionVerdict"],
                start,
                f"{capability['id']} must start NOT_TESTED",
            )

    def test_verdict_vocabulary_is_closed(self):
        self.assertEqual(self.manifest["verdicts"], VALID_VERDICTS)

    def test_exactly_ninety_one_capability_scenario_definitions(self):
        self.assertEqual(
            len(self.manifest["scenarioDefinitions"]), EXPECTED_CAPABILITY_SCENARIO_COUNT
        )

    def test_scenario_ids_are_finite_and_unique_across_bootstrap_and_capabilities(self):
        manifest = self.manifest
        ids = [item["scenarioId"] for item in manifest["scenarioDefinitions"]]
        ids.append(manifest["bootstrapScenario"]["scenarioId"])
        ids.extend(item["scenarioId"] for item in manifest["refutationScenarios"])
        self.assertEqual(len(ids), EXPECTED_TOTAL_SCENARIO_COUNT)
        self.assertEqual(
            len(set(ids)), EXPECTED_TOTAL_SCENARIO_COUNT, "scenario ids must be unique"
        )
        for scenario_id in ids:
            self.assertIsInstance(scenario_id, str)
            self.assertEqual(scenario_id, scenario_id.strip())
            self.assertTrue(scenario_id, "empty scenario id")
            self.assertNotIn("*", scenario_id, "wildcards are not finite ids")

    def test_capability_scenario_ids_map_one_for_one_to_definitions(self):
        manifest = self.manifest
        defined = {item["scenarioId"]: item for item in manifest["scenarioDefinitions"]}
        referenced = []
        for capability in manifest["capabilities"]:
            for scenario_id in capability["scenarioIds"]:
                referenced.append(scenario_id)
                self.assertIn(
                    scenario_id,
                    defined,
                    f"{capability['id']} references undefined scenario {scenario_id}",
                )
                self.assertEqual(
                    defined[scenario_id]["capabilityId"],
                    capability["id"],
                    f"{scenario_id} is classified under the wrong capability",
                )
        self.assertEqual(
            sorted(referenced),
            sorted(defined),
            "every definition must be referenced exactly once",
        )

    def test_every_scenario_definition_carries_every_required_field(self):
        for scenario in self.manifest["scenarioDefinitions"]:
            for field in REQUIRED_SCENARIO_FIELDS:
                self.assertIn(
                    field,
                    scenario,
                    f"{scenario.get('scenarioId')} is missing {field}; "
                    "no runtime scenario may inherit an omitted field",
                )
            self.assertIn(scenario["expectedVerdict"], VALID_VERDICTS)
            self.assertIsInstance(scenario["capabilityStamp"], bool)
            self.assertIsInstance(scenario["requiresHumanReview"], bool)
            self.assertIsInstance(scenario["modelRepeatCount"], int)

    def test_effect_cardinalities_are_exact_non_negative_integers(self):
        for scenario in self.manifest["scenarioDefinitions"]:
            required = scenario["requiredEffects"]
            forbidden = scenario["forbiddenEffects"]
            self.assertIsInstance(required, dict)
            self.assertIsInstance(forbidden, dict)
            self.assertTrue(required, f"{scenario['scenarioId']} declares no required effect")
            self.assertTrue(
                forbidden, f"{scenario['scenarioId']} declares no forbidden effect"
            )
            for key, count in list(required.items()) + list(forbidden.items()):
                self.assertIsInstance(
                    count, int, f"{scenario['scenarioId']}:{key} cardinality must be int"
                )
                self.assertGreaterEqual(count, 0)
                self.assertNotIn("*", key, "wildcard effect keys are not exact")
            for key, count in forbidden.items():
                self.assertEqual(
                    count, 0, f"{scenario['scenarioId']}:{key} forbidden must be exactly 0"
                )

    def test_fixture_and_oracle_values_are_repository_safe_logical_aliases(self):
        manifest = self.manifest
        scenarios = list(manifest["scenarioDefinitions"])
        scenarios.append(manifest["bootstrapScenario"])
        scenarios.extend(manifest["refutationScenarios"])
        for scenario in scenarios:
            for field in ("logicalFixtureKey", "oracleProjectionKey"):
                value = scenario[field]
                self.assertIsInstance(value, str)
                for marker in CONCRETE_IDENTIFIER_MARKERS:
                    self.assertNotIn(
                        marker,
                        value,
                        f"{scenario['scenarioId']}.{field} leaked a concrete identifier",
                    )
            self.assertTrue(
                scenario["logicalFixtureKey"].startswith(scenario["capabilityId"] + "/"),
                f"{scenario['scenarioId']} fixture alias must be capability-scoped",
            )

    def test_bootstrap_scenario_is_infrastructure_proof_not_a_capability_stamp(self):
        bootstrap = self.manifest["bootstrapScenario"]
        self.assertEqual(bootstrap["capabilityId"], BOOTSTRAP_CAPABILITY_ID)
        self.assertFalse(
            bootstrap["capabilityStamp"],
            "bootstrap proves the instrument, never an out-of-order capability",
        )
        self.assertEqual(bootstrap["expectedVerdict"], "PASS")
        self.assertEqual(bootstrap["launchClass"], "agent_safe")
        self.assertEqual(bootstrap["modelRepeatCount"], 0)
        capability_scenario_ids = {
            item["scenarioId"] for item in self.manifest["scenarioDefinitions"]
        }
        self.assertNotIn(bootstrap["scenarioId"], capability_scenario_ids)

    def test_refutation_scenario_expects_failure_and_forbids_a_stamp(self):
        refutations = self.manifest["refutationScenarios"]
        self.assertEqual(len(refutations), 1)
        refutation = refutations[0]
        self.assertEqual(refutation["expectedVerdict"], "FAIL")
        self.assertFalse(refutation["capabilityStamp"])
        self.assertEqual(refutation["forbiddenEffects"]["capability_stamp"], 0)

    def test_execution_contract_pins_model_repeat_and_human_review(self):
        contract = self.manifest["scenarioExecutionContract"]
        self.assertTrue(contract["defaultCapabilityRequiresUserRuntimeLaunch"])
        self.assertEqual(contract["modelRepeatCount"], 3)
        self.assertEqual(
            contract["humanReviewCapabilityIds"],
            ["initial-outreach-quality", "natural-reply-closure"],
        )
        self.assertEqual(contract["naturalnessRubricVersion"], "broker-naturalness-v1")
        known_ids = set(EXPECTED_CAPABILITY_ORDER)
        for capability_id in contract["humanReviewCapabilityIds"]:
            self.assertIn(capability_id, known_ids)

    def test_model_scenarios_repeat_three_times_and_deterministic_scenarios_zero(self):
        for scenario in self.manifest["scenarioDefinitions"]:
            if scenario["launchClass"] == "user_runtime":
                self.assertEqual(
                    scenario["modelRepeatCount"],
                    3,
                    f"{scenario['scenarioId']} must run three fresh passing runs",
                )
            elif scenario["launchClass"] == "agent_safe":
                self.assertEqual(scenario["modelRepeatCount"], 0)
            else:
                self.fail(f"unknown launchClass {scenario['launchClass']}")

    def test_human_review_scenarios_carry_the_pinned_rubric(self):
        rubric = self.manifest["scenarioExecutionContract"]["naturalnessRubricVersion"]
        review_capabilities = set(
            self.manifest["scenarioExecutionContract"]["humanReviewCapabilityIds"]
        )
        for scenario in self.manifest["scenarioDefinitions"]:
            if scenario["requiresHumanReview"]:
                self.assertIn(scenario["capabilityId"], review_capabilities)
                self.assertEqual(scenario["naturalnessRubricVersion"], rubric)
            else:
                self.assertEqual(scenario["naturalnessRubricVersion"], "")

    def test_agent_forbidden_effects_are_declared(self):
        policy = self.manifest["externalEffectPolicy"]
        self.assertEqual(
            policy["agentForbidden"],
            [
                "public-git-push",
                "production-traffic-change",
                "shared-customer-deployment",
                "real-openai-call",
                "public-drive-permission",
                "raw-review-output",
            ],
        )
        self.assertEqual(policy["modelScenarioBlockedReason"], "user_runtime_launch_required")
        self.assertEqual(policy["publicDrivePublicationVerdict"], "NOT_TESTED")

    def test_planned_and_existing_test_module_paths_are_declared(self):
        manifest = self.manifest
        self.assertEqual(
            manifest["plannedScenarioTestModule"],
            "EmailAutomation/tests/test_production_certification.py",
        )
        self.assertTrue(manifest["existingTestsAreRegressionEvidenceNotProductionStamps"])
        for capability in manifest["capabilities"]:
            self.assertTrue(
                capability["existingTestModules"],
                f"{capability['id']} declares no existing regression modules",
            )
            for module in capability["existingTestModules"]:
                self.assertNotIn("*", module, "test module paths must be exact, not globs")

    def test_required_new_red_scenarios_name_known_capabilities(self):
        known_capabilities = set(EXPECTED_CAPABILITY_ORDER)
        for capability_id, scenario_ids in self.manifest["requiredNewRedScenarios"].items():
            self.assertIn(capability_id, known_capabilities)
            self.assertTrue(scenario_ids, f"{capability_id} lists no required new RED")
            for scenario_id in scenario_ids:
                self.assertIsInstance(scenario_id, str)
                self.assertEqual(scenario_id, scenario_id.strip())
                self.assertNotIn("*", scenario_id, "wildcards are not finite ids")

    def test_required_new_red_scenario_alias_drift_is_pinned(self):
        """`requiredNewRedScenarios` is planning INTENT, not a registry reference.

        The approved manifest's own `scenarioDefinitionRule` only requires the 93
        scenarios to appear one-for-one in the in-image runtime registry; it never
        claims this intent list resolves to those finite ids. Five entries do
        resolve exactly and seven are descriptive near-misses of a defined
        scenario (`one-instant-before-due` for `before-due-zero-send`, and so on).

        The manifest is SHA-256 pinned with two independent APPROVEs, so it may not
        be amended here. This test therefore PINS the exact current split so the
        drift stays visible and cannot widen silently. Reconciling the seven names
        belongs to a reviewed planning successor, not to this task.
        """
        defined = {item["scenarioId"] for item in self.manifest["scenarioDefinitions"]}
        resolved, unresolved = [], []
        for scenario_ids in self.manifest["requiredNewRedScenarios"].values():
            for scenario_id in scenario_ids:
                (resolved if scenario_id in defined else unresolved).append(scenario_id)

        self.assertEqual(
            sorted(resolved),
            [
                "blank-invalid-duplicate-rows",
                "declined-not-reasked",
                "human-naturalness-outreach",
                "multi-tab-ambiguous",
            ],
        )
        self.assertEqual(
            sorted(unresolved),
            [
                "exactly-due",
                "final-envelope-empty-bcc",
                "human-naturalness-every-finite-output",
                "live-artifact-source-correlation",
                "one-instant-after-due",
                "one-instant-before-due",
                "production-resident-integrated-runner",
            ],
            "requiredNewRedScenarios alias drift changed; reconcile through a "
            "reviewed planning successor before editing this expectation",
        )


class ManifestHostileControlTests(unittest.TestCase):
    """A corrupted manifest must be rejected, not silently ranked."""

    def setUp(self):
        self.manifest = load_manifest()
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tmp = Path(self._tempdir.name)

    def _rank_with_manifest(self, manifest):
        manifest_path = self.tmp / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        frontier = json.loads(FRONTIER_PATH.read_text(encoding="utf-8"))
        frontier["approvedManifestPath"] = str(manifest_path)
        frontier_path = self.tmp / "frontier.json"
        frontier_path.write_text(json.dumps(frontier), encoding="utf-8")

        identity_path = self.tmp / "identity.json"
        identity_path.write_text(json.dumps(known_identity()), encoding="utf-8")
        stamps_path = self.tmp / "stamps.json"
        stamps_path.write_text(json.dumps([]), encoding="utf-8")

        return run_ranker(
            "--frontier", str(frontier_path),
            "--stamps", str(stamps_path),
            "--previous-identity", str(identity_path),
            "--current-identity", str(identity_path),
            "--changed-paths", "",
            "--json",
        )

    def test_baseline_manifest_is_accepted(self):
        code, stdout, stderr = self._rank_with_manifest(self.manifest)
        self.assertEqual(code, 0, f"unmodified manifest must rank: {stderr}")
        self.assertTrue(stdout.strip(), "ranker emitted no JSON")

    def test_wildcard_scenario_id_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenarioDefinitions"][0]["scenarioId"] = "standard-*"
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "a wildcard scenario id must fail closed")
        self.assertIn("wildcard", stderr.lower())

    def test_missing_scenario_field_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        del manifest["scenarioDefinitions"][0]["forbiddenEffects"]
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "an omitted field must not inherit a default")
        self.assertIn("forbiddenEffects", stderr)

    def test_duplicate_scenario_id_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenarioDefinitions"][1]["scenarioId"] = manifest["scenarioDefinitions"][0][
            "scenarioId"
        ]
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0)
        self.assertIn("duplicate", stderr.lower())

    def test_unclassified_scenario_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["capabilities"][0]["scenarioIds"] = [
            item
            for item in manifest["capabilities"][0]["scenarioIds"]
            if item != manifest["scenarioDefinitions"][0]["scenarioId"]
        ]
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "an orphaned definition must fail closed")
        self.assertIn("unreferenced", stderr.lower())

    def test_unknown_capability_reference_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenarioDefinitions"][0]["capabilityId"] = "not-a-capability"
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0)
        self.assertIn("unknown capability", stderr.lower())

    def test_concrete_identifier_in_fixture_alias_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenarioDefinitions"][0]["logicalFixtureKey"] = (
            "spreadsheet-admission/https://docs.google.com/spreadsheets/d/abc123"
        )
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "a concrete resource identity must never enter the manifest")
        self.assertIn("alias", stderr.lower())

    def test_self_asserted_success_effect_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["scenarioDefinitions"][0]["requiredEffects"] = {"scenario_reported_success": 1}
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "a scenario may not emit its own pass token")
        self.assertIn("observed", stderr.lower())

    def test_wrong_scenario_count_is_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        dropped = manifest["scenarioDefinitions"].pop()
        manifest["capabilities"] = [
            {
                **capability,
                "scenarioIds": [
                    item for item in capability["scenarioIds"] if item != dropped["scenarioId"]
                ],
            }
            for capability in manifest["capabilities"]
        ]
        code, _, stderr = self._rank_with_manifest(manifest)
        self.assertNotEqual(code, 0, "the scenario count is pinned at 91")
        self.assertIn("91", stderr)


class FrontierPolicyArtifactTests(unittest.TestCase):
    """frontier.json is static policy only, never dynamic production state."""

    def test_frontier_artifacts_exist(self):
        self.assertTrue(FRONTIER_PATH.is_file(), f"missing {FRONTIER_PATH}")
        self.assertTrue(IDENTITY_SCHEMA_PATH.is_file(), f"missing {IDENTITY_SCHEMA_PATH}")
        self.assertTrue(STAMPS_README_PATH.is_file(), f"missing {STAMPS_README_PATH}")
        self.assertTrue(RANKER_PATH.is_file(), f"missing {RANKER_PATH}")

    def test_frontier_declares_static_order_and_dependencies_only(self):
        frontier = json.loads(FRONTIER_PATH.read_text(encoding="utf-8"))
        self.assertEqual(frontier["capabilityOrder"], EXPECTED_CAPABILITY_ORDER)
        self.assertIn("approvedManifestPath", frontier)
        dependencies = frontier["dependencies"]
        self.assertEqual(
            sorted(dependencies["whole-scrub"]),
            sorted([c for c in EXPECTED_CAPABILITY_ORDER if c != "whole-scrub"]),
            "whole-scrub depends on every other capability",
        )
        for capability_id, requires in dependencies.items():
            self.assertIn(capability_id, EXPECTED_CAPABILITY_ORDER)
            for dependency in requires:
                self.assertIn(dependency, EXPECTED_CAPABILITY_ORDER)
                self.assertLess(
                    EXPECTED_CAPABILITY_ORDER.index(dependency),
                    EXPECTED_CAPABILITY_ORDER.index(capability_id),
                    "a dependency must outrank its dependent",
                )

    def test_frontier_carries_no_dynamic_state(self):
        raw = json.loads(FRONTIER_PATH.read_text(encoding="utf-8"))
        for banned in ("stamps", "activeCapability", "currentIdentity", "revision", "verdicts"):
            self.assertNotIn(
                banned,
                raw,
                f"frontier.json must not record dynamic state ({banned})",
            )

    def test_identity_schema_requires_every_fail_closed_field(self):
        schema = json.loads(IDENTITY_SCHEMA_PATH.read_text(encoding="utf-8"))
        required = set(schema["required"])
        for field in known_identity():
            self.assertIn(field, required, f"identity schema must require {field}")
        self.assertFalse(
            schema.get("additionalProperties", True),
            "an unknown identity field must fail closed, not be ignored",
        )

    def test_stamps_readme_declares_private_retention(self):
        text = STAMPS_README_PATH.read_text(encoding="utf-8")
        self.assertIn("sanitized", text.lower())
        for phrase in ("never committed", "private"):
            self.assertIn(phrase, text.lower())


class RankerSelectionTests(unittest.TestCase):
    """Exactly one active capability, at most one independent blocker."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tmp = Path(self._tempdir.name)

    def _write(self, name, payload):
        path = self.tmp / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def _rank(self, stamps=None, previous=None, current=None, changed_paths=""):
        stamps_path = self._write("stamps.json", stamps if stamps is not None else [])
        previous_path = self._write(
            "previous.json", previous if previous is not None else known_identity()
        )
        current_path = self._write(
            "current.json", current if current is not None else known_identity()
        )
        code, stdout, stderr = run_ranker(
            "--frontier", str(FRONTIER_PATH),
            "--stamps", stamps_path,
            "--previous-identity", previous_path,
            "--current-identity", current_path,
            "--changed-paths", changed_paths,
            "--json",
        )
        payload = json.loads(stdout) if code == 0 and stdout.strip() else None
        return code, payload, stderr

    def _rank_ok(self, **kwargs):
        """Rank and require a real payload.

        Returning None on failure would let an equality assertion pass vacuously
        (None == None), so every success-path test goes through this gate.
        """
        code, payload, stderr = self._rank(**kwargs)
        if code != 0 or payload is None:
            self.fail(f"ranker exited {code} with no JSON payload: {stderr}")
        return payload

    def test_cold_start_selects_exactly_one_capability(self):
        payload = self._rank_ok()
        self.assertIn(payload["activeCapability"], EXPECTED_CAPABILITY_ORDER)
        self.assertIsInstance(payload["reasonCodes"], list)
        self.assertTrue(payload["reasonCodes"])

    def test_null_frontend_anchor_blocks_spreadsheet_admission(self):
        payload = self._rank_ok()
        self.assertNotEqual(
            payload["activeCapability"],
            "spreadsheet-admission",
            "a null frontendCertificationSourceAnchor blocks spreadsheet admission",
        )
        self.assertIsNotNone(payload["blocker"], "the blocked capability must be reported")
        self.assertEqual(payload["blocker"]["capabilityId"], "spreadsheet-admission")
        self.assertEqual(payload["blocker"]["verdict"], "INSTRUMENT_BLOCKED")

    def test_at_most_one_blocker_accompanies_the_active_capability(self):
        payload = self._rank_ok()
        blocker = payload["blocker"]
        self.assertTrue(blocker is None or isinstance(blocker, dict))

    def test_dependencies_outrank_dependents(self):
        stamps = [
            {"capabilityId": c, "verdict": "PASS", "identity": known_identity()}
            for c in EXPECTED_CAPABILITY_ORDER
            if c not in ("whole-scrub", "spreadsheet-admission")
        ]
        payload = self._rank_ok(stamps=stamps)
        self.assertNotEqual(
            payload["activeCapability"],
            "whole-scrub",
            "whole-scrub cannot activate while a dependency is unstamped",
        )

    def test_whole_scrub_activates_only_when_every_dependency_is_stamped(self):
        stamps = [
            {"capabilityId": c, "verdict": "PASS", "identity": known_identity()}
            for c in EXPECTED_CAPABILITY_ORDER
            if c != "whole-scrub"
        ]
        payload = self._rank_ok(stamps=stamps)
        self.assertEqual(payload["activeCapability"], "whole-scrub")

    def test_a_safety_failure_outranks_core_completion(self):
        stamps = [
            {"capabilityId": c, "verdict": "PASS", "identity": known_identity()}
            for c in EXPECTED_CAPABILITY_ORDER
            if c != "whole-scrub"
        ]
        stamps.append(
            {
                "capabilityId": "initial-outreach-quality",
                "verdict": "FAIL",
                "safety": True,
                "identity": known_identity(),
            }
        )
        payload = self._rank_ok(stamps=stamps)
        self.assertEqual(
            payload["activeCapability"],
            "initial-outreach-quality",
            "a safety failure preempts whole-scrub completion",
        )
        self.assertIn("safety-failure", payload["reasonCodes"])

    def test_unrelated_backlog_never_changes_the_selection(self):
        baseline = self._rank_ok()
        with_backlog = self._rank_ok(
            changed_paths="docs/notes/results-workspace.md,src/ResultsWorkspace.jsx"
        )
        self.assertEqual(baseline["activeCapability"], with_backlog["activeCapability"])

    def test_selection_is_deterministic_under_stamp_reordering(self):
        stamps = [
            {"capabilityId": c, "verdict": "PASS", "identity": known_identity()}
            for c in EXPECTED_CAPABILITY_ORDER[:5]
        ]
        forward = self._rank_ok(stamps=stamps)
        backward = self._rank_ok(stamps=list(reversed(stamps)))
        self.assertEqual(forward, backward, "ranking must not depend on input order")

    def test_changed_production_path_invalidates_only_matching_stamps(self):
        stamps = [
            {
                "capabilityId": "authoritative-field-contract",
                "verdict": "PASS",
                "identity": known_identity(),
                "productionPaths": ["EmailAutomation/email_automation/column_config.py"],
            },
            {
                "capabilityId": "property-decision",
                "verdict": "PASS",
                "identity": known_identity(),
                "productionPaths": ["EmailAutomation/email_automation/processing.py"],
            },
        ]
        payload = self._rank_ok(
            stamps=stamps,
            changed_paths="EmailAutomation/email_automation/column_config.py",
        )
        self.assertEqual(payload["invalidatedStamps"], ["authoritative-field-contract"])


class RankerFailClosedTests(unittest.TestCase):
    """Unknown identity change fails closed; it never silently keeps a stamp."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.tmp = Path(self._tempdir.name)

    def _rank_identities(self, previous, current, stamps=None):
        def write(name, payload):
            path = self.tmp / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            return str(path)

        return run_ranker(
            "--frontier", str(FRONTIER_PATH),
            "--stamps", write("stamps.json", stamps if stamps is not None else []),
            "--previous-identity", write("previous.json", previous),
            "--current-identity", write("current.json", current),
            "--changed-paths", "",
            "--json",
        )

    def _stamped_everything(self):
        return [
            {"capabilityId": c, "verdict": "PASS", "identity": known_identity()}
            for c in EXPECTED_CAPABILITY_ORDER
        ]

    def test_each_identity_field_change_invalidates_every_stamp(self):
        for field, changed in (
            ("fullGitSha", "0" * 40),
            ("imageDigest", "sha256:" + "9" * 64),
            ("configDigest", "9" * 64),
            ("scenarioRegistryDigest", "9" * 64),
            ("promptDigest", "9" * 64),
            ("requestedModel", "gpt-5.3"),
            ("resolvedModel", "gpt-5.3"),
            ("modelFingerprint", "fp_" + "9" * 16),
            ("dependencyDigest", "9" * 64),
            ("fixtureConfigSecretVersion", 2),
        ):
            with self.subTest(field=field):
                code, stdout, stderr = self._rank_identities(
                    known_identity(),
                    known_identity(**{field: changed}),
                    stamps=self._stamped_everything(),
                )
                self.assertEqual(code, 0, stderr)
                payload = json.loads(stdout)
                self.assertEqual(
                    sorted(payload["invalidatedStamps"]),
                    sorted(EXPECTED_CAPABILITY_ORDER),
                    f"a changed {field} must invalidate every stamp",
                )

    def test_cross_repo_anchor_change_invalidates_every_stamp(self):
        current = known_identity()
        current["crossRepoAnchors"] = {
            "backend": BACKEND_SOURCE_ANCHOR,
            "frontend": "1" * 40,
        }
        code, stdout, stderr = self._rank_identities(
            known_identity(), current, stamps=self._stamped_everything()
        )
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            sorted(payload["invalidatedStamps"]), sorted(EXPECTED_CAPABILITY_ORDER)
        )

    def test_absent_identity_field_fails_closed(self):
        incomplete = known_identity()
        del incomplete["modelFingerprint"]
        code, _, stderr = self._rank_identities(known_identity(), incomplete)
        self.assertNotEqual(code, 0, "an absent identity field must fail closed")
        self.assertIn("modelFingerprint", stderr)

    def test_unknown_identity_field_fails_closed(self):
        extra = known_identity()
        extra["surpriseField"] = "unknown"
        code, _, stderr = self._rank_identities(known_identity(), extra)
        self.assertNotEqual(code, 0, "an unknown identity field must fail closed")
        self.assertIn("surpriseField", stderr)

    def test_ranker_never_mutates_tracked_product_files(self):
        before = FRONTIER_PATH.read_bytes()
        manifest_before = MANIFEST_PATH.read_bytes()
        self._rank_identities(known_identity(), known_identity())
        self.assertEqual(FRONTIER_PATH.read_bytes(), before)
        self.assertEqual(MANIFEST_PATH.read_bytes(), manifest_before)


class ManifestRuntimeParityTests(unittest.TestCase):
    """Task 1's joint-GREEN gate: the planning manifest and the in-image runtime
    registry must agree exactly.

    Task 0 validated the manifest alone because the runtime registry did not yet
    exist. Now that it does, neither task is GREEN unless the two are one-for-one.
    The runtime registry is the authority the deployed route actually loads; the
    manifest is planning input. A divergence means the image would certify
    something the approved plan never described.
    """

    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        from email_automation.certification import scenarios

        cls.scenarios = scenarios

    def _manifest_by_id(self):
        by_id = {item["scenarioId"]: item for item in self.manifest["scenarioDefinitions"]}
        by_id[self.manifest["bootstrapScenario"]["scenarioId"]] = self.manifest[
            "bootstrapScenario"
        ]
        for item in self.manifest["refutationScenarios"]:
            by_id[item["scenarioId"]] = item
        return by_id

    def test_scenario_ids_are_one_for_one(self):
        self.assertEqual(
            set(self.scenarios.scenario_ids()),
            set(self._manifest_by_id()),
            "runtime registry ids diverged from the approved manifest",
        )

    def test_scenario_fields_are_one_for_one(self):
        for scenario_id, expected in self._manifest_by_id().items():
            with self.subTest(scenario=scenario_id):
                actual = dict(self.scenarios.get(scenario_id))
                actual.pop("scenarioClass")
                self.assertEqual(actual, expected)

    def test_runtime_registry_digest_is_bindable(self):
        digest = self.scenarios.registry_digest()
        self.assertRegex(
            digest,
            r"^[0-9a-f]{64}$",
            "scenarioRegistryDigest must be a lowercase SHA-256 an identity can bind",
        )

    def test_every_capability_scenario_resolves_through_the_runtime_registry(self):
        for capability in self.manifest["capabilities"]:
            owned = {
                item["scenarioId"]
                for item in self.scenarios.capability_scenarios(capability["id"])
            }
            self.assertEqual(
                owned,
                set(capability["scenarioIds"]),
                f"{capability['id']} scenario ownership diverged in the runtime registry",
            )


if __name__ == "__main__":
    unittest.main()
