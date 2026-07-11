"""Production runtime dependency contract for the process-user service."""

import ast
from pathlib import Path
import re
import unittest

from packaging.requirements import Requirement


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
LOCK_PATH = REPO_ROOT / "requirements.lock"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"


def _active_requirements() -> list[str]:
    return [
        line.strip()
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _production_python_sources() -> list[Path]:
    excluded_roots = {"tests", ".venv", "venv", "build", "dist"}
    return [
        path
        for path in REPO_ROOT.rglob("*.py")
        if excluded_roots.isdisjoint(path.relative_to(REPO_ROOT).parts)
        and not any(part.startswith(".") for part in path.relative_to(REPO_ROOT).parts)
    ]


class RuntimeDependencyContractTests(unittest.TestCase):
    def test_dockerfile_pins_python_base_image_by_digest(self):
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$",
        )

    def test_firebase_admin_is_exactly_pinned_for_production(self):
        requirements = _active_requirements()
        self.assertEqual(requirements.count("firebase-admin==7.5.0"), 1)
        self.assertFalse(
            any(
                requirement.lower().startswith("firebase-admin")
                and requirement != "firebase-admin==7.5.0"
                for requirement in requirements
            ),
            "firebase-admin must have one exact production pin",
        )

    def test_production_firebase_admin_imports_map_to_declared_distribution(self):
        declared_distributions = {
            Requirement(requirement).name.lower().replace("_", "-")
            for requirement in _active_requirements()
        }
        firebase_imports = []
        for path in _production_python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                else:
                    continue
                firebase_imports.extend(
                    (path.relative_to(REPO_ROOT), module)
                    for module in modules
                    if module.split(".", 1)[0] == "firebase_admin"
                )

        self.assertTrue(firebase_imports, "production source must import firebase_admin")
        for path, module in firebase_imports:
            distribution = module.split(".", 1)[0].lower().replace("_", "-")
            self.assertIn(
                distribution,
                declared_distributions,
                f"{path} imports {module}, but distribution {distribution} is undeclared",
            )

    def test_dockerfile_installs_requirements_file(self):
        dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^COPY\s+requirements\.lock\s+\./\s*$",
        )
        self.assertRegex(
            dockerfile,
            re.compile(r"(?m)^RUN\s+pip\s+install\b[^\n]*\s-r\s+requirements\.lock\s*$"),
        )

    def test_deployment_lock_pins_every_distribution_with_hashes(self):
        lock_text = LOCK_PATH.read_text(encoding="utf-8")
        logical_lines = []
        current = ""
        for raw_line in lock_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            current = f"{current} {line}".strip()
            if current.endswith("\\"):
                current = current[:-1].strip()
                continue
            logical_lines.append(current)
            current = ""
        self.assertFalse(current, "lock file ended with an incomplete continuation")
        self.assertGreater(len(logical_lines), 20)

        locked_names = set()
        for line in logical_lines:
            requirement_text = line.split(" --hash=", 1)[0].strip()
            requirement = Requirement(requirement_text)
            self.assertRegex(str(requirement.specifier), r"^==[^,]+$")
            self.assertIn("--hash=sha256:", line)
            locked_names.add(requirement.name.lower().replace("_", "-"))

        direct_names = {
            Requirement(requirement).name.lower().replace("_", "-")
            for requirement in _active_requirements()
        }
        self.assertTrue(direct_names.issubset(locked_names))


if __name__ == "__main__":
    unittest.main()
