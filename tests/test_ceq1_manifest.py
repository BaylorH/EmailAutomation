"""Task 1 contracts for the sealed CE-Q1 qualification runtime."""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_PYTHON = Path(
    "/Users/baylorharrison/.local/share/uv/python/"
    "cpython-3.12.13-macos-aarch64-none/bin/python3.12"
)
TASK1_FILES = (
    ".gitignore",
    "requirements-ceq1.in",
    "requirements-ceq1.lock",
    "docs/release-safety/ceq1-wheelhouse-manifest.json",
    "docs/release-safety/ceq1-toolchain-manifest.json",
    "docs/release-safety/evidence/ceq1/README.md",
    "scripts/bootstrap_ceq1_runtime.py",
    "scripts/build_ceq1_wheelhouse.py",
    "scripts/run_ceq1_env.py",
    "tests/ceq1/__init__.py",
)
EXPECTED_PACKAGES = {
    "pytest": ("9.1.1", "omI8AR3KdOUNlTla9oxPI"),
    "pluggy": ("1.6.0", "EHZJDR6pWsFsWQYAMFTP_"),
    "iniconfig": ("2.3.0", "5Ebt4q15_KqBu2iptD-ep"),
    "pygments": ("2.20.0", "pFcp15BRXIJdISPErAGLj"),
    "packaging": ("26.2", "U8I70fV3E7adQmQckZ_fB"),
}


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / name
    if not path.is_file():
        raise AssertionError(f"missing Task 1 script: {name}")
    spec = importlib.util.spec_from_file_location(f"ceq1_{name[:-3]}", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load Task 1 script: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_hash(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _scan_production_imports() -> list[str]:
    files = list((REPO_ROOT / "email_automation").rglob("*.py"))
    files.extend(REPO_ROOT / name for name in ("main.py", "service.py", "scheduler_runner.py", "app.py"))
    violations: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.startswith(("tests.ceq1", "scripts.run_ceq1")) for name in names):
                violations.append(path.relative_to(REPO_ROOT).as_posix())
        if "tests/fixtures/ceq1" in source:
            violations.append(path.relative_to(REPO_ROOT).as_posix())
    return sorted(set(violations))


def _mini_distribution(root: Path, *, extra_pyc: bool = False) -> tuple[Path, dict[str, object]]:
    source = root / "archive-v0" / "mini-archive"
    dist_info = source / "demo_pkg-1.2.3.dist-info"
    package = source / "demo_pkg"
    dist_info.mkdir(parents=True)
    package.mkdir()
    payloads = {
        "demo_pkg/__init__.py": b"VALUE = 7\n",
        "demo_pkg-1.2.3.dist-info/METADATA": b"Metadata-Version: 2.4\nName: demo-pkg\nVersion: 1.2.3\n",
        "demo_pkg-1.2.3.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: ceq1-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    for relative, data in payloads.items():
        path = source / PurePosixPath(relative)
        path.write_bytes(data)
    record_path = "demo_pkg-1.2.3.dist-info/RECORD"
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    for relative in sorted(payloads):
        data = payloads[relative]
        writer.writerow((relative, f"sha256={_record_hash(data)}", str(len(data))))
    writer.writerow((record_path, "", ""))
    record = out.getvalue().encode("utf-8")
    (source / record_path).write_bytes(record)
    excluded: list[dict[str, object]] = []
    if extra_pyc:
        pyc = source / "demo_pkg" / "__pycache__" / "cached.cpython-312.pyc"
        pyc.parent.mkdir()
        pyc.write_bytes(b"synthetic-bytecode")
        excluded.append(
            {
                "path": pyc.relative_to(source).as_posix(),
                "size": pyc.stat().st_size,
                "sha256": _sha256(pyc.read_bytes()),
                "classification": "generated-pyc",
            }
        )
    members = []
    for relative in sorted((*payloads.keys(), record_path)):
        data = (source / relative).read_bytes()
        members.append({"path": relative, "size": len(data), "sha256": _sha256(data)})
    package_record: dict[str, object] = {
        "name": "demo-pkg",
        "normalizedName": "demo_pkg",
        "version": "1.2.3",
        "archiveId": "mini-archive",
        "recordSha256": _sha256(record),
        "members": members,
        "excluded": excluded,
        "wheel": {
            "filename": "demo_pkg-1.2.3-py3-none-any.whl",
            "memberCount": len(members),
            "size": 0,
            "sha256": "0" * 64,
        },
    }
    return source, package_record


class Ceq1BoundaryTests(unittest.TestCase):
    def test_task1_files_exist_and_production_dependency_is_one_way(self):
        self.assertEqual([], _scan_production_imports())
        missing = [relative for relative in TASK1_FILES if not (REPO_ROOT / relative).exists()]
        self.assertEqual([], missing)

    def test_runtime_quarantine_is_ignored_but_evidence_is_versioned(self):
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, lines.count(".ceq1-runtime/"))
        self.assertEqual(1, lines.count(".ceq1-venv/"))
        self.assertNotIn("docs/release-safety/evidence/ceq1/", lines)

    def test_requirements_boundary_is_exact_and_hash_closed(self):
        self.assertEqual("pytest==9.1.1\n", (REPO_ROOT / "requirements-ceq1.in").read_text())
        lock = (REPO_ROOT / "requirements-ceq1.lock").read_text(encoding="utf-8")
        for name, (version, _) in EXPECTED_PACKAGES.items():
            self.assertEqual(1, lock.count(f"{name}=={version}"), name)
        self.assertEqual(5, lock.count("--hash=sha256:"))
        self.assertNotIn("http", lock.lower())
        self.assertNotIn("file:", lock.lower())

    def test_evidence_readme_states_the_offline_nonclaims(self):
        text = (REPO_ROOT / "docs/release-safety/evidence/ceq1/README.md").read_text()
        for phrase in (
            "offline deterministic evidence",
            "does not certify production",
            "model",
            "mailbox",
            "delivery",
            "Google Sheets persistence",
            "cross-store atomicity",
        ):
            self.assertIn(phrase, text)


class Ceq1WheelBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = _load_script("build_ceq1_wheelhouse.py")

    def test_real_manifest_is_closed_path_free_and_binds_exact_cache_inputs(self):
        path = REPO_ROOT / "docs/release-safety/ceq1-wheelhouse-manifest.json"
        manifest = self.builder.load_closed_manifest(path)
        self.assertEqual(
            {"schemaVersion", "algorithmVersion", "python", "builderSha256", "packages"},
            set(manifest),
        )
        self.assertEqual(set(EXPECTED_PACKAGES), {p["normalizedName"] for p in manifest["packages"]})
        serialized = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("/Users/", serialized)
        for package in manifest["packages"]:
            expected_version, expected_archive = EXPECTED_PACKAGES[package["normalizedName"]]
            self.assertEqual(expected_version, package["version"])
            self.assertEqual(expected_archive, package["archiveId"])
            self.assertRegex(package["recordSha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(package["members"])

    def test_record_allowlist_build_is_deterministic_and_record_is_physically_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, package = _mini_distribution(root, extra_pyc=True)
            inspected = self.builder.inspect_source(source, package)
            self.assertEqual(package["members"], inspected["members"])
            self.assertEqual(package["excluded"], inspected["excluded"])
            first = self.builder.build_wheel(source, package, root / "a", verify_output=False)
            second = self.builder.build_wheel(source, package, root / "b", verify_output=False)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            receipt = self.builder.inspect_wheel(first)
            self.assertEqual(package["members"][-1]["path"], receipt["names"][-1])
            self.assertEqual(sorted(receipt["names"][:-1]), receipt["names"][:-1])
            self.assertEqual({0}, set(receipt["compressTypes"]))
            self.assertEqual({[1980, 1, 1, 0, 0, 0]}, {list(x) for x in receipt["timestamps"]})
            self.assertEqual({3}, set(receipt["createSystems"]))
            self.assertEqual({20}, set(receipt["createVersions"]))
            self.assertEqual({20}, set(receipt["extractVersions"]))
            self.assertEqual({0}, set(receipt["flagBits"]))
            self.assertEqual({0}, set(receipt["internalAttrs"]))

    def test_source_tampering_and_undeclared_extras_fail_closed(self):
        mutations = ("payload", "extra", "symlink", "hardlink", "fifo")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, package = _mini_distribution(root)
                if mutation == "payload":
                    (source / "demo_pkg/__init__.py").write_bytes(b"VALUE = 8\n")
                elif mutation == "extra":
                    (source / "unexpected.py").write_text("bad = True\n")
                elif mutation == "symlink":
                    os.symlink("demo_pkg/__init__.py", source / "alias.py")
                elif mutation == "hardlink":
                    os.link(source / "demo_pkg/__init__.py", source / "hard.py")
                else:
                    os.mkfifo(source / "named-pipe")
                with self.assertRaises((ValueError, OSError)):
                    self.builder.inspect_source(source, package)

    def test_unsafe_manifest_paths_and_metadata_fail_closed(self):
        cases = ("../escape", "/absolute", "back\\slash", "nönascii", "demo.data/file")
        for unsafe in cases:
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as tmp:
                source, package = _mini_distribution(Path(tmp))
                package["members"][0]["path"] = unsafe
                with self.assertRaises(ValueError):
                    self.builder.inspect_source(source, package)


class Ceq1BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = _load_script("bootstrap_ceq1_runtime.py")
        cls.wrapper = _load_script("run_ceq1_env.py")

    def test_bootstrap_profile_is_portable_and_command_vectors_are_closed(self):
        template = self.bootstrap.BOOTSTRAP_SEATBELT_TEMPLATE
        self.assertIn("(deny default)", template)
        self.assertNotIn("/Users/", template)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            profile, receipt = self.bootstrap.render_bootstrap_profile(root)
            self.assertIn(str(root), profile)
            self.assertNotIn(str(root), json.dumps(receipt, sort_keys=True))
            self.assertRegex(receipt["sandboxPolicyReceiptDigest"], r"^[0-9a-f]{64}$")
        commands = self.bootstrap.command_contract()
        self.assertTrue(commands)
        for command in commands:
            self.assertEqual("/usr/bin/env", command[0])
            self.assertEqual("-i", command[1])
            self.assertIn("/usr/bin/sandbox-exec", command)
            joined = " ".join(command)
            self.assertNotIn("$PWD", joined)
            self.assertNotIn("http://", joined)
        compile_command = next(c for c in commands if "compile" in c)
        for flag in ("--offline", "--no-config", "--no-python-downloads", "--generate-hashes"):
            self.assertIn(flag, compile_command)
        derived_install = next(c for c in commands if "--reinstall" in c)
        for flag in ("--offline", "--no-index", "--require-hashes", "--only-binary", "--link-mode"):
            self.assertIn(flag, derived_install)
        self.assertNotIn("--exact", derived_install)

    def test_derived_lock_is_canonical_and_uses_only_derived_hashes(self):
        manifest = json.loads(
            (REPO_ROOT / "docs/release-safety/ceq1-wheelhouse-manifest.json").read_text()
        )
        rendered = self.bootstrap.render_derived_lock(manifest)
        self.assertEqual((REPO_ROOT / "requirements-ceq1.lock").read_bytes(), rendered)
        self.assertEqual(5, rendered.count(b"--hash=sha256:"))

    def test_toolchain_manifest_is_path_free_and_recomputes(self):
        manifest_path = REPO_ROOT / "docs/release-safety/ceq1-toolchain-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(
            {
                "schemaVersion",
                "algorithmVersion",
                "artifacts",
                "lockfiles",
                "wheelhouseManifestSha256",
                "bootstrapSha256",
                "builderSha256",
                "seatbeltTemplateSha256",
                "sealedRuntime",
            },
            set(manifest),
        )
        self.assertNotIn("/Users/", json.dumps(manifest, sort_keys=True))
        self.bootstrap.validate_committed_toolchain(REPO_ROOT, manifest)

    def test_wrapper_requires_isolated_flags_and_builds_an_exact_environment(self):
        with self.assertRaises(RuntimeError):
            self.wrapper.require_isolated_flags(("python", "script.py"))
        self.wrapper.require_isolated_flags(("python", "-I", "-S", "-B", "script.py"))
        with tempfile.TemporaryDirectory() as tmp:
            env = self.wrapper.build_child_env(Path(tmp))
        self.assertEqual(set(self.wrapper.CLOSED_ENV_KEYS), set(env))
        forbidden = {
            "PYTHONPATH",
            "PYTHONHOME",
            "SSH_AUTH_SOCK",
            "OBSIDIAN_REST_API_KEY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
        }
        self.assertFalse(forbidden & set(env))
        self.assertEqual("paused", env["SITESIFT_OUTBOUND_MODE"])

    def test_wrapper_inspection_child_has_no_ambient_secret_or_extra_fd(self):
        script = REPO_ROOT / "scripts/run_ceq1_env.py"
        bundle_python = REPO_ROOT / ".ceq1-venv/python/bin/python3.12"
        if not bundle_python.is_file():
            self.fail("sealed runtime bundle is absent")
        inherited = os.open(REPO_ROOT / "requirements-ceq1.in", os.O_RDONLY)
        os.set_inheritable(inherited, True)
        try:
            parent_env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "OBSIDIAN_REST_API_KEY": "CEQ1_FORBIDDEN_SECRET",
                "SSH_AUTH_SOCK": "/tmp/forbidden-agent",
                "PYTHONPATH": "/tmp/forbidden-site",
            }
            result = subprocess.run(
                [
                    str(bundle_python),
                    "-I",
                    "-S",
                    "-B",
                    str(script),
                    "--inspect-runtime",
                ],
                cwd=REPO_ROOT,
                env=parent_env,
                pass_fds=(inherited,),
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        finally:
            os.close(inherited)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("CEQ1_FORBIDDEN_SECRET", result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual([0, 1, 2], receipt["fds"])
        self.assertEqual(sorted(self.wrapper.CLOSED_ENV_KEYS), receipt["environmentKeys"])
        root = str((REPO_ROOT / ".ceq1-venv").resolve())
        for key in ("executable", "prefix", "basePrefix", "stdlib", "platstdlib"):
            self.assertTrue(receipt[key].startswith(root), (key, receipt[key]))
        self.assertTrue(all(path.startswith(root) for path in receipt["loadedPaths"]))


if __name__ == "__main__":
    unittest.main()
