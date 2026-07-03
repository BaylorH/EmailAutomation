"""Registry integrity: every backend ownerModule must be a real file.

The release rubric is only trustworthy if the feature registry points at code
that actually exists. Prior to this test, `ownerModules.backend` could name a
module that had been renamed or never existed (e.g. `usage_tracking.py`,
`graph_scan_health.py`) and no test caught the drift — a "looks tracked, isn't"
failure the rubric is supposed to eliminate.
"""

import json
import os
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "docs" / "release-safety" / "feature-registry.json"


class FeatureRegistryOwnerModulesExistTests(unittest.TestCase):
    def _registry(self):
        return json.loads(REGISTRY_PATH.read_text())

    def test_every_backend_owner_module_exists_on_disk(self):
        registry = self._registry()
        missing = []
        for feature in registry.get("features", []):
            backend_modules = (feature.get("ownerModules", {}) or {}).get("backend", []) or []
            for module_path in backend_modules:
                if not (REPO_ROOT / module_path).exists():
                    missing.append((feature.get("id"), module_path))

        self.assertEqual(
            [],
            missing,
            "feature-registry.json ownerModules.backend must point at real files in this repo. "
            f"Missing: {missing}",
        )

    def test_backend_owner_modules_are_repo_python_paths(self):
        """Backend owner modules should be repo-relative .py paths, not stray names."""
        registry = self._registry()
        offenders = []
        for feature in registry.get("features", []):
            backend_modules = (feature.get("ownerModules", {}) or {}).get("backend", []) or []
            for module_path in backend_modules:
                if not module_path.endswith(".py"):
                    offenders.append((feature.get("id"), module_path))

        self.assertEqual(
            [],
            offenders,
            f"backend ownerModules must be .py file paths. Offenders: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
