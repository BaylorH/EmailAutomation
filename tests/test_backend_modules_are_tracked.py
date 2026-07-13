"""Coverage integrity: every backend module is tracked by the rubric.

Leadership requirement: every feature, no matter how small, is tracked on the
release map. A backend module that no registry feature owns is an untracked
feature — the failure mode that let real bugs ship. This test forces each
`email_automation/*.py` module to be EITHER owned by a registry feature OR
explicitly enumerated as a reviewed support/legacy module, so exclusions are
visible instead of silent.
"""

import glob
import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-registry.json"

# Modules that are intentionally NOT a product feature. Each must have a reason;
# adding to this list is a reviewed decision, not a silent gap.
SUPPORT_MODULES = {
    "email_automation/__init__.py": "package marker",
    "email_automation/app_config.py": "process config / env loading, not a feature",
    "email_automation/logging.py": "logging infrastructure, not a feature",
    "email_automation/email_operations.py": "LEGACY disabled send path; kept dead and guarded by tests/test_legacy_email_operations_disabled.py",
    "email_automation/operator_replay.py": "local, Baylor/BP21-only operator recovery utility; not deployed or normal-user callable",
}


class BackendModulesAreTrackedTests(unittest.TestCase):
    def _owned_backend_modules(self):
        registry = json.loads(REGISTRY_PATH.read_text())
        owned = set()
        for feature in registry.get("features", []):
            for module_path in (feature.get("ownerModules", {}) or {}).get("backend", []) or []:
                owned.add(module_path)
        return owned

    def test_every_backend_module_is_owned_or_reviewed_support(self):
        owned = self._owned_backend_modules()
        modules = sorted(
            p for p in glob.glob("email_automation/*.py")
        )
        untracked = [
            m for m in modules
            if m not in owned and m not in SUPPORT_MODULES
        ]
        self.assertEqual(
            [],
            untracked,
            "Every email_automation/*.py module must be owned by a registry feature "
            "or listed in SUPPORT_MODULES with a reason. Untracked: " + str(untracked),
        )

    def test_support_modules_still_exist(self):
        """Keep the support allowlist honest — no dangling entries."""
        stale = [m for m in SUPPORT_MODULES if not (REPO_ROOT / m).exists()]
        self.assertEqual([], stale, f"SUPPORT_MODULES lists non-existent files: {stale}")


if __name__ == "__main__":
    unittest.main()
