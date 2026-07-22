import ast
import unittest
from pathlib import Path


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "email_automation"
    / "claim_pipeline"
)
BANNED_IMPORT_PREFIXES = (
    "firebase_admin",
    "google.cloud.firestore",
    "openai",
    "requests",
    "email_automation.processing",
    "email_automation.messaging",
    "email_automation.sheets",
    "email_automation.followup",
)


class ClaimPipelineIsolationTests(unittest.TestCase):
    def test_foundation_has_no_service_or_side_effect_imports(self):
        imported_names = set()
        for path in PACKAGE_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_names.add(node.module)

        violations = sorted(
            imported
            for imported in imported_names
            if any(
                imported == prefix or imported.startswith(f"{prefix}.")
                for prefix in BANNED_IMPORT_PREFIXES
            )
        )
        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()
