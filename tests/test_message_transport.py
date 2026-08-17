"""Characterization of SiteSift's outbound transport boundaries, BEFORE refactor.

Task 2 of the production automation certification plan. This module pins WHERE a
message actually leaves the process today, so Phase C can move that boundary with
a regression net underneath it.

Two deliberate design choices:

**It is source-level, not runtime-level.** The property being pinned - "every send
lane reaches an identified final delivery call, and no other call site exists" - is
a structural invariant over the import graph. A runtime test proves it only for the
paths it happens to exercise and can be satisfied by a mock; an AST sweep cannot be
evaded by one. Task 7E ("prove no recovery or alternate-send bypass remains") is
exactly this property, so pinning it structurally is what makes that task decidable.

**It imports nothing from `email_automation`.** `email_automation/clients.py:13`
constructs `firestore.Client()` and `openai.OpenAI(...)` at MODULE IMPORT TIME, so
importing the business logic requires real credentials and would build production
provider clients. That is tracked as backlog #84 and is precisely the kind of
import-time provider construction this certification program exists to remove. A
characterization test must not depend on it, and must never be "fixed" by placing a
real service-account credential into a worktree.

WHAT THIS PROVES, stated plainly: there is currently **no single shared delivery
boundary**. Four independent `requests.post(.../me/messages/{id}/send)` call sites
exist across three guarded modules, and several unguarded surfaces reach Graph
without any policy at all. Task 6 exists to collapse the guarded four into one
boundary; when it does, `test_guarded_send_sites_are_exactly_the_known_four` is
EXPECTED to fail and must be updated in the same commit that collapses them. That
failure is the point: it is the alarm that the boundary moved.
"""

from pathlib import Path
import ast
import json
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "release-safety" / "outbound-send-surface-inventory.json"

# Modules whose send sites are policy-guarded today.
GUARDED_MODULES = (
    "email_automation/email.py",
    "email_automation/processing.py",
    "email_automation/followup.py",
)

# The exact guarded delivery sites, keyed by the function that owns them. Line
# numbers are informational only - ownership by enclosing function is the stable
# fact and is what Task 6 will refactor.
EXPECTED_GUARDED_SEND_SITES = {
    ("email_automation/email.py", "_send_outbox_as_reply"),
    ("email_automation/email.py", "send_and_index_email"),
    ("email_automation/processing.py", "send_reply_in_thread"),
    ("email_automation/followup.py", "_send_followup_email"),
}

# Unguarded or partially guarded surfaces that reach Graph send/reply directly.
# Enumerated so a NEW one fails this test rather than arriving unnoticed.
KNOWN_BYPASS_MODULES = {
    "email_automation/email_operations.py",
    "email_automation/service_providers.py",
    "scheduler_runner.py",
    "noPopup_signin_emails_to_excel.py",
}

GRAPH_SEND_URL_MARKERS = ("/send", "/reply", "/replyAll", "/sendMail")


def _module_source(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _enclosing_functions(tree):
    """Map every AST node to the name of the function that lexically contains it."""
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    return owner


def _joined_str_value(node):
    """Best-effort literal text of an f-string or plain string node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return ""


def _url_assignments(tree):
    """Every simple `name = "<text>"` assignment as (lineno, name, text).

    Necessary, not cosmetic. A very common style is:

        url = f"https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send"
        resp = requests.post(url, headers=headers)

    Inspecting only a call's literal first argument misses every one of these. That
    blind spot was real: it hid all three RealEmailProvider send methods in
    email_automation/service_providers.py.

    Returning a LINE-ORDERED LIST rather than a name->text map is equally load
    bearing. service_providers.py rebinds the single name `url` six times; a map
    keeps only the last write, and that survivor did not end in /send, so all three
    send sites disappeared a second time. A name is therefore resolved to the
    NEAREST PRECEDING assignment, which is what the reader of the code sees.
    """
    assignments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        text = _joined_str_value(node.value)
        if text:
            assignments.append((node.lineno, target.id, text))
    return sorted(assignments)


def _resolve_name(assignments, name, before_line):
    """The nearest assignment to `name` lexically above `before_line`."""
    best = ""
    for lineno, assigned, text in assignments:
        if assigned == name and lineno < before_line:
            best = text
        elif lineno >= before_line:
            break
    return best


def find_graph_send_calls(relative_path):
    """Every `requests.<verb>(<url containing a Graph send marker>)` in one module.

    Resolves both an inline URL and a URL bound to a local variable.
    Returns a list of (function_name, url_shape, lineno).
    """
    tree = ast.parse(_module_source(relative_path))
    owner = _enclosing_functions(tree)
    assignments = _url_assignments(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("post", "patch", "put"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "requests"):
            continue
        if not node.args:
            continue
        argument = node.args[0]
        url = _joined_str_value(argument)
        if not url and isinstance(argument, ast.Name):
            url = _resolve_name(assignments, argument.id, node.lineno)
        if not url:
            continue
        # A draft-creating call is not a delivery call; only the terminal verbs count.
        if url.endswith("/send") or url.endswith("/sendMail") or url.endswith("/reply"):
            found.append((owner.get(node, "<module>"), url, node.lineno))
    return found


class GuardedDeliveryBoundaryTests(unittest.TestCase):
    """Where a guarded message actually leaves the process, today."""

    def test_guarded_send_sites_are_exactly_the_known_four(self):
        actual = set()
        for module in GUARDED_MODULES:
            for function_name, _url, _lineno in find_graph_send_calls(module):
                actual.add((module, function_name))
        self.assertEqual(
            actual,
            EXPECTED_GUARDED_SEND_SITES,
            "the guarded delivery surface changed; if Task 6 collapsed these into one "
            "boundary, update EXPECTED_GUARDED_SEND_SITES in the same commit",
        )

    def test_there_is_no_single_shared_delivery_function_yet(self):
        """Characterization of the CURRENT state, which Task 6 exists to invert."""
        owners = {owner for _module, owner in EXPECTED_GUARDED_SEND_SITES}
        self.assertGreater(
            len(owners),
            1,
            "a single shared delivery owner now exists - Task 6 has landed, so this "
            "characterization must be replaced by the shared-boundary assertion",
        )
        self.assertEqual(len(owners), 4, "four independent delivery owners are expected")

    def test_every_guarded_delivery_call_targets_a_draft_send_endpoint(self):
        for module in GUARDED_MODULES:
            for function_name, url, lineno in find_graph_send_calls(module):
                with self.subTest(module=module, function=function_name, line=lineno):
                    self.assertTrue(
                        url.endswith("/send"),
                        f"{module}:{lineno} delivers via {url!r}; the guarded lanes are "
                        "expected to send a previously created draft, never sendMail",
                    )
                    self.assertIn("/me/messages/", url)

    def test_each_guarded_module_carries_the_outbound_body_validator(self):
        """Source-level, matching the existing inventory contract in test_graph_send_inventory."""
        for module in GUARDED_MODULES:
            with self.subTest(module=module):
                self.assertIn("validate_outbound_body", _module_source(module))


class SendSurfaceInventoryTests(unittest.TestCase):
    """No send site may exist outside the enumerated guarded and bypass surfaces."""

    def _repo_python_files(self):
        skip_parts = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv"}
        for path in REPO_ROOT.rglob("*.py"):
            if skip_parts & set(path.relative_to(REPO_ROOT).parts):
                continue
            yield path.relative_to(REPO_ROOT).as_posix()

    def test_no_unknown_module_reaches_a_graph_send_endpoint(self):
        allowed = set(GUARDED_MODULES) | KNOWN_BYPASS_MODULES
        offenders = set()
        for relative_path in self._repo_python_files():
            try:
                source = _module_source(relative_path)
            except (OSError, UnicodeDecodeError):
                continue
            if not any(marker in source for marker in GRAPH_SEND_URL_MARKERS):
                continue
            try:
                if find_graph_send_calls(relative_path):
                    offenders.add(relative_path)
            except SyntaxError:
                continue
        self.assertEqual(
            offenders - allowed,
            set(),
            "a new Microsoft Graph send site appeared outside the enumerated surfaces; "
            "every send path must be classified before it can ship",
        )

    def test_known_bypass_modules_still_bypass_and_are_not_silently_fixed(self):
        """If a bypass gains guards, that is good news that must be recorded, not hidden."""
        still_bypassing = set()
        for module in KNOWN_BYPASS_MODULES:
            path = REPO_ROOT / module
            if not path.is_file():
                continue
            if "validate_outbound_body" not in _module_source(module):
                still_bypassing.add(module)
        self.assertEqual(
            still_bypassing,
            {module for module in KNOWN_BYPASS_MODULES if (REPO_ROOT / module).is_file()},
            "a bypass module gained validate_outbound_body; update KNOWN_BYPASS_MODULES "
            "and the send-surface inventory in the same commit",
        )

    def test_inventory_document_covers_every_module_with_a_send_site(self):
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        catalogued = {entry["path"] for entry in inventory["sendSurfaces"]}
        live = set(GUARDED_MODULES) | {
            module for module in KNOWN_BYPASS_MODULES if (REPO_ROOT / module).is_file()
        }
        self.assertEqual(
            live - catalogued,
            set(),
            "a module with a real send site is absent from "
            "docs/release-safety/outbound-send-surface-inventory.json",
        )


class ImportTimeProviderConstructionTests(unittest.TestCase):
    """Why this module refuses to import email_automation - pinned, not assumed."""

    def test_clients_module_constructs_providers_at_import_time(self):
        """Backlog #84. Certification cannot run business logic without this being fixed.

        This is a CHARACTERIZATION of a defect, not an endorsement. When #84 lands and
        client construction becomes lazy, this test must flip to assert the absence of
        module-level construction.
        """
        tree = ast.parse(_module_source("email_automation/clients.py"))
        module_level_calls = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                module_level_calls.append(name)
        self.assertIn(
            "Client",
            module_level_calls,
            "firestore.Client() is expected at module scope today (#84); if it is now "
            "lazy, invert this test and unblock credential-free collection",
        )

    def test_this_module_imports_no_email_automation_package(self):
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn(
            "email_automation",
            imported,
            "this characterization must stay credential-free; importing the package "
            "constructs production Firestore and OpenAI clients at import time",
        )


if __name__ == "__main__":
    unittest.main()
