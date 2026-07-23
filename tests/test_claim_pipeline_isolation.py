import ast
import json
import re
import subprocess
import sys
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
EFFECT_ADAPTER_PATHS = (
    PACKAGE_ROOT / "effect_adapter.py",
    PACKAGE_ROOT / "effect_adapter_fixtures.py",
)
PRODUCTION_MODULE_PREFIXES = (
    "email_automation.ai_processing",
    "email_automation.email",
    "email_automation.followup",
    "email_automation.notifications",
    "email_automation.pending_responses",
    "email_automation.processing",
    "email_automation.service_providers",
    "email_automation.sheet_operations",
    "email_automation.sheets",
)
FORBIDDEN_EFFECT_BOUNDARY_TOKENS = frozenset(
    {
        "callable",
        "callback",
        "client",
        "driver",
        "executor",
        "firebase",
        "firestore",
        "followup",
        "google",
        "graph",
        "hook",
        "msal",
        "notifications",
        "outbox",
        "pending_responses",
        "processing",
        "protocol",
        "repository",
        "requests",
        "service",
        "sheets",
        "transport",
    }
)
EXPECTED_EFFECT_ADAPTER_API = {
    "ActionStateSnapshot",
    "ApprovalGrant",
    "DryRunCommitReceipt",
    "DryRunEffectReceipt",
    "DryRunReason",
    "DryRunStatus",
    "EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION",
    "EffectAdapterFixtureCatalog",
    "EffectAdapterFixtureCase",
    "EffectAdapterFixtureResult",
    "EffectAdapterFixtureValidationError",
    "EffectAdapterRequest",
    "evaluate_effect_plan",
    "load_effect_adapter_fixture_catalog",
    "run_effect_adapter_fixture_case",
}


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


def _import_violations(tree):
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(_resolved_import_names(node))
    return {
        imported
        for imported in imported_names
        if not any(
            imported == prefix or imported.startswith(f"{prefix}.")
            for prefix in ALLOWED_IMPORT_PREFIXES
        )
    }


def _identifier_tokens(name):
    normalized = name.casefold()
    words = tuple(
        part.casefold()
        for part in re.findall(
            r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+",
            name,
        )
    )
    return {
        normalized,
        *normalized.replace("-", "_").split("_"),
        *words,
        "_".join(words),
    }


def _boundary_surface_tokens(name):
    normalized = name.casefold()
    if normalized.endswith(("_id", "_ids", "_hash", "_version")):
        return {normalized} & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
    return _identifier_tokens(name) & FORBIDDEN_EFFECT_BOUNDARY_TOKENS


def _is_dataclass(class_node):
    return any(
        (
            isinstance(decorator, ast.Name)
            and decorator.id == "dataclass"
        )
        or (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
        )
        for decorator in class_node.decorator_list
    )


def _is_sorted_key_lambda(node, parents):
    keyword = parents.get(node)
    call = parents.get(keyword)
    return (
        isinstance(keyword, ast.keyword)
        and keyword.arg == "key"
        and isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "sorted"
    )


def _schema_key_strings(tree):
    keys = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {
            target.id
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if not any(target.endswith("_KEYS") for target in targets):
            continue
        keys.update(
            child.value
            for child in ast.walk(node.value)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
        )
    return keys


def _effect_boundary_violations(tree):
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations = set()

    for imported in _import_violations(tree):
        violations.add(f"import:{imported}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda) and not _is_sorted_key_lambda(node, parents):
            violations.add(f"lambda:{node.lineno}")
        if isinstance(node, ast.Name):
            forbidden = {node.id.casefold()} & {"callable", "protocol"}
            violations.update(f"identifier:{token}" for token in forbidden)
        elif isinstance(node, ast.Attribute):
            forbidden = {node.attr.casefold()} & {"callable", "protocol"}
            violations.update(f"identifier:{token}" for token in forbidden)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            forbidden = _boundary_surface_tokens(node.name)
            violations.update(f"function:{token}" for token in forbidden)
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            for argument in arguments:
                forbidden = _boundary_surface_tokens(argument.arg)
                violations.update(f"argument:{token}" for token in forbidden)
        elif isinstance(node, ast.ClassDef):
            forbidden = _boundary_surface_tokens(node.name)
            violations.update(f"class:{token}" for token in forbidden)
            if not _is_dataclass(node):
                continue
            for field in node.body:
                if not isinstance(field, ast.AnnAssign) or not isinstance(
                    field.target, ast.Name
                ):
                    continue
                forbidden = _boundary_surface_tokens(field.target.id)
                violations.update(f"dataclass-field:{token}" for token in forbidden)
                annotation_names = {
                    child.id.casefold()
                    for child in ast.walk(field.annotation)
                    if isinstance(child, ast.Name)
                }
                if annotation_names & {"callable", "protocol"}:
                    violations.add(f"function-valued-field:{field.target.id}")
                if isinstance(field.value, ast.Lambda):
                    violations.add(f"function-valued-field:{field.target.id}")

    for key in _schema_key_strings(tree):
        forbidden = _identifier_tokens(key) & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
        violations.update(f"schema-key:{token}" for token in forbidden)

    return violations


def _private_adapter_declarations():
    names = set()
    for path in EFFECT_ADAPTER_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names.update(
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("_")
        )
    return names


class ClaimPipelineIsolationTests(unittest.TestCase):
    def test_relative_imports_are_resolved_against_claim_pipeline_package(self):
        tree = ast.parse(
            "from ..processing import process_message\n"
            "from ..ai_processing import propose_sheet_updates\n"
            "from ..pending_responses import queue_pending_response\n"
            "from ..sheets import update_sheet\n"
            "from ..followup import schedule_followup\n"
            "from ..notifications import write_notification\n"
            "from .contracts import Actor\n"
        )

        self.assertEqual(
            {
                "email_automation.processing",
                "email_automation.ai_processing",
                "email_automation.pending_responses",
                "email_automation.sheets",
                "email_automation.followup",
                "email_automation.notifications",
            },
            _import_violations(tree),
        )

    def test_foundation_has_no_service_or_side_effect_imports(self):
        violations = set()
        for path in PACKAGE_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.update(_import_violations(tree))

        self.assertEqual(set(), violations)

    def test_effect_adapter_boundary_has_no_callable_or_service_surface(self):
        violations = set()
        for path in EFFECT_ADAPTER_PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.update(_effect_boundary_violations(tree))

        self.assertEqual(set(), violations)

    def test_effect_adapter_boundary_scanner_rejects_forbidden_shapes(self):
        tree = ast.parse(
            "from typing import Callable, Protocol\n"
            "from ..processing import process_message\n"
            "from dataclasses import dataclass\n"
            "_ROOT_KEYS = frozenset({'callback'})\n"
            "class Driver(Protocol):\n"
            "    pass\n"
            "@dataclass(frozen=True)\n"
            "class Request:\n"
            "    callback: Callable[[], None]\n"
            "factory = lambda: process_message\n"
        )

        violations = _effect_boundary_violations(tree)

        self.assertIn("import:email_automation.processing", violations)
        self.assertIn("identifier:callable", violations)
        self.assertIn("identifier:protocol", violations)
        self.assertIn("dataclass-field:callback", violations)
        self.assertIn("function-valued-field:callback", violations)
        self.assertIn("schema-key:callback", violations)
        self.assertTrue(any(item.startswith("lambda:") for item in violations))

    def test_package_import_does_not_load_production_service_modules(self):
        script = (
            "import json, sys\n"
            "from email_automation import claim_pipeline\n"
            f"prefixes = {PRODUCTION_MODULE_PREFIXES!r}\n"
            "loaded = sorted(name for name in sys.modules "
            "if any(name == prefix or name.startswith(prefix + '.') "
            "for prefix in prefixes))\n"
            "print(json.dumps(loaded))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual([], json.loads(completed.stdout))

    def test_effect_adapter_api_is_exposed_at_package_boundary(self):
        exported_names = set(claim_pipeline.__all__)

        self.assertEqual(
            set(),
            {
                name
                for name in EXPECTED_EFFECT_ADAPTER_API
                if not hasattr(claim_pipeline, name)
            },
        )
        self.assertLessEqual(EXPECTED_EFFECT_ADAPTER_API, exported_names)
        self.assertEqual(
            set(),
            {
                name
                for name in _private_adapter_declarations()
                if name in exported_names or hasattr(claim_pipeline, name)
            },
        )

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

    def test_provider_policy_shadow_api_is_exposed_at_package_boundary(self):
        expected_names = {
            "PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION",
            "PROVIDER_POLICY_SHADOW_PROFILE",
            "SUPPORTED_PROVIDER_POLICY_GAPS",
            "BudgetedProviderTransport",
            "ProviderBudgetExceeded",
            "ProviderBudgetLimits",
            "ProviderPolicyFixtureCase",
            "ProviderPolicyFixtureCatalog",
            "ProviderPolicyFixtureValidationError",
            "ProviderPolicyShadowCaseResult",
            "ProviderPolicyShadowIdentity",
            "ProviderPolicyShadowReport",
            "ProviderReservationSnapshot",
            "RecordedProviderQualityProposalAdapter",
            "load_provider_policy_fixture_catalog",
            "run_provider_policy_shadow",
            "select_provider_policy_cases",
        }

        self.assertEqual(
            set(),
            {name for name in expected_names if not hasattr(claim_pipeline, name)},
        )


if __name__ == "__main__":
    unittest.main()
