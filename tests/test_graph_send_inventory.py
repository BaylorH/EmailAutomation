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


def _repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in IGNORED_PATH_PARTS for part in rel.parts):
            continue
        if rel.parts[0] == "tests":
            continue
        yield rel


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


if __name__ == "__main__":
    unittest.main()
