"""Regression coverage for production access-token log redaction."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENTRYPOINTS = (REPO_ROOT / "main.py", REPO_ROOT / "scheduler_runner.py")
TOKEN_LOG_SURFACES = TOKEN_ENTRYPOINTS + (REPO_ROOT / "tests" / "e2e_helpers.py",)
LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
TAINT_PRESERVING_CALLS = frozenset(
    {"str", "repr", "bytes", "bytearray", "format", "dict", "list", "tuple", "set"}
)
TOKEN_METADATA_CALLS = frozenset({("_expires_in_seconds",)})
TOKEN_METADATA_KEYS = frozenset({"expires_in"})


def _qualified_name(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_qualified_name(node.value), node.attr)
    return ()


def _is_logger_receiver(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"logger", "logging", "log"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"logger", "logging", "log"} or _is_logger_receiver(
            node.value
        )
    if isinstance(node, ast.Call):
        return _is_logger_receiver(node.func)
    return False


def _is_log_sink(node: ast.expr) -> bool:
    qualified_name = _qualified_name(node)
    if qualified_name in {("print",), ("builtins", "print")}:
        return True

    return bool(
        isinstance(node, ast.Attribute)
        and node.attr in LOG_METHODS
        and _is_logger_receiver(node.value)
    )


def _subscript_key(node: ast.Subscript) -> object:
    return node.slice.value if isinstance(node.slice, ast.Constant) else None


def _token_metadata_taint_candidates(
    node: ast.Call,
) -> tuple[ast.expr, ...] | None:
    if _qualified_name(node.func) in TOKEN_METADATA_CALLS:
        if len(node.args) == 1 and not node.keywords:
            return ()
        return None
    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in TOKEN_METADATA_KEYS
    ):
        return None
    return (*node.args[1:], *(keyword.value for keyword in node.keywords))


def _contains_token_material(node: ast.AST, tainted_names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, ast.Name):
        return node.id == "access_token" or node.id in tainted_names
    if isinstance(node, ast.Attribute):
        return node.attr == "access_token" or _contains_token_material(
            node.value, tainted_names
        )
    if isinstance(node, ast.Subscript):
        key = _subscript_key(node)
        if key == "access_token":
            return True
        if key is not None:
            return _contains_token_material(node.slice, tainted_names)
        return _contains_token_material(
            node.value, tainted_names
        ) or _contains_token_material(node.slice, tainted_names)
    if isinstance(node, ast.Call):
        metadata_candidates = _token_metadata_taint_candidates(node)
        if metadata_candidates is not None:
            return any(
                _contains_token_material(candidate, tainted_names)
                for candidate in metadata_candidates
            )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "access_token"
        ):
            return True
        if isinstance(node.func, ast.Attribute) and _contains_token_material(
            node.func.value, tainted_names
        ):
            return True
        return any(
            _contains_token_material(argument, tainted_names)
            for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        )
    if isinstance(node, ast.Dict):
        return any(
            _contains_token_material(item, tainted_names)
            for item in (*node.keys, *node.values)
            if item is not None
        )
    return any(
        _contains_token_material(child, tainted_names)
        for child in ast.iter_child_nodes(node)
    )


def _assigned_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_assigned_names(item) for item in target.elts))
    if isinstance(target, (ast.Attribute, ast.Subscript)):
        return _assigned_names(target.value)
    return set()


def _assignment_value_is_tainted(value: ast.expr, tainted_names: set[str]) -> bool:
    if not isinstance(value, ast.Call):
        return _contains_token_material(value, tainted_names)
    metadata_candidates = _token_metadata_taint_candidates(value)
    if metadata_candidates is not None:
        return any(
            _contains_token_material(candidate, tainted_names)
            for candidate in metadata_candidates
        )

    qualified_name = _qualified_name(value.func)
    fetches_access_token = bool(
        isinstance(value.func, ast.Attribute)
        and value.func.attr == "get"
        and value.args
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value == "access_token"
    )
    preserves_arguments = qualified_name in {
        (name,) for name in TAINT_PRESERVING_CALLS
    }
    tainted_receiver = bool(
        isinstance(value.func, ast.Attribute)
        and _contains_token_material(value.func.value, tainted_names)
    )
    return fetches_access_token or tainted_receiver or (
        preserves_arguments and _contains_token_material(value, tainted_names)
    )


class _ScopeCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes.append(node)
        super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _nodes_in_scope(scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[ast.AST]:
    collector = _ScopeCollector()
    for statement in scope.body:
        collector.visit(statement)
    return collector.nodes


def _tainted_names(nodes: list[ast.AST]) -> set[str]:
    tainted = {"access_token"}
    assignments: list[tuple[list[ast.expr], ast.expr, bool]] = []
    for node in nodes:
        if isinstance(node, ast.Subscript) and _subscript_key(node) == "access_token":
            tainted.update(_assigned_names(node.value))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "access_token"
        ):
            tainted.update(_assigned_names(node.func.value))

        if isinstance(node, ast.Assign):
            assignments.append((node.targets, node.value, False))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(([node.target], node.value, False))
        elif isinstance(node, ast.NamedExpr):
            assignments.append(([node.target], node.value, False))
        elif isinstance(node, ast.AugAssign):
            assignments.append(([node.target], node.value, True))

    changed = True
    while changed:
        changed = False
        for targets, value, is_augmented in assignments:
            names = set().union(*(_assigned_names(target) for target in targets))
            if not (
                _assignment_value_is_tainted(value, tainted)
                or (is_augmented and bool(names & tainted))
            ):
                continue
            if not names <= tainted:
                tainted.update(names)
                changed = True
    return tainted


def _assignment_parts(
    node: ast.AST,
) -> tuple[list[ast.expr], ast.expr] | None:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    if isinstance(node, ast.NamedExpr):
        return [node.target], node.value
    if isinstance(node, ast.AugAssign):
        return [node.target], node.value
    return None


def _assignment_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for candidate in ast.walk(node):
        assignment = _assignment_parts(candidate)
        if assignment is None:
            continue
        targets, _ = assignment
        for target in targets:
            names.update(_assigned_names(target))
    return names


def _simple_target_names(target: ast.expr) -> set[str] | None:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in target.elts:
            item_names = _simple_target_names(item)
            if item_names is None:
                return None
            names.update(item_names)
        return names
    return None


def _direct_assignment(
    statement: ast.stmt,
) -> tuple[list[ast.expr], ast.expr, bool] | None:
    assignment = _assignment_parts(statement)
    if assignment is not None:
        targets, value = assignment
        return targets, value, isinstance(statement, ast.AugAssign)
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.NamedExpr):
        targets, value = _assignment_parts(statement.value) or ([], statement.value)
        return targets, value, False
    return None


def _token_container_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for candidate in ast.walk(node):
        if (
            isinstance(candidate, ast.Subscript)
            and _subscript_key(candidate) == "access_token"
        ):
            names.update(_assigned_names(candidate.value))
        elif (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr == "get"
            and candidate.args
            and isinstance(candidate.args[0], ast.Constant)
            and candidate.args[0].value == "access_token"
        ):
            names.update(_assigned_names(candidate.func.value))
    return names


def _forget_name_alias(
    name: str, aliases: dict[str, frozenset[str]]
) -> None:
    previous = aliases.pop(name, frozenset())
    remaining = previous - {name}
    for alias in remaining:
        if len(remaining) > 1:
            aliases[alias] = frozenset(remaining)
        else:
            aliases.pop(alias, None)


def _union_name_aliases(
    left: str, right: str, aliases: dict[str, frozenset[str]]
) -> None:
    equivalent = aliases.get(left, frozenset({left})) | aliases.get(
        right, frozenset({right})
    )
    for name in equivalent:
        aliases[name] = equivalent


def _update_name_aliases(
    targets: list[ast.expr],
    value: ast.expr,
    is_augmented: bool,
    aliases: dict[str, frozenset[str]],
) -> None:
    if is_augmented or not all(isinstance(target, ast.Name) for target in targets):
        return

    target_names = [target.id for target in targets if isinstance(target, ast.Name)]
    for name in target_names:
        if not (isinstance(value, ast.Name) and name == value.id):
            _forget_name_alias(name, aliases)

    if isinstance(value, ast.Name):
        for name in target_names:
            _union_name_aliases(name, value.id, aliases)


def _equivalent_name_aliases(
    names: set[str], aliases: dict[str, frozenset[str]]
) -> set[str]:
    if not names:
        return set()
    return set().union(
        *(aliases.get(name, frozenset({name})) for name in names)
    )


def _statement_block_containing(
    statements: list[ast.stmt], target: ast.AST
) -> tuple[list[ast.stmt], int] | None:
    for index, statement in enumerate(statements):
        if not any(candidate is target for candidate in ast.walk(statement)):
            continue

        for candidate in ast.walk(statement):
            for _, value in ast.iter_fields(candidate):
                if not (
                    isinstance(value, list)
                    and value
                    and all(isinstance(item, ast.stmt) for item in value)
                ):
                    continue
                if not any(
                    nested is target
                    for item in value
                    for nested in ast.walk(item)
                ):
                    continue
                nested_block = _statement_block_containing(value, target)
                if nested_block is not None:
                    return nested_block

        return statements, index
    return None


def _tainted_names_at_sink(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    nodes: list[ast.AST],
    sink: ast.Call,
) -> set[str]:
    potentially_tainted = _tainted_names(nodes)
    tainted = set(potentially_tainted)
    block_match = _statement_block_containing(scope.body, sink)
    if block_match is None:
        return tainted

    statements, sink_index = block_match
    aliases: dict[str, frozenset[str]] = {}
    for statement in statements[:sink_index]:
        token_container_names = _equivalent_name_aliases(
            _token_container_names(statement), aliases
        )
        assignment = _direct_assignment(statement)
        if assignment is None:
            tainted.update(_assignment_names(statement) & potentially_tainted)
            tainted.update(token_container_names)
            continue

        targets, value, is_augmented = assignment
        direct_names: set[str] = set()
        for target in targets:
            target_names = _simple_target_names(target)
            if target_names is None:
                tainted.update(_assignment_names(statement) & potentially_tainted)
                break
            direct_names.update(target_names)
        else:
            value_is_tainted = _assignment_value_is_tainted(value, tainted)
            for name in direct_names:
                if name == "access_token" or (
                    is_augmented and name in tainted
                ) or value_is_tainted:
                    tainted.add(name)
                else:
                    tainted.discard(name)

            nested_names = (
                _assignment_names(statement) - direct_names
            ) & potentially_tainted
            tainted.update(nested_names)

        _update_name_aliases(targets, value, is_augmented, aliases)
        tainted.update(token_container_names)
    return tainted


@pytest.mark.parametrize("entrypoint", TOKEN_LOG_SURFACES, ids=lambda path: path.name)
def test_entrypoint_never_prints_access_token_material(entrypoint: Path) -> None:
    """Neither the active worker nor the legacy runner may log token bytes."""

    source = entrypoint.read_text(encoding="utf-8")
    tree = ast.parse(source)
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    for scope in scopes:
        nodes = _nodes_in_scope(scope)
        for node in nodes:
            if not isinstance(node, ast.Call) or not _is_log_sink(node.func):
                continue

            tainted_names = _tainted_names_at_sink(scope, nodes, node)
            arguments = (*node.args, *(keyword.value for keyword in node.keywords))
            rendered = ast.get_source_segment(source, node) or ""
            assert not any(
                _contains_token_material(argument, tainted_names)
                for argument in arguments
            ), f"{entrypoint.name} prints access-token material: {rendered}"


@pytest.mark.parametrize(
    "source",
    (
        "access_token = get_token()\ntoken = access_token\nprint(token)\n",
        (
            "result = get_token_result()\n"
            'access_token = result["access_token"]\n'
            "print(result)\n"
        ),
        "import builtins\naccess_token = get_token()\nbuiltins.print(access_token)\n",
        "access_token = get_token()\nlogger.info(access_token)\n",
        "access_token = get_token()\nprint(access_token.strip())\n",
        (
            "import logging\n"
            "access_token = get_token()\n"
            "logging.getLogger(__name__).info(access_token)\n"
        ),
    ),
    ids=(
        "alias",
        "mapping",
        "builtins-print",
        "logger",
        "call-receiver",
        "chained-logger",
    ),
)
def test_guard_rejects_indirect_access_token_logging(
    tmp_path: Path, source: str
) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_rejects_mapping_logging_through_alias(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "result = get_token_result()\n"
        "alias = result\n"
        "access_token = alias['access_token']\n"
        "print(result)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_allows_unrelated_mapping_alias(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "result = get_token_result()\n"
        "alias = result\n"
        "public_result = get_public_result()\n"
        "public_alias = public_result\n"
        "access_token = alias['access_token']\n"
        "print(public_result)\n",
        encoding="utf-8",
    )

    test_entrypoint_never_prints_access_token_material(entrypoint)


@pytest.mark.parametrize("method_call", ("encode()", "upper()", "casefold()"))
def test_guard_rejects_access_token_receiver_call_logging(
    tmp_path: Path, method_call: str
) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        f"access_token = get_token()\nprint(access_token.{method_call})\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


@pytest.mark.parametrize(
    "source",
    (
        (
            "access_token = get_token()\n"
            "result = get_token_result()\n"
            "print(result.get('expires_in', access_token))\n"
        ),
        (
            "access_token = get_token()\n"
            "token = access_token\n"
            "result = get_token_result()\n"
            "print(result.get('expires_in', token))\n"
        ),
    ),
    ids=("direct-default", "alias-default"),
)
def test_guard_rejects_access_token_expiry_default(
    tmp_path: Path, source: str
) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_rejects_augassign_access_token_retaint(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "access_token = get_token()\n"
        "token = access_token\n"
        'token = "[redacted]"\n'
        "token += access_token\n"
        "print(token)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


@pytest.mark.parametrize(
    "augmented_assignment", ('token += "suffix"', "token *= 2")
)
def test_guard_preserves_token_taint_through_augassign(
    tmp_path: Path, augmented_assignment: str
) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "access_token = get_token()\n"
        "token = access_token\n"
        f"{augmented_assignment}\n"
        "print(token)\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_allows_harmless_access_token_identifier_text(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text('print("access_token")\n', encoding="utf-8")

    test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_allows_redacted_alias_overwrite(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "access_token = get_token()\n"
        "token = access_token\n"
        'token = "[redacted]"\n'
        "print(token)\n",
        encoding="utf-8",
    )

    test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_allows_augassign_after_redacted_alias_overwrite(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(
        "access_token = get_token()\n"
        "token = access_token\n"
        'token = "[redacted]"\n'
        'token += " suffix"\n'
        "print(token)\n",
        encoding="utf-8",
    )

    test_entrypoint_never_prints_access_token_material(entrypoint)


@pytest.mark.parametrize("entrypoint", TOKEN_ENTRYPOINTS, ids=lambda path: path.name)
def test_token_status_log_has_no_preview_label(entrypoint: Path) -> None:
    """The safe status log may report source/expiry, never a token preview."""

    source = entrypoint.read_text(encoding="utf-8")
    token_status_lines = [
        line for line in source.splitlines() if "Using {token_source}" in line
    ]

    assert token_status_lines, f"expected a token status log in {entrypoint.name}"
    assert all("preview" not in line.lower() for line in token_status_lines)
