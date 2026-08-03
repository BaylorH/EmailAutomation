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
DRAFT_DELETE_EMAIL_MODULE = "email_automation.email"
DRAFT_DELETE_EMAIL_PATH = Path("email_automation/email.py").as_posix()

EXPECTED_DRAFT_DELETE_CALLERS = Counter(
    {
        ("email_automation/email.py", "_send_outbox_as_reply"): 2,
        ("email_automation/email.py", "send_and_index_email"): 1,
        ("email_automation/followup.py", "_send_followup_email"): 13,
        ("email_automation/processing.py", "send_reply_in_thread"): 5,
    }
)

EXPECTED_LEGACY_MARKER_SYMBOLS = {
    "email_automation/messaging.py": {"has_processed", "mark_processed"},
    "scheduler_runner.py": {"has_processed", "mark_processed"},
    "email_automation/operator_replay.py": {
        "_begin_replay_claim",
        "_complete_replay_claim",
    },
}

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


def _dotted_expression(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_expression(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


class _DraftDeleteCallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path):
        self.relative_path = Path(relative_path).as_posix()
        self.scope_stack = []
        self.binding_stack = [
            {"helper_names": set(), "email_module_names": set()}
        ]
        self.callers = Counter()
        self.requests_delete_implementations = 0

    def _visit_scope(self, node, scope_kind):
        self.scope_stack.append((scope_kind, node.name))
        self.binding_stack.append(
            {"helper_names": set(), "email_module_names": set()}
        )
        self.generic_visit(node)
        self.binding_stack.pop()
        self.scope_stack.pop()

    def _bind_helper_name(self, name):
        self.binding_stack[-1]["helper_names"].add(name)

    def _bind_email_module_name(self, name):
        self.binding_stack[-1]["email_module_names"].add(name)

    def _is_bound(self, binding_kind, name):
        return any(
            name in bindings[binding_kind]
            for bindings in reversed(self.binding_stack)
        )

    def _is_bound_delete_helper_call(self, node):
        if isinstance(node.func, ast.Name):
            return self._is_bound("helper_names", node.func.id)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == DRAFT_DELETE_HELPER_NAME
        ):
            receiver = _dotted_expression(node.func.value)
            return bool(
                receiver
                and self._is_bound("email_module_names", receiver)
            )
        return False

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
        if (
            self.relative_path == DRAFT_DELETE_EMAIL_PATH
            and not self.scope_stack
            and node.name == DRAFT_DELETE_HELPER_NAME
        ):
            self._bind_helper_name(node.name)
        self._visit_scope(node, "function")

    def visit_AsyncFunctionDef(self, node):
        if (
            self.relative_path == DRAFT_DELETE_EMAIL_PATH
            and not self.scope_stack
            and node.name == DRAFT_DELETE_HELPER_NAME
        ):
            self._bind_helper_name(node.name)
        self._visit_scope(node, "function")

    def visit_ClassDef(self, node):
        self._visit_scope(node, "class")

    def visit_ImportFrom(self, node):
        imported_module = _resolve_import_from_module(self.relative_path, node)
        if imported_module == DRAFT_DELETE_EMAIL_MODULE:
            for alias in node.names:
                if alias.name == DRAFT_DELETE_HELPER_NAME:
                    self._bind_helper_name(alias.asname or alias.name)
        elif imported_module == "email_automation":
            for alias in node.names:
                if alias.name == "email":
                    self._bind_email_module_name(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name != DRAFT_DELETE_EMAIL_MODULE:
                continue
            self._bind_email_module_name(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node):
        if self._is_bound_delete_helper_call(node):
            self.callers[(self.relative_path, self._caller_scope())] += 1

        if (
            self._inside_email_delete_helper()
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "requests"
            and node.func.attr == "delete"
        ):
            self.requests_delete_implementations += 1

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
    target_names = set().union(*EXPECTED_LEGACY_MARKER_SYMBOLS.values())
    discovered = {}
    for relative_path in _application_python_files():
        names = {
            node.name
            for node in ast.walk(_parse_module(relative_path))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in target_names
        }
        if names:
            discovered[relative_path.as_posix()] = names
    return discovered


def _in_memory_draft_delete_callers(source, relative_path):
    visitor = _DraftDeleteCallVisitor(Path(relative_path))
    visitor.visit(ast.parse(source, filename=relative_path))
    return visitor.callers


class InventoryScannerRegressionTests(unittest.TestCase):
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
            EXPECTED_LEGACY_MARKER_SYMBOLS,
            _discover_legacy_authority_symbols(),
        )


if __name__ == "__main__":
    unittest.main()
