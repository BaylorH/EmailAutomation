import ast
import builtins
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
EXPECTED_PACKAGE_API = frozenset(
    """
ActionPlan
ActionStateSnapshot
ActionType
Actor
ActorRole
ApprovalGrant
ApprovalClass
CampaignContract
Claim
CLAIM_EXTRACTION_SCHEMA_VERSION
CLAIM_FIXTURE_SCHEMA_VERSION
ClaimExtractionIssue
ClaimExtractionRequest
ClaimExtractionResult
ClaimFixtureCase
ClaimFixtureCatalog
ClaimFixtureValidationError
ClaimConflict
ClaimModality
ClaimPolarity
ClaimPredicate
ClaimPipelineMode
CommitReceipt
CompletenessState
ConversationState
ContractAuthority
ContractViolation
DecisionSnapshot
Direction
DryRunCommitReceipt
DryRunEffectReceipt
DryRunReason
DryRunStatus
EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION
EffectReceipt
EffectStatus
EffectAdapterFixtureCase
EffectAdapterFixtureCatalog
EffectAdapterFixtureResult
EffectAdapterFixtureValidationError
EffectAdapterRequest
EntityRef
EntityPolicyResult
EntityMatch
EntityResolutionResult
EntitySeed
EntityType
EvidenceEnvelope
EvidenceFailure
EvidenceFreshness
EvidenceNormalizationResult
EvidenceSource
ExternalEvidenceInput
ExecutionScope
FIXTURE_SCHEMA_VERSION
FitState
FixtureCase
FixtureCatalog
FixtureValidationError
INTERPRETATION_FIXTURE_SCHEMA_VERSION
LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION
LegacyActionAttempt
LegacyProjection
LegacyShadowCaseResult
LegacyShadowDiscrepancy
LegacyShadowFixtureCase
LegacyShadowFixtureCatalog
LegacyShadowFixtureValidationError
LegacyShadowIdentity
LegacyShadowReport
InterpretationFixtureCase
InterpretationFixtureCatalog
InterpretationFixtureValidationError
InterpretationReplayResult
MarketState
MAX_REPLAY_CALLS
MAX_REPLAY_REPEATS
PlannedAction
PINNED_MODEL_ID
PINNED_PROMPT_HASH
PINNED_PROMPT_ID
PINNED_PROVIDER_ID
POLICY_FIXTURE_SCHEMA_VERSION
POLICY_REASON_CODES
PolicyEvaluationRequest
PolicyEvaluationResult
PolicyFixtureCase
PolicyFixtureCatalog
PolicyFixtureValidationError
PipelineGate
PipelineScope
ProposalAdapter
ProposalResponse
ProposalUsage
PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION
PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION
PROVIDER_POLICY_SHADOW_PROFILE
BudgetedProviderTransport
ProviderBudgetExceeded
ProviderBudgetLimits
ProviderPolicyFixtureCase
ProviderPolicyFixtureCatalog
ProviderPolicyFixtureValidationError
ProviderPolicyShadowCaseResult
ProviderPolicyShadowIdentity
ProviderPolicyShadowReport
ProviderReservationSnapshot
PinnedProviderProposalAdapter
ProviderTransportResult
ProviderQualityFixtureCase
ProviderQualityFixtureCatalog
ProviderQualityFixtureValidationError
ProviderReviewExpectation
RECORDED_MODEL_ID
RECORDED_PROMPT_HASH
RECORDED_PROMPT_ID
RECORDED_PROVIDER_ID
REQUIRED_DIMENSIONS
REQUIRED_POLICY_DIMENSIONS
SUPPORTED_REVIEW_CATEGORIES
SUPPORTED_PROVIDER_POLICY_GAPS
RawMessageEvidence
RecordedProposalAdapter
RecordedProviderQualityProposalAdapter
ReplayCaseResult
ReplayIdentity
ReplayReport
ResolutionIssue
canonicalize_address
compare_legacy_case
build_claim_extraction_request
extract_claims
extract_addresses
extract_suites
evaluate_effect_plan
evaluate_policy
load_effect_adapter_fixture_catalog
load_fixture_catalog
load_interpretation_fixture_catalog
load_legacy_shadow_fixture_catalog
load_claim_fixture_catalog
load_provider_quality_fixture_catalog
load_provider_policy_fixture_catalog
load_policy_fixture_catalog
normalize_message_evidence
parse_pipeline_mode
project_legacy_proposal
resolve_entities
run_claim_replay
run_effect_adapter_fixture_case
run_provider_policy_shadow
run_legacy_shadow
validate_action_plan
validate_claim_bundle
validate_decision
select_provider_policy_cases
""".split()
)
EXPECTED_PACKAGE_IMPORTS = {
    "claim_fixtures": """
        CLAIM_FIXTURE_SCHEMA_VERSION ClaimFixtureCase ClaimFixtureCatalog
        ClaimFixtureValidationError load_claim_fixture_catalog
    """.split(),
    "contracts": """
        ActionPlan ActionType Actor ActorRole ApprovalClass CampaignContract
        Claim ClaimModality ClaimPolarity ClaimPredicate CommitReceipt
        CompletenessState ConversationState ContractAuthority DecisionSnapshot
        Direction EffectReceipt EffectStatus EntityRef EntityType
        EvidenceEnvelope EvidenceFreshness EvidenceSource ExecutionScope
        FitState MarketState PlannedAction
    """.split(),
    "effect_adapter": """
        ActionStateSnapshot ApprovalGrant DryRunCommitReceipt
        DryRunEffectReceipt DryRunReason DryRunStatus EffectAdapterRequest
        evaluate_effect_plan
    """.split(),
    "effect_adapter_fixtures": """
        EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION EffectAdapterFixtureCase
        EffectAdapterFixtureCatalog EffectAdapterFixtureResult
        EffectAdapterFixtureValidationError load_effect_adapter_fixture_catalog
        run_effect_adapter_fixture_case
    """.split(),
    "entities": """
        EntityMatch EntityResolutionResult EntitySeed ResolutionIssue
        canonicalize_address extract_addresses extract_suites resolve_entities
    """.split(),
    "evidence": """
        EvidenceFailure EvidenceNormalizationResult ExternalEvidenceInput
        RawMessageEvidence normalize_message_evidence
    """.split(),
    "extraction": """
        CLAIM_EXTRACTION_SCHEMA_VERSION ClaimExtractionIssue
        ClaimExtractionRequest ClaimExtractionResult
        build_claim_extraction_request extract_claims
    """.split(),
    "fixtures": """
        FIXTURE_SCHEMA_VERSION REQUIRED_DIMENSIONS FixtureCase FixtureCatalog
        FixtureValidationError load_fixture_catalog
    """.split(),
    "interpretation_fixtures": """
        INTERPRETATION_FIXTURE_SCHEMA_VERSION InterpretationFixtureCase
        InterpretationFixtureCatalog InterpretationFixtureValidationError
        load_interpretation_fixture_catalog
    """.split(),
    "legacy_shadow": """
        LegacyActionAttempt LegacyProjection LegacyShadowCaseResult
        LegacyShadowDiscrepancy LegacyShadowIdentity LegacyShadowReport
        compare_legacy_case project_legacy_proposal run_legacy_shadow
    """.split(),
    "legacy_shadow_fixtures": """
        LEGACY_SHADOW_FIXTURE_SCHEMA_VERSION LegacyShadowFixtureCase
        LegacyShadowFixtureCatalog LegacyShadowFixtureValidationError
        load_legacy_shadow_fixture_catalog
    """.split(),
    "mode": """
        ClaimPipelineMode PipelineGate PipelineScope parse_pipeline_mode
    """.split(),
    "policy": """
        ClaimConflict EntityPolicyResult PolicyEvaluationRequest
        PolicyEvaluationResult evaluate_policy
    """.split(),
    "policy_fixtures": """
        POLICY_FIXTURE_SCHEMA_VERSION POLICY_REASON_CODES
        REQUIRED_POLICY_DIMENSIONS PolicyFixtureCase PolicyFixtureCatalog
        PolicyFixtureValidationError load_policy_fixture_catalog
    """.split(),
    "provider_policy_fixtures": """
        PROVIDER_POLICY_FIXTURE_SCHEMA_VERSION SUPPORTED_PROVIDER_POLICY_GAPS
        ProviderPolicyFixtureCase ProviderPolicyFixtureCatalog
        ProviderPolicyFixtureValidationError load_provider_policy_fixture_catalog
    """.split(),
    "provider_policy_shadow": """
        PROVIDER_POLICY_SHADOW_PROFILE BudgetedProviderTransport
        ProviderBudgetExceeded ProviderBudgetLimits
        ProviderPolicyShadowCaseResult ProviderPolicyShadowIdentity
        ProviderPolicyShadowReport ProviderReservationSnapshot
        RecordedProviderQualityProposalAdapter run_provider_policy_shadow
        select_provider_policy_cases
    """.split(),
    "provider_quality_fixtures": """
        PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION SUPPORTED_REVIEW_CATEGORIES
        ProviderQualityFixtureCase ProviderQualityFixtureCatalog
        ProviderQualityFixtureValidationError ProviderReviewExpectation
        load_provider_quality_fixture_catalog
    """.split(),
    "provider_replay": """
        PINNED_MODEL_ID PINNED_PROMPT_HASH PINNED_PROMPT_ID PINNED_PROVIDER_ID
        PinnedProviderProposalAdapter ProviderTransportResult
    """.split(),
    "replay": """
        InterpretationReplayResult MAX_REPLAY_CALLS MAX_REPLAY_REPEATS
        ProposalAdapter ProposalResponse ProposalUsage RECORDED_MODEL_ID
        RECORDED_PROMPT_HASH RECORDED_PROMPT_ID RECORDED_PROVIDER_ID
        RecordedProposalAdapter ReplayCaseResult ReplayIdentity ReplayReport
        run_claim_replay
    """.split(),
    "validation": """
        ContractViolation validate_action_plan validate_claim_bundle
        validate_decision
    """.split(),
}
EXPECTED_PACKAGE_BINDING_PROVENANCE = {
    name: f"email_automation.claim_pipeline.{module}.{name}"
    for module, names in EXPECTED_PACKAGE_IMPORTS.items()
    for name in names
}
EFFECT_ADAPTER_MODULES = frozenset(
    {
        "email_automation.claim_pipeline.effect_adapter",
        "email_automation.claim_pipeline.effect_adapter_fixtures",
    }
)
FORBIDDEN_FUNCTION_SYMBOLS = {
    "collections.abc.Callable": "callable",
    "types.FunctionType": "functiontype",
    "typing.Callable": "callable",
    "typing.Protocol": "protocol",
}
DYNAMIC_IMPORT_SYMBOLS = {
    "__import__": "__import__",
    "builtins.__import__": "__import__",
    "importlib.import_module": "importlib.import_module",
}
SCHEMA_DECLARATION_TOKENS = frozenset(
    {"field", "fields", "key", "keys", "schema"}
)
BUILTIN_CALLABLE_NAMES = frozenset(
    name
    for name in dir(builtins)
    if callable(getattr(builtins, name))
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


def _resolved_symbol(node, bindings):
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolved_symbol(node.value, bindings)
        if owner is not None:
            return f"{owner}.{node.attr}"
    if (
        isinstance(node, ast.Call)
        and _resolved_symbol(node.func, bindings) in {"getattr", "builtins.getattr"}
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        owner = _resolved_symbol(node.args[0], bindings)
        if owner in {"__builtins__", "builtins"}:
            return f"builtins.{node.args[1].value}"
    return None


def _imported_bindings(node):
    bindings = {}
    imported_symbols = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            bindings[bound_name] = alias.name
            imported_symbols.add(alias.name)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            module = (
                _resolved_import_names(node)[0]
                if node.level
                else node.module
            )
            for alias in node.names:
                resolved = f"{module}.{alias.name}"
                bindings[alias.asname or alias.name] = resolved
                imported_symbols.add(resolved)
        else:
            for alias, resolved in zip(
                node.names, _resolved_import_names(node), strict=True
            ):
                bindings[alias.asname or alias.name] = resolved
                imported_symbols.add(resolved)
    return bindings, imported_symbols


def _binding_environments(tree):
    bindings_by_node = {}
    imported_by_node = {}
    callables_by_node = {}

    def record(node, bindings, imported_symbols, callable_symbols):
        for child in ast.walk(node):
            bindings_by_node[child] = dict(bindings)
            imported_by_node[child] = frozenset(imported_symbols)
            callables_by_node[child] = frozenset(callable_symbols)

    def walk_scope(statements, inherited, imported, callables):
        bindings = dict(inherited)
        imported_symbols = set(imported)
        callable_symbols = set(callables)
        for statement in statements:
            record(
                statement,
                bindings,
                imported_symbols,
                callable_symbols,
            )
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                walk_scope(
                    statement.body,
                    bindings,
                    imported_symbols,
                    callable_symbols,
                )

            new_bindings, new_imports = _imported_bindings(statement)
            bindings.update(new_bindings)
            imported_symbols.update(new_imports)
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                symbol = f"<callable>:{statement.lineno}:{statement.name}"
                bindings[statement.name] = symbol
                callable_symbols.add(symbol)
            elif isinstance(statement, ast.ClassDef):
                bindings[statement.name] = (
                    f"<class>:{statement.lineno}:{statement.name}"
                )
            elif isinstance(statement, ast.Assign):
                resolved = _resolved_symbol(statement.value, bindings)
                for target in statement.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if resolved is None:
                        bindings.pop(target.id, None)
                    else:
                        bindings[target.id] = resolved
            elif isinstance(statement, ast.AnnAssign) and isinstance(
                statement.target, ast.Name
            ):
                resolved = _resolved_symbol(statement.value, bindings)
                if resolved is None:
                    bindings.pop(statement.target.id, None)
                else:
                    bindings[statement.target.id] = resolved

    record(tree, {}, set(), set())
    walk_scope(tree.body, {}, set(), set())
    return bindings_by_node, imported_by_node, callables_by_node


def _function_surface_token(node, bindings):
    resolved = _resolved_symbol(node, bindings)
    if resolved in FORBIDDEN_FUNCTION_SYMBOLS:
        return FORBIDDEN_FUNCTION_SYMBOLS[resolved]
    if isinstance(node, ast.Name):
        raw_name = node.id.casefold()
    elif isinstance(node, ast.Attribute):
        raw_name = node.attr.casefold()
    else:
        return None
    return {
        "callable": "callable",
        "functiontype": "functiontype",
        "protocol": "protocol",
    }.get(raw_name)


def _contains_function_surface(node, bindings):
    return node is not None and any(
        _function_surface_token(child, bindings) is not None
        for child in ast.walk(node)
    )


def _is_callable_reference(
    node,
    bindings,
    callable_symbols,
    imported_symbols,
):
    resolved = _resolved_symbol(node, bindings)
    if resolved in callable_symbols or resolved in BUILTIN_CALLABLE_NAMES:
        return True
    if resolved is not None and any(
        resolved == imported or resolved.startswith(f"{imported}.")
        for imported in imported_symbols
    ):
        return True
    return (
        resolved is not None
        and resolved.startswith("builtins.")
        and resolved.rsplit(".", 1)[-1] in BUILTIN_CALLABLE_NAMES
    )


def _function_defaults(node):
    positional = [*node.args.posonlyargs, *node.args.args]
    if node.args.defaults:
        for argument, default in zip(
            positional[-len(node.args.defaults):],
            node.args.defaults,
            strict=True,
        ):
            yield argument, default
    for argument, default in zip(
        node.args.kwonlyargs,
        node.args.kw_defaults,
        strict=True,
    ):
        if default is not None:
            yield argument, default


def _function_body_nodes(node):
    pending = list(reversed(node.body))
    while pending:
        child = pending.pop()
        yield child
        if isinstance(
            child,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
            ),
        ):
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(child))))


def _dataclass_default_expressions(value, bindings):
    if value is None:
        return ()
    if (
        isinstance(value, ast.Call)
        and _resolved_symbol(value.func, bindings) == "dataclasses.field"
    ):
        return tuple(
            keyword.value
            for keyword in value.keywords
            if keyword.arg in {"default", "default_factory"}
        )
    return (value,)


def _is_dataclass(class_node, bindings):
    return any(
        _resolved_symbol(
            decorator.func if isinstance(decorator, ast.Call) else decorator,
            bindings,
        )
        in {"dataclass", "dataclasses.dataclass"}
        for decorator in class_node.decorator_list
    )


def _schema_literal_keys(value):
    if isinstance(value, ast.Dict):
        keys = set()
        for key, item in zip(value.keys, value.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            keys.update(_schema_literal_keys(item))
        return keys
    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        keys = set()
        for item in value.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                keys.add(item.value)
            else:
                keys.update(_schema_literal_keys(item))
        return keys
    if isinstance(value, ast.Call):
        keys = set()
        for argument in value.args:
            keys.update(_schema_literal_keys(argument))
        for keyword in value.keywords:
            if keyword.arg is not None:
                keys.add(keyword.arg)
            keys.update(_schema_literal_keys(keyword.value))
        return keys
    return set()


def _schema_key_strings(tree):
    keys = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = {
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target, ast.Name
        ):
            targets = {node.target.id}
            value = node.value
        else:
            continue
        if value is None or not any(
            _identifier_tokens(target) & SCHEMA_DECLARATION_TOKENS
            for target in targets
        ):
            continue
        keys.update(_schema_literal_keys(value))
    return keys


def _effect_boundary_violations(tree):
    (
        bindings_by_node,
        imported_by_node,
        callables_by_node,
    ) = _binding_environments(tree)
    violations = set()

    for imported in _import_violations(tree):
        violations.add(f"import:{imported}")

    for node in ast.walk(tree):
        bindings = bindings_by_node.get(node, {})
        imported_symbols = imported_by_node.get(node, frozenset())
        callable_symbols = callables_by_node.get(node, frozenset())
        if isinstance(node, ast.Lambda):
            violations.add(f"lambda:{node.lineno}")
        if isinstance(node, (ast.Name, ast.Attribute)):
            function_token = _function_surface_token(node, bindings)
            if function_token is not None:
                violations.add(f"identifier:{function_token}")
            dynamic_import = DYNAMIC_IMPORT_SYMBOLS.get(
                _resolved_symbol(node, bindings)
            )
            if dynamic_import is not None:
                violations.add(f"dynamic-import:{dynamic_import}")
        if isinstance(node, ast.Call):
            for candidate in (node.func, node):
                dynamic_import = DYNAMIC_IMPORT_SYMBOLS.get(
                    _resolved_symbol(candidate, bindings)
                )
                if dynamic_import is not None:
                    violations.add(f"dynamic-import:{dynamic_import}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            forbidden = _boundary_surface_tokens(node.name)
            violations.update(f"function:{token}" for token in forbidden)
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
                forbidden = _boundary_surface_tokens(argument.arg)
                violations.update(f"argument:{token}" for token in forbidden)
            for argument, default in _function_defaults(node):
                if _is_callable_reference(
                    default,
                    bindings,
                    callable_symbols,
                    imported_symbols,
                ):
                    violations.add(
                        f"function-valued-default:{argument.arg}"
                    )
            parameter_names = {argument.arg for argument in arguments}
            class_method = any(
                _resolved_symbol(
                    decorator.func
                    if isinstance(decorator, ast.Call)
                    else decorator,
                    bindings,
                )
                in {"builtins.classmethod", "classmethod"}
                for decorator in node.decorator_list
            )
            safe_invoked_parameters = {
                argument.arg
                for argument in arguments
                if (argument.arg == "cls" and class_method)
                or _resolved_symbol(argument.annotation, bindings)
                in {"builtins.type", "type"}
            }
            for child in _function_body_nodes(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id in parameter_names
                    and child.func.id not in safe_invoked_parameters
                ):
                    violations.add(
                        f"callable-parameter:{child.func.id}"
                    )
        elif isinstance(node, ast.ClassDef):
            forbidden = _boundary_surface_tokens(node.name)
            violations.update(f"class:{token}" for token in forbidden)
            if not _is_dataclass(node, bindings):
                continue
            for field in node.body:
                if not isinstance(field, ast.AnnAssign) or not isinstance(
                    field.target, ast.Name
                ):
                    continue
                field_bindings = bindings_by_node.get(field, bindings)
                field_imports = imported_by_node.get(
                    field, imported_symbols
                )
                field_callables = callables_by_node.get(
                    field, callable_symbols
                )
                forbidden = _boundary_surface_tokens(field.target.id)
                violations.update(f"dataclass-field:{token}" for token in forbidden)
                if _contains_function_surface(
                    field.annotation, field_bindings
                ) or _contains_function_surface(
                    field.value, field_bindings
                ):
                    violations.add(f"function-valued-field:{field.target.id}")
                if any(
                    _is_callable_reference(
                        default,
                        field_bindings,
                        field_callables,
                        field_imports,
                    )
                    for default in _dataclass_default_expressions(
                        field.value, field_bindings
                    )
                ):
                    violations.add(f"function-valued-field:{field.target.id}")

    for key in _schema_key_strings(tree):
        forbidden = _identifier_tokens(key) & FORBIDDEN_EFFECT_BOUNDARY_TOKENS
        violations.update(f"schema-key:{token}" for token in forbidden)

    return violations


def _assigns_all(node):
    return isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "__all__"
        for target in node.targets
    )


def _literal_all_names(tree):
    assignments = [
        node
        for node in tree.body
        if _assigns_all(node)
    ]
    if len(assignments) != 1:
        return set(), False
    assignment = assignments[0]
    if not isinstance(assignment.value, (ast.List, ast.Tuple)):
        return set(), False
    if not all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in assignment.value.elts
    ):
        return set(), False
    values = tuple(item.value for item in assignment.value.elts)
    if len(values) != len(set(values)):
        return set(values), False
    for node in tree.body:
        if node is assignment:
            continue
        if any(
            isinstance(child, ast.Name) and child.id == "__all__"
            for child in ast.walk(node)
        ):
            return set(values), False
    return set(values), True


def _target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_target_names(item))
        return names
    return set()


def _package_public_bindings(tree):
    bindings = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.add(node.name)
        elif isinstance(node, ast.Import):
            bindings.update(
                alias.asname or alias.name.split(".", 1)[0]
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            bindings.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bindings.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            bindings.update(_target_names(node.target))
    return {
        name
        for name in bindings
        if name != "__all__" and not name.startswith("_")
    }


def _package_binding_provenance(tree):
    bindings = {}
    for node in tree.body:
        imported, _ = _imported_bindings(node)
        bindings.update(imported)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings[node.name] = f"<local-function>.{node.name}"
        elif isinstance(node, ast.ClassDef):
            bindings[node.name] = f"<local-class>.{node.name}"
        elif isinstance(node, ast.Assign):
            resolved = _resolved_symbol(node.value, bindings)
            provenance = resolved or f"<{type(node.value).__name__}>"
            for target in node.targets:
                for name in _target_names(target):
                    bindings[name] = provenance
        elif isinstance(node, ast.AnnAssign):
            resolved = _resolved_symbol(node.value, bindings)
            provenance = resolved or f"<{type(node.value).__name__}>"
            for name in _target_names(node.target):
                bindings[name] = provenance
    return {
        name: provenance
        for name, provenance in bindings.items()
        if name != "__all__" and not name.startswith("_")
    }


def _effect_adapter_package_imports(tree):
    imports = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved_modules = _resolved_import_names(node)
        if len(resolved_modules) != 1 or resolved_modules[0] not in EFFECT_ADAPTER_MODULES:
            continue
        imports.update(
            (alias.name, alias.asname or alias.name)
            for alias in node.names
        )
    return imports


def _effect_adapter_api_violations(tree):
    imports = _effect_adapter_package_imports(tree)
    bound_public_names = {
        bound_name
        for source_name, bound_name in imports
        if not bound_name.startswith("_")
    }
    exported_names, literal_exports = _literal_all_names(tree)
    package_bindings = _package_public_bindings(tree)
    package_provenance = _package_binding_provenance(tree)
    violations = {
        f"private-source:{source_name}->{bound_name}"
        for source_name, bound_name in imports
        if source_name.startswith("_")
    }
    violations.update(
        f"missing-bound:{name}"
        for name in EXPECTED_EFFECT_ADAPTER_API - bound_public_names
    )
    violations.update(
        f"unexpected-bound:{name}"
        for name in bound_public_names - EXPECTED_EFFECT_ADAPTER_API
    )
    if not literal_exports:
        violations.add("dynamic-package-exports")
        return violations
    violations.update(
        f"missing-export:{name}"
        for name in EXPECTED_EFFECT_ADAPTER_API - exported_names
    )
    violations.update(
        f"package-export-without-binding:{name}"
        for name in exported_names - package_bindings
    )
    violations.update(
        f"package-binding-without-export:{name}"
        for name in package_bindings - exported_names
    )
    violations.update(
        f"unexpected-package-api:{name}"
        for name in (exported_names | package_bindings) - EXPECTED_PACKAGE_API
    )
    violations.update(
        f"missing-package-export:{name}"
        for name in EXPECTED_PACKAGE_API - exported_names
    )
    violations.update(
        f"missing-package-binding:{name}"
        for name in EXPECTED_PACKAGE_API - package_bindings
    )
    violations.update(
        f"rebound-package-api:{name}"
        for name, expected in EXPECTED_PACKAGE_BINDING_PROVENANCE.items()
        if name in package_provenance
        and package_provenance[name] != expected
    )
    return violations


class ClaimPipelineIsolationTests(unittest.TestCase):
    def test_forbidden_effect_boundary_tokens_are_closed(self):
        self.assertEqual(
            frozenset(
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
            ),
            FORBIDDEN_EFFECT_BOUNDARY_TOKENS,
        )

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

    def test_effect_adapter_boundary_scanner_rejects_type_aliases(self):
        tree = ast.parse(
            "from typing import Callable as Fn, Protocol as P\n"
            "from types import FunctionType as FT\n"
            "import typing as type_defs\n"
            "import types as runtime_types\n"
            "from dataclasses import dataclass as record\n"
            "import dataclasses as records\n"
            "class Boundary(P):\n"
            "    pass\n"
            "@record(frozen=True)\n"
            "class FirstRequest:\n"
            "    worker: Fn\n"
            "    runner: FT = FT\n"
            "@records.dataclass\n"
            "class SecondRequest:\n"
            "    sender: type_defs.Callable = runtime_types.FunctionType\n"
        )

        violations = _effect_boundary_violations(tree)

        self.assertIn("identifier:callable", violations)
        self.assertIn("identifier:protocol", violations)
        self.assertIn("identifier:functiontype", violations)
        self.assertIn("function-valued-field:worker", violations)
        self.assertIn("function-valued-field:runner", violations)
        self.assertIn("function-valued-field:sender", violations)

    def test_effect_adapter_boundary_scanner_resolves_chained_dataclass_aliases(
        self,
    ):
        tree = ast.parse(
            "from dataclasses import dataclass\n"
            "from typing import Callable as Fn\n"
            "record = dataclass\n"
            "@record\n"
            "class Request:\n"
            "    worker: Fn\n"
        )

        self.assertIn(
            "function-valued-field:worker",
            _effect_boundary_violations(tree),
        )

    def test_effect_adapter_boundary_scanner_retains_dangerous_alias_history(
        self,
    ):
        tree = ast.parse(
            "from dataclasses import dataclass\n"
            "record = dataclass\n"
            "@record\n"
            "class Request:\n"
            "    graph: object\n"
            "record = object\n"
        )

        self.assertIn(
            "dataclass-field:graph",
            _effect_boundary_violations(tree),
        )

    def test_effect_adapter_boundary_scanner_keeps_aliases_in_lexical_scope(
        self,
    ):
        unrelated_local_alias = ast.parse(
            "from dataclasses import dataclass\n"
            "def unrelated():\n"
            "    safe_decorator = dataclass\n"
            "@safe_decorator\n"
            "class Request:\n"
            "    graph: object\n"
        )
        nested_alias = ast.parse(
            "from dataclasses import dataclass\n"
            "def build_request():\n"
            "    record = dataclass\n"
            "    @record\n"
            "    class Request:\n"
            "        graph: object\n"
        )

        self.assertNotIn(
            "dataclass-field:graph",
            _effect_boundary_violations(unrelated_local_alias),
        )
        self.assertIn(
            "dataclass-field:graph",
            _effect_boundary_violations(nested_alias),
        )

    def test_effect_adapter_boundary_scanner_rejects_schema_declaration_bypasses(
        self,
    ):
        cases = {
            "annotated keys": (
                "_ROOT_KEYS: frozenset[str] = frozenset({'callback'})\n"
            ),
            "schema declaration": "SCHEMA = frozenset({'callback'})\n",
            "field declaration": (
                "MESSAGE_FIELDS = frozenset({'callback'})\n"
            ),
        }

        for label, source in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    "schema-key:callback",
                    _effect_boundary_violations(ast.parse(source)),
                )

        ordinary_constants = ast.parse(
            "DESCRIPTION = 'callback service prose'\n"
            "STATUS_VALUES = frozenset({'callback'})\n"
        )
        self.assertNotIn(
            "schema-key:callback",
            _effect_boundary_violations(ordinary_constants),
        )
        mapping_schema = ast.parse(
            "SCHEMA = {'description': 'graph service prose'}\n"
        )
        mapping_violations = _effect_boundary_violations(mapping_schema)
        self.assertNotIn("schema-key:graph", mapping_violations)
        self.assertNotIn("schema-key:service", mapping_violations)
        self.assertIn(
            "schema-key:callback",
            _effect_boundary_violations(
                ast.parse("SCHEMA = {'callback': 'ordinary prose'}\n")
            ),
        )
        nested_schema_cases = {
            "properties mapping": (
                "SCHEMA = {'properties': {'callback': {}}}\n",
                "schema-key:callback",
            ),
            "nested field collection": (
                "SCHEMA_FIELDS = {'request': {'graph'}}\n",
                "schema-key:graph",
            ),
        }
        for label, (source, expected) in nested_schema_cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

    def test_effect_adapter_boundary_scanner_rejects_named_callable_defaults(
        self,
    ):
        cases = {
            "local function default": (
                "def helper():\n"
                "    pass\n"
                "def execute(value=helper):\n"
                "    pass\n",
                "function-valued-default:value",
            ),
            "builtin function default": (
                "def execute(ordering=sorted):\n"
                "    pass\n",
                "function-valued-default:ordering",
            ),
            "dataclass local default": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = field(default=helper)\n",
                "function-valued-field:worker",
            ),
            "dataclass builtin default factory": (
                "from dataclasses import dataclass, field\n"
                "@dataclass\n"
                "class Request:\n"
                "    ordering: object = field(default_factory=sorted)\n",
                "function-valued-field:ordering",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

    def test_effect_adapter_boundary_scanner_rejects_imported_callable_injection(
        self,
    ):
        cases = {
            "function default": (
                "from .validation import validate_action_plan\n"
                "def run(fn=validate_action_plan):\n"
                "    pass\n",
                "function-valued-default:fn",
            ),
            "dataclass default": (
                "from dataclasses import dataclass, field\n"
                "from .validation import validate_action_plan\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = field(default=validate_action_plan)\n",
                "function-valued-field:worker",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

    def test_effect_adapter_boundary_scanner_rejects_invoked_parameters(self):
        invoked_parameter = ast.parse(
            "def run(fn):\n"
            "    return fn()\n"
        )
        ordinary_pure_call = ast.parse(
            "def run(values):\n"
            "    return sorted(values)\n"
        )
        disguised_parameter = ast.parse(
            "def run(cls):\n"
            "    return cls()\n"
        )
        declared_type = ast.parse(
            "def parse(enum_type: type):\n"
            "    return enum_type('value')\n"
        )

        self.assertIn(
            "callable-parameter:fn",
            _effect_boundary_violations(invoked_parameter),
        )
        self.assertIn(
            "callable-parameter:cls",
            _effect_boundary_violations(disguised_parameter),
        )
        self.assertNotIn(
            "callable-parameter:values",
            _effect_boundary_violations(ordinary_pure_call),
        )
        self.assertNotIn(
            "callable-parameter:enum_type",
            _effect_boundary_violations(declared_type),
        )

    def test_effect_adapter_boundary_scanner_retains_callable_alias_history(
        self,
    ):
        cases = {
            "function default alias": (
                "def helper():\n"
                "    pass\n"
                "callback = helper\n"
                "def run(fn=callback):\n"
                "    pass\n"
                "callback = None\n",
                "function-valued-default:fn",
            ),
            "field alias": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "make_field = field\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = make_field(default=helper)\n"
                "make_field = object\n",
                "function-valued-field:worker",
            ),
            "field factory alias": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "make_field = field\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = make_field(default_factory=helper)\n"
                "make_field = object\n",
                "function-valued-field:worker",
            ),
            "field default callable alias": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "callback = helper\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = field(default=callback)\n"
                "callback = None\n",
                "function-valued-field:worker",
            ),
            "field factory callable alias": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "callback = helper\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = field(default_factory=callback)\n"
                "callback = None\n",
                "function-valued-field:worker",
            ),
            "imported callable alias": (
                "from .validation import validate_action_plan\n"
                "callback = validate_action_plan\n"
                "def run(fn=callback):\n"
                "    pass\n"
                "callback = None\n",
                "function-valued-default:fn",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

    def test_effect_adapter_boundary_scanner_checks_every_parameter_kind(self):
        tree = ast.parse(
            "def configure(service, /, client, *graph, transport, **repository):\n"
            "    pass\n"
        )

        violations = _effect_boundary_violations(tree)

        self.assertIn("argument:service", violations)
        self.assertIn("argument:client", violations)
        self.assertIn("argument:graph", violations)
        self.assertIn("argument:transport", violations)
        self.assertIn("argument:repository", violations)

    def test_effect_adapter_boundary_scanner_rejects_dynamic_imports(self):
        cases = {
            "direct builtin": (
                "direct = __import__('email_automation.processing')\n",
                "dynamic-import:__import__",
            ),
            "aliased builtin": (
                "from builtins import __import__ as load_builtin\n"
                "loaded = load_builtin('email_automation.processing')\n",
                "dynamic-import:__import__",
            ),
            "module attribute": (
                "import importlib\n"
                "loaded = importlib.import_module('email_automation.processing')\n",
                "dynamic-import:importlib.import_module",
            ),
            "aliased module attribute": (
                "import importlib as loader\n"
                "loaded = loader.import_module('email_automation.processing')\n",
                "dynamic-import:importlib.import_module",
            ),
            "aliased imported function": (
                "from importlib import import_module as load\n"
                "loaded = load('email_automation.processing')\n",
                "dynamic-import:importlib.import_module",
            ),
            "rebound builtin": (
                "load = __import__\n"
                "loaded = load('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "rebound imported function": (
                "from importlib import import_module\n"
                "load = import_module\n"
                "loaded = load('email_automation.messaging')\n",
                "dynamic-import:importlib.import_module",
            ),
            "getattr builtin": (
                "load = getattr(__builtins__, '__import__')\n"
                "loaded = load('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "stored builtin tuple": (
                "loads = (__import__,)\n"
                "loaded = loads[0]('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

    def test_effect_adapter_files_reject_every_lambda(self):
        lambdas = set()
        for path in EFFECT_ADAPTER_PATHS:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            lambdas.update(
                f"{path.name}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Lambda)
            )

        self.assertEqual(set(), lambdas)

    def test_package_import_loads_only_claim_pipeline_modules(self):
        script = (
            "import json, sys\n"
            "attempts = []\n"
            "class ImportAttemptSentinel:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if (fullname.startswith('email_automation.') "
            "and fullname != 'email_automation.claim_pipeline' "
            "and not fullname.startswith('email_automation.claim_pipeline.')):\n"
            "            attempts.append(fullname)\n"
            "        return None\n"
            "sentinel = ImportAttemptSentinel()\n"
            "sys.meta_path.insert(0, sentinel)\n"
            "from email_automation import claim_pipeline\n"
            "package_attempts = list(attempts)\n"
            "probe = 'email_automation._claim_pipeline_isolation_probe'\n"
            "try:\n"
            "    __import__(probe)\n"
            "except ImportError:\n"
            "    pass\n"
            "sys.modules.pop(probe, None)\n"
            "loaded = sorted(name for name in sys.modules "
            "if name.startswith('email_automation.') "
            "and name != 'email_automation.claim_pipeline' "
            "and not name.startswith('email_automation.claim_pipeline.'))\n"
            "print(json.dumps({'attempts': package_attempts, "
            "'loaded': loaded, 'probeSeen': probe in attempts}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        result = json.loads(completed.stdout)
        self.assertEqual([], result["attempts"])
        self.assertEqual([], result["loaded"])
        self.assertTrue(result["probeSeen"])

    def test_effect_adapter_api_is_exposed_at_package_boundary(self):
        initializer_path = PACKAGE_ROOT / "__init__.py"
        initializer_tree = ast.parse(
            initializer_path.read_text(encoding="utf-8"),
            filename=str(initializer_path),
        )

        self.assertEqual(set(), _effect_adapter_api_violations(initializer_tree))
        self.assertEqual(
            set(),
            {
                name
                for name in EXPECTED_EFFECT_ADAPTER_API
                if not hasattr(claim_pipeline, name)
            },
        )

    def test_package_api_binding_provenance_and_runtime_identity_are_exact(self):
        self.assertEqual(
            EXPECTED_PACKAGE_API,
            frozenset(EXPECTED_PACKAGE_BINDING_PROVENANCE),
        )
        for name, provenance in EXPECTED_PACKAGE_BINDING_PROVENANCE.items():
            module_name, source_name = provenance.rsplit(".", 1)
            with self.subTest(name=name):
                self.assertIs(
                    getattr(claim_pipeline, name),
                    getattr(sys.modules[module_name], source_name),
                )

    def test_effect_adapter_api_lock_rejects_approved_name_rebinding(self):
        initializer_source = (
            PACKAGE_ROOT / "__init__.py"
        ).read_text(encoding="utf-8")
        cases = {
            "foreign import alias": (
                "from .contracts import Actor as evaluate_effect_plan\n"
            ),
            "private local helper": (
                "def _private_helper():\n"
                "    pass\n"
                "evaluate_effect_plan = _private_helper\n"
            ),
            "none assignment": "evaluate_effect_plan = None\n",
        }

        for label, appended_source in cases.items():
            with self.subTest(label=label):
                tree = ast.parse(initializer_source + appended_source)
                self.assertIn(
                    "rebound-package-api:evaluate_effect_plan",
                    _effect_adapter_api_violations(tree),
                )

    def test_effect_adapter_api_lock_rejects_extra_and_private_aliases(self):
        initializer_source = (
            (PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8")
        )
        extra_report_tree = ast.parse(initializer_source)
        extra_report_all = next(
            node
            for node in extra_report_tree.body
            if _assigns_all(node)
        )
        extra_report_all.value.elts.append(
            ast.Constant("EffectAdapterReport")
        )
        extra_report_tree.body.extend(
            ast.parse(
                "from .effect_adapter import EffectAdapterReport\n"
            ).body
        )

        private_alias_tree = ast.parse(initializer_source)
        private_alias_all = next(
            node
            for node in private_alias_tree.body
            if _assigns_all(node)
        )
        private_alias_all.value.elts.append(
            ast.Constant("publish_report")
        )
        private_alias_tree.body.extend(
            ast.parse(
                "from .effect_adapter import _commit as publish_report\n"
            ).body
        )
        cases = {
            "extra report": (
                extra_report_tree,
                "unexpected-bound:EffectAdapterReport",
            ),
            "private helper alias": (
                private_alias_tree,
                "private-source:_commit->publish_report",
            ),
        }

        for label, (tree, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_adapter_api_violations(tree),
                )

    def test_effect_adapter_api_lock_rejects_invented_and_dynamic_exports(self):
        initializer_path = PACKAGE_ROOT / "__init__.py"
        initializer_source = initializer_path.read_text(encoding="utf-8")

        unbound_export_tree = ast.parse(initializer_source)
        unbound_all = next(
            node
            for node in unbound_export_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        unbound_all.value.elts.append(ast.Constant("build_effect_report"))

        bound_export_tree = ast.parse(initializer_source)
        bound_all = next(
            node
            for node in bound_export_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        bound_all.value.elts.append(ast.Constant("build_effect_report"))
        bound_export_tree.body.append(
            ast.Assign(
                targets=[ast.Name(id="build_effect_report", ctx=ast.Store())],
                value=ast.Constant(None),
            )
        )

        orphan_binding_tree = ast.parse(initializer_source)
        orphan_binding_tree.body.append(
            ast.Assign(
                targets=[ast.Name(id="build_report", ctx=ast.Store())],
                value=ast.Constant(None),
            )
        )

        foreign_report_tree = ast.parse(initializer_source)
        foreign_report_all = next(
            node
            for node in foreign_report_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        foreign_report_all.value.elts.append(
            ast.Constant("UnexpectedReport")
        )
        foreign_report_tree.body.extend(
            ast.parse(
                "from .contracts import UnexpectedReport\n"
            ).body
        )

        local_helper_tree = ast.parse(initializer_source)
        local_helper_all = next(
            node
            for node in local_helper_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        local_helper_all.value.elts.append(
            ast.Constant("build_helper")
        )
        local_helper_tree.body.extend(
            ast.parse(
                "def build_helper():\n"
                "    pass\n"
            ).body
        )

        neutral_foreign_tree = ast.parse(initializer_source)
        neutral_foreign_all = next(
            node
            for node in neutral_foreign_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        neutral_foreign_all.value.elts.append(
            ast.Constant("NeutralUtility")
        )
        neutral_foreign_tree.body.extend(
            ast.parse(
                "from .contracts import NeutralUtility\n"
            ).body
        )

        neutral_local_tree = ast.parse(initializer_source)
        neutral_local_all = next(
            node
            for node in neutral_local_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        neutral_local_all.value.elts.append(ast.Constant("compose"))
        neutral_local_tree.body.extend(
            ast.parse(
                "def compose():\n"
                "    pass\n"
            ).body
        )

        starred_tree = ast.parse(initializer_source)
        starred_all = next(
            node
            for node in starred_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        starred_all.value = ast.List(
            elts=[
                ast.Starred(
                    value=ast.Name(id="__all__", ctx=ast.Load()),
                    ctx=ast.Load(),
                ),
                ast.Constant("build_effect_report"),
            ],
            ctx=ast.Load(),
        )

        append_tree = ast.parse(initializer_source)
        append_tree.body.append(
            ast.Expr(
                value=ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="__all__", ctx=ast.Load()),
                        attr="append",
                        ctx=ast.Load(),
                    ),
                    args=[ast.Constant("build_effect_report")],
                    keywords=[],
                )
            )
        )

        nonliteral_tree = ast.parse(initializer_source)
        nonliteral_all = next(
            node
            for node in nonliteral_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        nonliteral_all.value = ast.Name(id="exports", ctx=ast.Load())

        invented_export_trees = {}
        for invented_name in (
            "DryRunBuilder",
            "AdapterBuilder",
            "NeutralPublicUtility",
            "Surprise",
        ):
            tree = ast.parse(initializer_source)
            all_assignment = next(
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in node.targets
                )
            )
            all_assignment.value.elts.append(ast.Constant(invented_name))
            tree.body.append(
                ast.Assign(
                    targets=[
                        ast.Name(id=invented_name, ctx=ast.Store())
                    ],
                    value=ast.Constant(None),
                )
            )
            invented_export_trees[invented_name] = tree

        cases = {
            "unbound export": (
                unbound_export_tree,
                "package-export-without-binding:build_effect_report",
            ),
            "bound invented export": (
                bound_export_tree,
                "unexpected-package-api:build_effect_report",
            ),
            "binding without export": (
                orphan_binding_tree,
                "package-binding-without-export:build_report",
            ),
            "foreign report export": (
                foreign_report_tree,
                "unexpected-package-api:UnexpectedReport",
            ),
            "local helper export": (
                local_helper_tree,
                "unexpected-package-api:build_helper",
            ),
            "neutral foreign export": (
                neutral_foreign_tree,
                "unexpected-package-api:NeutralUtility",
            ),
            "neutral local export": (
                neutral_local_tree,
                "unexpected-package-api:compose",
            ),
            "starred reassignment": (starred_tree, "dynamic-package-exports"),
            "append mutation": (append_tree, "dynamic-package-exports"),
            "nonliteral assignment": (
                nonliteral_tree,
                "dynamic-package-exports",
            ),
        }
        cases.update(
            {
                f"invented {name}": (
                    tree,
                    f"unexpected-package-api:{name}",
                )
                for name, tree in invented_export_trees.items()
            }
        )
        for label, (tree, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_adapter_api_violations(tree),
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
