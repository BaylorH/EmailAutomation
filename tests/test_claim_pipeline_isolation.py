import ast
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from email_automation import claim_pipeline
from email_automation.claim_pipeline import effect_adapter
from email_automation.claim_pipeline import effect_adapter_fixtures


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "email_automation"
    / "claim_pipeline"
)
INITIALIZER_PATH = PACKAGE_ROOT / "__init__.py"
EFFECT_ADAPTER_PATH = PACKAGE_ROOT / "effect_adapter.py"
EFFECT_ADAPTER_FIXTURES_PATH = (
    PACKAGE_ROOT / "effect_adapter_fixtures.py"
)
EFFECT_ADAPTER_PATHS = (
    EFFECT_ADAPTER_PATH,
    EFFECT_ADAPTER_FIXTURES_PATH,
)
REVIEWED_SOURCE_DIGESTS = {
    INITIALIZER_PATH: (
        "6aa6e12ca1ff46265e8ea7233e5d647a491970770246bd98d62ca0e2307a6577"
    ),
    EFFECT_ADAPTER_PATH: (
        "d66a01c7c35bc0015b8d697bae77ca73d8ce23d1d5fd1c77099076bdca21cdaf"
    ),
    EFFECT_ADAPTER_FIXTURES_PATH: (
        "c2c38d1f943eaa0be279ec047fb45bffe4a87a5441f7a9e1234b4260b647fba1"
    ),
}
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
CALLABLE_SURFACE_NAMES = frozenset(
    {"callable", "functiontype", "protocol"}
)
EXPECTED_EFFECT_ADAPTER_API = frozenset(
    {
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
)
EXPECTED_EFFECT_ADAPTER_IMPORTS = {
    "effect_adapter": frozenset(
        {
            "ActionStateSnapshot",
            "ApprovalGrant",
            "DryRunCommitReceipt",
            "DryRunEffectReceipt",
            "DryRunReason",
            "DryRunStatus",
            "EffectAdapterRequest",
            "evaluate_effect_plan",
        }
    ),
    "effect_adapter_fixtures": frozenset(
        {
            "EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION",
            "EffectAdapterFixtureCatalog",
            "EffectAdapterFixtureCase",
            "EffectAdapterFixtureResult",
            "EffectAdapterFixtureValidationError",
            "load_effect_adapter_fixture_catalog",
            "run_effect_adapter_fixture_case",
        }
    ),
}
EFFECT_ADAPTER_MODULES = {
    "effect_adapter": effect_adapter,
    "effect_adapter_fixtures": effect_adapter_fixtures,
}


def _sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_reviewed_digest(path, expected):
    actual = _sha256_file(path)
    if actual != expected:
        raise AssertionError(
            "reviewed source digest mismatch for "
            f"{path.name}: expected {expected}, got {actual}"
        )


def _resolved_import_names(node):
    package = ("email_automation", "claim_pipeline")
    if not node.level:
        if not node.module:
            return ()
        return tuple(
            f"{node.module}.{alias.name}" for alias in node.names
        )
    keep = max(0, len(package) - (node.level - 1))
    base = package[:keep]
    if node.module:
        return (".".join((*base, *node.module.split("."))),)
    return tuple(
        ".".join((*base, alias.name)) for alias in node.names
    )


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
            imported == prefix
            or imported.startswith(f"{prefix}.")
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
        return (
            {normalized} & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
        )
    return (
        _identifier_tokens(name)
        & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
    )


def _decorator_symbol(decorator):
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Name):
        return decorator.id
    if (
        isinstance(decorator, ast.Attribute)
        and isinstance(decorator.value, ast.Name)
    ):
        return f"{decorator.value.id}.{decorator.attr}"
    return None


def _is_dataclass(class_node):
    return any(
        _decorator_symbol(decorator)
        in {"dataclass", "dataclasses.dataclass"}
        for decorator in class_node.decorator_list
    )


def _attribute_name(node, argument):
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == argument
    ):
        return node.attr
    return None


def _is_canonical_sorted_key_lambda(node, parents):
    arguments = node.args
    positional = arguments.posonlyargs + arguments.args
    if (
        len(positional) != 1
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or arguments.kwonlyargs
        or arguments.defaults
        or arguments.kw_defaults
    ):
        return False
    keyword = parents.get(node)
    call = parents.get(keyword)
    if (
        not isinstance(keyword, ast.keyword)
        or keyword.arg != "key"
        or not isinstance(call, ast.Call)
        or not isinstance(call.func, ast.Name)
        or call.func.id != "sorted"
    ):
        return False
    argument = positional[0].arg
    if _attribute_name(node.body, argument) == "grant_id":
        return True
    return (
        isinstance(node.body, ast.Tuple)
        and tuple(
            _attribute_name(item, argument)
            for item in node.body.elts
        )
        == ("sequence", "action_id")
    )


def _schema_key_strings(tree):
    keys = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            targets = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            value = node.value
        elif isinstance(node.target, ast.Name):
            targets = {node.target.id}
            value = node.value
        else:
            continue
        if not any(target.endswith("_KEYS") for target in targets):
            continue
        keys.update(
            child.value
            for child in ast.walk(value)
            if isinstance(child, ast.Constant)
            and isinstance(child.value, str)
        )
    return keys


def _annotation_surface_names(annotation):
    names = set()
    for child in ast.walk(annotation):
        if isinstance(child, ast.Name):
            names.add(child.id.casefold())
        elif isinstance(child, ast.Attribute):
            names.add(child.attr.casefold())
    return names


def _effect_boundary_violations(tree):
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    violations = {
        f"import:{imported}"
        for imported in _import_violations(tree)
    }

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Lambda)
            and not _is_canonical_sorted_key_lambda(node, parents)
        ):
            violations.add(f"lambda:{node.lineno}")
        if isinstance(node, ast.Name):
            forbidden = (
                {node.id.casefold()} & CALLABLE_SURFACE_NAMES
            )
            violations.update(
                f"identifier:{token}" for token in forbidden
            )
        elif isinstance(node, ast.Attribute):
            forbidden = (
                {node.attr.casefold()} & CALLABLE_SURFACE_NAMES
            )
            violations.update(
                f"identifier:{token}" for token in forbidden
            )
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            violations.update(
                f"function:{token}"
                for token in _boundary_surface_tokens(node.name)
            )
            arguments = [
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ]
            if node.args.vararg is not None:
                arguments.append(node.args.vararg)
            if node.args.kwarg is not None:
                arguments.append(node.args.kwarg)
            for argument in arguments:
                violations.update(
                    f"argument:{token}"
                    for token in _boundary_surface_tokens(
                        argument.arg
                    )
                )
        elif isinstance(node, ast.ClassDef):
            violations.update(
                f"class:{token}"
                for token in _boundary_surface_tokens(node.name)
            )
            if not _is_dataclass(node):
                continue
            for field in node.body:
                if (
                    not isinstance(field, ast.AnnAssign)
                    or not isinstance(field.target, ast.Name)
                ):
                    continue
                violations.update(
                    f"dataclass-field:{token}"
                    for token in _boundary_surface_tokens(
                        field.target.id
                    )
                )
                if (
                    _annotation_surface_names(field.annotation)
                    & CALLABLE_SURFACE_NAMES
                ) or isinstance(field.value, ast.Lambda):
                    violations.add(
                        f"function-valued-field:{field.target.id}"
                    )

    for key in _schema_key_strings(tree):
        violations.update(
            f"schema-key:{token}"
            for token in (
                _identifier_tokens(key)
                & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
            )
        )
    return violations


def _private_adapter_declarations():
    names = set()
    for path in EFFECT_ADAPTER_PATHS:
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        names.update(
            node.name
            for node in tree.body
            if isinstance(
                node,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            )
            and node.name.startswith("_")
        )
    return names


def _effect_adapter_import_bindings(tree):
    bindings = []
    for node in tree.body:
        if (
            not isinstance(node, ast.ImportFrom)
            or node.level != 1
            or node.module not in EXPECTED_EFFECT_ADAPTER_IMPORTS
        ):
            continue
        bindings.extend(
            (
                node.module,
                alias.name,
                alias.asname or alias.name,
            )
            for alias in node.names
        )
    return tuple(bindings)


class ClaimPipelineIsolationTests(unittest.TestCase):
    def test_reviewed_effect_sources_are_byte_pinned(self):
        for path, expected in REVIEWED_SOURCE_DIGESTS.items():
            with self.subTest(path=path.name):
                _assert_reviewed_digest(path, expected)

    def test_reviewed_digest_rejects_one_byte_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviewed.py"
            path.write_bytes(b"reviewed source\n")
            expected = _sha256_file(path)
            _assert_reviewed_digest(path, expected)
            path.write_bytes(b"reviewed source!\n")
            with self.assertRaisesRegex(
                AssertionError, "reviewed source digest mismatch"
            ):
                _assert_reviewed_digest(path, expected)

    def test_relative_imports_are_resolved_against_claim_pipeline_package(
        self,
    ):
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
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
            violations.update(_import_violations(tree))

        self.assertEqual(set(), violations)

    def test_effect_adapter_boundary_smoke_gate(self):
        violations = set()
        for path in EFFECT_ADAPTER_PATHS:
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
            violations.update(_effect_boundary_violations(tree))

        self.assertEqual(set(), violations)

    def test_effect_adapter_boundary_smoke_rejects_forbidden_shapes(self):
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

        self.assertIn(
            "import:email_automation.processing", violations
        )
        self.assertIn("identifier:callable", violations)
        self.assertIn("identifier:protocol", violations)
        self.assertIn("dataclass-field:callback", violations)
        self.assertIn(
            "function-valued-field:callback", violations
        )
        self.assertIn("schema-key:callback", violations)
        self.assertTrue(
            any(item.startswith("lambda:") for item in violations)
        )

    def test_effect_adapter_allows_only_canonical_sorted_key_lambdas(
        self,
    ):
        adapter_tree = ast.parse(
            EFFECT_ADAPTER_PATH.read_text(encoding="utf-8"),
            filename=str(EFFECT_ADAPTER_PATH),
        )
        fixture_tree = ast.parse(
            EFFECT_ADAPTER_FIXTURES_PATH.read_text(encoding="utf-8"),
            filename=str(EFFECT_ADAPTER_FIXTURES_PATH),
        )
        adapter_lambdas = [
            node
            for node in ast.walk(adapter_tree)
            if isinstance(node, ast.Lambda)
        ]

        self.assertEqual(2, len(adapter_lambdas))
        self.assertFalse(
            any(
                item.startswith("lambda:")
                for item in _effect_boundary_violations(adapter_tree)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Lambda)
                for node in ast.walk(fixture_tree)
            )
        )

        rejected = {
            "arbitrary": "callback = lambda value: value\n",
            "call": (
                "items = sorted("
                "values, key=lambda item: helper(item))\n"
            ),
            "wrong location": (
                "callback = lambda item: item.grant_id\n"
            ),
            "wrong attribute": (
                "items = sorted("
                "values, key=lambda item: item.callback)\n"
            ),
        }
        for label, source in rejected.items():
            with self.subTest(label=label):
                self.assertTrue(
                    any(
                        item.startswith("lambda:")
                        for item in _effect_boundary_violations(
                            ast.parse(source)
                        )
                    )
                )

    def test_package_import_attempts_only_claim_pipeline_modules(self):
        script = (
            "import json, sys\n"
            "attempts = []\n"
            "class ImportAttemptSentinel:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if (fullname.startswith('email_automation.') "
            "and fullname != 'email_automation.claim_pipeline' "
            "and not fullname.startswith("
            "'email_automation.claim_pipeline.')):\n"
            "            attempts.append(fullname)\n"
            "        return None\n"
            "sentinel = ImportAttemptSentinel()\n"
            "sys.meta_path.insert(0, sentinel)\n"
            "from email_automation import claim_pipeline\n"
            "package_attempts = list(attempts)\n"
            "probe = 'email_automation._claim_pipeline_probe'\n"
            "try:\n"
            "    __import__(probe)\n"
            "except ImportError:\n"
            "    pass\n"
            "print(json.dumps({"
            "'attempts': package_attempts, "
            "'probeSeen': probe in attempts}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)

        self.assertEqual([], result["attempts"])
        self.assertTrue(result["probeSeen"])

    def test_effect_adapter_api_is_exact_and_identity_bound(self):
        initializer_tree = ast.parse(
            INITIALIZER_PATH.read_text(encoding="utf-8"),
            filename=str(INITIALIZER_PATH),
        )
        bindings = _effect_adapter_import_bindings(initializer_tree)
        bound_names = {
            bound_name for _, _, bound_name in bindings
        }

        self.assertEqual(
            EXPECTED_EFFECT_ADAPTER_API, bound_names
        )
        self.assertLessEqual(
            EXPECTED_EFFECT_ADAPTER_API,
            set(claim_pipeline.__all__),
        )
        for module_name, source_name, bound_name in bindings:
            with self.subTest(name=bound_name):
                self.assertFalse(source_name.startswith("_"))
                self.assertIs(
                    getattr(claim_pipeline, bound_name),
                    getattr(
                        EFFECT_ADAPTER_MODULES[module_name],
                        source_name,
                    ),
                )

    def test_private_adapter_declarations_are_not_exported(self):
        exported_names = set(claim_pipeline.__all__)

        self.assertEqual(
            set(),
            {
                name
                for name in _private_adapter_declarations()
                if name in exported_names
                or hasattr(claim_pipeline, name)
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
            {
                name
                for name in expected_names
                if not hasattr(claim_pipeline, name)
            },
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
            {
                name
                for name in expected_names
                if not hasattr(claim_pipeline, name)
            },
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
            {
                name
                for name in expected_names
                if not hasattr(claim_pipeline, name)
            },
        )

    def test_provider_policy_shadow_api_is_exposed_at_package_boundary(
        self,
    ):
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
            {
                name
                for name in expected_names
                if not hasattr(claim_pipeline, name)
            },
        )


if __name__ == "__main__":
    unittest.main()
