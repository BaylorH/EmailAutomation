"""WS-B: secret-coverage delta code contract (worklist #7, code half).

The legacy GHA workflow injects three env vars the Cloud Run job spec
(deploy/cloudrun-job.yaml) intentionally omits:

  - CLIENT_ID        — read only by noPopup_signin_emails_to_excel.py (not run)
  - FIREBASE_SA_KEY  — read only by config.py (Flask app.py, not main.py)
  - AZURE_TENANT_ID  — read only by config.py; the scheduler's AUTHORITY is
                       hardcoded to /common in email_automation/app_config.py

The omission is only safe while main.py's import closure keeps NOT reading
those names. This test walks the AST of main.py and every module it can pull
in (email_automation/, firebase_helpers.py) and fails if any of them gains an
os.getenv / os.environ read of one of the omitted names — at which point the
job yaml must be updated BEFORE the code change lands.

config.py and noPopup_signin_emails_to_excel.py are deliberately NOT scanned:
they are allowed to keep reading these vars (they are not part of the
scheduler entrypoint).
"""

import ast
import os
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Env names present in .github/workflows/email.yml but intentionally absent
# from deploy/cloudrun-job.yaml.
OMITTED_ENV_NAMES = frozenset({"CLIENT_ID", "FIREBASE_SA_KEY", "AZURE_TENANT_ID"})

# main.py's import closure: the scheduler entrypoint plus everything it can
# import at runtime. Kept conservative (whole email_automation package) so a
# new deep import can't dodge the scan.
SCANNED_FILES = ["main.py", "firebase_helpers.py"]
SCANNED_PACKAGE_DIRS = ["email_automation"]


def _scheduler_closure_files():
    paths = [os.path.join(REPO_ROOT, rel) for rel in SCANNED_FILES]
    for pkg in SCANNED_PACKAGE_DIRS:
        pkg_dir = os.path.join(REPO_ROOT, pkg)
        for dirpath, _dirnames, filenames in os.walk(pkg_dir):
            if "__pycache__" in dirpath:
                continue
            for name in sorted(filenames):
                if name.endswith(".py"):
                    paths.append(os.path.join(dirpath, name))
    return paths


def _string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _env_reads(tree):
    """Yield every string env-var name read via os.getenv / os.environ.get /
    os.environ[...] / os.environ.setdefault in the given AST."""
    for node in ast.walk(tree):
        # os.getenv("X") / getenv("X") / os.environ.get("X") / environ.get("X")
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name in {"getenv", "get", "setdefault"}:
                value = _string_value(node.args[0])
                if value is not None:
                    yield value, node.lineno
        # os.environ["X"] / environ["X"]
        if isinstance(node, ast.Subscript):
            base = node.value
            is_environ = (
                isinstance(base, ast.Attribute) and base.attr == "environ"
            ) or (isinstance(base, ast.Name) and base.id == "environ")
            if is_environ:
                value = _string_value(node.slice)
                if value is not None:
                    yield value, node.lineno


class SchedulerEnvClosureContractTests(unittest.TestCase):
    def test_scheduler_closure_never_reads_omitted_legacy_env_vars(self):
        violations = []
        for path in _scheduler_closure_files():
            with open(path, "r") as f:
                tree = ast.parse(f.read(), filename=path)
            for env_name, lineno in _env_reads(tree):
                if env_name in OMITTED_ENV_NAMES:
                    rel = os.path.relpath(path, REPO_ROOT)
                    violations.append(f"{rel}:{lineno} reads env {env_name!r}")

        self.assertEqual(
            [],
            violations,
            "main.py's import closure started reading env vars that "
            "deploy/cloudrun-job.yaml intentionally omits. Either revert the "
            "read or add the var to the job spec (and its secret, if secret) "
            "FIRST:\n  " + "\n  ".join(violations),
        )

    def test_scan_actually_covers_the_closure(self):
        """Sanity: the scanner sees main.py + a non-trivial package surface,
        and the AST walker detects a known env read (AZURE_API_APP_ID in
        app_config.py) — guards against a silently vacuous scan."""
        paths = _scheduler_closure_files()
        rels = {os.path.relpath(p, REPO_ROOT) for p in paths}
        self.assertIn("main.py", rels)
        self.assertIn("firebase_helpers.py", rels)
        self.assertIn(os.path.join("email_automation", "app_config.py"), rels)
        self.assertGreater(len(paths), 5)

        app_config = os.path.join(REPO_ROOT, "email_automation", "app_config.py")
        with open(app_config, "r") as f:
            tree = ast.parse(f.read(), filename=app_config)
        names = {name for name, _ in _env_reads(tree)}
        self.assertIn("AZURE_API_APP_ID", names)


if __name__ == "__main__":
    unittest.main()
