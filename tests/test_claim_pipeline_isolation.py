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
CALLABLE_ARGUMENT_POSITIONS = {
    "filter": frozenset({0}),
    "functools.reduce": frozenset({0}),
    "map": frozenset({0}),
}
CALLABLE_KEYWORD_ARGUMENTS = {
    "max": frozenset({"key"}),
    "min": frozenset({"key"}),
    "sorted": frozenset({"key"}),
}
PURE_PARAMETER_METHOD_NAMES = frozenset(
    {
        "_identity",
        "get",
        "items",
        "read_text",
        "startswith",
        "strip",
        "to_dict",
    }
)
NON_STRUCTURAL_SCHEMA_KEYS = frozenset(
    {
        "const",
        "default",
        "deprecated",
        "description",
        "enum",
        "example",
        "examples",
        "metadata",
        "read_only",
        "readonly",
        "title",
        "write_only",
        "writeonly",
    }
)
MAX_STRUCTURE_DEPTH = 32
UNKNOWN_DANGEROUS_ORIGIN = "<unknown-dangerous-origin>"
_MISSING_SYMBOL = object()


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
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
        and _resolved_symbol(node.value, bindings)
        in {"__builtins__", "builtins"}
    ):
        return f"builtins.{node.slice.value}"
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


def _structured_item(value, key, depth=0):
    if depth >= MAX_STRUCTURE_DEPTH:
        return None
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and value[0] == "sequence"
        and isinstance(key, int)
        and -len(value[1]) <= key < len(value[1])
    ):
        return value[1][key]
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "mapping":
        for item_key, item_value in value[1]:
            if item_key == key:
                return item_value
    return None


def _structured_leaves(value, depth=0):
    if depth >= MAX_STRUCTURE_DEPTH:
        return {UNKNOWN_DANGEROUS_ORIGIN}
    if isinstance(value, str):
        return {value}
    if isinstance(value, frozenset):
        return set(value)
    if isinstance(value, tuple) and len(value) == 2:
        if value[0] == "sequence":
            leaves = set()
            for item in value[1]:
                leaves.update(_structured_leaves(item, depth + 1))
            return leaves
        if value[0] == "mapping":
            leaves = set()
            for _, item in value[1]:
                leaves.update(_structured_leaves(item, depth + 1))
            return leaves
    return set()


def _symbol_structure(node, bindings, structures, depth=0):
    if node is None:
        return None
    if depth >= MAX_STRUCTURE_DEPTH:
        return UNKNOWN_DANGEROUS_ORIGIN
    if isinstance(node, ast.NamedExpr):
        return _symbol_structure(
            node.value, bindings, structures, depth + 1
        )
    if isinstance(node, ast.Name) and node.id in structures:
        return structures[node.id]
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        items = []
        for item in node.elts:
            value = _symbol_structure(
                item.value if isinstance(item, ast.Starred) else item,
                bindings,
                structures,
                depth + 1,
            )
            if (
                isinstance(item, ast.Starred)
                and isinstance(value, tuple)
                and len(value) == 2
                and value[0] == "sequence"
            ):
                items.extend(value[1])
            else:
                items.append(value)
        return (
            "sequence",
            tuple(items),
        )
    if isinstance(node, ast.Dict):
        entries = []
        for key, item in zip(node.keys, node.values, strict=True):
            if key is None:
                unpacked = _symbol_structure(
                    item, bindings, structures, depth + 1
                )
                if unpacked in {"__builtins__", "builtins"}:
                    entries.append(
                        ("__import__", "builtins.__import__")
                    )
                elif (
                    isinstance(unpacked, tuple)
                    and len(unpacked) == 2
                    and unpacked[0] == "mapping"
                ):
                    entries.extend(unpacked[1])
                elif unpacked == UNKNOWN_DANGEROUS_ORIGIN:
                    entries.append((None, unpacked))
                continue
            if isinstance(key, ast.Constant):
                entries.append(
                    (
                        key.value,
                        _symbol_structure(
                            item,
                            bindings,
                            structures,
                            depth + 1,
                        ),
                    )
                )
        return (
            "mapping",
            tuple(entries),
        )
    if isinstance(node, ast.Subscript):
        owner = _symbol_structure(
            node.value, bindings, structures, depth + 1
        )
        if isinstance(node.slice, ast.Constant):
            selected = _structured_item(
                owner, node.slice.value, depth + 1
            )
            if selected is not None:
                return selected
            resolved = _resolved_symbol(node, bindings)
            if resolved is not None:
                return resolved
        return frozenset(_structured_leaves(owner, depth + 1))
    if isinstance(node, ast.Call):
        callee = _resolved_symbol(node.func, bindings)
        if callee in {"iter", "list", "set", "tuple"} and node.args:
            return _symbol_structure(
                node.args[0], bindings, structures, depth + 1
            )
        copied_builtins = (
            callee == "dict"
            and node.args
            and _resolved_symbol(node.args[0], bindings)
            in {"__builtins__", "builtins"}
        ) or callee in {
            "__builtins__.copy",
            "builtins.__dict__.copy",
        } or (
            callee == "dict"
            and node.args
            and bool(
                _structured_leaves(
                    _symbol_structure(
                        node.args[0],
                        bindings,
                        structures,
                        depth + 1,
                    )
                )
                & DYNAMIC_IMPORT_SYMBOLS.keys()
            )
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "copy"
            and _resolved_symbol(node.func.value, bindings)
            in {"__builtins__", "builtins"}
        )
        if copied_builtins:
            return (
                "mapping",
                (("__import__", "builtins.__import__"),),
            )
    return _resolved_symbol(node, bindings)


def _bind_structured_target(target, value, bindings, structures):
    if isinstance(target, ast.Starred):
        _bind_structured_target(
            target.value, value, bindings, structures
        )
        return
    if isinstance(target, ast.Name):
        if value is None:
            structures.pop(target.id, None)
            bindings.pop(target.id, None)
        else:
            structures[target.id] = value
            if isinstance(value, str):
                bindings[target.id] = value
            else:
                bindings.pop(target.id, None)
        return
    if (
        isinstance(target, (ast.List, ast.Tuple))
        and isinstance(value, tuple)
        and len(value) == 2
        and value[0] == "sequence"
    ):
        targets = target.elts
        values = value[1]
        starred = next(
            (
                index
                for index, item in enumerate(targets)
                if isinstance(item, ast.Starred)
            ),
            None,
        )
        if starred is None:
            for target_item, item_value in zip(
                targets, values, strict=False
            ):
                _bind_structured_target(
                    target_item, item_value, bindings, structures
                )
            return
        trailing = len(targets) - starred - 1
        for index in range(starred):
            if index < len(values):
                _bind_structured_target(
                    targets[index], values[index], bindings, structures
                )
        stop = len(values) - trailing
        _bind_structured_target(
            targets[starred],
            ("sequence", tuple(values[starred:stop])),
            bindings,
            structures,
        )
        for offset in range(1, trailing + 1):
            _bind_structured_target(
                targets[-offset],
                values[-offset],
                bindings,
                structures,
            )


def _apply_named_expression_bindings(node, bindings, structures):
    if isinstance(
        node,
        (
            ast.FunctionDef,
            ast.AsyncFunctionDef,
            ast.ClassDef,
            ast.Lambda,
        ),
    ):
        return
    for child in ast.iter_child_nodes(node):
        _apply_named_expression_bindings(
            child, bindings, structures
        )
    if isinstance(node, ast.NamedExpr):
        _bind_structured_target(
            node.target,
            _symbol_structure(node.value, bindings, structures),
            bindings,
            structures,
        )


def _match_pattern_bindings(
    pattern, subject, bindings, structures
):
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            _match_pattern_bindings(
                pattern.pattern, subject, bindings, structures
            )
        if pattern.name is not None:
            _bind_structured_target(
                ast.Name(id=pattern.name, ctx=ast.Store()),
                subject,
                bindings,
                structures,
            )
    elif isinstance(pattern, ast.MatchStar) and pattern.name is not None:
        _bind_structured_target(
            ast.Name(id=pattern.name, ctx=ast.Store()),
            subject,
            bindings,
            structures,
        )
    elif (
        isinstance(pattern, ast.MatchSequence)
        and isinstance(subject, tuple)
        and len(subject) == 2
        and subject[0] == "sequence"
    ):
        for item_pattern, item_value in zip(
            pattern.patterns, subject[1], strict=False
        ):
            _match_pattern_bindings(
                item_pattern, item_value, bindings, structures
            )
    elif (
        isinstance(pattern, ast.MatchMapping)
        and isinstance(subject, tuple)
        and len(subject) == 2
        and subject[0] == "mapping"
    ):
        for key, item_pattern in zip(
            pattern.keys, pattern.patterns, strict=True
        ):
            if not isinstance(key, ast.Constant):
                continue
            item_value = _structured_item(subject, key.value)
            if item_value is not None:
                _match_pattern_bindings(
                    item_pattern,
                    item_value,
                    bindings,
                    structures,
                )
    elif isinstance(pattern, ast.MatchOr):
        for alternative in pattern.patterns:
            _match_pattern_bindings(
                alternative, subject, bindings, structures
            )


def _binding_environments(tree):
    bindings_by_node = {}
    imported_by_node = {}
    callables_by_node = {}
    structures_by_node = {}

    def record(
        node,
        bindings,
        imported_symbols,
        callable_symbols,
        structures,
    ):
        for child in ast.walk(node):
            bindings_by_node[child] = dict(bindings)
            imported_by_node[child] = frozenset(imported_symbols)
            callables_by_node[child] = frozenset(callable_symbols)
            structures_by_node[child] = dict(structures)

    def merge_states(states):
        imported_symbols = set().union(
            *(state[1] for state in states)
        )
        callable_symbols = set().union(
            *(state[2] for state in states)
        )
        structures = {}
        bindings = {}
        names = set().union(*(state[0] for state in states))
        relevant_symbols = {
            *callable_symbols,
            *DYNAMIC_IMPORT_SYMBOLS,
            *FORBIDDEN_FUNCTION_SYMBOLS,
            "dataclasses.dataclass",
            "dataclasses.field",
        }
        for name in names:
            values = {
                state_bindings[name]
                for state_bindings, _, _, _ in states
                if name in state_bindings
            }
            risky = sorted(
                value
                for value in values
                if value in relevant_symbols
                or _imported_symbol_is_callable(
                    value, imported_symbols
                )
            )
            if risky:
                bindings[name] = risky[0]
            elif len(values) == 1:
                bindings[name] = values.pop()
        structure_names = set().union(*(state[3] for state in states))
        for name in structure_names:
            values = {
                state_structures[name]
                for _, _, _, state_structures in states
                if name in state_structures
            }
            risky = [
                value
                for value in values
                if any(
                    leaf in relevant_symbols
                    or _imported_symbol_is_callable(
                        leaf, imported_symbols
                    )
                    for leaf in _structured_leaves(value)
                )
            ]
            if risky:
                structures[name] = sorted(risky, key=repr)[0]
            elif len(values) == 1:
                structures[name] = values.pop()
        return (
            bindings,
            imported_symbols,
            callable_symbols,
            structures,
        )

    def walk_scope(
        statements,
        inherited,
        imported,
        callables,
        inherited_structures,
    ):
        bindings = dict(inherited)
        imported_symbols = set(imported)
        callable_symbols = set(callables)
        structures = dict(inherited_structures)
        for statement in statements:
            record(
                statement,
                bindings,
                imported_symbols,
                callable_symbols,
                structures,
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
                    structures,
                )
            elif isinstance(statement, ast.If):
                condition_bindings = dict(bindings)
                condition_structures = dict(structures)
                _apply_named_expression_bindings(
                    statement.test,
                    condition_bindings,
                    condition_structures,
                )
                body_state = walk_scope(
                    statement.body,
                    condition_bindings,
                    imported_symbols,
                    callable_symbols,
                    condition_structures,
                )
                else_state = walk_scope(
                    statement.orelse,
                    condition_bindings,
                    imported_symbols,
                    callable_symbols,
                    condition_structures,
                )
                (
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                ) = merge_states(
                    [
                        (
                            condition_bindings,
                            set(imported_symbols),
                            set(callable_symbols),
                            condition_structures,
                        ),
                        body_state,
                        else_state,
                    ]
                )
                continue
            elif isinstance(
                statement,
                (ast.For, ast.AsyncFor, ast.While),
            ):
                initial_bindings = dict(bindings)
                initial_structures = dict(structures)
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    iterable = _symbol_structure(
                        statement.iter,
                        bindings,
                        structures,
                    )
                    iteration_values = (
                        iterable[1]
                        if isinstance(iterable, tuple)
                        and len(iterable) == 2
                        and iterable[0] == "sequence"
                        else ()
                    )
                    body_states = []
                    for iteration_value in iteration_values:
                        iteration_bindings = dict(bindings)
                        iteration_structures = dict(structures)
                        _bind_structured_target(
                            statement.target,
                            iteration_value,
                            iteration_bindings,
                            iteration_structures,
                        )
                        body_states.append(
                            walk_scope(
                                statement.body,
                                iteration_bindings,
                                imported_symbols,
                                callable_symbols,
                                iteration_structures,
                            )
                        )
                    if not body_states:
                        body_states.append(
                            walk_scope(
                                statement.body,
                                bindings,
                                imported_symbols,
                                callable_symbols,
                                structures,
                            )
                        )
                else:
                    _apply_named_expression_bindings(
                        statement.test,
                        initial_bindings,
                        initial_structures,
                    )
                    body_states = [
                        walk_scope(
                            statement.body,
                            initial_bindings,
                            imported_symbols,
                            callable_symbols,
                            initial_structures,
                        )
                    ]
                loop_state = merge_states(
                    [
                        (
                            initial_bindings,
                            set(imported_symbols),
                            set(callable_symbols),
                            initial_structures,
                        ),
                        *body_states,
                    ]
                )
                else_state = walk_scope(
                    statement.orelse,
                    *loop_state,
                )
                (
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                ) = merge_states([loop_state, else_state])
                continue
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                body_state = walk_scope(
                    statement.body,
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                )
                (
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                ) = merge_states(
                    [
                        (
                            dict(bindings),
                            set(imported_symbols),
                            set(callable_symbols),
                            dict(structures),
                        ),
                        body_state,
                    ]
                )
                continue
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                initial_state = (
                    dict(bindings),
                    set(imported_symbols),
                    set(callable_symbols),
                    dict(structures),
                )
                body_state = walk_scope(statement.body, *initial_state)
                else_state = walk_scope(statement.orelse, *body_state)
                handler_states = [
                    walk_scope(handler.body, *initial_state)
                    for handler in statement.handlers
                ]
                merged = merge_states(
                    [initial_state, body_state, else_state, *handler_states]
                )
                final_state = walk_scope(statement.finalbody, *merged)
                (
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                ) = merge_states([merged, final_state])
                continue
            elif isinstance(statement, ast.Match):
                subject_bindings = dict(bindings)
                subject_structures = dict(structures)
                _apply_named_expression_bindings(
                    statement.subject,
                    subject_bindings,
                    subject_structures,
                )
                subject = _symbol_structure(
                    statement.subject,
                    subject_bindings,
                    subject_structures,
                )
                initial_state = (
                    subject_bindings,
                    set(imported_symbols),
                    set(callable_symbols),
                    subject_structures,
                )
                case_states = []
                for case in statement.cases:
                    case_bindings = dict(subject_bindings)
                    case_structures = dict(subject_structures)
                    _match_pattern_bindings(
                        case.pattern,
                        subject,
                        case_bindings,
                        case_structures,
                    )
                    if case.guard is not None:
                        _apply_named_expression_bindings(
                            case.guard,
                            case_bindings,
                            case_structures,
                        )
                    case_states.append(
                        walk_scope(
                            case.body,
                            case_bindings,
                            imported_symbols,
                            callable_symbols,
                            case_structures,
                        )
                    )
                (
                    bindings,
                    imported_symbols,
                    callable_symbols,
                    structures,
                ) = merge_states([initial_state, *case_states])
                continue

            _apply_named_expression_bindings(
                statement, bindings, structures
            )
            new_bindings, new_imports = _imported_bindings(statement)
            bindings.update(new_bindings)
            structures.update(new_bindings)
            imported_symbols.update(new_imports)
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                symbol = f"<callable>:{statement.lineno}:{statement.name}"
                bindings[statement.name] = symbol
                structures[statement.name] = symbol
                callable_symbols.add(symbol)
            elif isinstance(statement, ast.ClassDef):
                bindings[statement.name] = (
                    f"<class>:{statement.lineno}:{statement.name}"
                )
                structures[statement.name] = bindings[statement.name]
            elif isinstance(statement, ast.Assign):
                value = _symbol_structure(
                    statement.value, bindings, structures
                )
                for target in statement.targets:
                    _bind_structured_target(
                        target, value, bindings, structures
                    )
            elif isinstance(statement, ast.AnnAssign):
                _bind_structured_target(
                    statement.target,
                    _symbol_structure(
                        statement.value, bindings, structures
                    ),
                    bindings,
                    structures,
                )

        return bindings, imported_symbols, callable_symbols, structures

    record(tree, {}, set(), set(), {})
    walk_scope(tree.body, {}, set(), set(), {})
    return (
        bindings_by_node,
        imported_by_node,
        callables_by_node,
        structures_by_node,
    )


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


def _loaded_symbol_value(symbol):
    parts = symbol.split(".")
    for index in range(len(parts), 0, -1):
        module = sys.modules.get(".".join(parts[:index]))
        if module is None:
            continue
        value = module
        for attribute in parts[index:]:
            namespace = getattr(value, "__dict__", {})
            if attribute not in namespace:
                return _MISSING_SYMBOL
            value = namespace[attribute]
        return value
    return _MISSING_SYMBOL


def _is_imported_reference(symbol, imported_symbols):
    return symbol is not None and any(
        symbol == imported or symbol.startswith(f"{imported}.")
        for imported in imported_symbols
    )


def _imported_symbol_is_callable(symbol, imported_symbols):
    if not _is_imported_reference(symbol, imported_symbols):
        return False
    value = _loaded_symbol_value(symbol)
    return value is _MISSING_SYMBOL or callable(value)


def _is_callable_reference(
    node,
    bindings,
    callable_symbols,
    imported_symbols,
    structures,
):
    value = _symbol_structure(node, bindings, structures)
    for resolved in _structured_leaves(value):
        if resolved == UNKNOWN_DANGEROUS_ORIGIN:
            return True
        if resolved in callable_symbols or resolved in BUILTIN_CALLABLE_NAMES:
            return True
        if (
            _is_imported_reference(resolved, imported_symbols)
            and _imported_symbol_is_callable(
                resolved, imported_symbols
            )
        ):
            return True
        if (
            resolved.startswith("builtins.")
            and resolved.rsplit(".", 1)[-1] in BUILTIN_CALLABLE_NAMES
        ):
            return True
    return False


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


def _assigned_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        names = set()
        for item in target.elts:
            names.update(_assigned_names(item))
        return names
    return set()


def _scope_bound_names(statements):
    names = set()
    for statement in statements:
        if isinstance(
            statement,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            names.add(statement.name)
            continue
        if isinstance(statement, ast.Import):
            names.update(
                alias.asname or alias.name.split(".", 1)[0]
                for alias in statement.names
            )
        elif isinstance(statement, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                names.update(_assigned_names(target))
        elif isinstance(statement, ast.AnnAssign):
            names.update(_assigned_names(statement.target))
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            names.update(_assigned_names(statement.target))

        child_lists = []
        if isinstance(statement, ast.If):
            child_lists = [statement.body, statement.orelse]
        elif isinstance(
            statement, (ast.For, ast.AsyncFor, ast.While)
        ):
            child_lists = [statement.body, statement.orelse]
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            child_lists = [statement.body]
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            child_lists = [
                statement.body,
                statement.orelse,
                statement.finalbody,
                *(handler.body for handler in statement.handlers),
            ]
        elif isinstance(statement, ast.Match):
            child_lists = [case.body for case in statement.cases]
        for child_list in child_lists:
            names.update(_scope_bound_names(child_list))
    return names


def _parameter_call_origins(
    node,
    safe_names,
    bindings_by_node,
):
    parameter_names = {
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        if argument.arg not in safe_names
    }
    if node.args.vararg is not None:
        parameter_names.add(node.args.vararg.arg)
    if node.args.kwarg is not None:
        parameter_names.add(node.args.kwarg.arg)
    found = set()

    def origin_sequence(values):
        return ("origin-sequence", tuple(values))

    def stored_origin(key, value):
        return ("stored-origin", key, value)

    def storage_key(value):
        if isinstance(value, ast.Name):
            return ("name", value.id)
        if isinstance(value, ast.Attribute):
            owner = storage_key(value.value)
            if owner is not None:
                return ("attribute", owner, value.attr)
        if isinstance(value, ast.Subscript) and isinstance(
            value.slice, ast.Constant
        ):
            owner = storage_key(value.value)
            if owner is not None:
                return ("item", owner, value.slice.value)
        return None

    def literal_item(value, key, depth=0):
        if depth >= MAX_STRUCTURE_DEPTH:
            return UNKNOWN_DANGEROUS_ORIGIN
        if value == UNKNOWN_DANGEROUS_ORIGIN:
            return value
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and value[0] == "origin-sequence"
        ):
            if isinstance(key, int) and -len(value[1]) <= key < len(
                value[1]
            ):
                return value[1][key]
            return None
        if isinstance(value, (ast.List, ast.Tuple)):
            entries = container_entries(value, {})
            if isinstance(key, int) and -len(entries) <= key < len(entries):
                return entries[key][1]
            return None
        if isinstance(value, ast.Dict):
            for item_key, item_value in zip(
                value.keys, value.values, strict=True
            ):
                if (
                    isinstance(item_key, ast.Constant)
                    and item_key.value == key
                ):
                    return item_value
            return None
        if isinstance(value, ast.Subscript) and isinstance(
            value.slice, ast.Constant
        ):
            selected = literal_item(
                value.value, value.slice.value, depth + 1
            )
            if selected is not None:
                return literal_item(selected, key, depth + 1)
        return None

    def container_entries(value, aliases, depth=0):
        if depth >= MAX_STRUCTURE_DEPTH:
            return ((None, UNKNOWN_DANGEROUS_ORIGIN),)
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and value[0] == "origin-sequence"
        ):
            return tuple(enumerate(value[1]))
        if (
            isinstance(value, tuple)
            and len(value) == 3
            and value[0] == "stored-origin"
        ):
            owner = value[1]
        else:
            owner = None
        if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
            values = []
            for item in value.elts:
                if isinstance(item, ast.Starred):
                    expanded = container_entries(
                        item.value, aliases, depth + 1
                    )
                    if expanded:
                        values.extend(
                            expanded_item
                            for _, expanded_item in expanded
                        )
                        continue
                values.append(item)
            return tuple(enumerate(values))
        if isinstance(value, ast.Dict):
            return tuple(
                (key.value, item)
                for key, item in zip(
                    value.keys, value.values, strict=True
                )
                if isinstance(key, ast.Constant)
            )
        if isinstance(value, ast.Call):
            bindings = bindings_by_node.get(value, {})
            callee = _resolved_symbol(value.func, bindings)
            if callee in {"iter", "list", "set", "tuple"} and value.args:
                return container_entries(
                    value.args[0], aliases, depth + 1
                )
            if callee in {"enumerate", "builtins.enumerate"} and value.args:
                return tuple(
                    (
                        index,
                        origin_sequence((ast.Constant(index), item)),
                    )
                    for index, (_, item) in enumerate(
                        container_entries(
                            value.args[0], aliases, depth + 1
                        )
                    )
                )
        if owner is None:
            owner = storage_key(value)
        if owner is None:
            return ()
        entries = []
        for key, item_origins in aliases.items():
            if (
                isinstance(key, tuple)
                and len(key) == 3
                and key[0] == "item"
                and key[1] == owner
            ):
                entries.append(
                    (key[2], stored_origin(key, item_origins))
                )
        return tuple(sorted(entries, key=lambda item: repr(item[0])))

    def origins(value, aliases, depth=0):
        if depth >= MAX_STRUCTURE_DEPTH:
            return frozenset(parameter_names)
        if value == UNKNOWN_DANGEROUS_ORIGIN:
            return frozenset(parameter_names)
        if isinstance(value, frozenset):
            return value
        if (
            isinstance(value, tuple)
            and len(value) == 3
            and value[0] == "stored-origin"
        ):
            return value[2]
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and value[0] == "origin-sequence"
        ):
            return frozenset().union(
                *(
                    origins(item, aliases, depth + 1)
                    for item in value[1]
                )
            )
        if isinstance(value, ast.Name):
            return aliases.get(value.id, frozenset())
        if isinstance(value, ast.Attribute):
            if value.attr == "__call__":
                return origins(value.value, aliases, depth + 1)
            stored = aliases.get(storage_key(value), frozenset())
            return stored or origins(value.value, aliases, depth + 1)
        if isinstance(value, ast.Subscript):
            if isinstance(value.slice, ast.Constant):
                selected = literal_item(
                    value.value, value.slice.value
                )
                if selected is not None:
                    return origins(selected, aliases, depth + 1)
                stored = aliases.get(storage_key(value), frozenset())
                if stored:
                    return stored
            return frozenset().union(
                *(
                    origins(item, aliases, depth + 1)
                    for _, item in container_entries(
                        value.value, aliases, depth + 1
                    )
                )
            )
        if isinstance(value, ast.NamedExpr):
            return origins(value.value, aliases, depth + 1)
        if (
            isinstance(value, ast.Call)
            and _resolved_symbol(
                value.func, bindings_by_node.get(value, {})
            )
            in {"getattr", "builtins.getattr"}
            and len(value.args) >= 2
            and isinstance(value.args[1], ast.Constant)
            and isinstance(value.args[1].value, str)
        ):
            return origins(value.args[0], aliases, depth + 1)
        return frozenset()

    def rebased_storage_key(key, source, target):
        if key == source:
            return target
        if (
            isinstance(key, tuple)
            and len(key) == 3
            and key[0] in {"attribute", "item"}
        ):
            owner = rebased_storage_key(key[1], source, target)
            if owner is not None:
                return (key[0], owner, key[2])
        return None

    def store(target, value, aliases):
        if isinstance(target, ast.Starred):
            store(target.value, value, aliases)
            return
        if isinstance(target, ast.Name):
            target_key = ("name", target.id)
            for key in list(aliases):
                if isinstance(key, tuple) and rebased_storage_key(
                    key, target_key, target_key
                ) is not None:
                    aliases.pop(key, None)
            assigned = origins(value, aliases)
            if assigned:
                aliases[target.id] = assigned
            else:
                aliases.pop(target.id, None)
            for item_key, item in container_entries(value, aliases):
                key = ("item", target_key, item_key)
                aliases[key] = origins(item, aliases)
                store_nested(key, item, aliases)
            source_key = storage_key(value)
            if source_key is not None:
                for key, item_origins in list(aliases.items()):
                    if not isinstance(key, tuple):
                        continue
                    rebased = rebased_storage_key(
                        key, source_key, target_key
                    )
                    if rebased is not None and rebased != target_key:
                        aliases[rebased] = item_origins
            return
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            key = storage_key(target)
            assigned = origins(value, aliases)
            if key is not None and assigned:
                aliases[key] = assigned
            elif key is not None:
                aliases.pop(key, None)
            return
        if isinstance(target, (ast.List, ast.Tuple)):
            values = tuple(
                item for _, item in container_entries(value, aliases)
            )
            starred = next(
                (
                    index
                    for index, item in enumerate(target.elts)
                    if isinstance(item, ast.Starred)
                ),
                None,
            )
            if starred is None:
                for target_item, value_item in zip(
                    target.elts, values, strict=False
                ):
                    store(target_item, value_item, aliases)
                return
            trailing = len(target.elts) - starred - 1
            for index in range(starred):
                if index < len(values):
                    store(target.elts[index], values[index], aliases)
            stop = len(values) - trailing
            store(
                target.elts[starred],
                origin_sequence(values[starred:stop]),
                aliases,
            )
            for offset in range(1, trailing + 1):
                if offset <= len(values):
                    store(target.elts[-offset], values[-offset], aliases)

    def store_nested(owner, value, aliases, depth=0):
        if depth >= MAX_STRUCTURE_DEPTH:
            aliases[owner] = frozenset(parameter_names)
            return
        for item_key, item in container_entries(value, aliases, depth):
            key = ("item", owner, item_key)
            aliases[key] = origins(item, aliases)
            store_nested(key, item, aliases, depth + 1)

    def bind_pattern(pattern, subject, aliases):
        if isinstance(pattern, ast.MatchAs):
            if pattern.pattern is not None:
                bind_pattern(pattern.pattern, subject, aliases)
            if pattern.name is not None:
                store(
                    ast.Name(id=pattern.name, ctx=ast.Store()),
                    subject,
                    aliases,
                )
        elif isinstance(pattern, ast.MatchSequence):
            entries = container_entries(subject, aliases)
            for item_pattern, item_value in zip(
                pattern.patterns,
                (item for _, item in entries),
                strict=False,
            ):
                bind_pattern(item_pattern, item_value, aliases)
        elif isinstance(pattern, ast.MatchMapping):
            entries = dict(container_entries(subject, aliases))
            for key, item_pattern in zip(
                pattern.keys, pattern.patterns, strict=True
            ):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in entries
                ):
                    bind_pattern(
                        item_pattern,
                        entries[key.value],
                        aliases,
                    )
        elif isinstance(pattern, ast.MatchOr):
            for alternative in pattern.patterns:
                bind_pattern(alternative, subject, aliases)

    def scan_expression(value, aliases):
        if isinstance(
            value,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
            ),
        ):
            return
        if isinstance(value, ast.NamedExpr):
            scan_expression(value.value, aliases)
            store(value.target, value.value, aliases)
            return
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Attribute):
                stored_method = aliases.get(
                    storage_key(value.func), frozenset()
                )
                found.update(stored_method)
                if (
                    value.func.attr
                    not in PURE_PARAMETER_METHOD_NAMES
                ):
                    found.update(origins(value.func.value, aliases))
            else:
                found.update(origins(value.func, aliases))
            bindings = bindings_by_node.get(value, {})
            callee = _resolved_symbol(value.func, bindings)
            if callee is not None and callee.startswith("builtins."):
                callee = callee.removeprefix("builtins.")
            for position in CALLABLE_ARGUMENT_POSITIONS.get(
                callee, frozenset()
            ):
                if position < len(value.args):
                    found.update(origins(value.args[position], aliases))
            callable_keywords = CALLABLE_KEYWORD_ARGUMENTS.get(
                callee, frozenset()
            )
            for keyword in value.keywords:
                if keyword.arg in callable_keywords:
                    found.update(origins(keyword.value, aliases))
        for child in ast.iter_child_nodes(value):
            scan_expression(child, aliases)

    def merge_aliases(states):
        merged = {}
        for name in set().union(*states):
            combined = frozenset().union(
                *(state.get(name, frozenset()) for state in states)
            )
            if combined or isinstance(name, tuple):
                merged[name] = combined
        return merged

    def walk(statements, inherited):
        aliases = dict(inherited)
        for statement in statements:
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                for decorator in statement.decorator_list:
                    scan_expression(decorator, aliases)
                    found.update(origins(decorator, aliases))
                for default in (
                    *statement.args.defaults,
                    *(
                        default
                        for default in statement.args.kw_defaults
                        if default is not None
                    ),
                ):
                    scan_expression(default, aliases)
                local_names = {
                    argument.arg
                    for argument in (
                        *statement.args.posonlyargs,
                        *statement.args.args,
                        *statement.args.kwonlyargs,
                    )
                }
                if statement.args.vararg is not None:
                    local_names.add(statement.args.vararg.arg)
                if statement.args.kwarg is not None:
                    local_names.add(statement.args.kwarg.arg)
                local_names.update(_scope_bound_names(statement.body))
                captured = {
                    name: origin
                    for name, origin in aliases.items()
                    if not (
                        isinstance(name, str) and name in local_names
                    )
                }
                walk(statement.body, captured)
                aliases.pop(statement.name, None)
                continue
            if isinstance(statement, ast.ClassDef):
                for decorator in statement.decorator_list:
                    scan_expression(decorator, aliases)
                    found.update(origins(decorator, aliases))
                local_names = {
                    statement.name,
                    *_scope_bound_names(statement.body),
                }
                captured = {
                    name: origin
                    for name, origin in aliases.items()
                    if not (
                        isinstance(name, str) and name in local_names
                    )
                }
                walk(statement.body, captured)
                aliases.pop(statement.name, None)
                continue
            if isinstance(statement, ast.If):
                scan_expression(statement.test, aliases)
                aliases = merge_aliases(
                    [
                        aliases,
                        walk(statement.body, aliases),
                        walk(statement.orelse, aliases),
                    ]
                )
                continue
            if isinstance(
                statement, (ast.For, ast.AsyncFor, ast.While)
            ):
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    scan_expression(statement.iter, aliases)
                    iteration_values = tuple(
                        item
                        for _, item in container_entries(
                            statement.iter, aliases
                        )
                    )
                    body_states = []
                    for iteration_value in iteration_values:
                        iteration_aliases = dict(aliases)
                        store(
                            statement.target,
                            iteration_value,
                            iteration_aliases,
                        )
                        body_states.append(
                            walk(statement.body, iteration_aliases)
                        )
                    if not body_states:
                        body_states.append(
                            walk(statement.body, aliases)
                        )
                else:
                    scan_expression(statement.test, aliases)
                    body_states = [walk(statement.body, aliases)]
                loop_aliases = merge_aliases(
                    [aliases, *body_states]
                )
                aliases = merge_aliases(
                    [loop_aliases, walk(statement.orelse, loop_aliases)]
                )
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    scan_expression(item.context_expr, aliases)
                aliases = merge_aliases(
                    [aliases, walk(statement.body, aliases)]
                )
                continue
            if isinstance(statement, (ast.Try, ast.TryStar)):
                body_aliases = walk(statement.body, aliases)
                states = [
                    aliases,
                    body_aliases,
                    walk(statement.orelse, body_aliases),
                    *(
                        walk(handler.body, aliases)
                        for handler in statement.handlers
                    ),
                ]
                merged = merge_aliases(states)
                aliases = merge_aliases(
                    [merged, walk(statement.finalbody, merged)]
                )
                continue
            if isinstance(statement, ast.Match):
                scan_expression(statement.subject, aliases)
                states = [aliases]
                for case in statement.cases:
                    case_aliases = dict(aliases)
                    bind_pattern(
                        case.pattern,
                        statement.subject,
                        case_aliases,
                    )
                    if case.guard is not None:
                        scan_expression(case.guard, case_aliases)
                    states.append(walk(case.body, case_aliases))
                aliases = merge_aliases(states)
                continue

            scan_expression(statement, aliases)
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    store(target, statement.value, aliases)
            elif isinstance(statement, ast.AnnAssign):
                store(statement.target, statement.value, aliases)
        return aliases

    walk(
        node.body,
        {name: frozenset({name}) for name in parameter_names},
    )
    return found


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


def _is_dataclass(class_node, bindings, structures):
    return any(
        bool(
            _structured_leaves(
                _symbol_structure(
                    decorator.func
                    if isinstance(decorator, ast.Call)
                    else decorator,
                    bindings,
                    structures,
                )
            )
            & {"dataclass", "dataclasses.dataclass"}
        )
        for decorator in class_node.decorator_list
    )


def _schema_literal_keys(value, context="schema"):
    if isinstance(value, ast.Dict):
        keys = set()
        for key, item in zip(value.keys, value.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
                tokens = _identifier_tokens(key.value)
                if context == "schema" and (
                    tokens & NON_STRUCTURAL_SCHEMA_KEYS
                ):
                    continue
                child_context = (
                    "fields"
                    if tokens & {"field", "fields", "properties"}
                    else "schema"
                )
                keys.update(
                    _schema_literal_keys(item, child_context)
                )
                continue
            keys.update(_schema_literal_keys(item, context))
        return keys
    if isinstance(value, (ast.List, ast.Set, ast.Tuple)):
        keys = set()
        for item in value.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                keys.add(item.value)
            else:
                keys.update(_schema_literal_keys(item, context))
        return keys
    if isinstance(value, ast.Call):
        keys = set()
        for argument in value.args:
            keys.update(_schema_literal_keys(argument, context))
        for keyword in value.keywords:
            if keyword.arg is not None:
                keys.add(keyword.arg)
                tokens = _identifier_tokens(keyword.arg)
                if context == "schema" and (
                    tokens & NON_STRUCTURAL_SCHEMA_KEYS
                ):
                    continue
                child_context = (
                    "fields"
                    if tokens & {"field", "fields", "properties"}
                    else "schema"
                )
                keys.update(
                    _schema_literal_keys(
                        keyword.value, child_context
                    )
                )
                continue
            keys.update(_schema_literal_keys(keyword.value, context))
        return keys
    return set()


def _schema_key_strings(tree):
    keys = set()
    for node in ast.walk(tree):
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
        root_context = (
            "fields"
            if any(
                _identifier_tokens(target) & {"field", "fields", "key", "keys"}
                for target in targets
            )
            else "schema"
        )
        keys.update(_schema_literal_keys(value, root_context))
    return keys


def _is_allowed_sorted_key_lambda(node, parents, bindings):
    arguments = node.args
    if (
        len(arguments.posonlyargs) + len(arguments.args) != 1
        or arguments.vararg is not None
        or arguments.kwarg is not None
        or arguments.kwonlyargs
        or arguments.defaults
        or arguments.kw_defaults
    ):
        return False
    argument = (arguments.posonlyargs + arguments.args)[0].arg
    keyword = parents.get(node)
    call = parents.get(keyword)
    if (
        not isinstance(keyword, ast.keyword)
        or keyword.arg != "key"
        or not isinstance(call, ast.Call)
        or _resolved_symbol(call.func, bindings)
        not in {"builtins.sorted", "sorted"}
    ):
        return False

    def attribute_name(value):
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == argument
        ):
            return value.attr
        return None

    if attribute_name(node.body) == "grant_id":
        return True
    return (
        isinstance(node.body, ast.Tuple)
        and tuple(attribute_name(item) for item in node.body.elts)
        == ("sequence", "action_id")
    )


def _effect_boundary_violations(tree):
    (
        bindings_by_node,
        imported_by_node,
        callables_by_node,
        structures_by_node,
    ) = _binding_environments(tree)
    violations = set()
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    for imported in _import_violations(tree):
        violations.add(f"import:{imported}")

    for node in ast.walk(tree):
        bindings = bindings_by_node.get(node, {})
        imported_symbols = imported_by_node.get(node, frozenset())
        callable_symbols = callables_by_node.get(node, frozenset())
        structures = structures_by_node.get(node, {})
        if isinstance(node, ast.Lambda) and not _is_allowed_sorted_key_lambda(
            node,
            parents,
            bindings,
        ):
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
                for resolved in _structured_leaves(
                    _symbol_structure(
                        candidate, bindings, structures
                    )
                ):
                    dynamic_import = DYNAMIC_IMPORT_SYMBOLS.get(
                        resolved
                    )
                    if dynamic_import is not None:
                        violations.add(
                            f"dynamic-import:{dynamic_import}"
                        )
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
                    structures,
                ):
                    violations.add(
                        f"function-valued-default:{argument.arg}"
                    )
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
            violations.update(
                f"callable-parameter:{name}"
                for name in _parameter_call_origins(
                    node,
                    safe_invoked_parameters,
                    bindings_by_node,
                )
            )
        elif isinstance(node, ast.ClassDef):
            forbidden = _boundary_surface_tokens(node.name)
            violations.update(f"class:{token}" for token in forbidden)
            if not _is_dataclass(node, bindings, structures):
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
                field_structures = structures_by_node.get(
                    field, structures
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
                        field_structures,
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


def _module_control_flow_bodies(node):
    if isinstance(node, ast.If):
        return (node.body, node.orelse)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return (node.body, node.orelse)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return (node.body,)
    if isinstance(node, (ast.Try, ast.TryStar)):
        return (
            node.body,
            node.orelse,
            node.finalbody,
            *(handler.body for handler in node.handlers),
        )
    if isinstance(node, ast.Match):
        return tuple(case.body for case in node.cases)
    return ()


def _module_scope_nodes(statements):
    for node in statements:
        yield node
        for body in _module_control_flow_bodies(node):
            yield from _module_scope_nodes(body)


def _package_public_bindings(tree):
    bindings = set()
    for node in _module_scope_nodes(tree.body):
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
    def merge(states):
        merged = {}
        for name in set().union(*(state.keys() for state in states)):
            merged[name] = frozenset().union(
                *(state.get(name, frozenset()) for state in states)
            )
        return merged

    def resolved(node, bindings):
        if isinstance(node, ast.Name):
            return bindings.get(node.id, frozenset({node.id}))
        if isinstance(node, ast.Attribute):
            owners = resolved(node.value, bindings)
            return frozenset(f"{owner}.{node.attr}" for owner in owners)
        return frozenset({f"<{type(node).__name__}>"})

    def walk(statements, inherited):
        bindings = dict(inherited)
        for node in statements:
            if isinstance(node, ast.If):
                bindings = merge(
                    [
                        walk(node.body, bindings),
                        walk(node.orelse, bindings),
                    ]
                )
                continue
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                loop_state = merge(
                    [bindings, walk(node.body, bindings)]
                )
                bindings = merge(
                    [loop_state, walk(node.orelse, loop_state)]
                )
                continue
            if isinstance(node, (ast.With, ast.AsyncWith)):
                bindings = merge(
                    [bindings, walk(node.body, bindings)]
                )
                continue
            if isinstance(node, (ast.Try, ast.TryStar)):
                body_state = walk(node.body, bindings)
                states = [
                    bindings,
                    walk(node.orelse, body_state),
                    *(
                        walk(handler.body, bindings)
                        for handler in node.handlers
                    ),
                ]
                bindings = walk(node.finalbody, merge(states))
                continue
            if isinstance(node, ast.Match):
                bindings = merge(
                    [
                        bindings,
                        *(
                            walk(case.body, bindings)
                            for case in node.cases
                        ),
                    ]
                )
                continue

            imported, _ = _imported_bindings(node)
            bindings.update(
                {
                    name: frozenset({provenance})
                    for name, provenance in imported.items()
                }
            )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bindings[node.name] = frozenset(
                    {f"<local-function>.{node.name}"}
                )
            elif isinstance(node, ast.ClassDef):
                bindings[node.name] = frozenset(
                    {f"<local-class>.{node.name}"}
                )
            elif isinstance(node, ast.Assign):
                provenance = resolved(node.value, bindings)
                for target in node.targets:
                    for name in _target_names(target):
                        bindings[name] = provenance
            elif isinstance(node, ast.AnnAssign):
                provenance = resolved(node.value, bindings)
                for name in _target_names(node.target):
                    bindings[name] = provenance
        return bindings

    bindings = walk(tree.body, {})
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
        and package_provenance[name] != frozenset({expected})
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

    def test_effect_adapter_boundary_scanner_resolves_unpacking_and_walrus_aliases(
        self,
    ):
        cases = {
            "tuple-unpacked dataclass": (
                "from dataclasses import dataclass\n"
                "record, = (dataclass,)\n"
                "@record\n"
                "class Request:\n"
                "    graph: object\n",
                "dataclass-field:graph",
            ),
            "walrus dataclass": (
                "from dataclasses import dataclass\n"
                "if (record := dataclass):\n"
                "    @record\n"
                "    class Request:\n"
                "        graph: object\n",
                "dataclass-field:graph",
            ),
            "walrus callable": (
                "def helper():\n"
                "    pass\n"
                "if (callback := helper):\n"
                "    def run(fn=callback):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "walrus function default value": (
                "def helper():\n"
                "    pass\n"
                "def run(fn=(callback := helper)):\n"
                "    pass\n",
                "function-valued-default:fn",
            ),
            "walrus dataclass default value": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = field(default=(callback := helper))\n",
                "function-valued-field:worker",
            ),
            "starred unpacked dataclass": (
                "from dataclasses import dataclass\n"
                "first, *records = (object, dataclass)\n"
                "@records[0]\n"
                "class Request:\n"
                "    graph: object\n",
                "dataclass-field:graph",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
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

    def test_effect_adapter_boundary_scanner_tracks_control_flow_bindings(
        self,
    ):
        cases = {
            "if callable default": (
                "if enabled:\n"
                "    from .validation import validate_action_plan\n"
                "    helper = validate_action_plan\n"
                "    def run(fn=helper):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "try field alias": (
                "from dataclasses import dataclass\n"
                "try:\n"
                "    from dataclasses import field\n"
                "    def helper():\n"
                "        pass\n"
                "    @dataclass\n"
                "    class Request:\n"
                "        worker: object = field(default=helper)\n"
                "except Exception:\n"
                "    pass\n",
                "function-valued-field:worker",
            ),
            "loop declaration": (
                "for item in values:\n"
                "    def helper():\n"
                "        pass\n"
                "    def run(fn=helper):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "with declaration": (
                "with manager:\n"
                "    def helper():\n"
                "        pass\n"
                "    def run(fn=helper):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "match declaration": (
                "match value:\n"
                "    case _:\n"
                "        def helper():\n"
                "            pass\n"
                "        def run(fn=helper):\n"
                "            pass\n",
                "function-valued-default:fn",
            ),
            "post-if callable alias": (
                "from .validation import validate_action_plan\n"
                "helper = None\n"
                "if enabled:\n"
                "    helper = validate_action_plan\n"
                "def run(fn=helper):\n"
                "    pass\n",
                "function-valued-default:fn",
            ),
            "post-if callable wins over constant": (
                "from .effect_adapter_fixtures import "
                "EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION\n"
                "from .validation import validate_action_plan\n"
                "helper = EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION\n"
                "if enabled:\n"
                "    helper = validate_action_plan\n"
                "def run(fn=helper):\n"
                "    pass\n",
                "function-valued-default:fn",
            ),
            "post-try field alias": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "make_field = object\n"
                "try:\n"
                "    make_field = field\n"
                "except Exception:\n"
                "    pass\n"
                "@dataclass\n"
                "class Request:\n"
                "    worker: object = make_field(default=helper)\n",
                "function-valued-field:worker",
            ),
            "loop target callable": (
                "def helper():\n"
                "    pass\n"
                "for callback in (helper,):\n"
                "    def run(fn=callback):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "loop target field": (
                "from dataclasses import dataclass, field\n"
                "def helper():\n"
                "    pass\n"
                "for make_field in (field,):\n"
                "    @dataclass\n"
                "    class Request:\n"
                "        worker: object = make_field(default=helper)\n",
                "function-valued-field:worker",
            ),
            "match capture callable": (
                "def helper():\n"
                "    pass\n"
                "match helper:\n"
                "    case callback:\n"
                "        def run(fn=callback):\n"
                "            pass\n",
                "function-valued-default:fn",
            ),
            "aliased loop source": (
                "def helper():\n"
                "    pass\n"
                "callbacks = (helper,)\n"
                "for callback in callbacks:\n"
                "    def run(fn=callback):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "constructed loop source": (
                "def helper():\n"
                "    pass\n"
                "for callback in iter((helper,)):\n"
                "    def run(fn=callback):\n"
                "        pass\n",
                "function-valued-default:fn",
            ),
            "aliased sequence match": (
                "def helper():\n"
                "    pass\n"
                "callbacks = (helper,)\n"
                "match callbacks:\n"
                "    case (callback,):\n"
                "        def run(fn=callback):\n"
                "            pass\n",
                "function-valued-default:fn",
            ),
            "mapping pattern capture": (
                "def helper():\n"
                "    pass\n"
                "callbacks = {'main': helper}\n"
                "match callbacks:\n"
                "    case {'main': callback}:\n"
                "        def run(fn=callback):\n"
                "            pass\n",
                "function-valued-default:fn",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
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
        metadata_schema = ast.parse(
            "SCHEMA = {\n"
            "    'metadata': {'graph': 'documentation label'},\n"
            "    'examples': [{'service': 'example value'}],\n"
            "}\n"
        )
        metadata_violations = _effect_boundary_violations(metadata_schema)
        self.assertNotIn("schema-key:graph", metadata_violations)
        self.assertNotIn("schema-key:service", metadata_violations)
        nested_metadata_schema = ast.parse(
            "SCHEMA = {\n"
            "    'properties': {\n"
            "        'item': {\n"
            "            'metadata': {'graph': 'documentation label'},\n"
            "            'default': {'callback': 'example default'},\n"
            "            'examples': [{'service': 'example value'}],\n"
            "        },\n"
            "    },\n"
            "}\n"
        )
        nested_metadata_violations = _effect_boundary_violations(
            nested_metadata_schema
        )
        self.assertNotIn("schema-key:graph", nested_metadata_violations)
        self.assertNotIn("schema-key:callback", nested_metadata_violations)
        self.assertNotIn("schema-key:service", nested_metadata_violations)
        named_metadata_fields = ast.parse(
            "SCHEMA = {\n"
            "    'properties': {\n"
            "        'metadata': {'properties': {'graph': {}}},\n"
            "        'default': {'properties': {'service': {}}},\n"
            "        'examples': {'properties': {'callback': {}}},\n"
            "    },\n"
            "}\n"
        )
        named_field_violations = _effect_boundary_violations(
            named_metadata_fields
        )
        self.assertIn("schema-key:graph", named_field_violations)
        self.assertIn("schema-key:service", named_field_violations)
        self.assertIn("schema-key:callback", named_field_violations)
        explicit_field_collection = ast.parse(
            "SCHEMA_FIELDS = {'metadata': {'graph'}}\n"
        )
        self.assertIn(
            "schema-key:graph",
            _effect_boundary_violations(explicit_field_collection),
        )
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

    def test_effect_adapter_boundary_scanner_checks_nested_schema_declarations(
        self,
    ):
        cases = {
            "function schema": (
                "def build():\n"
                "    schema = {'properties': {'callback': {}}}\n",
                "schema-key:callback",
            ),
            "class fields": (
                "class Definition:\n"
                "    SCHEMA_FIELDS = {'request': {'graph'}}\n",
                "schema-key:graph",
            ),
        }

        for label, (source, expected) in cases.items():
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

        depth_limited_default = "helper"
        for _ in range(MAX_STRUCTURE_DEPTH + 1):
            depth_limited_default = f"[{depth_limited_default}]"
        depth_limited_tree = ast.parse(
            "def helper():\n"
            "    pass\n"
            f"def run(fn={depth_limited_default}):\n"
            "    pass\n"
        )
        self.assertIn(
            "function-valued-default:fn",
            _effect_boundary_violations(depth_limited_tree),
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

        ordinary_imported_defaults = {
            "enum member": (
                "from .effect_adapter import DryRunStatus\n"
                "def run(status=DryRunStatus.BLOCKED):\n"
                "    pass\n"
            ),
            "schema version": (
                "from .effect_adapter_fixtures import "
                "EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION\n"
                "def run(version=EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION):\n"
                "    pass\n"
            ),
        }
        for label, source in ordinary_imported_defaults.items():
            with self.subTest(label=label):
                self.assertFalse(
                    any(
                        item.startswith("function-valued-default:")
                        for item in _effect_boundary_violations(
                            ast.parse(source)
                        )
                    )
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

    def test_effect_adapter_boundary_scanner_tracks_parameter_indirection(self):
        cases = {
            "assigned alias": (
                "def run(fn):\n"
                "    runner = fn\n"
                "    return runner()\n"
            ),
            "dunder call": (
                "def run(fn):\n"
                "    return fn.__call__()\n"
            ),
            "higher-order call": (
                "def run(fn, values):\n"
                "    return map(fn, values)\n"
            ),
            "nested capture": (
                "def run(fn):\n"
                "    def invoke():\n"
                "        return fn()\n"
                "    return invoke()\n"
            ),
            "nested decorator": (
                "def run(fn):\n"
                "    @fn\n"
                "    def wrapped():\n"
                "        pass\n"
                "    return wrapped\n"
            ),
            "literal tuple subscript": (
                "def run(fn):\n"
                "    return (fn,)[0]()\n"
            ),
            "literal list subscript": (
                "def run(fn):\n"
                "    return [fn][0]()\n"
            ),
            "aliased container subscript": (
                "def run(fn):\n"
                "    runners = (fn,)\n"
                "    return runners[0]()\n"
            ),
            "attribute storage": (
                "def run(fn, holder):\n"
                "    holder.runner = fn\n"
                "    return holder.runner()\n"
            ),
            "reflective dunder call": (
                "def run(fn):\n"
                "    return getattr(fn, '__call__')()\n"
            ),
            "walrus call alias": (
                "def run(fn):\n"
                "    return (runner := fn)()\n"
            ),
            "nested class decorator": (
                "def run(fn):\n"
                "    @fn\n"
                "    class Wrapped:\n"
                "        pass\n"
                "    return Wrapped\n"
            ),
            "aliased getattr": (
                "def run(fn):\n"
                "    lookup = getattr\n"
                "    return lookup(fn, '__call__')()\n"
            ),
            "injected service method": (
                "def run(adapter):\n"
                "    return adapter.execute()\n"
            ),
            "dict subscript": (
                "def run(fn):\n"
                "    return {'main': fn}['main']()\n"
            ),
            "recursively nested containers": (
                "def run(fn):\n"
                "    return [(fn,)][0][0]()\n"
            ),
            "deep mixed containers": (
                "def run(fn):\n"
                "    handlers = {'outer': [{'inner': (fn,)}]}\n"
                "    return handlers['outer'][0]['inner'][0]()\n"
            ),
            "aliased loop container": (
                "def run(fn):\n"
                "    callbacks = (fn,)\n"
                "    for callback in callbacks:\n"
                "        return callback()\n"
            ),
            "aliased match container": (
                "def run(fn):\n"
                "    callbacks = {'main': fn}\n"
                "    match callbacks:\n"
                "        case {'main': callback}:\n"
                "            return callback()\n"
            ),
            "flattened starred container": (
                "def run(fn):\n"
                "    source = (fn,)\n"
                "    callbacks = (*source,)\n"
                "    return callbacks[0]()\n"
            ),
            "starred assignment target": (
                "def run(fn):\n"
                "    first, *runners = (object, fn)\n"
                "    return runners[0]()\n"
            ),
            "computed container subscript": (
                "def run(fn, key):\n"
                "    callbacks = (fn,)\n"
                "    return callbacks[key]()\n"
            ),
            "enumerated loop callback": (
                "def run(fn):\n"
                "    callbacks = (fn,)\n"
                "    for _, callback in enumerate(callbacks):\n"
                "        return callback()\n"
            ),
            "saved iterator loop source": (
                "def run(fn):\n"
                "    callbacks = (fn,)\n"
                "    source = iter(callbacks)\n"
                "    for callback in source:\n"
                "        return callback()\n"
            ),
            "nested sequence capture": (
                "def run(fn):\n"
                "    callbacks = ((fn,),)\n"
                "    match callbacks:\n"
                "        case ((callback,),):\n"
                "            return callback()\n"
            ),
            "nested mapping capture": (
                "def run(fn):\n"
                "    callbacks = {'outer': {'inner': fn}}\n"
                "    match callbacks:\n"
                "        case {'outer': {'inner': callback}}:\n"
                "            return callback()\n"
            ),
            "generic injected service method": (
                "def run(worker):\n"
                "    return worker.run()\n"
            ),
            "annotated injected service method": (
                "def run(adapter: object):\n"
                "    return adapter.execute()\n"
            ),
            "attribute decorator owner": (
                "def run(adapter):\n"
                "    @adapter.decorate\n"
                "    def wrapped():\n"
                "        pass\n"
                "    return wrapped\n"
            ),
            "extracted attribute method": (
                "def run(adapter):\n"
                "    action = adapter.execute\n"
                "    return action()\n"
            ),
            "extracted getattr method": (
                "def run(adapter):\n"
                "    action = getattr(adapter, 'execute')\n"
                "    return action()\n"
            ),
            "nested class body method": (
                "def run(adapter):\n"
                "    class Wrapped:\n"
                "        action = adapter.execute\n"
                "        @adapter.decorate\n"
                "        def invoke(self):\n"
                "            pass\n"
                "    return Wrapped\n"
            ),
            "nested class captured invocation": (
                "def run(adapter):\n"
                "    class Wrapped:\n"
                "        def invoke(self):\n"
                "            return adapter.execute()\n"
                "    return Wrapped\n"
            ),
        }

        for label, source in cases.items():
            with self.subTest(label=label):
                parameter = (
                    "adapter"
                    if label
                    in {
                        "injected service method",
                        "annotated injected service method",
                        "attribute decorator owner",
                        "extracted attribute method",
                        "extracted getattr method",
                        "nested class body method",
                        "nested class captured invocation",
                    }
                    else "worker"
                    if label == "generic injected service method"
                    else "fn"
                )
                self.assertIn(
                    f"callable-parameter:{parameter}",
                    _effect_boundary_violations(ast.parse(source)),
                )

        ordinary_alias = ast.parse(
            "def run(value):\n"
            "    item = value\n"
            "    return (item,)\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(ordinary_alias),
        )
        stored_only = ast.parse(
            "def run(value, holder):\n"
            "    values = (value,)\n"
            "    holder.item = value\n"
            "    return values, holder\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(stored_only),
        )
        nested_storage_only = ast.parse(
            "def run(value):\n"
            "    values = {'main': [(value,)]}\n"
            "    return values\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(nested_storage_only),
        )
        expanded_storage_only = ast.parse(
            "def run(value, key):\n"
            "    source = (value,)\n"
            "    values = (*source,)\n"
            "    return values[key]\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(expanded_storage_only),
        )
        proven_pure_method = ast.parse(
            "def normalize(value):\n"
            "    return value.strip()\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(proven_pure_method),
        )
        nested_value = "fn"
        for _ in range(12):
            nested_value = f"[{nested_value}]"
        deep_call = ast.parse(
            "def run(fn):\n"
            f"    return {nested_value}{'[0]' * 12}()\n"
        )
        self.assertIn(
            "callable-parameter:fn",
            _effect_boundary_violations(deep_call),
        )
        depth_limited_value = "fn"
        for _ in range(MAX_STRUCTURE_DEPTH + 1):
            depth_limited_value = f"[{depth_limited_value}]"
        depth_limited_call = ast.parse(
            "def run(fn):\n"
            f"    return {depth_limited_value}"
            f"{'[0]' * (MAX_STRUCTURE_DEPTH + 1)}()\n"
        )
        self.assertIn(
            "callable-parameter:fn",
            _effect_boundary_violations(depth_limited_call),
        )
        cyclic_aliases = ast.parse(
            "def run(value):\n"
            "    first = second\n"
            "    second = first\n"
            "    return first, second, value\n"
        )
        self.assertNotIn(
            "callable-parameter:value",
            _effect_boundary_violations(cyclic_aliases),
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
            "flattened stored builtin tuple": (
                "source = (__import__,)\n"
                "loads = (*source,)\n"
                "loaded = loads[0]('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "computed stored builtin subscript": (
                "loads = (__import__,)\n"
                "index = 0\n"
                "loaded = loads[index]('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "builtins mapping lookup": (
                "load = __builtins__['__import__']\n"
                "loaded = load('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "dict copied builtins": (
                "namespace = dict(__builtins__)\n"
                "loaded = namespace['__import__']"
                "('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "method copied builtins": (
                "namespace = __builtins__.copy()\n"
                "loaded = namespace['__import__']"
                "('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "unpacked builtins mapping": (
                "namespace = {**__builtins__}\n"
                "loaded = namespace['__import__']"
                "('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
            "saved builtins copy method": (
                "copy_namespace = __builtins__.copy\n"
                "namespace = copy_namespace()\n"
                "loaded = namespace['__import__']"
                "('email_automation.messaging')\n",
                "dynamic-import:__import__",
            ),
        }

        for label, (source, expected) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected,
                    _effect_boundary_violations(ast.parse(source)),
                )

        safe_mapping = ast.parse(
            "namespace = {'safe': object}\n"
            "value = namespace['safe']\n"
        )
        self.assertFalse(
            any(
                item.startswith("dynamic-import:")
                for item in _effect_boundary_violations(safe_mapping)
            )
        )

    def test_effect_adapter_files_allow_only_pure_sorted_key_lambdas(self):
        adapter_path = PACKAGE_ROOT / "effect_adapter.py"
        adapter_tree = ast.parse(
            adapter_path.read_text(encoding="utf-8"),
            filename=str(adapter_path),
        )
        adapter_lambdas = [
            node for node in ast.walk(adapter_tree) if isinstance(node, ast.Lambda)
        ]

        self.assertEqual(2, len(adapter_lambdas))
        self.assertEqual(set(), _effect_boundary_violations(adapter_tree))
        fixture_tree = ast.parse(
            (PACKAGE_ROOT / "effect_adapter_fixtures.py").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(
            any(isinstance(node, ast.Lambda) for node in ast.walk(fixture_tree))
        )

        allowed = {
            "single attribute": (
                "items = sorted(values, key=lambda item: item.grant_id)\n"
            ),
            "attribute tuple": (
                "items = sorted(\n"
                "    values,\n"
                "    key=lambda item: (item.sequence, item.action_id),\n"
                ")\n"
            ),
        }
        for label, source in allowed.items():
            with self.subTest(label=label):
                self.assertFalse(
                    any(
                        item.startswith("lambda:")
                        for item in _effect_boundary_violations(
                            ast.parse(source)
                        )
                    )
                )

        rejected = {
            "arbitrary lambda": "callback = lambda value: value\n",
            "call in sorted key": (
                "items = sorted(values, key=lambda item: helper(item))\n"
            ),
            "allowed shape outside sorted": (
                "callback = lambda item: item.grant_id\n"
            ),
            "unrelated sorted attribute": (
                "items = sorted(values, key=lambda item: item.callback)\n"
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
            "if rebinding": (
                "if enabled:\n"
                "    evaluate_effect_plan = None\n"
            ),
            "try rebinding": (
                "try:\n"
                "    evaluate_effect_plan = None\n"
                "except Exception:\n"
                "    pass\n"
            ),
            "match rebinding": (
                "match mode:\n"
                "    case _:\n"
                "        evaluate_effect_plan = None\n"
            ),
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
