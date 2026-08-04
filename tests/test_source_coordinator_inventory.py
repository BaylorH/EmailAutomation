import ast
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_COORDINATOR_RELATIVE_PATH = Path("email_automation/source_coordinator.py")
SOURCE_COORDINATOR_PATH = REPO_ROOT / SOURCE_COORDINATOR_RELATIVE_PATH
FORBIDDEN_SOURCE_COORDINATOR_IMPORT_ROOTS = {
    "googleapiclient",
    "openai",
    "requests",
}
FORBIDDEN_SOURCE_COORDINATOR_APPLICATION_MODULES = {
    "ai_processing",
    "email",
    "file_handling",
    "sheet_operations",
    "sheets",
}
DRAFT_DELETE_MANIFEST_PATH = REPO_ROOT / "tests/fixtures/graph_draft_delete_callers.json"
DRAFT_DELETE_HELPER_NAME = "_delete_graph_reply_draft"
DRAFT_DELETE_EMAIL_MODULE = "email_automation.email"
DRAFT_DELETE_EMAIL_PATH = "email_automation/email.py"
SOURCE_ADMISSION_METHOD = "admit_or_repair_source_identity"
SOURCE_ADMISSION_PRIVATE_ENVELOPE = "_SourceAdmissionEnvelope"
SOURCE_CLASSIFICATION_PRIVATE_EVIDENCE = "_VerifiedHardOptoutEvidence"
SOURCE_RETAINED_TERMINAL_PRIVATE_EVIDENCE = (
    "_VerifiedRetainedTerminalEvidence"
)
SOURCE_CLASSIFICATION_PRIVATE_NAMES = {
    SOURCE_CLASSIFICATION_PRIVATE_EVIDENCE,
    SOURCE_RETAINED_TERMINAL_PRIVATE_EVIDENCE,
}
SOURCE_CLASSIFICATION_VERIFIER_NAME = "hard_optout_verifier"
SOURCE_CLASSIFICATION_ORCHESTRATOR = "classify_source_once"
SOURCE_ADMISSION_ALLOWED_ADAPTERS = {
    ("email_automation/processing.py", "process_inbox_message"),
    ("email_automation/operator_replay.py", "replay_exact_message"),
}
SOURCE_ADMISSION_ALLOWED_CALLABLE_SCOPES = {
    *((path, (function,)) for path, function in SOURCE_ADMISSION_ALLOWED_ADAPTERS),
    (
        "email_automation/operator_replay.py",
        ("replay_exact_message", "_under_lease"),
    ),
}
SOURCE_COORDINATOR_FACTORY_CALLABLE_SCOPE = (
    "email_automation/processing.py",
    ("build_source_coordinator",),
)

EXPECTED_DRAFT_DELETE_MANIFEST = {
    "schemaVersion": 1,
    "deferred": [
        {"path": DRAFT_DELETE_EMAIL_PATH, "function": "_send_outbox_as_reply", "count": 2},
        {"path": DRAFT_DELETE_EMAIL_PATH, "function": "send_and_index_email", "count": 1},
        {"path": "email_automation/followup.py", "function": "_send_followup_email", "count": 13},
    ],
    "m2Owned": [
        {"path": "email_automation/processing.py", "function": "send_reply_in_thread", "count": 5}
    ],
}
EXPECTED_DRAFT_DELETE_CALLERS = Counter(
    {
        (DRAFT_DELETE_EMAIL_PATH, "_send_outbox_as_reply"): 2,
        (DRAFT_DELETE_EMAIL_PATH, "send_and_index_email"): 1,
        ("email_automation/followup.py", "_send_followup_email"): 13,
        ("email_automation/processing.py", "send_reply_in_thread"): 5,
    }
)
EXPECTED_LEGACY_MARKER_DEFINITIONS = Counter(
    {
        ("email_automation/messaging.py", "has_processed"): 1,
        ("email_automation/messaging.py", "mark_processed"): 1,
        ("email_automation/messaging.py", "_legacy_has_processed"): 1,
        ("email_automation/messaging.py", "_legacy_mark_processed"): 1,
        ("scheduler_runner.py", "has_processed"): 1,
        ("scheduler_runner.py", "mark_processed"): 1,
        ("scheduler_runner.py", "_legacy_has_processed"): 1,
        ("scheduler_runner.py", "_legacy_mark_processed"): 1,
        ("email_automation/operator_replay.py", "_begin_replay_claim"): 1,
        ("email_automation/operator_replay.py", "_complete_replay_claim"): 1,
    }
)
_EXCLUDED_APPLICATION_PARTS = {
    ".git", ".mypy_cache", ".pytest_cache", ".venv", "__pycache__",
    "node_modules", "site-packages", "vendor", "venv",
}


class DraftDeleteBindingInventoryError(AssertionError):
    """The conservative inventory cannot safely classify protected syntax."""


def _fail_inventory(reason):
    raise DraftDeleteBindingInventoryError(reason)


def _is_application_python_file(relative_path):
    return not (
        (relative_path.parts and relative_path.parts[0] == "tests")
        or relative_path.name.startswith("test_")
        or any(part in _EXCLUDED_APPLICATION_PARTS for part in relative_path.parts)
    )


def _application_python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative_path = path.relative_to(REPO_ROOT)
        if _is_application_python_file(relative_path):
            yield relative_path


def _parse_module(relative_path):
    path = REPO_ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _resolve_import_from_module(node, relative_path):
    if not node.level:
        return node.module or ""
    package_parts = list(Path(relative_path).parent.parts)
    parents_to_drop = node.level - 1
    if parents_to_drop > len(package_parts):
        return ""
    if parents_to_drop:
        package_parts = package_parts[:-parents_to_drop]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


def _source_coordinator_import_paths(node):
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, False
        return
    if not isinstance(node, ast.ImportFrom):
        return

    module = _resolve_import_from_module(node, SOURCE_COORDINATOR_RELATIVE_PATH)
    if module:
        yield module, False
    for alias in node.names:
        imported_path = f"{module}.{alias.name}" if module else alias.name
        yield imported_path, False

    if node.level:
        relative_module = node.module or ""
        if relative_module:
            yield relative_module, True
        for alias in node.names:
            relative_path = (
                f"{relative_module}.{alias.name}"
                if relative_module
                else alias.name
            )
            yield relative_path, True


def _source_coordinator_import_path_is_forbidden(path, *, relative):
    parts = path.split(".")
    if not parts:
        return False
    if parts[0] in FORBIDDEN_SOURCE_COORDINATOR_IMPORT_ROOTS:
        return True
    if relative and parts[0] in FORBIDDEN_SOURCE_COORDINATOR_APPLICATION_MODULES:
        return True
    return (
        len(parts) > 1
        and parts[0] == "email_automation"
        and parts[1] in FORBIDDEN_SOURCE_COORDINATOR_APPLICATION_MODULES
    )


def _source_coordinator_forbidden_imports(tree):
    forbidden = []
    seen = set()
    for node in ast.walk(tree):
        for path, relative in _source_coordinator_import_paths(node):
            finding = (node.lineno, path)
            if (
                finding not in seen
                and _source_coordinator_import_path_is_forbidden(
                    path, relative=relative
                )
            ):
                seen.add(finding)
                forbidden.append(finding)
    return forbidden


_REFLECTIVE_ACCESS_NAMES = {
    "getattr",
    "setattr",
    "delattr",
    "__getattribute__",
    "__setattr__",
    "__delattr__",
    "__dict__",
    "attrgetter",
    "methodcaller",
    "vars",
}
_PROTECTED_REFLECTION_NAMES = {
    SOURCE_ADMISSION_METHOD,
    SOURCE_ADMISSION_PRIVATE_ENVELOPE,
    *SOURCE_CLASSIFICATION_PRIVATE_NAMES,
}
def _is_reflective_attribute_reference(node):
    return (
        isinstance(node, ast.Name)
        and node.id in _REFLECTIVE_ACCESS_NAMES
    ) or (
        isinstance(node, ast.Attribute)
        and node.attr in _REFLECTIVE_ACCESS_NAMES
    ) or (
        isinstance(node, ast.ImportFrom)
        and any(
            imported.name in _REFLECTIVE_ACCESS_NAMES
            for imported in node.names
        )
    )


def _literal_fragments(tree):
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and type(node.value) is str
        and node.value
        and any(
            node.value in protected_name
            for protected_name in _PROTECTED_REFLECTION_NAMES
        )
    }


def _can_segment_static_name(name, fragments):
    reachable = {0}
    for start in range(len(name)):
        if start not in reachable:
            continue
        for fragment in fragments:
            if name.startswith(fragment, start):
                reachable.add(start + len(fragment))
    return len(name) in reachable


def _reflectively_assembled_protected_names(tree):
    if not any(
        _is_reflective_attribute_reference(node) for node in ast.walk(tree)
    ):
        return set()
    fragments = _literal_fragments(tree)
    return {
        name
        for name in _PROTECTED_REFLECTION_NAMES
        if _can_segment_static_name(name, fragments)
    }


class _SourceAdmissionContractVisitor(ast.NodeVisitor):
    """Reject private-envelope access and non-adapter admission references."""

    def __init__(self, relative_path, reflectively_assembled_names):
        self.relative_path = Path(relative_path).as_posix()
        self.violations = []
        self._function_scopes = []
        self._class_depth = 0
        self._direct_call_references = set()
        self._reflectively_assembled_names = reflectively_assembled_names
        self._recorded_reflection_names = set()

    def _record(self, node, reason):
        self.violations.append((getattr(node, "lineno", 0), reason))

    def _inside_reviewed_adapter(self):
        return (
            self._class_depth == 0
            and (self.relative_path, tuple(self._function_scopes))
            in SOURCE_ADMISSION_ALLOWED_CALLABLE_SCOPES
        )

    def _record_reflective_fragment_assembly(self, node):
        if (
            SOURCE_ADMISSION_METHOD in self._reflectively_assembled_names
            and SOURCE_ADMISSION_METHOD not in self._recorded_reflection_names
        ):
            self._record(node, "dynamic admission method reference")
            self._recorded_reflection_names.add(SOURCE_ADMISSION_METHOD)
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and SOURCE_ADMISSION_PRIVATE_ENVELOPE
            in self._reflectively_assembled_names
            and SOURCE_ADMISSION_PRIVATE_ENVELOPE
            not in self._recorded_reflection_names
        ):
            self._record(node, "dynamic private admission envelope reference")
            self._recorded_reflection_names.add(SOURCE_ADMISSION_PRIVATE_ENVELOPE)
        if self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix():
            for private_name in sorted(SOURCE_CLASSIFICATION_PRIVATE_NAMES):
                if (
                    private_name in self._reflectively_assembled_names
                    and private_name not in self._recorded_reflection_names
                ):
                    self._record(
                        node,
                        "dynamic private classification authority reference",
                    )
                    self._recorded_reflection_names.add(private_name)

    def visit_FunctionDef(self, node):
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.name == SOURCE_ADMISSION_PRIVATE_ENVELOPE
        ):
            self._record(node, "private admission envelope definition")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.name == SOURCE_ADMISSION_METHOD
        ):
            self._record(node, "admission method definition/propagation")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.name in SOURCE_CLASSIFICATION_PRIVATE_NAMES
        ):
            self._record(node, "private classification authority definition")
        _visit_function_metadata(self, node)
        self._function_scopes.append(node.name)
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._function_scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.name == SOURCE_ADMISSION_PRIVATE_ENVELOPE
        ):
            self._record(node, "private admission envelope definition")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.name in SOURCE_CLASSIFICATION_PRIVATE_NAMES
        ):
            self._record(node, "private classification authority definition")
        _visit_class_metadata(self, node)
        self._class_depth += 1
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._class_depth -= 1

    def visit_Lambda(self, node):
        self.visit(node.args)
        self._function_scopes.append(
            f"<lambda@{node.lineno}:{node.col_offset}>"
        )
        try:
            self.visit(node.body)
        finally:
            self._function_scopes.pop()

    def _visit_comprehension_scope(self, node):
        self._function_scopes.append(
            f"<{type(node).__name__.lower()}@{node.lineno}:{node.col_offset}>"
        )
        try:
            self.generic_visit(node)
        finally:
            self._function_scopes.pop()

    def visit_GeneratorExp(self, node):
        self._visit_comprehension_scope(node)

    def visit_ListComp(self, node):
        self._visit_comprehension_scope(node)

    def visit_SetComp(self, node):
        self._visit_comprehension_scope(node)

    def visit_DictComp(self, node):
        self._visit_comprehension_scope(node)

    def visit_ImportFrom(self, node):
        if _is_reflective_attribute_reference(node):
            self._record_reflective_fragment_assembly(node)
        for imported in node.names:
            if imported.name == SOURCE_ADMISSION_PRIVATE_ENVELOPE:
                self._record(node, "private admission envelope import")
            if imported.name == SOURCE_ADMISSION_METHOD:
                self._record(node, "admission method import/propagation")
            if imported.name in SOURCE_CLASSIFICATION_PRIVATE_NAMES:
                self._record(node, "private classification authority import")

    def visit_Call(self, node):
        protected_reference = (
            isinstance(node.func, ast.Name)
            and node.func.id == SOURCE_ADMISSION_METHOD
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == SOURCE_ADMISSION_METHOD
        )
        if protected_reference:
            self._direct_call_references.add(id(node.func))
            if not self._inside_reviewed_adapter():
                self._record(node, "admission call outside reviewed adapter")
        self.generic_visit(node)

    def visit_Name(self, node):
        if _is_reflective_attribute_reference(node):
            self._record_reflective_fragment_assembly(node)
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.id == SOURCE_ADMISSION_PRIVATE_ENVELOPE
        ):
            self._record(node, "private admission envelope reference")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.id in SOURCE_CLASSIFICATION_PRIVATE_NAMES
        ):
            self._record(node, "private classification authority reference")
        if (
            node.id == SOURCE_ADMISSION_METHOD
            and id(node) not in self._direct_call_references
        ):
            self._record(node, "admission method propagation")

    def visit_Attribute(self, node):
        if _is_reflective_attribute_reference(node):
            self._record_reflective_fragment_assembly(node)
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.attr == SOURCE_ADMISSION_PRIVATE_ENVELOPE
        ):
            self._record(node, "private admission envelope attribute")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.attr in SOURCE_CLASSIFICATION_PRIVATE_NAMES
        ):
            self._record(node, "private classification authority attribute")
        if (
            node.attr == SOURCE_ADMISSION_METHOD
            and id(node) not in self._direct_call_references
        ):
            self._record(node, "admission method propagation")
        self.visit(node.value)

    def visit_Constant(self, node):
        if type(node.value) is not str:
            return
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.value == SOURCE_ADMISSION_PRIVATE_ENVELOPE
        ):
            self._record(node, "dynamic private admission envelope reference")
        if (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
            and node.value in SOURCE_CLASSIFICATION_PRIVATE_NAMES
        ):
            self._record(node, "dynamic private classification authority reference")
        if node.value == SOURCE_ADMISSION_METHOD:
            self._record(node, "dynamic admission method reference")


def _leading_import_ids(tree):
    leading = set()
    for index, statement in enumerate(tree.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and type(statement.value.value) is str
        ):
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            leading.add(id(statement))
            continue
        break
    return leading


class _TrustScope:
    def __init__(self, kind, name, local_names=()):
        self.kind = kind
        self.name = name
        self.instances = {local_name: False for local_name in local_names}
        self.static_strings = {}
        self.constructor_shadowed = "SourceCoordinator" in local_names


class _SourceClassificationTrustVisitor(ast.NodeVisitor):
    """Inventory explicit production wiring; runtime code enforces authority.

    This is intentionally a bounded AST contract, not a sandbox or a general
    Python data-flow proof. It rejects reviewed import/construction shapes and
    explicit verifier names, keywords, and direct reflective attribute access.
    """

    def __init__(self, relative_path, *, tree):
        self.relative_path = Path(relative_path).as_posix()
        self.violations = []
        self._enabled = (
            self.relative_path != SOURCE_COORDINATOR_RELATIVE_PATH.as_posix()
        )
        self._parents = {
            id(child): parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        self._leading_import_ids = _leading_import_ids(tree)
        self._reviewed_paths = {
            path for path, _ in SOURCE_ADMISSION_ALLOWED_ADAPTERS
        }
        self._has_constructor_import = any(
            isinstance(node, ast.ImportFrom)
            and _resolve_import_from_module(node, self.relative_path)
            == "email_automation.source_coordinator"
            and any(
                imported.name == "SourceCoordinator"
                for imported in node.names
            )
            for node in ast.walk(tree)
        )
        self._importlib_aliases = {"importlib"}
        self._sys_aliases = {"sys"}
        self._builtins_aliases = {"builtins"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import):
                continue
            for imported in node.names:
                bound = imported.asname or imported.name.split(".", 1)[0]
                if imported.name == "importlib":
                    self._importlib_aliases.add(bound)
                elif imported.name == "sys":
                    self._sys_aliases.add(bound)
                elif imported.name == "builtins":
                    self._builtins_aliases.add(bound)
        self._constructor_active = False
        self._allowed_constructor_refs = set()
        self._allowed_instance_refs = set()
        self._allowed_namespace_refs = set()
        self._scopes = [_TrustScope("module", "<module>")]

    def _record(self, node, reason):
        self.violations.append((getattr(node, "lineno", 0), reason))

    def _check_verifier_name(self, node, name):
        if name in {
            SOURCE_CLASSIFICATION_VERIFIER_NAME,
            f"_{SOURCE_CLASSIFICATION_VERIFIER_NAME}",
        }:
            self._record(node, "hard opt-out verifier propagation is unreviewed")

    def _lookup_static_string(self, name):
        for scope in reversed(self._scopes):
            if name in scope.static_strings:
                return scope.static_strings[name]
            if name in scope.instances:
                return None
        return None

    def _static_string(self, node):
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return node.value
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
        ):
            left = self._static_string(node.left)
            right = self._static_string(node.right)
            if left is not None and right is not None:
                return left + right
            return None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                candidate = (
                    self._static_string(value.value)
                    if isinstance(value, ast.FormattedValue)
                    else self._static_string(value)
                )
                if candidate is None:
                    return None
                parts.append(candidate)
            return "".join(parts)
        if isinstance(node, ast.Name):
            return self._lookup_static_string(node.id)
        return None

    def _set_static_string(self, target, value):
        if isinstance(target, ast.Name):
            if value is None:
                self._scopes[-1].static_strings.pop(target.id, None)
            else:
                self._scopes[-1].static_strings[target.id] = value

    def _current_callable_scope(self):
        if any(scope.kind in {"class", "lambda"} for scope in self._scopes):
            return None
        return tuple(
            scope.name
            for scope in self._scopes
            if scope.kind == "function"
        )

    def _reviewed_callable(self):
        callable_scope = self._current_callable_scope()
        return (
            callable_scope is not None
            and (self.relative_path, callable_scope)
            in SOURCE_ADMISSION_ALLOWED_CALLABLE_SCOPES
        )

    def _instance_is_protected(self, name):
        for scope in reversed(self._scopes):
            if name in scope.instances:
                return bool(scope.instances.get(name))
        return False

    def _set_instance_binding(self, name, protected):
        self._scopes[-1].instances[name] = bool(protected)

    def _constructor_name_is_protected(self, node):
        if not (
            isinstance(node, ast.Name)
            and node.id == "SourceCoordinator"
            and self._constructor_active
        ):
            return False
        return not any(
            scope.constructor_shadowed for scope in self._scopes[1:]
        )

    def _constructor_call(self, node):
        return (
            isinstance(node, ast.Call)
            and self._constructor_name_is_protected(node.func)
        )

    def _simple_constructor_assignment(self, node):
        parent = self._parents.get(id(node))
        if (
            isinstance(parent, ast.Assign)
            and parent.value is node
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
        ):
            return True
        return (
            isinstance(parent, ast.AnnAssign)
            and parent.value is node
            and isinstance(parent.target, ast.Name)
        )

    def _reviewed_factory_constructor_return(self, node):
        parent = self._parents.get(id(node))
        return (
            isinstance(parent, ast.Return)
            and parent.value is node
            and (
                self.relative_path,
                self._current_callable_scope(),
            )
            == SOURCE_COORDINATOR_FACTORY_CALLABLE_SCOPE
        )

    def _bind_assignment_target(self, target, *, protected_instance=False):
        if isinstance(target, ast.Name):
            if (
                target.id == "SourceCoordinator"
                and self._constructor_active
            ):
                if self._scopes[-1].kind == "module":
                    self._record(target, "SourceCoordinator rebinding is unreviewed")
                    self._constructor_active = False
                else:
                    self._scopes[-1].constructor_shadowed = True
            self._set_instance_binding(target.id, protected_instance)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._bind_assignment_target(item)
        elif isinstance(target, ast.Starred):
            self._bind_assignment_target(target.value)

    def _safe_namespace_membership(self, call):
        parent = self._parents.get(id(call))
        return (
            isinstance(parent, ast.Compare)
            and len(parent.ops) == 1
            and len(parent.comparators) == 1
            and isinstance(parent.ops[0], ast.In)
            and parent.comparators[0] is call
            and isinstance(parent.left, ast.Constant)
            and type(parent.left.value) is str
            and parent.left.value == "saga"
            and isinstance(call.func, ast.Name)
            and call.func.id == "locals"
            and not call.args
            and not call.keywords
        )

    def _namespace_call_name(self, func):
        if isinstance(func, ast.Name) and func.id in {
            "globals",
            "locals",
            "eval",
            "exec",
        }:
            return func.id
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self._builtins_aliases
            and func.attr in {"globals", "locals", "eval", "exec"}
        ):
            return func.attr
        return None

    def _dynamic_import_attribute(self, node):
        return (
            isinstance(node.value, ast.Name)
            and (
                (
                    node.value.id in self._importlib_aliases
                    and node.attr == "import_module"
                )
                or (
                    node.value.id in self._builtins_aliases
                    and node.attr == "__import__"
                )
                or (
                    node.value.id in self._sys_aliases
                    and node.attr == "modules"
                )
            )
        )

    def visit_Module(self, node):
        if not self._enabled:
            return
        for statement in node.body:
            self.visit(statement)

    def visit_Import(self, node):
        for imported in node.names:
            bound = imported.asname or imported.name.split(".", 1)[0]
            self._check_verifier_name(node, bound)
            if imported.name == "email_automation.source_coordinator":
                self._record(node, "source coordinator module import is unreviewed")
            if bound == "SourceCoordinator":
                self._record(node, "SourceCoordinator rebinding is unreviewed")
                self._constructor_active = False

    def visit_ImportFrom(self, node):
        module = _resolve_import_from_module(node, self.relative_path)
        if module == "email_automation.source_coordinator":
            exact_absolute_source = (
                node.level == 0
                and node.module == "email_automation.source_coordinator"
            )
            for imported in node.names:
                self._check_verifier_name(node, imported.name)
                if imported.asname:
                    self._check_verifier_name(node, imported.asname)
                if imported.name == "*":
                    self._record(node, "source coordinator star import is unreviewed")
                if imported.name != "SourceCoordinator":
                    continue
                if (
                    not exact_absolute_source
                    or len(node.names) != 1
                    or imported.asname is not None
                    or id(node) not in self._leading_import_ids
                    or self.relative_path not in self._reviewed_paths
                ):
                    self._record(node, "SourceCoordinator import is outside the reviewed grammar")
                if self._constructor_active:
                    self._record(node, "SourceCoordinator rebinding is unreviewed")
                self._constructor_active = True
            return
        for imported in node.names:
            self._check_verifier_name(node, imported.name)
            if imported.asname:
                self._check_verifier_name(node, imported.asname)
            bound = imported.asname or imported.name
            if bound == "SourceCoordinator":
                self._record(node, "SourceCoordinator import is outside the reviewed grammar")
            if (
                module == "email_automation"
                and imported.name == "source_coordinator"
            ):
                self._record(node, "source coordinator module import is unreviewed")
            if imported.name == "*" and self._has_constructor_import:
                self._record(node, "star import can rebind SourceCoordinator")
            if (
                (module == "importlib" and imported.name == "import_module")
                or (module == "builtins" and imported.name == "__import__")
                or (module == "sys" and imported.name == "modules")
            ):
                self._record(node, "dynamic module acquisition primitive is unreviewed")

    def visit_FunctionDef(self, node):
        self._check_verifier_name(node, node.name)
        if node.name == "SourceCoordinator" and self._constructor_active:
            self._record(node, "SourceCoordinator rebinding is unreviewed")
        _visit_function_metadata(self, node)
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        local_names = {argument.arg for argument in arguments}
        if node.args.vararg:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg:
            local_names.add(node.args.kwarg.arg)
        self._scopes.append(_TrustScope("function", node.name, local_names))
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._scopes.pop()
        self._set_instance_binding(node.name, False)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        local_names = {argument.arg for argument in arguments}
        if node.args.vararg:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg:
            local_names.add(node.args.kwarg.arg)
        self.visit(node.args)
        self._scopes.append(_TrustScope("lambda", "<lambda>", local_names))
        try:
            self.visit(node.body)
        finally:
            self._scopes.pop()

    def visit_ClassDef(self, node):
        self._check_verifier_name(node, node.name)
        if node.name == "SourceCoordinator" and self._constructor_active:
            self._record(node, "SourceCoordinator rebinding is unreviewed")
        _visit_class_metadata(self, node)
        self._scopes.append(_TrustScope("class", node.name))
        try:
            for statement in node.body:
                self.visit(statement)
        finally:
            self._scopes.pop()
        self._set_instance_binding(node.name, False)

    def visit_arg(self, node):
        self._check_verifier_name(node, node.arg)
        if node.annotation:
            self.visit(node.annotation)

    def visit_Assign(self, node):
        constructor_value = self._constructor_call(node.value)
        static_string = self._static_string(node.value)
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
        simple_target = (
            constructor_value
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        )
        for target in node.targets:
            self._bind_assignment_target(
                target,
                protected_instance=bool(simple_target),
            )
            self._set_static_string(target, static_string)

    def visit_AnnAssign(self, node):
        constructor_value = (
            node.value is not None and self._constructor_call(node.value)
        )
        static_string = (
            self._static_string(node.value) if node.value is not None else None
        )
        if node.value:
            self.visit(node.value)
        self.visit(node.target)
        self.visit(node.annotation)
        self._bind_assignment_target(
            node.target,
            protected_instance=bool(
                constructor_value and isinstance(node.target, ast.Name)
            ),
        )
        self._set_static_string(node.target, static_string)

    def visit_NamedExpr(self, node):
        static_string = self._static_string(node.value)
        self.visit(node.value)
        self.visit(node.target)
        self._bind_assignment_target(node.target)
        self._set_static_string(node.target, static_string)

    def visit_Call(self, node):
        if any(
            keyword.arg
            in {
                SOURCE_CLASSIFICATION_VERIFIER_NAME,
                f"_{SOURCE_CLASSIFICATION_VERIFIER_NAME}",
            }
            for keyword in node.keywords
        ):
            self._record(node, "hard opt-out verifier injection is unreviewed")

        reflective_name = None
        if isinstance(node.func, ast.Name):
            reflective_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            reflective_name = node.func.attr
        if (
            reflective_name
            in {
                "getattr",
                "setattr",
                "delattr",
                "__getattribute__",
                "__setattr__",
                "__delattr__",
            }
            and len(node.args) >= 2
            and self._static_string(node.args[1])
            in {
                SOURCE_CLASSIFICATION_VERIFIER_NAME,
                f"_{SOURCE_CLASSIFICATION_VERIFIER_NAME}",
            }
        ):
            self._record(node, "dynamic hard opt-out verifier reference")

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "SourceCoordinator"
        ):
            self._record(node, "SourceCoordinator attribute construction is unreviewed")

        if self._constructor_name_is_protected(node.func):
            self._allowed_constructor_refs.add(id(node.func))
            reviewed_factory_return = (
                self._reviewed_factory_constructor_return(node)
            )
            if not (
                self._reviewed_callable()
                or reviewed_factory_return
            ):
                self._record(node, "SourceCoordinator construction is outside a reviewed adapter")
            if not (
                self._simple_constructor_assignment(node)
                or reviewed_factory_return
            ):
                self._record(node, "SourceCoordinator result propagation is unreviewed")
            if any(keyword.arg is None for keyword in node.keywords):
                self._record(node, "SourceCoordinator constructor expansion is unreviewed")
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and self._instance_is_protected(node.func.value.id)
        ):
            if (
                self._reviewed_callable()
                and not node.func.attr.startswith("_")
            ):
                self._allowed_instance_refs.add(id(node.func.value))
            else:
                self._record(node, "source coordinator method call is outside the reviewed grammar")

        namespace_name = self._namespace_call_name(node.func)
        if isinstance(node.func, ast.Name) and namespace_name:
            self._allowed_namespace_refs.add(id(node.func))
        if namespace_name in {"eval", "exec"}:
            self._record(node, "dynamic execution primitive is unreviewed")
        elif (
            namespace_name in {"globals", "locals"}
            and self._has_constructor_import
            and not self._safe_namespace_membership(node)
        ):
            self._record(node, "source coordinator namespace recovery is unreviewed")

        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            self._record(node, "dynamic module acquisition primitive is unreviewed")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self._check_verifier_name(node, node.attr)
        if self._dynamic_import_attribute(node):
            self._record(node, "dynamic module acquisition primitive is unreviewed")
        self.generic_visit(node)

    def visit_Name(self, node):
        self._check_verifier_name(node, node.id)
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in {"eval", "exec", "__import__"}:
            parent = self._parents.get(id(node))
            if not (isinstance(parent, ast.Call) and parent.func is node):
                self._record(node, "dynamic execution or import propagation is unreviewed")
        if (
            node.id in {"globals", "locals"}
            and self._has_constructor_import
            and id(node) not in self._allowed_namespace_refs
        ):
            self._record(node, "namespace primitive propagation is unreviewed")
        if self._constructor_name_is_protected(node):
            if id(node) not in self._allowed_constructor_refs:
                self._record(node, "SourceCoordinator class propagation is unreviewed")
            return
        if (
            self._instance_is_protected(node.id)
            and id(node) not in self._allowed_instance_refs
        ):
            self._record(node, "source coordinator instance propagation is unreviewed")

    def visit_Constant(self, node):
        if node.value in {
            SOURCE_CLASSIFICATION_VERIFIER_NAME,
            f"_{SOURCE_CLASSIFICATION_VERIFIER_NAME}",
        }:
            self._record(node, "hard opt-out verifier name construction is unreviewed")

def _source_admission_contract_violations(source, relative_path):
    tree = ast.parse(source, filename=str(relative_path))
    visitor = _SourceAdmissionContractVisitor(
        relative_path,
        _reflectively_assembled_protected_names(tree),
    )
    visitor.visit(tree)
    trust_visitor = _SourceClassificationTrustVisitor(
        relative_path,
        tree=tree,
    )
    trust_visitor.visit(tree)
    return visitor.violations + trust_visitor.violations


def _discover_source_admission_contract_violations():
    violations = []
    for relative_path in _application_python_files():
        source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        for line, reason in _source_admission_contract_violations(
            source, relative_path
        ):
            violations.append((relative_path.as_posix(), line, reason))
    return violations


def _visit_function_metadata(visitor, node):
    for item in (*node.decorator_list, *getattr(node, "type_params", ())):
        visitor.visit(item)
    visitor.visit(node.args)
    if node.returns:
        visitor.visit(node.returns)


def _visit_class_metadata(visitor, node):
    for item in (
        *node.decorator_list,
        *getattr(node, "type_params", ()),
        *node.bases,
        *node.keywords,
    ):
        visitor.visit(item)


class _DraftDeleteCallVisitor(ast.NodeVisitor):
    """Count direct protected calls; reject propagation instead of modeling it."""

    def __init__(self, relative_path):
        self.relative_path = Path(relative_path).as_posix()
        self.callers = Counter()
        self._scopes = []
        self._canonical_helper_definition = None

    def _validate_protected_binding(self, name, *, allowed=False):
        if name == DRAFT_DELETE_HELPER_NAME and not allowed:
            _fail_inventory("protected helper binding is unsupported")

    def visit_Module(self, node):
        if self.relative_path == DRAFT_DELETE_EMAIL_PATH:
            helpers = [
                statement
                for statement in node.body
                if type(statement) is ast.FunctionDef
                and statement.name == DRAFT_DELETE_HELPER_NAME
            ]
            if len(helpers) == 1:
                self._canonical_helper_definition = helpers[0]
        for statement in node.body:
            self.visit(statement)

    def _caller_key(self):
        if not self._scopes:
            return "<module>"
        key = ".".join(name for _, name in self._scopes)
        return f"{key}.<class-body>" if self._scopes[-1][0] == "class" else key

    def _visit_body(self, kind, name, body):
        self._scopes.append((kind, name))
        try:
            for statement in body:
                self.visit(statement)
        finally:
            self._scopes.pop()

    def visit_FunctionDef(self, node):
        self._validate_protected_binding(
            node.name, allowed=node is self._canonical_helper_definition
        )
        # Definition-time expressions belong to the enclosing syntactic scope.
        _visit_function_metadata(self, node)
        self._visit_body("function", node.name, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node):
        self._validate_protected_binding(node.name)
        _visit_class_metadata(self, node)
        self._visit_body("class", node.name, node.body)

    def visit_Lambda(self, node):
        self.visit(node.args)
        self._scopes.append(("lambda", f"<lambda@{node.lineno}:{node.col_offset}>"))
        try:
            self.visit(node.body)
        finally:
            self._scopes.pop()

    def visit_Call(self, node):
        direct_name = isinstance(node.func, ast.Name) and node.func.id == DRAFT_DELETE_HELPER_NAME
        direct_attribute = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == DRAFT_DELETE_HELPER_NAME
        )
        if not (direct_name or direct_attribute):
            self.generic_visit(node)
            return
        self.callers[(self.relative_path, self._caller_key())] += 1
        if direct_attribute:
            self.visit(node.func.value)
        for item in (*node.args, *(keyword.value for keyword in node.keywords)):
            self.visit(item)

    def visit_Name(self, node):
        self._validate_protected_binding(node.id)

    def visit_Attribute(self, node):
        if node.attr == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper attribute is not a direct call")
        self.generic_visit(node)

    def visit_arg(self, node):
        self._validate_protected_binding(node.arg)
        if node.annotation:
            self.visit(node.annotation)

    def _visit_type_parameter(self, node):
        self._validate_protected_binding(node.name)
        self.generic_visit(node)

    visit_TypeVar = _visit_type_parameter
    visit_ParamSpec = _visit_type_parameter
    visit_TypeVarTuple = _visit_type_parameter

    def visit_Global(self, node):
        for name in node.names:
            self._validate_protected_binding(name)

    visit_Nonlocal = visit_Global
    def _visit_named_binding(self, node):
        self._validate_protected_binding(node.name)
        self.generic_visit(node)

    visit_ExceptHandler = _visit_named_binding
    visit_MatchAs = _visit_named_binding
    visit_MatchStar = _visit_named_binding
    def visit_MatchMapping(self, node):
        self._validate_protected_binding(node.rest)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".", 1)[0]
            self._validate_protected_binding(bound_name)

    def visit_ImportFrom(self, node):
        module = _resolve_import_from_module(node, self.relative_path)
        for alias in node.names:
            if alias.name == "*":
                _fail_inventory("star imports can hide protected helper bindings")
            bound_name = alias.asname or alias.name
            allowed = (
                module == DRAFT_DELETE_EMAIL_MODULE
                and alias.name == DRAFT_DELETE_HELPER_NAME
                and bound_name == DRAFT_DELETE_HELPER_NAME
            )
            if alias.name == DRAFT_DELETE_HELPER_NAME and not allowed:
                _fail_inventory("protected helper import source is unsupported")
            self._validate_protected_binding(bound_name, allowed=allowed)


class _RequestsDeleteVisitor(ast.NodeVisitor):
    """Recursively validate requests usage inside the protected helper only."""

    def __init__(self):
        self.delete_calls = 0

    def _reject_binding(self, name):
        if name == "requests":
            _fail_inventory("requests is rebound inside the protected helper")

    def visit_Call(self, node):
        approved = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "delete"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
        )
        if not approved:
            self.generic_visit(node)
            return
        self.delete_calls += 1
        for item in (*node.args, *(keyword.value for keyword in node.keywords)):
            self.visit(item)

    def visit_Name(self, node):
        if node.id != "requests":
            return
        if isinstance(node.ctx, ast.Load):
            _fail_inventory("requests load is not a direct requests.delete receiver")
        self._reject_binding(node.id)

    def visit_arg(self, node):
        self._reject_binding(node.arg)
        if node.annotation:
            self.visit(node.annotation)

    def _visit_type_parameter(self, node):
        self._reject_binding(node.name)
        self.generic_visit(node)

    visit_TypeVar = _visit_type_parameter
    visit_ParamSpec = _visit_type_parameter
    visit_TypeVarTuple = _visit_type_parameter

    def _visit_named_scope(self, node):
        self._reject_binding(node.name)
        self.generic_visit(node)

    visit_FunctionDef = _visit_named_scope
    visit_AsyncFunctionDef = _visit_named_scope
    visit_ClassDef = _visit_named_scope

    def visit_Global(self, node):
        if "requests" in node.names:
            self._reject_binding("requests")

    visit_Nonlocal = visit_Global

    def _visit_named_binding(self, node):
        self._reject_binding(node.name)
        self.generic_visit(node)

    visit_ExceptHandler = _visit_named_binding
    visit_MatchAs = _visit_named_binding
    visit_MatchStar = _visit_named_binding

    def visit_MatchMapping(self, node):
        self._reject_binding(node.rest)
        self.generic_visit(node)


class _ModuleRequestsGuard(_RequestsDeleteVisitor):
    """Reject module-scope syntax that can replace or propagate requests."""

    def visit_Call(self, node):
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._reject_binding(node.name)
        # Inspect only expressions evaluated while defining the function.
        _visit_function_metadata(self, node)

    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node):
        self._reject_binding(node.name)
        _visit_class_metadata(self, node)

    def visit_Lambda(self, node):
        self.visit(node.args)


def _count_bounded_requests_delete(tree):
    top_level_imports = {
        id(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    expected_imports = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal)) and "requests" in node.names:
            _fail_inventory("global/nonlocal requests mutation is unsupported")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                source_root = alias.name.split(".", 1)[0]
                bound_name = alias.asname or source_root
                if source_root != "requests" and bound_name != "requests":
                    continue
                if id(node) in top_level_imports and alias.name == "requests" and alias.asname is None:
                    expected_imports += 1
                else:
                    _fail_inventory("requests must use one unaliased top-level import")
        elif isinstance(node, ast.ImportFrom):
            if id(node) in top_level_imports and any(alias.name == "*" for alias in node.names):
                _fail_inventory("module-scope star import can shadow requests")
            source_root = (node.module or "").split(".", 1)[0]
            bound_names = {
                alias.asname or alias.name.split(".", 1)[0]
                for alias in node.names if alias.name != "*"
            }
            imported_names = {alias.name for alias in node.names}
            if source_root == "requests" or "requests" in imported_names | bound_names:
                _fail_inventory("requests from-import/deceptive binding is unsupported")
    if expected_imports != 1:
        _fail_inventory("expected one unaliased top-level import requests")

    helpers = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == DRAFT_DELETE_HELPER_NAME
    ]
    if len(helpers) != 1:
        _fail_inventory("expected one top-level draft delete helper")

    _ModuleRequestsGuard().visit(tree)
    visitor = _RequestsDeleteVisitor()
    arguments = helpers[0].args
    for argument in (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        arguments.vararg,
        arguments.kwarg,
    ):
        if argument:
            visitor._reject_binding(argument.arg)
    for statement in helpers[0].body:
        visitor.visit(statement)
    if visitor.delete_calls != 1:
        _fail_inventory("expected exactly one requests.delete call in the helper")
    return visitor.delete_calls


def _scan_draft_delete_calls():
    callers, provider_deletes, application_paths = Counter(), 0, set()
    for relative_path in _application_python_files():
        path = relative_path.as_posix()
        application_paths.add(path)
        tree = _parse_module(relative_path)
        visitor = _DraftDeleteCallVisitor(relative_path)
        visitor.visit(tree)
        callers.update(visitor.callers)
        if path == DRAFT_DELETE_EMAIL_PATH:
            provider_deletes += _count_bounded_requests_delete(tree)
    return callers, provider_deletes, application_paths


def _load_manifest(path=None):
    path = path or DRAFT_DELETE_MANIFEST_PATH
    if not path.exists():
        raise AssertionError("graph draft delete caller manifest is missing")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_draft_delete_callers(manifest):
    callers = Counter()
    for group in ("deferred", "m2Owned"):
        for entry in manifest[group]:
            callers[(Path(entry["path"]).as_posix(), entry["function"])] += entry["count"]
    return callers


def _discover_draft_delete_callers(manifest_callers):
    callers, _, application_paths = _scan_draft_delete_calls()
    if not {Path(path).as_posix() for path, _ in manifest_callers} <= application_paths:
        raise AssertionError("graph draft delete manifest names a non-application path")
    return callers


class _LegacyDefinitionVisitor(ast.NodeVisitor):
    """Find definitions in module-execution flow while pruning lexical scopes."""

    def __init__(self, path, target_names, discovered):
        self.path = path
        self.target_names = target_names
        self.discovered = discovered

    def visit_FunctionDef(self, node):
        if node.name in self.target_names:
            self.discovered[(self.path, node.name)] += 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        return

    def visit_Lambda(self, node):
        return


def _legacy_symbols_in_modules(modules):
    target_names = {name for _, name in EXPECTED_LEGACY_MARKER_DEFINITIONS}
    discovered = Counter()
    for relative_path, tree in modules:
        path = Path(relative_path).as_posix()
        _LegacyDefinitionVisitor(path, target_names, discovered).visit(tree)
    return discovered


def _discover_legacy_authority_symbols():
    return _legacy_symbols_in_modules(
        (path, _parse_module(path)) for path in _application_python_files()
    )


def _in_memory_draft_delete_callers(source, relative_path):
    visitor = _DraftDeleteCallVisitor(relative_path)
    visitor.visit(ast.parse(source, filename=relative_path))
    return visitor.callers


def _in_memory_delete_provider_count(source):
    return _count_bounded_requests_delete(ast.parse(source, filename=DRAFT_DELETE_EMAIL_PATH))


class DraftDeleteScannerTests(unittest.TestCase):
    def test_any_direct_name_or_attribute_call_counts(self):
        source = """
_delete_graph_reply_draft()
def arbitrary_receiver():
    unrelated._delete_graph_reply_draft()
def escaped_module():
    (lambda: email_module)()._delete_graph_reply_draft()
"""
        self.assertEqual(
            Counter({
                ("email_automation/unrelated.py", "<module>"): 1,
                ("email_automation/unrelated.py", "arbitrary_receiver"): 1,
                ("email_automation/unrelated.py", "escaped_module"): 1,
            }),
            _in_memory_draft_delete_callers(source, "email_automation/unrelated.py"),
        )

    def test_lambda_body_call_uses_qualified_lambda_scope(self):
        source = "def outer():\n    callback = lambda: _delete_graph_reply_draft()\n"
        expected = Counter(
            {("email_automation/followup.py", "outer.<lambda@2:15>"): 1}
        )
        self.assertEqual(
            expected,
            _in_memory_draft_delete_callers(source, "email_automation/followup.py"),
        )

    def test_lambda_default_call_stays_in_enclosing_scope(self):
        source = (
            "def outer():\n"
            "    callback = lambda value=_delete_graph_reply_draft(): value\n"
        )
        expected = Counter({("email_automation/followup.py", "outer"): 1})
        self.assertEqual(
            expected,
            _in_memory_draft_delete_callers(source, "email_automation/followup.py"),
        )

    def test_protected_reference_propagation_is_rejected(self):
        sources = {
            "assignment": "alias = _delete_graph_reply_draft",
            "attribute": "alias = email_module._delete_graph_reply_draft",
            "argument": "consume(_delete_graph_reply_draft)",
            "return": "def cleanup():\n    return _delete_graph_reply_draft\n",
            "default": "def cleanup(value=_delete_graph_reply_draft):\n    pass\n",
            "parameter": "def cleanup(_delete_graph_reply_draft):\n    pass\n",
        }
        for case, source in sources.items():
            with self.subTest(case=case), self.assertRaises(DraftDeleteBindingInventoryError):
                _in_memory_draft_delete_callers(source, "email_automation/followup.py")

    def test_exact_helper_import_identity_is_allowed(self):
        statements = (
            "from .email import _delete_graph_reply_draft",
            "from email_automation.email import _delete_graph_reply_draft as _delete_graph_reply_draft",
        )
        expected = Counter({("email_automation/followup.py", "<module>"): 1})
        for statement in statements:
            with self.subTest(statement=statement):
                source = f"{statement}\n_delete_graph_reply_draft()\n"
                self.assertEqual(expected, _in_memory_draft_delete_callers(source, "email_automation/followup.py"))

    def test_helper_import_alias_or_star_is_rejected(self):
        statements = (
            "from .email import _delete_graph_reply_draft as hidden",
            "from email_automation.email import *",
        )
        for statement in statements:
            with self.subTest(statement=statement), self.assertRaises(DraftDeleteBindingInventoryError):
                _in_memory_draft_delete_callers(statement, "email_automation/followup.py")

    def test_protected_name_rebindings_are_rejected(self):
        helper = DRAFT_DELETE_HELPER_NAME
        sources = {
            "foreign function": f"def {helper}(): pass\n{helper}()",
            "nested function": f"def outer():\n def {helper}(): pass\n {helper}()",
            "async function": f"async def {helper}(): pass\n{helper}()",
            "class": f"class {helper}: pass\n{helper}()",
            "import alias": f"import unrelated as {helper}\n{helper}()",
            "foreign from alias": f"from unrelated import fake as {helper}\n{helper}()",
            "exact module fake": f"from email_automation.email import fake as {helper}\n{helper}()",
            "foreign star": f"from unrelated import *\n{helper}()",
            "type var": f"def cleanup[{helper}]():\n {helper}()",
            "param spec": f"def cleanup[**{helper}]():\n {helper}()",
            "type var tuple": f"def cleanup[*{helper}]():\n {helper}()",
        }
        for case, source in sources.items():
            with self.subTest(case=case), self.assertRaises(
                DraftDeleteBindingInventoryError
            ):
                _in_memory_draft_delete_callers(
                    source, "email_automation/followup.py"
                )

    def test_scope_keys_are_qualified_and_definition_time_is_enclosing(self):
        source = """
client._delete_graph_reply_draft()
def wrap(value): return value
@wrap(client._delete_graph_reply_draft())
def outer(default=client._delete_graph_reply_draft()):
    client._delete_graph_reply_draft()
    def inner(): client._delete_graph_reply_draft()
    class Local: client._delete_graph_reply_draft()
class A:
    client._delete_graph_reply_draft()
    def cleanup(self): client._delete_graph_reply_draft()
class B:
    def cleanup(self): client._delete_graph_reply_draft()
"""
        expected = Counter({
            ("email_automation/followup.py", "<module>"): 3,
            ("email_automation/followup.py", "outer"): 1,
            ("email_automation/followup.py", "outer.inner"): 1,
            ("email_automation/followup.py", "outer.Local.<class-body>"): 1,
            ("email_automation/followup.py", "A.<class-body>"): 1,
            ("email_automation/followup.py", "A.cleanup"): 1,
            ("email_automation/followup.py", "B.cleanup"): 1,
        })
        self.assertEqual(expected, _in_memory_draft_delete_callers(source, "email_automation/followup.py"))


class ProviderDeleteValidatorTests(unittest.TestCase):
    def assertProviderRejected(self, source):
        with self.assertRaises(DraftDeleteBindingInventoryError):
            _in_memory_delete_provider_count(source)

    def test_recursive_lambda_provider_delete_counts_once(self):
        source = (
            "import requests\ndef _delete_graph_reply_draft():\n"
            " return run(lambda: requests.delete('x'))"
        )
        self.assertEqual(1, _in_memory_delete_provider_count(source))

    def test_provider_delete_rejects_shadowed_requests_receiver(self):
        self.assertProviderRejected(
            "import requests\ndef _delete_graph_reply_draft(requests): requests.delete('x')"
        )

    def test_provider_delete_rejects_aliased_requests_module(self):
        self.assertProviderRejected(
            "import requests as http\ndef _delete_graph_reply_draft(): http.delete('x')"
        )

    def test_provider_delete_rejects_nested_second_delete(self):
        self.assertProviderRejected(
            "import requests\ndef _delete_graph_reply_draft():\n"
            " requests.delete('one')\n def nested(): requests.delete('two')"
        )

    def test_provider_gate_rejects_hidden_or_ambiguous_shapes(self):
        sources = {
            "spoofed binding": "import unrelated as requests\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "from import": "from requests import delete\ndef _delete_graph_reply_draft(): delete('x')",
            "duplicate import": "import requests\nimport requests\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "missing helper": "import requests\n",
            "duplicate helper": (
                "import requests\ndef _delete_graph_reply_draft(): requests.delete('x')\n"
                "def _delete_graph_reply_draft(): requests.delete('x')"
            ),
            "module propagation": "import requests\ndef _delete_graph_reply_draft():\n alias = requests\n alias.delete('x')",
            "other method": "import requests\ndef _delete_graph_reply_draft(): requests.get('x')",
            "local assignment": "import requests\ndef _delete_graph_reply_draft():\n requests = client\n requests.delete('x')",
            "module rebinding": "import requests\nrequests = client\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "module alias": "import requests\nhttp = requests\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "module star": "import requests\nfrom unrelated import *\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "default-only": "import requests\ndef _delete_graph_reply_draft(value=requests.delete('x')): pass",
            "decorator-only": "import requests\n@decorate(requests.delete('x'))\ndef _delete_graph_reply_draft(): pass",
            "annotation-only": "import requests\ndef _delete_graph_reply_draft() -> requests.delete('x'): pass",
            "function global": "import requests\ndef mutate():\n global requests\n requests = client\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "class global": "import requests\nclass Mutate:\n global requests\n requests = client\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "renamed from import": "import requests\nfrom unrelated import requests as http\ndef _delete_graph_reply_draft(): requests.delete('x')",
            "type var": "import requests\ndef _delete_graph_reply_draft[requests](): requests.delete('x')",
            "param spec": "import requests\ndef _delete_graph_reply_draft[**requests](): requests.delete('x')",
            "type var tuple": "import requests\ndef _delete_graph_reply_draft[*requests](): requests.delete('x')",
            "nested typed function": "import requests\ndef _delete_graph_reply_draft():\n def nested[requests](): requests.delete('x')",
            "nested typed class": "import requests\ndef _delete_graph_reply_draft():\n class Nested[requests]: requests.delete('x')",
        }
        for case, source in sources.items():
            with self.subTest(case=case):
                self.assertProviderRejected(source)


class InventoryContractTests(unittest.TestCase):
    def assertSourceCoordinatorImportsRejected(self, source):
        in_memory_module = mock.Mock()
        in_memory_module.exists.return_value = True
        in_memory_module.read_text.return_value = source
        with mock.patch(
            f"{__name__}.SOURCE_COORDINATOR_PATH", in_memory_module
        ), self.assertRaises(AssertionError):
            self.test_source_coordinator_has_no_provider_or_effect_imports()

    def assertSourceCoordinatorImportsAllowed(self, source):
        in_memory_module = mock.Mock()
        in_memory_module.exists.return_value = True
        in_memory_module.read_text.return_value = source
        with mock.patch(f"{__name__}.SOURCE_COORDINATOR_PATH", in_memory_module):
            self.test_source_coordinator_has_no_provider_or_effect_imports()

    def test_absolute_application_effect_imports_are_rejected(self):
        sources = {
            "direct import": "import email_automation.email",
            "aliased import": "import email_automation.sheets as sheets",
            "package from import": "from email_automation import sheets",
            "effect from import": (
                "from email_automation.ai_processing "
                "import propose_sheet_updates"
            ),
            "deep direct import": "import email_automation.email.graph.client",
            "deep from import": (
                "from email_automation.sheet_operations.internal "
                "import update"
            ),
        }
        for case, source in sources.items():
            with self.subTest(case=case):
                self.assertSourceCoordinatorImportsRejected(source)

    def test_benign_absolute_application_imports_remain_allowed(self):
        sources = (
            "import email_automation",
            "import email_automation.scheduler_scope",
            "from email_automation import scheduler_scope",
            "from email_automation.scheduler_scope import SchedulerScopeError",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertSourceCoordinatorImportsAllowed(source)

    def test_provider_and_relative_effect_imports_remain_rejected(self):
        sources = (
            "import requests.adapters",
            "from openai import OpenAI",
            "import googleapiclient.discovery",
            "from .requests import delete",
            "from .openai.client import OpenAI",
            "from .email import send_message",
            "from . import sheets",
            "from .file_handling.internal import load_file",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertSourceCoordinatorImportsRejected(source)

    def test_source_coordinator_has_no_provider_or_effect_imports(self):
        self.assertTrue(
            SOURCE_COORDINATOR_PATH.exists(),
            "source coordinator module is missing",
        )
        if not SOURCE_COORDINATOR_PATH.exists():
            return

        tree = ast.parse(
            SOURCE_COORDINATOR_PATH.read_text(encoding="utf-8"),
            filename=str(SOURCE_COORDINATOR_PATH),
        )
        self.assertEqual([], _source_coordinator_forbidden_imports(tree))

    def test_source_admission_private_type_and_call_sites_are_bounded(self):
        self.assertEqual([], _discover_source_admission_contract_violations())

    def test_classification_private_evidence_and_orchestrator_signature_are_bounded(self):
        self.assertTrue(SOURCE_COORDINATOR_PATH.exists())
        tree = ast.parse(
            SOURCE_COORDINATOR_PATH.read_text(encoding="utf-8"),
            filename=str(SOURCE_COORDINATOR_PATH),
        )
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == SOURCE_CLASSIFICATION_ORCHESTRATOR
        ]
        self.assertEqual(1, len(definitions))
        self.assertIsNone(definitions[0].args.vararg)
        self.assertIsNone(definitions[0].args.kwarg)
        parameters = tuple(argument.arg for argument in definitions[0].args.args)
        keyword_only = tuple(
            argument.arg for argument in definitions[0].args.kwonlyargs
        )
        all_parameters = parameters + keyword_only
        self.assertEqual(
            (
                "self",
                "user_id",
                "canonical_source_id",
                "lease_seconds",
                "classification_input",
                "classifier",
            ),
            all_parameters,
        )
        for forbidden in ("deterministic_evidence", "owner_kind", "winner"):
            self.assertNotIn(forbidden, all_parameters)

        deterministic_definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "persist_deterministic_classification_snapshot"
        ]
        self.assertEqual(1, len(deterministic_definitions))
        self.assertIsNone(deterministic_definitions[0].args.vararg)
        self.assertIsNone(deterministic_definitions[0].args.kwarg)
        deterministic_parameters = tuple(
            argument.arg
            for argument in (
                deterministic_definitions[0].args.args
                + deterministic_definitions[0].args.kwonlyargs
            )
        )
        self.assertEqual(
            (
                "self",
                "user_id",
                "canonical_source_id",
                "classification_epoch",
                "classification_claim_id",
                "classification_input",
            ),
            deterministic_parameters,
        )
        for forbidden in (
            "deterministic_evidence",
            "proposal_evidence",
            "owner_kind",
            "winner",
        ):
            self.assertNotIn(forbidden, deterministic_parameters)

        module_bindings = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        module_bindings.update(
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            if isinstance(target, ast.Name)
        )
        self.assertNotIn("_mint_verified_hard_optout_evidence", module_bindings)
        self.assertNotIn(
            "_VERIFIED_HARD_OPTOUT_EVIDENCE_CAPABILITY",
            module_bindings,
        )

    def test_classification_gate_rejects_private_verified_evidence_access(self):
        mutations = {
            "direct import": (
                "from email_automation.source_coordinator import "
                "_VerifiedHardOptoutEvidence\n"
                "value = _VerifiedHardOptoutEvidence({})"
            ),
            "module attribute": (
                "import email_automation.source_coordinator as coordinator\n"
                "value = coordinator._VerifiedHardOptoutEvidence"
            ),
            "dynamic lookup": (
                "value = getattr(coordinator, '_VerifiedHardOptoutEvidence')"
            ),
            "fragmented lookup": (
                "name = '_VerifiedHard' + 'OptoutEvidence'\n"
                "value = getattr(coordinator, name)"
            ),
            "spoofed definition": "class _VerifiedHardOptoutEvidence: pass",
            "module dictionary fragmented lookup": (
                "prefix = '_VerifiedHard'\n"
                "suffix = 'OptoutEvidence'\n"
                "value = coordinator.__dict__[prefix + suffix]"
            ),
            "vars fragmented lookup": (
                "prefix = '_VerifiedHard'\n"
                "namespace = vars(coordinator)\n"
                "value = namespace[prefix + 'OptoutEvidence']"
            ),
            "dictionary propagation": (
                "namespace = coordinator.__dict__\n"
                "prefix = '_VerifiedHard'\n"
                "constructor = namespace[prefix + 'OptoutEvidence']\n"
                "value = constructor({})"
            ),
            "retained evidence direct import": (
                "from email_automation.source_coordinator import "
                "_VerifiedRetainedTerminalEvidence\n"
                "value = object.__new__(_VerifiedRetainedTerminalEvidence)"
            ),
            "retained evidence module attribute": (
                "import email_automation.source_coordinator as coordinator\n"
                "value = coordinator._VerifiedRetainedTerminalEvidence"
            ),
            "retained evidence fragmented lookup": (
                "name = '_VerifiedRetained' + 'TerminalEvidence'\n"
                "value = getattr(coordinator, name)"
            ),
            "retained evidence spoofed definition": (
                "class _VerifiedRetainedTerminalEvidence: pass"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source, "email_automation/unreviewed.py"
                    )
                )

    def test_classification_gate_rejects_reflection_and_verifier_injection(self):
        coordinator_import = (
            "import email_automation.source_coordinator as coordinator\n"
        )
        mutations = {
            "vars suffix iteration": (
                coordinator_import
                + "namespace = vars(coordinator)\n"
                "value = next(v for name, v in namespace.items() "
                "if name.endswith('Evidence'))"
            ),
            "dir introspection": coordinator_import + "names = dir(coordinator)",
            "hex decoded getattr": (
                coordinator_import
                + "name = bytes.fromhex("
                "'5f5665726966696564486172644f70746f757445766964656e6365'"
                ").decode()\n"
                "value = getattr(coordinator, name)"
            ),
            "propagated alias getattribute": (
                coordinator_import
                + "alias = coordinator\n"
                "name = '_VerifiedHard' + 'OptoutEvidence'\n"
                "value = alias.__getattribute__(name)"
            ),
            "object getattribute": (
                coordinator_import
                + "name = bytes.fromhex("
                "'5f5665726966696564486172644f70746f757445766964656e6365'"
                ").decode()\n"
                "value = object.__getattribute__(coordinator, name)"
            ),
            "bound getattribute": (
                coordinator_import
                + "reader = coordinator.__getattribute__\n"
                "name = bytes.fromhex("
                "'5f5665726966696564486172644f70746f757445766964656e6365'"
                ").decode()\n"
                "value = reader(name)"
            ),
            "aliased attrgetter": (
                "from operator import attrgetter as lookup\n"
                + coordinator_import
                + "name = bytes.fromhex("
                "'5f5665726966696564486172644f70746f757445766964656e6365'"
                ").decode()\n"
                "value = lookup(name)(coordinator)"
            ),
            "aliased methodcaller": (
                "from operator import methodcaller as invoke\n"
                + coordinator_import
                + "name = bytes.fromhex("
                "'5f5665726966696564486172644f70746f757445766964656e6365'"
                ").decode()\n"
                "value = invoke('__getattribute__', name)(coordinator)"
            ),
            "base64 decoded dictionary lookup": (
                "import base64\n"
                + coordinator_import
                + "name = base64.b64decode("
                "'X1ZlcmlmaWVkSGFyZE9wdG91dEV2aWRlbmNl'"
                ").decode()\n"
                "value = coordinator.__dict__[name]"
            ),
            "method globals": (
                coordinator_import
                + "value = coordinator.SourceCoordinator."
                "persist_deterministic_classification_snapshot.__globals__"
            ),
            "verifier keyword": (
                coordinator_import
                + "value = coordinator.SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now, "
                "hard_optout_verifier=verify)"
            ),
            "verifier attribute propagation": (
                coordinator_import
                + "value = instance._hard_optout_verifier"
            ),
            "fragmented setattr injection": (
                coordinator_import
                + "instance = coordinator.SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now)\n"
                "name = '_hard_' + 'optout_verifier'\n"
                "setattr(instance, name, verify)"
            ),
            "object setattr injection": (
                coordinator_import
                + "instance = coordinator.SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now)\n"
                "name = '_hard_' + 'optout_verifier'\n"
                "object.__setattr__(instance, name, verify)"
            ),
            "aliased object setattr injection": (
                coordinator_import
                + "instance = coordinator.SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now)\n"
                "setter = object.__setattr__\n"
                "name = '_hard_' + 'optout_verifier'\n"
                "setter(instance, name, verify)"
            ),
            "verifier rebinding": "hard_optout_verifier = verify",
            "hidden constructor injection": (
                coordinator_import
                + "options = {'hard_optout_verifier': verify}\n"
                "value = coordinator.SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now, **options)"
            ),
            "aliased hidden constructor injection": (
                "from email_automation.source_coordinator import "
                "SourceCoordinator as SC\n"
                "options = build_options()\n"
                "value = SC(client, uuid_factory=make_id, "
                "now_factory=now, **options)"
            ),
            "unbound init injection": (
                "from email_automation.source_coordinator import "
                "SourceCoordinator as SC\n"
                "SC.__init__(instance, client, uuid_factory=make_id, "
                "now_factory=now, **options)"
            ),
            "subclass injection": (
                "from email_automation.source_coordinator import "
                "SourceCoordinator as SC\n"
                "class UnsafeCoordinator(SC):\n"
                "    def __init__(self, client, **options):\n"
                "        super().__init__(client, uuid_factory=make_id, "
                "now_factory=now, **options)"
            ),
            "alias assigned after function": (
                coordinator_import
                + "def recover():\n"
                "    return vars(alias)\n"
                "alias = coordinator"
            ),
            "dynamic importlib acquisition": (
                "import importlib\n"
                "module = importlib.import_module("
                "'email_automation.source_coordinator')\n"
                "value = vars(module)"
            ),
            "dynamic sys modules acquisition": (
                "import sys\n"
                "module = sys.modules['email_automation.source_coordinator']\n"
                "value = vars(module)"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/unreviewed.py",
                    )
                )

    def test_classification_gate_does_not_ban_unrelated_getattr(self):
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                "value = getattr(record, 'id', None)",
                "email_automation/unrelated.py",
            ),
        )

    def test_classification_gate_allows_unrelated_operations_after_constructor_import(self):
        source = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "from operator import attrgetter\n"
            "def process_inbox_message():\n"
            "    identifier = getattr(record, 'id', None)\n"
            "    pairs = list(mapping.items())\n"
            "    matched = filename.endswith('.json')\n"
            "    reader = attrgetter('id')(record)\n"
            "    result = other(**options)\n"
            "    coordinator = SourceCoordinator("
            "client, uuid_factory=make_id, now_factory=now)"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                source,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_allows_unrelated_setattr_and_public_enum_reflection(self):
        source = (
            "from email_automation.source_coordinator import "
            "CoordinatorMode\n"
            "prefix = '_hard_'\n"
            "suffix = 'optout_verifier'\n"
            "setattr(record, 'status', prefix + suffix)\n"
            "value = getattr(CoordinatorMode, 'value', None)"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                source,
                "email_automation/reviewed_adapter.py",
            ),
        )

    def test_classification_gate_rejects_constructor_escape_and_nested_imports(self):
        direct_import = (
            "from email_automation.source_coordinator import "
            "SourceCoordinator as SC\n"
        )
        mutations = {
            "assignment": direct_import + "factory = SC",
            "container": direct_import + "factories = [SC]",
            "argument": direct_import + "register(SC)",
            "return": direct_import + "def factory():\n    return SC",
            "unbound init": direct_import + "SC.__init__(instance, client)",
            "nested direct import": (
                "def build(options):\n"
                "    from email_automation.source_coordinator import "
                "SourceCoordinator as SC\n"
                "    return SC(client, **options)"
            ),
            "nested module import": (
                "def build(options):\n"
                "    import email_automation.source_coordinator as coordinator\n"
                "    return coordinator.SourceCoordinator(client, **options)"
            ),
            "star import": (
                "from email_automation.source_coordinator import *\n"
                "value = SourceCoordinator(client, **options)"
            ),
            "definition before import": (
                "def build(options):\n"
                "    return SC(client, **options)\n"
                "from email_automation.source_coordinator import "
                "SourceCoordinator as SC"
            ),
            "returned constructor result": (
                direct_import
                + "def build():\n"
                "    return SC(client, uuid_factory=make_id, now_factory=now)"
            ),
            "passed constructor result": (
                direct_import
                + "mutate(SC(client, uuid_factory=make_id, now_factory=now))"
            ),
            "contained constructor result": (
                direct_import
                + "values = [SC(client, uuid_factory=make_id, now_factory=now)]"
            ),
            "attribute assigned constructor result": (
                direct_import
                + "holder.coordinator = SC("
                "client, uuid_factory=make_id, now_factory=now)"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/unreviewed.py",
                    )
                )

        annotation_only = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "def accept(value: 'SourceCoordinator') -> 'SourceCoordinator':\n"
            "    return value"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                annotation_only,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_rejects_import_rebinding_before_or_after_use(self):
        before_shadow = (
            "from email_automation.source_coordinator import "
            "SourceCoordinator\n"
            "SourceCoordinator(client, **options)\n"
            "SourceCoordinator = unrelated"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                before_shadow,
                "email_automation/processing.py",
            )
        )

        after_shadow = (
            "from email_automation.source_coordinator import "
            "SourceCoordinator\n"
            "SourceCoordinator = unrelated\n"
            "value = getattr(SourceCoordinator, 'ordinary', None)\n"
            "result = SourceCoordinator(**options)"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                after_shadow,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_rejects_production_verifier_wiring_globally(self):
        mutations = {
            "aliased constructor keyword": (
                "factory = recover_constructor()\n"
                "value = factory(client, hard_optout_verifier=verify)"
            ),
            "ordinary call keyword": "register(hard_optout_verifier=verify)",
            "private attribute fragments": (
                "name = '_hard_' + 'optout_verifier'\n"
                "setattr(value, name, verify)"
            ),
            "public verifier fragments": (
                "name = 'hard_' + 'optout_verifier'\n"
                "getattr(value, name)"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/processing.py",
                    )
                )

    def test_classification_gate_rejects_executable_annotations(self):
        source = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "def process_inbox_message("
            "value: register(SourceCoordinator)):\n"
            "    pass"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                source,
                "email_automation/processing.py",
            )
        )

    def test_classification_gate_requires_exact_constructor_import(self):
        constructor_call = (
            ".SourceCoordinator(client, uuid_factory=make_id, "
            "now_factory=now, hard_optout_verifier=verify)"
        )
        mutations = {
            "package from import": (
                "from email_automation import source_coordinator as sc\n"
                "value = sc" + constructor_call
            ),
            "relative package import": (
                "from . import source_coordinator as sc\n"
                "value = sc" + constructor_call
            ),
            "direct module import": (
                "import email_automation.source_coordinator as sc\n"
                "value = sc" + constructor_call
            ),
            "relative direct class import": (
                "from .source_coordinator import SourceCoordinator\n"
                "value = SourceCoordinator(client)"
            ),
            "foreign constructor rebinding": (
                "from email_automation.source_coordinator import SourceCoordinator\n"
                "from evil import Factory as SourceCoordinator"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/processing.py",
                    )
                )

    def test_classification_gate_allows_lexically_shadowed_coordinator_names(self):
        constructor_shadow = (
            "from email_automation.source_coordinator import "
            "SourceCoordinator\n"
            "def unrelated(SourceCoordinator):\n"
            "    return getattr(SourceCoordinator, 'ordinary', None)"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                constructor_shadow,
                "email_automation/processing.py",
            ),
        )

        instance_shadow = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "def process_inbox_message():\n"
            "    coordinator = SourceCoordinator("
            "client, uuid_factory=make_id, now_factory=now)\n"
            "    return coordinator.classify_source_once("
            "user_id, source_id, lease_seconds, classification_input, classifier)\n"
            "def unrelated(coordinator):\n"
            "    setattr(coordinator, 'status', 'ready')"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                instance_shadow,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_rejects_receiverless_namespace_access(self):
        constructor_import = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
        )
        mutations = {
            "globals": constructor_import + "namespace = globals()",
            "locals": constructor_import + "namespace = locals()",
            "eval": constructor_import + "value = eval(expression)",
            "exec": constructor_import + "exec(source)",
            "propagated globals": (
                constructor_import
                + "namespace = globals()\n"
                "name = bytes.fromhex('536f75726365436f6f7264696e61746f72').decode()\n"
                "factory = namespace[name]"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/unreviewed.py",
                    )
                )

        exact_safe_membership = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "value = 'saga' in locals()"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                exact_safe_membership,
                "email_automation/processing.py",
            ),
        )
        chained_membership = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "value = 'saga' in locals() == sink"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                chained_membership,
                "email_automation/processing.py",
            )
        )

    def test_classification_gate_rejects_statically_named_dynamic_acquisition(self):
        mutations = {
            "importlib name binding": (
                "import importlib\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "module = importlib.import_module(name)"
            ),
            "sys modules name binding": (
                "import sys\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "module = sys.modules[name]"
            ),
            "propagated importlib loader": (
                "import importlib\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "loader = importlib.import_module\n"
                "module = loader(name)"
            ),
            "propagated sys modules": (
                "import sys\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "modules = sys.modules\n"
                "module = modules[name]"
            ),
            "builtins import": (
                "import builtins\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "module = builtins.__import__(name)"
            ),
            "imported builtins loader": (
                "from builtins import __import__ as loader\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "module = loader(name)"
            ),
            "sys modules get": (
                "import sys\n"
                "name = 'email_automation.' + 'source_coordinator'\n"
                "module = sys.modules.get(name)"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/unreviewed.py",
                    )
                )

    def test_classification_gate_rejects_coordinator_instance_escape(self):
        prefix = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "coordinator = SourceCoordinator("
            "client, uuid_factory=make_id, now_factory=now)\n"
        )
        mutations = {
            "helper argument": prefix + "mutate(coordinator)",
            "return": prefix + "def leak():\n    return coordinator",
            "container": prefix + "values = [coordinator]",
            "alias": prefix + "alias = coordinator",
            "conditional taint": (
                "from email_automation.source_coordinator import SourceCoordinator\n"
                "coordinator = (SourceCoordinator("
                "client, uuid_factory=make_id, now_factory=now) "
                "if enabled else fallback)\n"
                "name = '_hard_' + 'optout_verifier'\n"
                "setattr(coordinator, name, verify)"
            ),
            "bound public method": (
                prefix + "callback = coordinator.classify_source_once"
            ),
            "type getattribute": prefix + "type.__getattribute__(coordinator, name)",
            "type setattr": prefix + "type.__setattr__(coordinator, name, verify)",
            "type delattr": prefix + "type.__delattr__(coordinator, name)",
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        "email_automation/unreviewed.py",
                    )
                )

    def test_classification_gate_allows_task7_public_coordinator_calls(self):
        source = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "def process_inbox_message():\n"
            "    coordinator = SourceCoordinator("
            "client, uuid_factory=make_id, now_factory=now)\n"
            "    admitted = coordinator.admit_or_repair_source_identity("
            "user_id, source)\n"
            "    result = coordinator.classify_source_once("
            "user_id, source_id, lease_seconds, classification_input, classifier)\n"
            "    snapshot = coordinator."
            "require_authoritative_classification_snapshot(user_id, source_id)\n"
            "    setattr(record, 'status', 'ready')\n"
            "    return admitted, result, snapshot"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                source,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_allows_current_processing_with_future_import(self):
        source = (REPO_ROOT / "email_automation/processing.py").read_text(
            encoding="utf-8"
        )
        constructor_import = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
        )
        if constructor_import not in source:
            source = constructor_import + source
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                source,
                "email_automation/processing.py",
            ),
        )

    def test_classification_gate_allows_only_exact_production_factory_return(self):
        factory = (
            "from email_automation.source_coordinator import SourceCoordinator\n"
            "def build_source_coordinator(fs_client) -> 'SourceCoordinator':\n"
            "    return SourceCoordinator("
            "fs_client, uuid_factory=make_id, now_factory=now)"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                factory,
                "email_automation/processing.py",
            ),
        )

        mutations = {
            "wrong path": (
                factory,
                "email_automation/unreviewed.py",
            ),
            "wrong function": (
                factory.replace(
                    "build_source_coordinator",
                    "alternate_source_coordinator_factory",
                ),
                "email_automation/processing.py",
            ),
            "nested factory": (
                "from email_automation.source_coordinator import SourceCoordinator\n"
                "def wrapper():\n"
                "    def build_source_coordinator(fs_client):\n"
                "        return SourceCoordinator("
                "fs_client, uuid_factory=make_id, now_factory=now)",
                "email_automation/processing.py",
            ),
        }
        for case, (source, relative_path) in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source,
                        relative_path,
                    )
                )

    def test_source_admission_gate_rejects_private_envelope_mutations(self):
        mutations = {
            "direct import": (
                "from email_automation.source_coordinator import "
                "_SourceAdmissionEnvelope\n"
                "value = _SourceAdmissionEnvelope((), 'kind', 'hash')"
            ),
            "aliased import": (
                "from email_automation.source_coordinator import "
                "_SourceAdmissionEnvelope as Envelope\n"
                "value = Envelope((), 'kind', 'hash')"
            ),
            "module attribute": (
                "import email_automation.source_coordinator as coordinator\n"
                "value = coordinator._SourceAdmissionEnvelope"
            ),
            "dynamic lookup": (
                "value = getattr(coordinator, '_SourceAdmissionEnvelope')"
            ),
            "spoofed definition": "class _SourceAdmissionEnvelope: pass",
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source, "email_automation/unreviewed.py"
                    )
                )

    def test_source_admission_gate_rejects_unreviewed_calls_and_propagation(self):
        mutations = {
            "module call": "coordinator.admit_or_repair_source_identity()",
            "wrong function": (
                "def run():\n"
                " coordinator.admit_or_repair_source_identity()"
            ),
            "propagated method": (
                "def run():\n"
                " callback = coordinator.admit_or_repair_source_identity\n"
                " callback()"
            ),
            "dynamic method": (
                "def run():\n"
                " callback = getattr(coordinator, "
                "'admit_or_repair_source_identity')\n"
                " callback()"
            ),
            "same-name class method": (
                "class Spoof:\n"
                " def process_inbox_message(self):\n"
                "  coordinator.admit_or_repair_source_identity()"
            ),
            "spoofed admission definition": (
                "def admit_or_repair_source_identity():\n"
                " pass"
            ),
            "escaping nested class": (
                "def process_inbox_message():\n"
                " class Hidden:\n"
                "  def run(self):\n"
                "   coordinator.admit_or_repair_source_identity()"
            ),
            "escaping lambda": (
                "def process_inbox_message():\n"
                " return lambda: "
                "coordinator.admit_or_repair_source_identity()"
            ),
            "unreviewed nested closure": (
                "def replay_exact_message():\n"
                " def later():\n"
                "  coordinator.admit_or_repair_source_identity()\n"
                " return later"
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source, "email_automation/processing.py"
                    )
                )

    def test_source_admission_gate_rejects_bounded_reflection_strings(self):
        mutations = {
            "concatenated admission": (
                'name = "admit_or_repair_" + "source_identity"\n'
                "getattr(coordinator, name)()"
            ),
            "propagated concatenation": (
                'prefix = "admit_or_repair_"\n'
                'suffix = "source_identity"\n'
                "name = prefix + suffix\n"
                "getattr(coordinator, name)()"
            ),
            "constant-only admission f-string": (
                'name = f"{\'admit_or_repair_\'}{\'source_identity\'}"\n'
                "getattr(coordinator, name)()"
            ),
            "formatted constant name": (
                'suffix = "source_identity"\n'
                'name = f"admit_or_repair_{suffix}"\n'
                "builtins.getattr(coordinator, name)()"
            ),
            "concatenated private envelope": (
                'name = "_SourceAdmission" + "Envelope"\n'
                "Envelope = getattr(coordinator_module, name)"
            ),
            "ambiguous protected reassignment": (
                'name = "admit_or_repair_" + "source_identity"\n'
                "name = runtime_name\n"
                "getattr(coordinator, name)()"
            ),
            "tuple destructured admission": (
                'prefix, suffix = ("admit_or_repair_", "source_identity")\n'
                "getattr(coordinator, prefix + suffix)()"
            ),
            "list destructured private envelope": (
                '[prefix, suffix] = ["_SourceAdmission", "Envelope"]\n'
                "getattr(coordinator_module, prefix + suffix)"
            ),
            "for-bound admission": (
                'for prefix in ("admit_or_repair_",):\n'
                ' getattr(coordinator, prefix + "source_identity")()'
            ),
            "comprehension-bound admission": (
                '[getattr(coordinator, prefix + "source_identity")() '
                'for prefix in ("admit_or_repair_",)]'
            ),
            "bounded conditional admission": (
                'prefix = "admit_or_repair_" if enabled else "ordinary_"\n'
                'getattr(coordinator, prefix + "source_identity")()'
            ),
            "candidate pressure preserves protected prefix": (
                "\n".join(
                    [*(f'name = "a{index:02d}"' for index in range(16)),
                     'name = "admit_or_repair_"',
                     'getattr(coordinator, name + "source_identity")()']
                )
            ),
            "comprehension walrus binds containing scope": (
                'name = "ordinary_"\n'
                '[(name := "admit_or_repair_") for _ in (0,)]\n'
                'getattr(coordinator, name + "source_identity")()'
            ),
            "class first iterable list comprehension": (
                "class Hidden:\n"
                ' prefix = "admit_or_repair_"\n'
                ' values = [getattr(coordinator, name)() '
                'for name in (prefix + "source_identity",)]'
            ),
            "class first iterable set comprehension": (
                "class Hidden:\n"
                ' prefix = "admit_or_repair_"\n'
                ' values = {getattr(coordinator, name)() '
                'for name in (prefix + "source_identity",)}'
            ),
            "class first iterable dict comprehension": (
                "class Hidden:\n"
                ' prefix = "admit_or_repair_"\n'
                ' values = {name: getattr(coordinator, name)() '
                'for name in (prefix + "source_identity",)}'
            ),
            "class first iterable private generator": (
                "class Hidden:\n"
                ' prefix = "_SourceAdmission"\n'
                ' values = tuple(getattr(coordinator_module, name) '
                'for name in (prefix + "Envelope",))'
            ),
            "conditional tuple destructuring": (
                'prefix, suffix = (("admit_or_repair_", "source_identity") '
                'if enabled else ("ordinary_", "callback"))\n'
                'getattr(coordinator, prefix + suffix)()'
            ),
            "conditional for binding": (
                'for prefix in (("admit_or_repair_",) '
                'if enabled else ("ordinary_",)):\n'
                ' getattr(coordinator, prefix + "source_identity")()'
            ),
            "conditional comprehension binding": (
                '[getattr(coordinator, prefix + "source_identity")() '
                'for prefix in (("admit_or_repair_",) '
                'if enabled else ("ordinary_",))]'
            ),
            "imported getattr alias": (
                "from builtins import getattr as read_attr\n"
                'prefix = "admit_or_repair_"\n'
                'read_attr(coordinator, prefix + "source_identity")()'
            ),
            "propagated getattr alias": (
                "read_attr = getattr\n"
                'prefix = "admit_or_repair_"\n'
                'read_attr(coordinator, prefix + "source_identity")()'
            ),
            "imported attrgetter alias": (
                "from operator import attrgetter as pick\n"
                'prefix = "_SourceAdmission"\n'
                'pick(prefix + "Envelope")(coordinator_module)'
            ),
            "imported methodcaller alias": (
                "from operator import methodcaller as invoke\n"
                'prefix = "admit_or_repair_"\n'
                'invoke(prefix + "source_identity")(coordinator)'
            ),
        }
        for case, source in mutations.items():
            with self.subTest(case=case):
                self.assertTrue(
                    _source_admission_contract_violations(
                        source, "email_automation/unreviewed.py"
                    )
                )

        benign = (
            'prefix = "ordinary_"\n'
            'suffix = "callback"\n'
            "name = prefix + suffix\n"
            "callback = getattr(coordinator, name)"
        )
        self.assertEqual(
            [],
            _source_admission_contract_violations(
                benign, "email_automation/unreviewed.py"
            ),
        )
        lexical_shadow = (
            'prefix = "admit_or_repair_"\n'
            "def run():\n"
            ' prefix = "ordinary_"\n'
            ' return getattr(coordinator, prefix + "source_identity")'
        )
        self.assertTrue(
            _source_admission_contract_violations(
                lexical_shadow, "email_automation/unreviewed.py"
            )
        )
        nested_class_shadow = (
            'name = "ordinary_"\n'
            "class Outer:\n"
            ' name = "admit_or_repair_"\n'
            " class Inner:\n"
            '  value = getattr(coordinator, name + "source_identity")'
        )
        self.assertTrue(
            _source_admission_contract_violations(
                nested_class_shadow, "email_automation/unreviewed.py"
            )
        )

        reviewed_reflection = (
            "def process_inbox_message():\n"
            ' name = "admit_or_repair_" + "source_identity"\n'
            " getattr(coordinator, name)()"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                reviewed_reflection, "email_automation/processing.py"
            )
        )

    def test_source_admission_gate_allows_only_exact_reviewed_adapters(self):
        allowed = {
            "email_automation/processing.py": (
                "def process_inbox_message():\n"
                " coordinator.admit_or_repair_source_identity()"
            ),
            "email_automation/operator_replay.py": (
                "def replay_exact_message():\n"
                " def _under_lease():\n"
                "  coordinator.admit_or_repair_source_identity()\n"
                " _under_lease()"
            ),
        }
        for relative_path, source in allowed.items():
            with self.subTest(relative_path=relative_path):
                self.assertEqual(
                    [],
                    _source_admission_contract_violations(source, relative_path),
                )

        wrong_path = (
            "def process_inbox_message():\n"
            " coordinator.admit_or_repair_source_identity()"
        )
        self.assertTrue(
            _source_admission_contract_violations(
                wrong_path, "email_automation/unreviewed.py"
            )
        )

    def test_application_file_exclusions_are_preserved(self):
        self.assertTrue(_is_application_python_file(Path(DRAFT_DELETE_EMAIL_PATH)))
        self.assertFalse(_is_application_python_file(Path("tests/helper.py")))
        self.assertFalse(_is_application_python_file(Path("src/test_helper.py")))
        for excluded in _EXCLUDED_APPLICATION_PARTS:
            with self.subTest(excluded=excluded):
                self.assertFalse(_is_application_python_file(Path(excluded) / "helper.py"))

    def test_missing_manifest_fails_before_read(self):
        missing = mock.Mock()
        missing.exists.return_value = False
        with self.assertRaisesRegex(AssertionError, "manifest is missing"):
            _load_manifest(missing)
        missing.read_text.assert_not_called()

    def test_graph_draft_delete_callers_match_manifest_and_source(self):
        manifest = _load_manifest()
        self.assertEqual(EXPECTED_DRAFT_DELETE_MANIFEST, manifest)
        manifest_callers = _manifest_draft_delete_callers(manifest)
        self.assertEqual(EXPECTED_DRAFT_DELETE_CALLERS, manifest_callers)
        self.assertEqual(EXPECTED_DRAFT_DELETE_CALLERS, _discover_draft_delete_callers(manifest_callers))
        deferred = sum(entry["count"] for entry in manifest["deferred"])
        m2_owned = sum(entry["count"] for entry in manifest["m2Owned"])
        self.assertEqual((16, 5, 21), (deferred, m2_owned, deferred + m2_owned))

    def test_delete_helper_has_one_requests_delete_implementation(self):
        _, provider_deletes, _ = _scan_draft_delete_calls()
        self.assertEqual(1, provider_deletes)

    def test_pre_b1_legacy_authority_symbols_are_inventoried(self):
        self.assertEqual(EXPECTED_LEGACY_MARKER_DEFINITIONS, _discover_legacy_authority_symbols())

    def test_marker_compatibility_wrappers_quarantine_direct_storage(self):
        def top_level_function(tree, name):
            definitions = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ]
            self.assertEqual(1, len(definitions), name)
            return definitions[0]

        def called_names(function):
            names = set()
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    names.add(node.func.attr)
            return names

        expectations = {
            "email_automation/messaging.py": {
                "has_processed": {
                    "resolve_source_coordinator_mode",
                    "_legacy_has_processed",
                    "_shadow_marker_disposition",
                    "_settle_canonical_marker",
                },
                "mark_processed": {
                    "resolve_source_coordinator_mode",
                    "_legacy_mark_processed",
                    "_shadow_marker_disposition",
                    "_settle_canonical_marker",
                },
            },
            "scheduler_runner.py": {
                "has_processed": {
                    "resolve_source_coordinator_mode",
                    "_legacy_has_processed",
                    "_compat_has_processed",
                },
                "mark_processed": {
                    "resolve_source_coordinator_mode",
                    "_legacy_mark_processed",
                    "_compat_mark_processed",
                },
            },
        }
        for relative_path, wrappers in expectations.items():
            tree = _parse_module(Path(relative_path))
            for name, required_calls in wrappers.items():
                with self.subTest(relative_path=relative_path, name=name):
                    calls = called_names(top_level_function(tree, name))
                    self.assertTrue(required_calls <= calls)
                    self.assertNotIn("_processed_ref", calls)
                    self.assertNotIn("set", calls)
                    self.assertNotIn("get", calls)

            for name, required_effect in (
                ("_legacy_has_processed", "get"),
                ("_legacy_mark_processed", "set"),
            ):
                with self.subTest(relative_path=relative_path, name=name):
                    calls = called_names(top_level_function(tree, name))
                    self.assertIn("_processed_ref", calls)
                    self.assertIn(required_effect, calls)

    def test_operator_replay_legacy_claim_writers_are_disabled_only(self):
        tree = _parse_module(Path("email_automation/operator_replay.py"))
        replay_functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "replay_exact_message"
        ]
        self.assertEqual(1, len(replay_functions))
        replay_function = replay_functions[0]

        def call_name(node):
            if isinstance(node.func, ast.Name):
                return node.func.id
            if isinstance(node.func, ast.Attribute):
                return node.func.attr
            return None

        def is_enforced_test(node):
            return (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == "source_mode"
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Is)
                and len(node.comparators) == 1
                and isinstance(node.comparators[0], ast.Attribute)
                and isinstance(node.comparators[0].value, ast.Name)
                and node.comparators[0].value.id == "CoordinatorMode"
                and node.comparators[0].attr == "ENFORCED"
            )

        calls = [
            node
            for node in ast.walk(replay_function)
            if isinstance(node, ast.Call)
            and call_name(node)
            in {"_begin_replay_claim", "_complete_replay_claim"}
        ]
        self.assertEqual(2, len(calls))
        enforced_branches = [
            node
            for node in ast.walk(replay_function)
            if isinstance(node, ast.If) and is_enforced_test(node.test)
        ]
        self.assertGreaterEqual(len(enforced_branches), 2)
        for legacy_call in calls:
            self.assertTrue(
                any(
                    legacy_call in {
                        descendant
                        for statement in branch.orelse
                        for descendant in ast.walk(statement)
                    }
                    for branch in enforced_branches
                ),
                f"{call_name(legacy_call)} escaped the disabled-only branch",
            )
            self.assertFalse(
                any(
                    legacy_call in {
                        descendant
                        for statement in branch.body
                        for descendant in ast.walk(statement)
                    }
                    for branch in enforced_branches
                )
            )

        replay_calls = {
            call_name(node)
            for node in ast.walk(replay_function)
            if isinstance(node, ast.Call)
        }
        self.assertIn("resolve_source_coordinator_mode", replay_calls)
        self.assertIn("admit_or_repair_source_identity", replay_calls)

    def test_legacy_inventory_only_accepts_top_level_definitions(self):
        sources = {
            "email_automation/messaging.py": (
                "def has_processed(): pass\n"
                "class Hidden:\n def mark_processed(self): pass\n"
                "def wrapper():\n def mark_processed(): pass"
            ),
            "scheduler_runner.py": "def has_processed(): pass\ndef mark_processed(): pass",
            "email_automation/operator_replay.py": "def _begin_replay_claim(): pass\ndef _complete_replay_claim(): pass",
        }
        modules = [(path, ast.parse(source)) for path, source in sources.items()]
        discovered = _legacy_symbols_in_modules(modules)
        self.assertNotEqual(EXPECTED_LEGACY_MARKER_DEFINITIONS, discovered)
        self.assertNotIn(("email_automation/messaging.py", "mark_processed"), discovered)

    def test_legacy_inventory_counts_module_control_flow_definitions(self):
        blocks = {
            "if": "if enabled:\n def mark_processed(): pass",
            "try": "try:\n def mark_processed(): pass\nexcept Exception:\n pass",
            "match": "match value:\n case _:\n  def mark_processed(): pass",
            "for": "for item in items:\n def mark_processed(): pass",
            "with": "with context():\n def mark_processed(): pass",
        }
        expected = Counter(
            {("email_automation/messaging.py", "mark_processed"): 2}
        )
        for case, block in blocks.items():
            with self.subTest(case=case):
                tree = ast.parse(f"def mark_processed(): pass\n{block}")
                discovered = _legacy_symbols_in_modules(
                    [("email_automation/messaging.py", tree)]
                )
                self.assertEqual(expected, discovered)


if __name__ == "__main__":
    unittest.main()
