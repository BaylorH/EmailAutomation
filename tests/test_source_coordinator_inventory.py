import ast
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
DRAFT_DELETE_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "graph_draft_delete_callers.json"
)
DRAFT_DELETE_HELPER_NAME = "_delete_graph_reply_draft"
DRAFT_DELETE_EMAIL_PACKAGE = "email_automation"
DRAFT_DELETE_EMAIL_MODULE = "email_automation.email"
DRAFT_DELETE_EMAIL_MODULE_NAME = "email"
DRAFT_DELETE_EMAIL_PATH = Path("email_automation/email.py").as_posix()

EXPECTED_DRAFT_DELETE_CALLERS = Counter(
    {
        ("email_automation/email.py", "_send_outbox_as_reply"): 2,
        ("email_automation/email.py", "send_and_index_email"): 1,
        ("email_automation/followup.py", "_send_followup_email"): 13,
        ("email_automation/processing.py", "send_reply_in_thread"): 5,
    }
)

EXPECTED_LEGACY_MARKER_DEFINITIONS = Counter(
    {
        ("email_automation/messaging.py", "has_processed"): 1,
        ("email_automation/messaging.py", "mark_processed"): 1,
        ("scheduler_runner.py", "has_processed"): 1,
        ("scheduler_runner.py", "mark_processed"): 1,
        ("email_automation/operator_replay.py", "_begin_replay_claim"): 1,
        ("email_automation/operator_replay.py", "_complete_replay_claim"): 1,
    }
)

_HELPER_BINDING = "helper"
_EMAIL_PACKAGE_BINDING = "email_package"
_EMAIL_MODULE_BINDING = "email_module"
_NONHELPER_BINDING = "nonhelper"
_AMBIGUOUS_BINDING = "ambiguous"

# Bounded binding contract: assignment results are helper, nonhelper, or
# ambiguous. The email-package and email-module markers are provenance used
# only to resolve the exact module and helper attributes. Unsupported
# helper-derived expressions and destructuring become ambiguous and fail the
# inventory deterministically; this scanner does not attempt general runtime
# inference.


class DraftDeleteBindingInventoryError(AssertionError):
    """A helper-derived binding cannot be classified safely by the inventory."""


_EXCLUDED_APPLICATION_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
}


def _application_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        relative_path = path.relative_to(REPO_ROOT)
        if relative_path.parts[0] == "tests":
            continue
        if path.name.startswith("test_"):
            continue
        if any(part in _EXCLUDED_APPLICATION_PARTS for part in relative_path.parts):
            continue
        yield relative_path


def _parse_module(relative_path):
    source_path = REPO_ROOT / relative_path
    return ast.parse(
        source_path.read_text(encoding="utf-8"),
        filename=str(relative_path),
    )


def _resolve_import_from_module(relative_path, node):
    if node.level == 0:
        return node.module

    package_parts = list(Path(relative_path).parent.parts)
    parent_levels = node.level - 1
    if parent_levels > len(package_parts):
        return None
    if parent_levels:
        package_parts = package_parts[:-parent_levels]
    if node.module:
        package_parts.extend(node.module.split("."))
    return ".".join(package_parts)


class _ScopeBindingCollector(ast.NodeVisitor):
    def __init__(self, relative_path, *, module_scope):
        self.relative_path = Path(relative_path).as_posix()
        self.module_scope = module_scope
        self.bindings = {}
        self.assignments = []

    def _add_binding(self, name, binding_kind):
        self.bindings.setdefault(name, set()).add(binding_kind)

    def _add_nonhelper_target(self, target):
        if isinstance(target, ast.Name):
            self._add_binding(target.id, _NONHELPER_BINDING)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for element in target.elts:
                self._add_nonhelper_target(element)
        elif isinstance(target, ast.Starred):
            self._add_nonhelper_target(target.value)

    def _add_assignment(self, target, value):
        if isinstance(target, (ast.Name, ast.List, ast.Tuple, ast.Starred)):
            self.assignments.append((target, value))

    def visit_FunctionDef(self, node):
        if (
            self.module_scope
            and self.relative_path == DRAFT_DELETE_EMAIL_PATH
            and node.name == DRAFT_DELETE_HELPER_NAME
        ):
            self._add_binding(node.name, _HELPER_BINDING)
        else:
            self._add_binding(node.name, _NONHELPER_BINDING)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node):
        self._add_binding(node.name, _NONHELPER_BINDING)

    def visit_Lambda(self, node):
        return

    def visit_ImportFrom(self, node):
        imported_module = _resolve_import_from_module(self.relative_path, node)
        for alias in node.names:
            if alias.name == "*":
                continue
            bound_name = alias.asname or alias.name
            if (
                imported_module == DRAFT_DELETE_EMAIL_MODULE
                and alias.name == DRAFT_DELETE_HELPER_NAME
            ):
                self._add_binding(bound_name, _HELPER_BINDING)
            elif (
                imported_module == DRAFT_DELETE_EMAIL_PACKAGE
                and alias.name == DRAFT_DELETE_EMAIL_MODULE_NAME
            ):
                self._add_binding(bound_name, _EMAIL_MODULE_BINDING)
            else:
                self._add_binding(bound_name, _NONHELPER_BINDING)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name == DRAFT_DELETE_EMAIL_MODULE:
                if alias.asname:
                    self._add_binding(alias.asname, _EMAIL_MODULE_BINDING)
                else:
                    self._add_binding(
                        DRAFT_DELETE_EMAIL_PACKAGE,
                        _EMAIL_PACKAGE_BINDING,
                    )
            else:
                bound_name = alias.asname or alias.name.split(".", 1)[0]
                self._add_binding(bound_name, _NONHELPER_BINDING)

    def visit_Assign(self, node):
        for target in node.targets:
            self._add_assignment(target, node.value)
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        if node.value is None:
            self._add_nonhelper_target(node.target)
        else:
            self._add_assignment(node.target, node.value)
            self.visit(node.value)

    def visit_NamedExpr(self, node):
        self._add_assignment(node.target, node.value)
        self.visit(node.value)

    def visit_AugAssign(self, node):
        self._add_nonhelper_target(node.target)
        self.visit(node.value)

    def visit_For(self, node):
        self._add_nonhelper_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self.visit_For(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._add_nonhelper_target(item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node):
        self.visit_With(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self._add_binding(node.name, _NONHELPER_BINDING)
        self.generic_visit(node)


def _function_argument_names(arguments):
    positional = list(arguments.posonlyargs) + list(arguments.args)
    names = [argument.arg for argument in positional + list(arguments.kwonlyargs)]
    if arguments.vararg is not None:
        names.append(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.append(arguments.kwarg.arg)
    return names


def _lookup_binding_states(name, scoped_bindings):
    crossed_function = False
    for scope_kind, bindings in reversed(scoped_bindings):
        if scope_kind == "class" and crossed_function:
            continue
        if name in bindings:
            return set(bindings[name])
        if scope_kind == "function":
            crossed_function = True
    return set()


def _has_helper_provenance(states):
    return bool(
        states
        & {
            _HELPER_BINDING,
            _EMAIL_PACKAGE_BINDING,
            _EMAIL_MODULE_BINDING,
            _AMBIGUOUS_BINDING,
        }
    )


def _helper_reference_status(node, scoped_bindings):
    if isinstance(node, ast.Name):
        states = _lookup_binding_states(node.id, scoped_bindings)
        if not states:
            return None
        return _has_helper_provenance(states)
    if (
        isinstance(node, ast.Attribute)
        and node.attr == DRAFT_DELETE_HELPER_NAME
    ):
        receiver_states = _expression_binding_states(
            node.value,
            scoped_bindings,
        )
        if not receiver_states:
            return None
        return bool(
            receiver_states
            & {_EMAIL_MODULE_BINDING, _AMBIGUOUS_BINDING}
        )
    if isinstance(
        node,
        (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
    ):
        return False

    statuses = [
        _helper_reference_status(child, scoped_bindings)
        for child in ast.iter_child_nodes(node)
    ]
    if any(status is True for status in statuses):
        return True
    if any(status is None for status in statuses):
        return None
    return False


def _expression_binding_states(node, scoped_bindings):
    if isinstance(node, ast.Name):
        return _lookup_binding_states(node.id, scoped_bindings)
    if (
        isinstance(node, ast.Attribute)
        and node.attr == DRAFT_DELETE_EMAIL_MODULE_NAME
    ):
        receiver_states = _expression_binding_states(
            node.value,
            scoped_bindings,
        )
        if not receiver_states:
            return set()
        if receiver_states == {_EMAIL_PACKAGE_BINDING}:
            return {_EMAIL_MODULE_BINDING}
        if receiver_states & {_EMAIL_PACKAGE_BINDING, _AMBIGUOUS_BINDING}:
            return {_AMBIGUOUS_BINDING}
        return {_NONHELPER_BINDING}
    if (
        isinstance(node, ast.Attribute)
        and node.attr == DRAFT_DELETE_HELPER_NAME
    ):
        receiver_states = _expression_binding_states(
            node.value,
            scoped_bindings,
        )
        if not receiver_states:
            return set()
        if receiver_states == {_EMAIL_MODULE_BINDING}:
            return {_HELPER_BINDING}
        if receiver_states & {_EMAIL_MODULE_BINDING, _AMBIGUOUS_BINDING}:
            return {_AMBIGUOUS_BINDING}
        return {_NONHELPER_BINDING}
    if isinstance(node, ast.IfExp):
        true_states = _expression_binding_states(node.body, scoped_bindings)
        false_states = _expression_binding_states(node.orelse, scoped_bindings)
        if true_states and true_states == false_states:
            return true_states
        if _has_helper_provenance(true_states | false_states):
            return {_AMBIGUOUS_BINDING}
        if not true_states or not false_states:
            return set()
        return {_NONHELPER_BINDING}
    if isinstance(node, ast.NamedExpr):
        return _expression_binding_states(node.value, scoped_bindings)
    if isinstance(
        node,
        (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
    ):
        return {_NONHELPER_BINDING}

    helper_reference = _helper_reference_status(node, scoped_bindings)
    if helper_reference is True:
        return {_AMBIGUOUS_BINDING}
    if helper_reference is None:
        return set()
    return {_NONHELPER_BINDING}


def _target_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return [
            name
            for element in target.elts
            for name in _target_names(element)
        ]
    return []


def _is_bounded_assignment_target(target):
    if isinstance(target, ast.Name):
        return True
    if isinstance(target, ast.Starred):
        return _is_bounded_assignment_target(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return all(
            _is_bounded_assignment_target(element) for element in target.elts
        )
    return False


def _supported_binding_source_nodes(node, scoped_bindings):
    if node is None:
        return None
    states = _expression_binding_states(node, scoped_bindings)
    if states not in (
        {_HELPER_BINDING},
        {_EMAIL_PACKAGE_BINDING},
        {_EMAIL_MODULE_BINDING},
    ):
        return None

    if isinstance(node, ast.Name):
        return {id(node)}
    if (
        isinstance(node, ast.Attribute)
        and node.attr == DRAFT_DELETE_HELPER_NAME
    ):
        return {id(node)}
    if isinstance(node, ast.IfExp):
        true_nodes = _supported_binding_source_nodes(
            node.body,
            scoped_bindings,
        )
        false_nodes = _supported_binding_source_nodes(
            node.orelse,
            scoped_bindings,
        )
        if true_nodes is not None and false_nodes is not None:
            return true_nodes | false_nodes
    return None


def _approved_assignment_reference_nodes(target, value, scoped_bindings):
    if isinstance(target, ast.Name):
        source_nodes = _supported_binding_source_nodes(value, scoped_bindings)
        if source_nodes is not None:
            return {id(target)} | source_nodes
        return set()

    is_bounded_pairing = (
        isinstance(target, (ast.List, ast.Tuple))
        and isinstance(value, (ast.List, ast.Tuple))
        and len(target.elts) == len(value.elts)
        and not any(isinstance(element, ast.Starred) for element in target.elts)
    )
    if not is_bounded_pairing:
        return set()

    approved_nodes = set()
    for target_element, value_element in zip(target.elts, value.elts):
        approved_nodes.update(
            _approved_assignment_reference_nodes(
                target_element,
                value_element,
                scoped_bindings,
            )
        )
    return approved_nodes


def _approved_call_target_reference_nodes(node, scoped_bindings):
    if _expression_binding_states(node, scoped_bindings) != {_HELPER_BINDING}:
        return set()

    if isinstance(node, ast.NamedExpr):
        value_nodes = _approved_call_target_reference_nodes(
            node.value,
            scoped_bindings,
        )
        if value_nodes:
            return {id(node.target)} | value_nodes
        return set()
    if isinstance(node, ast.IfExp):
        true_nodes = _approved_call_target_reference_nodes(
            node.body,
            scoped_bindings,
        )
        false_nodes = _approved_call_target_reference_nodes(
            node.orelse,
            scoped_bindings,
        )
        if true_nodes and false_nodes:
            return true_nodes | false_nodes
        return set()

    source_nodes = _supported_binding_source_nodes(node, scoped_bindings)
    return source_nodes if source_nodes is not None else set()


def _assignment_binding_updates(target, value, scoped_bindings):
    if isinstance(target, ast.Name):
        value_states = _expression_binding_states(value, scoped_bindings)
        if not value_states:
            return None
        return [(target.id, value_states)]

    if isinstance(target, (ast.List, ast.Tuple)):
        is_bounded_pairing = (
            isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
            and not any(
                isinstance(element, ast.Starred) for element in target.elts
            )
        )
        if is_bounded_pairing:
            updates = []
            for target_element, value_element in zip(target.elts, value.elts):
                element_updates = _assignment_binding_updates(
                    target_element,
                    value_element,
                    scoped_bindings,
                )
                if element_updates is None:
                    return None
                updates.extend(element_updates)
            return updates

    target_names = _target_names(target)
    if not target_names:
        return []
    helper_reference = _helper_reference_status(value, scoped_bindings)
    if helper_reference is None:
        return None
    binding_kind = (
        _AMBIGUOUS_BINDING if helper_reference else _NONHELPER_BINDING
    )
    return [(name, {binding_kind}) for name in target_names]


def _analyze_scope_bindings(node, relative_path, scope_kind, outer_bindings):
    collector = _ScopeBindingCollector(
        relative_path,
        module_scope=scope_kind == "module",
    )
    if scope_kind == "function":
        for argument_name in _function_argument_names(node.args):
            collector._add_binding(argument_name, _NONHELPER_BINDING)
    for statement in node.body:
        collector.visit(statement)

    bindings = collector.bindings
    pending_assignments = list(collector.assignments)
    while pending_assignments:
        unresolved = []
        changed = False
        scoped_bindings = outer_bindings + [(scope_kind, bindings)]
        for target, value in pending_assignments:
            updates = _assignment_binding_updates(target, value, scoped_bindings)
            if updates is None:
                unresolved.append((target, value))
                continue
            for target_name, value_states in updates:
                target_states = bindings.setdefault(target_name, set())
                previous_states = set(target_states)
                target_states.update(value_states)
                changed = changed or target_states != previous_states
        if not unresolved:
            break
        if not changed:
            for target, _ in unresolved:
                for target_name in _target_names(target):
                    bindings.setdefault(target_name, set()).add(
                        _NONHELPER_BINDING
                    )
            break
        pending_assignments = unresolved

    for name, states in bindings.items():
        helper_related = states & {
            _HELPER_BINDING,
            _EMAIL_PACKAGE_BINDING,
            _EMAIL_MODULE_BINDING,
        }
        if _AMBIGUOUS_BINDING in states or (helper_related and len(states) > 1):
            raise DraftDeleteBindingInventoryError(
                "unsupported or ambiguous draft delete binding "
                f"(ambiguous draft delete alias) {name!r} in "
                f"{Path(relative_path).as_posix()}"
            )
    return bindings


class _DraftDeleteCallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path):
        self.relative_path = Path(relative_path).as_posix()
        self.scope_stack = []
        self.binding_stack = []
        self.approved_reference_stack = []
        self.callers = Counter()
        self.requests_delete_implementations = 0

    def visit_Module(self, node):
        bindings = _analyze_scope_bindings(
            node,
            self.relative_path,
            "module",
            [],
        )
        self.binding_stack.append(("module", bindings))
        self.generic_visit(node)
        self.binding_stack.pop()

    def _visit_scope(self, node, scope_kind):
        bindings = _analyze_scope_bindings(
            node,
            self.relative_path,
            scope_kind,
            self.binding_stack,
        )
        self.scope_stack.append((scope_kind, node.name))
        self.binding_stack.append((scope_kind, bindings))
        for statement in node.body:
            self.visit(statement)
        self.binding_stack.pop()
        self.scope_stack.pop()

    def _visit_if_present(self, node):
        if node is not None:
            self.visit(node)

    def _visit_function_scope(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            self._visit_if_present(default)

        arguments = (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            self._visit_if_present(argument.annotation)
        self._visit_if_present(node.returns)

        self._visit_scope(node, "function")

    def _visit_class_scope(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_scope(node, "class")

    def _caller_scope(self):
        if not self.scope_stack:
            return "<module>"
        scope_kind, scope_name = self.scope_stack[-1]
        if scope_kind == "function":
            return scope_name
        class_names = [
            name for kind, name in self.scope_stack if kind == "class"
        ]
        return f"<class:{'.'.join(class_names)}>"

    def _inside_email_delete_helper(self):
        return (
            self.relative_path == DRAFT_DELETE_EMAIL_PATH
            and self.scope_stack
            == [("function", DRAFT_DELETE_HELPER_NAME)]
        )

    def visit_FunctionDef(self, node):
        self._visit_function_scope(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function_scope(node)

    def visit_ClassDef(self, node):
        self._visit_class_scope(node)

    def visit_Lambda(self, node):
        defaults = list(node.args.defaults) + [
            default for default in node.args.kw_defaults if default is not None
        ]
        for default in defaults:
            self.visit(default)

        bindings = {
            name: {_NONHELPER_BINDING}
            for name in _function_argument_names(node.args)
        }
        self.binding_stack.append(("function", bindings))
        self.visit(node.body)
        self.binding_stack.pop()

    def _visit_comprehension(self, node, result_nodes):
        if not node.generators:
            for result_node in result_nodes:
                self.visit(result_node)
            return

        self.visit(node.generators[0].iter)
        bindings = {}
        self.binding_stack.append(("function", bindings))
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            for name in _target_names(generator.target):
                bindings[name] = {_NONHELPER_BINDING}
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for result_node in result_nodes:
            self.visit(result_node)
        self.binding_stack.pop()

    def visit_ListComp(self, node):
        self._visit_comprehension(node, [node.elt])

    def visit_SetComp(self, node):
        self._visit_comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node):
        self._visit_comprehension(node, [node.elt])

    def visit_DictComp(self, node):
        self._visit_comprehension(node, [node.key, node.value])

    def _visit_assignment(self, targets, value):
        approved_nodes = set()
        if all(_is_bounded_assignment_target(target) for target in targets):
            for target in targets:
                approved_nodes.update(
                    _approved_assignment_reference_nodes(
                        target,
                        value,
                        self.binding_stack,
                    )
                )

        self.approved_reference_stack.append(approved_nodes)
        try:
            for target in targets:
                self.visit(target)
            self._visit_if_present(value)
        finally:
            self.approved_reference_stack.pop()

    def _reference_is_approved(self, node):
        node_id = id(node)
        return any(
            node_id in approved_nodes
            for approved_nodes in reversed(self.approved_reference_stack)
        )

    def _raise_unsupported_reference(self, node):
        raise DraftDeleteBindingInventoryError(
            "unsupported draft delete helper propagation at "
            f"{self.relative_path}:{node.lineno}"
        )

    def visit_Name(self, node):
        if self._reference_is_approved(node):
            return
        states = _lookup_binding_states(node.id, self.binding_stack)
        if _HELPER_BINDING in states or _AMBIGUOUS_BINDING in states:
            self._raise_unsupported_reference(node)

    def visit_Attribute(self, node):
        if self._reference_is_approved(node):
            self.generic_visit(node)
            return
        if node.attr == DRAFT_DELETE_HELPER_NAME:
            states = _expression_binding_states(node, self.binding_stack)
            if states == {_HELPER_BINDING}:
                self._raise_unsupported_reference(node)
            if _AMBIGUOUS_BINDING in states:
                raise DraftDeleteBindingInventoryError(
                    "unsupported or ambiguous draft delete receiver at "
                    f"{self.relative_path}:{node.lineno}"
                )
        self.generic_visit(node)

    def visit_Assign(self, node):
        self._visit_assignment(node.targets, node.value)

    def visit_AnnAssign(self, node):
        self.visit(node.annotation)
        self._visit_assignment([node.target], node.value)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self.visit(node.target)

    def visit_Call(self, node):
        call_target_states = _expression_binding_states(
            node.func,
            self.binding_stack,
        )
        is_delete_helper_call = call_target_states == {_HELPER_BINDING}
        is_ambiguous_helper_call = (
            _AMBIGUOUS_BINDING in call_target_states
            or (
                _HELPER_BINDING in call_target_states
                and not is_delete_helper_call
            )
        )
        if is_ambiguous_helper_call:
            raise DraftDeleteBindingInventoryError(
                "unsupported or ambiguous draft delete call target at "
                f"{self.relative_path}:{node.lineno}"
            )

        if is_delete_helper_call:
            self.callers[(self.relative_path, self._caller_scope())] += 1

        if (
            self._inside_email_delete_helper()
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and node.func.attr == "delete"
        ):
            self.requests_delete_implementations += 1

        if is_delete_helper_call:
            approved_nodes = _approved_call_target_reference_nodes(
                node.func,
                self.binding_stack,
            )
            if not approved_nodes:
                self._raise_unsupported_reference(node.func)
            self.approved_reference_stack.append(approved_nodes)
            try:
                self.visit(node.func)
            finally:
                self.approved_reference_stack.pop()
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return

        self.generic_visit(node)


def _scan_draft_delete_calls():
    callers = Counter()
    requests_delete_implementations = 0
    application_paths = set()

    for relative_path in _application_python_files():
        application_paths.add(relative_path.as_posix())
        visitor = _DraftDeleteCallVisitor(relative_path)
        visitor.visit(_parse_module(relative_path))
        callers.update(visitor.callers)
        requests_delete_implementations += visitor.requests_delete_implementations

    return callers, requests_delete_implementations, application_paths


def _manifest_draft_delete_callers(manifest):
    callers = Counter()
    for ownership_group in ("deferred", "m2Owned"):
        for entry in manifest[ownership_group]:
            manifest_path = Path(entry["path"]).as_posix()
            callers[(manifest_path, entry["function"])] += entry["count"]
    return callers


def _discover_draft_delete_callers(manifest_callers):
    callers, _, application_paths = _scan_draft_delete_calls()
    manifest_paths = {Path(path).as_posix() for path, _ in manifest_callers}
    if not manifest_paths <= application_paths:
        raise AssertionError("graph draft delete manifest names a non-application path")
    return callers


def _discover_legacy_authority_symbols():
    target_names = {
        name for _, name in EXPECTED_LEGACY_MARKER_DEFINITIONS
    }
    discovered = Counter()
    for relative_path in _application_python_files():
        path = relative_path.as_posix()
        for node in ast.walk(_parse_module(relative_path)):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in target_names
            ):
                discovered[(path, node.name)] += 1
    return discovered


def _in_memory_draft_delete_callers(source, relative_path):
    visitor = _DraftDeleteCallVisitor(Path(relative_path))
    visitor.visit(ast.parse(source, filename=relative_path))
    return visitor.callers


class InventoryScannerRegressionTests(unittest.TestCase):
    def test_unaliased_email_module_import_call_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
import email_automation.email

def cleanup():
    email_automation.email._delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_named_expression_email_module_receiver_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
import email_automation.email as email_module

def cleanup():
    (alias := email_module)._delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_conditional_email_module_receiver_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
import email_automation.email as email_module

def cleanup(flag):
    (
        email_module if flag else email_module
    )._delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_named_expression_direct_helper_callee_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft as helper

def cleanup():
    (alias := helper)()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_named_expression_helper_alias_outside_callee_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

def cleanup():
    (alias := helper)
    alias()
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_returned_helper_reference_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

def expose_helper():
    return helper
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_yielded_helper_reference_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

def expose_helper():
    yield helper
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_lambda_body_helper_reference_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

expose_helper = lambda: helper
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_direct_helper_call_survives_global_reference_validation(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft as helper

def cleanup():
    helper()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_supported_helper_alias_survives_global_reference_validation(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft as helper

def cleanup():
    callback = helper
    callback()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_function_default_helper_propagation_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

def cleanup(callback=helper):
    callback()
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_lambda_default_helper_propagation_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

cleanup = lambda callback=helper: callback()
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_comprehension_helper_propagation_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

results = [callback() for callback in [helper]]
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_attribute_assignment_helper_propagation_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

box.callback = helper
box.callback()
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported draft delete helper propagation",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_conditional_helper_alias_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as helper

def cleanup(flag, fallback):
    alias = helper if flag else fallback
    alias()
"""
        with self.assertRaisesRegex(
            DraftDeleteBindingInventoryError,
            "unsupported or ambiguous draft delete binding",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_single_item_destructured_helper_alias_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft as helper

def cleanup():
    (alias,) = (helper,)
    alias()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_lambda_parameter_shadowing_helper_name_is_not_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft

cleanup = lambda _delete_graph_reply_draft: _delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(Counter(), callers)

    def test_comprehension_target_shadowing_helper_name_is_not_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft

results = [
    _delete_graph_reply_draft()
    for _delete_graph_reply_draft in callbacks
]
""",
            "email_automation/followup.py",
        )
        self.assertEqual(Counter(), callers)

    def test_simple_assignment_alias_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft

def cleanup():
    alias = _delete_graph_reply_draft
    alias()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_forward_global_helper_definition_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
def cleanup():
    _delete_graph_reply_draft()

def _delete_graph_reply_draft():
    pass
""",
            "email_automation/email.py",
        )
        self.assertEqual(
            Counter({("email_automation/email.py", "cleanup"): 1}),
            callers,
        )

    def test_import_after_function_definition_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
def cleanup():
    discard_reply()

from .email import _delete_graph_reply_draft as discard_reply
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_parameter_shadowing_helper_name_is_not_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft

def cleanup(_delete_graph_reply_draft):
    _delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(Counter(), callers)

    def test_local_nonhelper_shadowing_helper_name_is_not_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft

def cleanup():
    _delete_graph_reply_draft = client.cleanup
    _delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(Counter(), callers)

    def test_ambiguous_assignment_alias_fails_closed(self):
        source = """
from .email import _delete_graph_reply_draft as delete_draft

def cleanup(flag):
    alias = delete_draft
    if flag:
        alias = client.cleanup
    alias()
"""
        with self.assertRaisesRegex(
            AssertionError,
            "ambiguous draft delete alias",
        ):
            _in_memory_draft_delete_callers(
                source,
                "email_automation/followup.py",
            )

    def test_module_scope_delete_call_is_surfaced(self):
        callers = _in_memory_draft_delete_callers(
            """
def _delete_graph_reply_draft():
    pass

_delete_graph_reply_draft()
""",
            "email_automation/email.py",
        )
        self.assertEqual(
            Counter({("email_automation/email.py", "<module>"): 1}),
            callers,
        )

    def test_class_scope_delete_call_is_surfaced(self):
        callers = _in_memory_draft_delete_callers(
            """
def _delete_graph_reply_draft():
    pass

class Cleanup:
    _delete_graph_reply_draft()
""",
            "email_automation/email.py",
        )
        self.assertEqual(
            Counter({("email_automation/email.py", "<class:Cleanup>"): 1}),
            callers,
        )

    def test_unrelated_attribute_receiver_is_not_a_delete_helper_call(self):
        callers = _in_memory_draft_delete_callers(
            """
def cleanup():
    client._delete_graph_reply_draft()
""",
            "email_automation/unrelated.py",
        )
        self.assertEqual(Counter(), callers)

    def test_aliased_exact_helper_import_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
from .email import _delete_graph_reply_draft as discard_reply

def cleanup():
    discard_reply()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_exact_email_module_alias_call_is_counted(self):
        callers = _in_memory_draft_delete_callers(
            """
import email_automation.email as email_module

def cleanup():
    email_module._delete_graph_reply_draft()
""",
            "email_automation/followup.py",
        )
        self.assertEqual(
            Counter({("email_automation/followup.py", "cleanup"): 1}),
            callers,
        )

    def test_unrelated_module_alias_is_not_a_delete_helper_call(self):
        callers = _in_memory_draft_delete_callers(
            """
import unrelated.email as email_module

def cleanup():
    email_module._delete_graph_reply_draft()
""",
            "email_automation/unrelated.py",
        )
        self.assertEqual(Counter(), callers)

    def test_extra_legacy_authority_definition_fails_inventory(self):
        modules = {
            "email_automation/messaging.py": ast.parse(
                "def has_processed(): pass\ndef mark_processed(): pass\n"
            ),
            "scheduler_runner.py": ast.parse(
                "def has_processed(): pass\ndef mark_processed(): pass\n"
            ),
            "email_automation/operator_replay.py": ast.parse(
                "def _begin_replay_claim(): pass\n"
                "def _complete_replay_claim(): pass\n"
            ),
            "email_automation/duplicate_writer.py": ast.parse(
                "def mark_processed(): pass\n"
            ),
        }

        with mock.patch(
            f"{__name__}._application_python_files",
            return_value=[Path(path) for path in modules],
        ), mock.patch(
            f"{__name__}._parse_module",
            side_effect=lambda path: modules[Path(path).as_posix()],
        ):
            inventory_case = LegacyMarkerInventoryTests(
                "test_pre_b1_legacy_authority_symbols_are_inventoried"
            )
            with self.assertRaises(AssertionError):
                inventory_case.test_pre_b1_legacy_authority_symbols_are_inventoried()

    def test_duplicate_legacy_definition_in_expected_file_fails_inventory(self):
        duplicate_sources = {
            "top_level": "def mark_processed(): pass\n",
            "method": "class Duplicate:\n    def mark_processed(self): pass\n",
            "nested": (
                "def wrapper():\n"
                "    def mark_processed(): pass\n"
                "    return mark_processed\n"
            ),
        }

        for duplicate_kind, duplicate_source in duplicate_sources.items():
            with self.subTest(duplicate_kind=duplicate_kind):
                modules = {
                    "email_automation/messaging.py": ast.parse(
                        "def has_processed(): pass\n"
                        "def mark_processed(): pass\n"
                        + duplicate_source
                    ),
                    "scheduler_runner.py": ast.parse(
                        "def has_processed(): pass\n"
                        "def mark_processed(): pass\n"
                    ),
                    "email_automation/operator_replay.py": ast.parse(
                        "def _begin_replay_claim(): pass\n"
                        "def _complete_replay_claim(): pass\n"
                    ),
                }

                with mock.patch(
                    f"{__name__}._application_python_files",
                    return_value=[Path(path) for path in modules],
                ), mock.patch(
                    f"{__name__}._parse_module",
                    side_effect=lambda path: modules[Path(path).as_posix()],
                ):
                    inventory_case = LegacyMarkerInventoryTests(
                        "test_pre_b1_legacy_authority_symbols_are_inventoried"
                    )
                    with self.assertRaises(AssertionError):
                        inventory_case.test_pre_b1_legacy_authority_symbols_are_inventoried()


class GraphDraftDeleteInventoryTests(unittest.TestCase):
    def test_graph_draft_delete_callers_match_manifest_and_source(self):
        if not DRAFT_DELETE_MANIFEST_PATH.exists():
            self.fail("graph draft delete caller manifest is missing")
            return

        manifest = json.loads(DRAFT_DELETE_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            {"schemaVersion", "deferred", "m2Owned"},
            set(manifest),
        )
        self.assertEqual(1, manifest["schemaVersion"])
        self.assertEqual(
            [
                {
                    "path": "email_automation/email.py",
                    "function": "_send_outbox_as_reply",
                    "count": 2,
                },
                {
                    "path": "email_automation/email.py",
                    "function": "send_and_index_email",
                    "count": 1,
                },
                {
                    "path": "email_automation/followup.py",
                    "function": "_send_followup_email",
                    "count": 13,
                },
            ],
            manifest["deferred"],
        )
        self.assertEqual(
            [
                {
                    "path": "email_automation/processing.py",
                    "function": "send_reply_in_thread",
                    "count": 5,
                }
            ],
            manifest["m2Owned"],
        )
        manifest_callers = _manifest_draft_delete_callers(manifest)
        source_callers = _discover_draft_delete_callers(manifest_callers)

        self.assertEqual(EXPECTED_DRAFT_DELETE_CALLERS, manifest_callers)
        self.assertEqual(EXPECTED_DRAFT_DELETE_CALLERS, source_callers)

        deferred_count = sum(entry["count"] for entry in manifest["deferred"])
        m2_owned_count = sum(entry["count"] for entry in manifest["m2Owned"])
        self.assertEqual(3 + 13, deferred_count)
        self.assertEqual(5, m2_owned_count)
        self.assertEqual(21, deferred_count + m2_owned_count)

    def test_delete_helper_has_one_requests_delete_implementation(self):
        _, requests_delete_implementations, _ = _scan_draft_delete_calls()
        self.assertEqual(1, requests_delete_implementations)


class LegacyMarkerInventoryTests(unittest.TestCase):
    def test_pre_b1_legacy_authority_symbols_are_inventoried(self):
        self.assertEqual(
            EXPECTED_LEGACY_MARKER_DEFINITIONS,
            _discover_legacy_authority_symbols(),
        )


if __name__ == "__main__":
    unittest.main()
