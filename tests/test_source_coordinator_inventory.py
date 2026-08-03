import ast
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
DRAFT_DELETE_MANIFEST_PATH = REPO_ROOT / "tests/fixtures/graph_draft_delete_callers.json"
DRAFT_DELETE_HELPER_NAME = "_delete_graph_reply_draft"
DRAFT_DELETE_EMAIL_MODULE = "email_automation.email"
DRAFT_DELETE_EMAIL_PATH = "email_automation/email.py"

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
        ("scheduler_runner.py", "has_processed"): 1,
        ("scheduler_runner.py", "mark_processed"): 1,
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
        # Definition-time expressions belong to the enclosing syntactic scope.
        _visit_function_metadata(self, node)
        self._visit_body("function", node.name, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef
    def visit_ClassDef(self, node):
        _visit_class_metadata(self, node)
        self._visit_body("class", node.name, node.body)

    def visit_Lambda(self, node):
        self.visit(node.args)

    def visit_arg(self, node):
        if node.annotation:
            self.visit(node.annotation)
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
        if node.id == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper reference is not a direct call")

    def visit_Attribute(self, node):
        if node.attr == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper attribute is not a direct call")
        self.generic_visit(node)

    def visit_arg(self, node):
        if node.arg == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper name is rebound as an argument")
        if node.annotation:
            self.visit(node.annotation)

    def visit_Global(self, node):
        if DRAFT_DELETE_HELPER_NAME in node.names:
            _fail_inventory("protected helper global/nonlocal binding is unsupported")

    visit_Nonlocal = visit_Global
    def _visit_named_binding(self, node):
        if node.name == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper binding is unsupported")
        self.generic_visit(node)

    visit_ExceptHandler = _visit_named_binding
    visit_MatchAs = _visit_named_binding
    visit_MatchStar = _visit_named_binding
    def visit_MatchMapping(self, node):
        if node.rest == DRAFT_DELETE_HELPER_NAME:
            _fail_inventory("protected helper pattern binding is unsupported")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if _resolve_import_from_module(node, self.relative_path) != DRAFT_DELETE_EMAIL_MODULE:
            return
        for alias in node.names:
            if alias.name == "*":
                _fail_inventory("star import from the protected helper module")
            if (
                alias.name == DRAFT_DELETE_HELPER_NAME
                and alias.asname not in (None, DRAFT_DELETE_HELPER_NAME)
            ):
                _fail_inventory("renaming the protected helper is unsupported")


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


def _legacy_symbols_in_modules(modules):
    target_names = {name for _, name in EXPECTED_LEGACY_MARKER_DEFINITIONS}
    discovered = Counter()
    for relative_path, tree in modules:
        for node in tree.body:  # Topology is part of the legacy authority contract.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_names:
                discovered[(Path(relative_path).as_posix(), node.name)] += 1
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

    def test_legacy_inventory_only_accepts_top_level_definitions(self):
        sources = {
            "email_automation/messaging.py": "def has_processed(): pass\nclass Hidden:\n def mark_processed(self): pass",
            "scheduler_runner.py": "def has_processed(): pass\ndef mark_processed(): pass",
            "email_automation/operator_replay.py": "def _begin_replay_claim(): pass\ndef _complete_replay_claim(): pass",
        }
        modules = [(path, ast.parse(source)) for path, source in sources.items()]
        discovered = _legacy_symbols_in_modules(modules)
        self.assertNotEqual(EXPECTED_LEGACY_MARKER_DEFINITIONS, discovered)
        self.assertNotIn(("email_automation/messaging.py", "mark_processed"), discovered)


if __name__ == "__main__":
    unittest.main()
