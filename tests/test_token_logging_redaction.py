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


def _qualified_name(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_qualified_name(node.value), node.attr)
    return ()


def _is_log_sink(node: ast.expr) -> bool:
    qualified_name = _qualified_name(node)
    if qualified_name in {("print",), ("builtins", "print")}:
        return True

    return bool(
        qualified_name
        and qualified_name[-1] in LOG_METHODS
        and any(part in {"logger", "logging", "log"} for part in qualified_name[:-1])
    )


def _subscript_key(node: ast.Subscript) -> object:
    return node.slice.value if isinstance(node.slice, ast.Constant) else None


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
        if _qualified_name(node.func) in TOKEN_METADATA_CALLS:
            return False
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "access_token"
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
    return fetches_access_token or (
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
    assignments: list[tuple[list[ast.expr], ast.expr]] = []
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
            assignments.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments.append(([node.target], node.value))
        elif isinstance(node, ast.NamedExpr):
            assignments.append(([node.target], node.value))

    changed = True
    while changed:
        changed = False
        for targets, value in assignments:
            if not _assignment_value_is_tainted(value, tainted):
                continue
            names = set().union(*(_assigned_names(target) for target in targets))
            if not names <= tainted:
                tainted.update(names)
                changed = True
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
        tainted_names = _tainted_names(nodes)
        for node in nodes:
            if not isinstance(node, ast.Call) or not _is_log_sink(node.func):
                continue

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
    ),
    ids=("alias", "mapping", "builtins-print", "logger"),
)
def test_guard_rejects_indirect_access_token_logging(
    tmp_path: Path, source: str
) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text(source, encoding="utf-8")

    with pytest.raises(AssertionError, match="prints access-token material"):
        test_entrypoint_never_prints_access_token_material(entrypoint)


def test_guard_allows_harmless_access_token_identifier_text(tmp_path: Path) -> None:
    entrypoint = tmp_path / "sample.py"
    entrypoint.write_text('print("access_token")\n', encoding="utf-8")

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
