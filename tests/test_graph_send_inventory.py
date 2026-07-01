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
        allowed_statuses = {"guarded", "provider", "legacy_disabled", "legacy_script"}

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


if __name__ == "__main__":
    unittest.main()
