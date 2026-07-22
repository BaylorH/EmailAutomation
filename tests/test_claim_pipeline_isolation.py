import ast
import unittest
from pathlib import Path

from email_automation import claim_pipeline


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "email_automation"
    / "claim_pipeline"
)
ALLOWED_IMPORT_PREFIXES = (
    "__future__",
    "collections.abc",
    "dataclasses",
    "datetime",
    "email_automation.claim_pipeline",
    "enum",
    "hashlib",
    "json",
    "math",
    "pathlib",
    "re",
    "types",
    "typing",
)


def _resolved_import_names(node):
    package = ("email_automation", "claim_pipeline")
    if not node.level:
        if not node.module:
            return ()
        return tuple(f"{node.module}.{alias.name}" for alias in node.names)
    keep = max(0, len(package) - (node.level - 1))
    base = package[:keep]
    if node.module:
        return (".".join((*base, *node.module.split("."))),)
    return tuple(".".join((*base, alias.name)) for alias in node.names)


class ClaimPipelineIsolationTests(unittest.TestCase):
    def test_relative_imports_are_resolved_against_claim_pipeline_package(self):
        tree = ast.parse(
            "from ..processing import process_message\n"
            "from .. import property_images\n"
            "from .contracts import Actor\n"
            "from email_automation import ai_processing\n"
        )
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(_resolved_import_names(node))

        self.assertEqual(
            {
                "email_automation.processing",
                "email_automation.property_images",
                "email_automation.claim_pipeline.contracts",
                "email_automation.ai_processing",
            },
            imported_names,
        )

    def test_foundation_has_no_service_or_side_effect_imports(self):
        imported_names = set()
        for path in PACKAGE_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_names.update(_resolved_import_names(node))

        violations = sorted(
            imported
            for imported in imported_names
            if not any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in ALLOWED_IMPORT_PREFIXES
            )
        )
        self.assertEqual([], violations)

    def test_interpretation_api_is_exposed_at_package_boundary(self):
        expected_names = {
            "CLAIM_EXTRACTION_SCHEMA_VERSION",
            "CLAIM_FIXTURE_SCHEMA_VERSION",
            "ClaimExtractionIssue",
            "ClaimExtractionRequest",
            "ClaimExtractionResult",
            "ClaimFixtureCase",
            "ClaimFixtureCatalog",
            "ClaimFixtureValidationError",
            "EntityMatch",
            "EntityResolutionResult",
            "EntitySeed",
            "EvidenceFailure",
            "EvidenceNormalizationResult",
            "ExternalEvidenceInput",
            "INTERPRETATION_FIXTURE_SCHEMA_VERSION",
            "InterpretationFixtureCase",
            "InterpretationFixtureCatalog",
            "InterpretationFixtureValidationError",
            "InterpretationReplayResult",
            "RawMessageEvidence",
            "RecordedProposalAdapter",
            "ReplayCaseResult",
            "ReplayIdentity",
            "ReplayReport",
            "ResolutionIssue",
            "canonicalize_address",
            "build_claim_extraction_request",
            "extract_claims",
            "extract_addresses",
            "extract_suites",
            "load_interpretation_fixture_catalog",
            "load_claim_fixture_catalog",
            "normalize_message_evidence",
            "resolve_entities",
            "run_claim_replay",
        }

        self.assertEqual(
            set(),
            {name for name in expected_names if not hasattr(claim_pipeline, name)},
        )

    def test_policy_api_is_exposed_at_package_boundary(self):
        expected_names = {
            "POLICY_FIXTURE_SCHEMA_VERSION",
            "POLICY_REASON_CODES",
            "REQUIRED_POLICY_DIMENSIONS",
            "ClaimConflict",
            "EntityPolicyResult",
            "PolicyEvaluationRequest",
            "PolicyEvaluationResult",
            "PolicyFixtureCase",
            "PolicyFixtureCatalog",
            "PolicyFixtureValidationError",
            "evaluate_policy",
            "load_policy_fixture_catalog",
        }

        self.assertEqual(
            set(),
            {name for name in expected_names if not hasattr(claim_pipeline, name)},
        )

    def test_legacy_shadow_api_is_exposed_at_package_boundary(self):
        expected_names = {
            "LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION",
            "LegacyActionAttempt",
            "LegacyProjection",
            "LegacyShadowCaseResult",
            "LegacyShadowDiscrepancy",
            "LegacyShadowFixtureCase",
            "LegacyShadowFixtureCatalog",
            "LegacyShadowFixtureValidationError",
            "LegacyShadowIdentity",
            "LegacyShadowReport",
            "compare_legacy_case",
            "load_legacy_shadow_fixture_catalog",
            "project_legacy_proposal",
            "run_legacy_shadow",
        }

        self.assertEqual(
            set(),
            {name for name in expected_names if not hasattr(claim_pipeline, name)},
        )


if __name__ == "__main__":
    unittest.main()
