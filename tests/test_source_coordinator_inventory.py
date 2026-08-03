import ast
import json
import unittest
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DRAFT_DELETE_MANIFEST_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "graph_draft_delete_callers.json"
)

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


def _direct_call_name(node):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _DraftDeleteCallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path):
        self.relative_path = str(relative_path)
        self.function_stack = []
        self.callers = Counter()
        self.requests_delete_implementations = 0

    def _visit_function(self, node):
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def visit_Call(self, node):
        if (
            self.function_stack
            and _direct_call_name(node) == "_delete_graph_reply_draft"
        ):
            self.callers[(self.relative_path, self.function_stack[-1])] += 1

        if (
            self.function_stack
            and self.function_stack[-1] == "_delete_graph_reply_draft"
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
        application_paths.add(str(relative_path))
        visitor = _DraftDeleteCallVisitor(relative_path)
        visitor.visit(_parse_module(relative_path))
        callers.update(visitor.callers)
        requests_delete_implementations += visitor.requests_delete_implementations

    return callers, requests_delete_implementations, application_paths


def _manifest_draft_delete_callers(manifest):
    callers = Counter()
    for ownership_group in ("deferred", "m2Owned"):
        for entry in manifest[ownership_group]:
            callers[(entry["path"], entry["function"])] += entry["count"]
    return callers


def _discover_draft_delete_callers(manifest_callers):
    callers, _, application_paths = _scan_draft_delete_calls()
    manifest_paths = {path for path, _ in manifest_callers}
    if not manifest_paths <= application_paths:
        raise AssertionError("graph draft delete manifest names a non-application path")
    return callers


def _top_level_function_names(relative_path):
    return {
        node.name
        for node in _parse_module(Path(relative_path)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


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
        actual = {
            path: expected_names & _top_level_function_names(path)
            for path, expected_names in EXPECTED_LEGACY_MARKER_SYMBOLS.items()
        }
        self.assertEqual(EXPECTED_LEGACY_MARKER_SYMBOLS, actual)


if __name__ == "__main__":
    unittest.main()
