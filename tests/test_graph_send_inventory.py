import ast
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
FINAL_SEND_LITERAL_PATTERN = re.compile(
    r"/me/sendMail"
    r"|/me/messages/(?:\\{[^}]+\\}|[^\\s\"']+)/send"
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
        allowed_statuses = {
            "gateway",
            "guarded",
            "provider",
            "legacy_disabled",
            "legacy_script",
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

    def test_active_send_lanes_have_no_direct_irreversible_graph_send(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        entries = {
            entry["path"]: entry
            for entry in inventory.get("sendSurfaces", [])
        }
        active_paths = {
            path
            for path, entry in entries.items()
            if entry.get("policyStatus") == "guarded"
        }
        offenders = []

        for relative_path in sorted(active_paths):
            path = REPO_ROOT / relative_path
            source = path.read_text(encoding="utf-8")
            self.assertIn(
                "execute_graph_draft_final_send",
                source,
                f"{relative_path} must route final sends through the adapter",
            )
            for match in FINAL_SEND_LITERAL_PATTERN.finditer(source):
                offenders.append(
                    f"{relative_path}:{source.count(chr(10), 0, match.start()) + 1}"
                )

        self.assertEqual(
            [],
            offenders,
            "Active Graph final sends must route through the single "
            "GraphFinalSendAdapter: " + ", ".join(offenders),
        )

    def test_no_unapproved_final_send_literal_can_hide_behind_an_alternate_http_call(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        explicitly_excluded = {
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("policyStatus")
            in {"legacy_disabled", "legacy_script", "provider"}
        }
        allowed = {
            "email_automation/graph_final_send.py",
            *explicitly_excluded,
        }
        offenders = []

        for rel in _repo_python_files():
            relative_path = str(rel)
            if relative_path in allowed:
                continue
            source = (REPO_ROOT / rel).read_text(
                encoding="utf-8",
                errors="ignore",
            )
            for match in FINAL_SEND_LITERAL_PATTERN.finditer(source):
                offenders.append(
                    f"{relative_path}:"
                    f"{source.count(chr(10), 0, match.start()) + 1}"
                )

        self.assertEqual(
            [],
            offenders,
            "Final Graph endpoint literals are allowed only in the single "
            "adapter or explicitly disabled legacy/provider files; this "
            "catches .post, .request, aliases, and wrapper call shapes: "
            + ", ".join(offenders),
        )

    def test_pending_response_retries_delegate_to_the_guarded_processing_lane(self):
        source = (
            REPO_ROOT / "email_automation" / "pending_responses.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "processing_module.send_reply_in_thread",
            source,
        )
        self.assertNotRegex(source, FINAL_SEND_LITERAL_PATTERN)

    def test_every_active_adapter_call_supplies_exact_authority_and_stable_identity_inputs(self):
        expected_call_counts = {
            "email_automation/email.py": 2,
            "email_automation/processing.py": 1,
            "email_automation/followup.py": 1,
        }
        required_keywords = {
            "user_id",
            "client_id",
            "run_id",
            "effect_type",
            "effect_key",
            "content",
            "draft_id",
            "headers",
            "http_post",
        }

        for relative_path, expected_count in expected_call_counts.items():
            source = (REPO_ROOT / relative_path).read_text(
                encoding="utf-8"
            )
            tree = ast.parse(source)
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "execute_graph_draft_final_send"
            ]
            with self.subTest(path=relative_path):
                self.assertEqual(len(calls), expected_count)
                for call in calls:
                    keywords = {
                        keyword.arg: keyword.value
                        for keyword in call.keywords
                    }
                    self.assertTrue(
                        required_keywords
                        <= set(keywords),
                    )
                    self.assertEqual(
                        ast.unparse(keywords["http_post"]),
                        "requests.post",
                    )
                    self.assertEqual(
                        ast.unparse(keywords["run_id"]),
                        "resolve_effect_run_id()",
                    )

    def test_every_outbox_production_caller_supplies_a_business_effect_key(self):
        source = (
            REPO_ROOT / "email_automation" / "email.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        call_names = {
            "send_and_index_email",
            "_send_outbox_as_reply",
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in call_names
        ]

        self.assertEqual(len(calls), 5)
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertIn(
                    "effect_key",
                    {keyword.arg for keyword in call.keywords},
                )

    def test_processing_and_followup_effect_keys_bind_business_identity(self):
        processing_source = (
            REPO_ROOT / "email_automation" / "processing.py"
        ).read_text(encoding="utf-8")
        followup_source = (
            REPO_ROOT / "email_automation" / "followup.py"
        ).read_text(encoding="utf-8")
        email_source = (
            REPO_ROOT / "email_automation" / "email.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'effect_key=f"inbox-reply:{thread_id}:{current_msg_id}"',
            processing_source,
        )
        self.assertIn(
            'effect_key=f"followup:{thread_id}:{followup_index}"',
            followup_source,
        )
        self.assertIn(
            'combined_effect_key = "combined:" + ",".join(',
            email_source,
        )


if __name__ == "__main__":
    unittest.main()
