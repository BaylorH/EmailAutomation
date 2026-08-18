import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "release-safety" / "outbound-send-surface-inventory.json"

GRAPH_SEND_PATTERN = re.compile(
    r"/me/(?:sendMail|messages/\{[^}]+\}/(?:reply|send|createReply|createReplyAll))"
    r"|graph\.microsoft\.com/v1\.0/me/(?:sendMail|messages/.*/(?:reply|send))"
)

IGNORED_PATH_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "venv",
}

RAW_EMAIL_PROVIDER_SEND_PATTERN = re.compile(
    r"get_provider\(['\"]email['\"]\)"
    r"|RealEmailProvider\("
    r"|\.(?:send_draft|send_new_message|reply_to_message)\("
)
LEGACY_EMAIL_OPERATIONS_FLAG = "SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS"


def _repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in IGNORED_PATH_PARTS for part in rel.parts):
            continue
        if rel.parts[0] == "tests":
            continue
        yield rel


def _workflow_files():
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    if not workflows_dir.exists():
        return []
    return sorted(
        path.relative_to(REPO_ROOT)
        for path in workflows_dir.glob("*")
        if path.suffix in {".yml", ".yaml"}
    )


class GraphSendInventoryTests(unittest.TestCase):
    def test_inventory_exists_and_covers_every_graph_send_surface(self):
        self.assertTrue(
            INVENTORY_PATH.exists(),
            "docs/release-safety/outbound-send-surface-inventory.json must document every Graph send surface",
        )

        inventory = json.loads(INVENTORY_PATH.read_text())
        registered_paths = {
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("path")
        }

        discovered_paths = set()
        for rel in _repo_python_files():
            text = (REPO_ROOT / rel).read_text(errors="ignore")
            if GRAPH_SEND_PATTERN.search(text):
                discovered_paths.add(str(rel))

        self.assertEqual(
            discovered_paths,
            registered_paths,
            "Graph send/reply endpoints changed; update the outbound send inventory and safety notes.",
        )

    def test_inventory_marks_each_surface_policy_status(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        # "shared_boundary" is the convergence point Task 6 created. It is
        # deliberately NOT "guarded": it carries no policy of its own, because
        # rendering and safety stay with the caller. The category exists so that
        # distinction is recorded rather than mistaken for an unguarded surface -
        # see test_the_shared_boundary_carries_no_policy_of_its_own below.
        allowed_statuses = {
            "guarded", "provider", "legacy_disabled", "legacy_script", "shared_boundary",
        }

        for entry in inventory.get("sendSurfaces", []):
            with self.subTest(path=entry.get("path")):
                self.assertIn(entry.get("policyStatus"), allowed_statuses)
                self.assertTrue(entry.get("trigger"))
                self.assertTrue(entry.get("risk"))
                self.assertTrue(entry.get("nextGate"))

    def test_legacy_email_operations_inventory_matches_runtime_quarantine(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        entries = {
            entry["path"]: entry
            for entry in inventory.get("sendSurfaces", [])
        }

        self.assertEqual(
            "legacy_disabled",
            entries["email_automation/email_operations.py"]["policyStatus"],
        )

    def test_the_shared_boundary_carries_no_policy_of_its_own(self):
        """Its emptiness is the design, so it has to be asserted, not assumed.

        If someone later moves ``validate_outbound_body`` into the transport, the
        four converging lanes stop being able to apply their own distinct policy
        and the boundary silently becomes a policy engine. That is a decision
        worth failing a test over rather than discovering in production.
        """
        inventory = json.loads(INVENTORY_PATH.read_text())
        boundaries = [
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("policyStatus") == "shared_boundary"
        ]
        self.assertEqual(boundaries, ["email_automation/message_transport.py"])
        for path in boundaries:
            with self.subTest(path=path):
                text = (REPO_ROOT / path).read_text(errors="ignore")
                self.assertNotIn("validate_outbound_body", text)

    def test_active_send_surfaces_reference_shared_body_policy(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        guarded_paths = [
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("policyStatus") == "guarded"
        ]

        for path in guarded_paths:
            with self.subTest(path=path):
                text = (REPO_ROOT / path).read_text(errors="ignore")
                self.assertIn(
                    "validate_outbound_body",
                    text,
                    f"{path} is an active Graph send surface and must use the shared body policy",
                )

    def test_production_workflows_do_not_enable_legacy_email_operations(self):
        for rel in _workflow_files():
            with self.subTest(path=str(rel)):
                text = (REPO_ROOT / rel).read_text(errors="ignore")
                self.assertNotIn(
                    LEGACY_EMAIL_OPERATIONS_FLAG,
                    text,
                    "Production workflows must not opt into legacy direct-Graph send helpers",
                )

    def test_production_code_does_not_call_raw_email_provider_senders_directly(self):
        offenders = []
        for rel in _repo_python_files():
            if str(rel) == "email_automation/service_providers.py":
                continue
            text = (REPO_ROOT / rel).read_text(errors="ignore")
            if RAW_EMAIL_PROVIDER_SEND_PATTERN.search(text):
                offenders.append(str(rel))

        self.assertEqual(
            [],
            offenders,
            "Production code should route sends through policy-aware modules, not raw RealEmailProvider helpers.",
        )


# ---------------------------------------------------------------------------
# Task 7E - no recovery or alternate-send bypass
# ---------------------------------------------------------------------------
#
# sendSurfaces answers "what can send". These tests answer the wider question
# the certification program actually needs: what can touch the mailbox AT ALL.
# A send-only inventory cannot see a destructive mailbox call or an unrouted
# mailbox read, and both are effects a zero-effect run must not cause. Task 7E
# found exactly that gap, so the enumeration now covers every verb.

import ast
import collections

SEND_SUFFIXES = ("/send", "/sendMail", "/reply", "/replyAll")
SKIP_PARTS = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv"}
SHARED_BOUNDARY = "email_automation/message_transport.py"


def _literal_url(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    return ""


def _mailbox_calls(relative_path):
    """Every Graph /me/ call in one module, classified by verb.

    URL-based, so a module that assembles its URL by concatenation is
    UNDERCOUNTED here. That limitation is real and is why the inventory also
    carries sendCapableByName - see
    test_a_name_detected_sender_is_recorded_even_though_the_url_scan_misses_it.
    """
    source = (REPO_ROOT / relative_path).read_text(errors="ignore")
    if "/me/" not in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                text = _literal_url(node.value)
                if text:
                    assigned[node.targets[0].id] = text
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("get", "post", "patch", "put", "delete") or not node.args:
            continue
        url = _literal_url(node.args[0])
        if not url and isinstance(node.args[0], ast.Name):
            url = assigned.get(node.args[0].id, "")
        if "/me/" not in url:
            continue
        if url.rstrip().endswith(SEND_SUFFIXES):
            kind = "send"
        elif func.attr == "delete":
            kind = "destructive"
        elif func.attr in ("post", "patch", "put"):
            kind = "write"
        else:
            kind = "read"
        found.append((owner.get(node, "<module>"), kind, url, node.lineno))
    return found


def _production_files():
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT)
        if SKIP_PARTS & set(relative.parts):
            continue
        yield relative.as_posix()


class AlternateSendBypassTests(unittest.TestCase):
    """Task 7E's proof, structural rather than by mock."""

    def setUp(self):
        self.inventory = json.loads(INVENTORY_PATH.read_text())
        self.documented = {
            entry["path"]: entry
            for entry in self.inventory.get("mailboxCapableSurfaces", [])
        }

    def _discovered(self):
        discovered = {}
        for relative in _production_files():
            calls = _mailbox_calls(relative)
            if calls:
                discovered[relative] = calls
        return discovered

    # -- the enumeration is complete --------------------------------------

    def test_every_mailbox_capable_module_is_documented(self):
        """A NEW module touching the mailbox fails here rather than arriving unseen."""
        discovered = set(self._discovered())
        self.assertEqual(
            discovered - set(self.documented),
            set(),
            "a module reaches the mailbox but is absent from mailboxCapableSurfaces",
        )

    def test_no_documented_surface_has_quietly_disappeared(self):
        """``no_graph_access`` entries are deliberate NEGATIVE records.

        dead_letter_recovery is listed precisely because it holds no Graph call:
        the claim "recovery cannot deliver by itself" is worth stating and
        keeping under test, and it would be lost if the record were pruned for
        having nothing to find.
        """
        discovered = set(self._discovered())
        asserted_absent = {
            path for path, entry in self.documented.items()
            if entry.get("disposition") == "no_graph_access"
        }
        self.assertEqual(
            set(self.documented) - discovered - asserted_absent,
            set(),
            "mailboxCapableSurfaces names a module that no longer reaches the mailbox; "
            "remove it deliberately rather than leaving the record stale",
        )

    def test_a_no_graph_access_record_really_has_no_graph_access(self):
        """The negative record has to stay true, or it is worse than absent."""
        for path, entry in self.documented.items():
            if entry.get("disposition") != "no_graph_access":
                continue
            with self.subTest(path=path):
                self.assertEqual(_mailbox_calls(path), [])

    def test_every_documented_surface_carries_a_disposition_and_a_reason(self):
        for path, entry in self.documented.items():
            with self.subTest(path=path):
                self.assertTrue(entry.get("disposition"))
                self.assertTrue(entry.get("note"))

    # -- no send escapes the boundary or the enumerated bypasses -----------

    def test_only_the_boundary_and_enumerated_bypasses_hold_a_send_call(self):
        allowed_dispositions = {
            "shared_boundary",
            "provider_bypass",
            "legacy_disabled_bypass",
            "legacy_script_bypass",
        }
        offenders = {}
        for path, calls in self._discovered().items():
            if not any(kind == "send" for _owner, kind, _url, _line in calls):
                continue
            disposition = self.documented.get(path, {}).get("disposition")
            if disposition not in allowed_dispositions:
                offenders[path] = disposition
        self.assertEqual(
            offenders, {},
            "a module holds a send call without being the shared boundary or an "
            "enumerated, unreachable bypass",
        )

    def test_the_shared_boundary_still_owns_exactly_one_send(self):
        sends = [c for c in _mailbox_calls(SHARED_BOUNDARY) if c[1] == "send"]
        self.assertEqual(len(sends), 1, f"expected one send at the boundary, found {sends}")

    def test_no_converged_lane_regained_a_send_call(self):
        for entry in self.inventory.get("convergedLanes", []):
            with self.subTest(path=entry["path"]):
                sends = [c for c in _mailbox_calls(entry["path"]) if c[1] == "send"]
                self.assertEqual(sends, [], "a converged lane grew a send call back")

    # -- the two named recovery paths -------------------------------------

    def test_dead_letter_recovery_holds_no_graph_access_at_all(self):
        """Recovery re-enters through the outbox; it never delivers by itself."""
        self.assertEqual(_mailbox_calls("email_automation/dead_letter_recovery.py"), [])

    def test_dead_letter_recovery_reaches_delivery_only_by_queueing_an_outbox_item(self):
        source = (REPO_ROOT / "email_automation/dead_letter_recovery.py").read_text()
        self.assertIn("requeuedOutboxId", source)
        for forbidden in ("/me/messages", "sendMail", "send_new_message"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)

    def test_operator_replay_can_read_the_mailbox_but_never_send(self):
        calls = _mailbox_calls("email_automation/operator_replay.py")
        self.assertTrue(calls, "operator replay is expected to read one exact message")
        self.assertEqual([c for c in calls if c[1] in ("send", "destructive")], [])

    # -- the gap Task 7E found --------------------------------------------

    def test_a_destructive_mailbox_call_is_documented_even_though_it_never_sends(self):
        """The send-only inventory could not see this, which is the point.

        Deleting a user's mail is a destructive mailbox effect. A certification
        run must not cause one, and an inventory that only asks 'can it send?'
        would have reported this surface as clean.
        """
        destructive = {
            path: [c for c in calls if c[1] == "destructive"]
            for path, calls in self._discovered().items()
        }
        destructive = {p: c for p, c in destructive.items() if c}
        self.assertIn("app.py", destructive)
        for path in destructive:
            with self.subTest(path=path):
                self.assertIn(path, self.documented)

    def test_a_name_detected_sender_is_recorded_even_though_the_url_scan_misses_it(self):
        """The URL scan undercounts concatenated URLs; say so rather than imply completeness."""
        by_name = self.inventory.get("sendCapableByName", [])
        self.assertIn("email_automation/service_providers.py", by_name)
        for path in by_name:
            with self.subTest(path=path):
                self.assertIn(path, self.documented)

    # -- the bypasses are unreachable, not merely enumerated ---------------

    def test_the_legacy_send_module_is_gated_at_every_send_entry_point(self):
        source = (REPO_ROOT / "email_automation/email_operations.py").read_text()
        tree = ast.parse(source)
        gate = "_require_legacy_email_operations_enabled"
        ungated = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("send_"):
                continue
            body = ast.get_source_segment(source, node) or ""
            if gate not in body[:800]:
                ungated.append(node.name)
        self.assertEqual(ungated, [], "a legacy send helper is not gated at entry")

    def test_no_production_module_imports_a_legacy_or_script_bypass(self):
        forbidden = {
            path.rsplit("/", 1)[-1][:-3]
            for path, entry in self.documented.items()
            if entry["disposition"] in ("legacy_disabled_bypass", "legacy_script_bypass")
        }
        offenders = collections.defaultdict(list)
        for relative in _production_files():
            if relative.rsplit("/", 1)[-1][:-3] in forbidden:
                continue
            source = (REPO_ROOT / relative).read_text(errors="ignore")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.rsplit(".", 1)[-1] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.rsplit(".", 1)[-1]]
                for name in names:
                    if name in forbidden:
                        offenders[relative].append(name)
        self.assertEqual(
            dict(offenders), {},
            "production code imports a legacy or script send bypass",
        )


if __name__ == "__main__":
    unittest.main()
