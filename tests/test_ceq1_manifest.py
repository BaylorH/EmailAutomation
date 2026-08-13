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
from unittest import mock
import zipfile


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
            record_path = next(row["path"] for row in package["members"] if row["path"].endswith("/RECORD"))
            self.assertEqual(record_path, receipt["names"][-1])
            self.assertEqual(sorted(receipt["names"][:-1]), receipt["names"][:-1])
            self.assertEqual({0}, set(receipt["compressTypes"]))
            self.assertEqual({(1980, 1, 1, 0, 0, 0)}, {tuple(x) for x in receipt["timestamps"]})
            self.assertEqual({3}, set(receipt["createSystems"]))
            self.assertEqual({20}, set(receipt["createVersions"]))
            self.assertEqual({20}, set(receipt["extractVersions"]))
            self.assertEqual({0}, set(receipt["flagBits"]))
            self.assertEqual({0}, set(receipt["internalAttrs"]))

    def test_source_tampering_and_undeclared_extras_fail_closed(self):
        mutations = (
            "payload",
            "extra",
            "symlink",
            "hardlink",
            "fifo",
            "member-mode",
            "directory-mode",
            "empty-directory",
        )
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
                elif mutation == "fifo":
                    os.mkfifo(source / "named-pipe")
                elif mutation == "member-mode":
                    (source / "demo_pkg/__init__.py").chmod(0o755)
                elif mutation == "directory-mode":
                    (source / "demo_pkg").chmod(0o700)
                else:
                    (source / "empty-directory").mkdir()
                with self.assertRaises((ValueError, OSError)):
                    self.builder.inspect_source(source, package)

    def test_finished_wheel_payload_and_record_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, package = _mini_distribution(root)
            wheel = self.builder.build_wheel(source, package, root / "wheel", verify_output=False)
            for target in ("demo_pkg/__init__.py", "demo_pkg-1.2.3.dist-info/RECORD"):
                with self.subTest(target=target):
                    changed = root / f"changed-{target.rsplit('/', 1)[-1]}.whl"
                    with zipfile.ZipFile(wheel, "r") as source_archive:
                        members = [
                            (info, source_archive.read(info.filename))
                            for info in source_archive.infolist()
                        ]
                    with zipfile.ZipFile(
                        changed,
                        "w",
                        compression=zipfile.ZIP_STORED,
                        allowZip64=False,
                    ) as output_archive:
                        for info, data in members:
                            output_archive.writestr(
                                info,
                                b"tampered" if info.filename == target else data,
                            )
                    with self.assertRaises(ValueError):
                        self.builder.inspect_wheel(changed, validate_record=True)

    def test_unsafe_manifest_paths_and_metadata_fail_closed(self):
        cases = (
            "../escape",
            "/absolute",
            "back\\slash",
            "nönascii",
            "demo.data/file",
            "./alias",
            "alias//child",
            "trailing/",
        )
        for unsafe in cases:
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as tmp:
                source, package = _mini_distribution(Path(tmp))
                package["members"][0]["path"] = unsafe
                with self.assertRaises(ValueError):
                    self.builder.inspect_source(source, package)

    def test_package_identity_and_cache_link_are_closed(self):
        manifest = json.loads(
            (REPO_ROOT / "docs/release-safety/ceq1-wheelhouse-manifest.json").read_text()
        )
        for field, value in (
            ("normalizedName", "../escape"),
            ("normalizedName", "Name With Space"),
            ("version", "1/../../escape"),
            ("archiveId", "not an opaque id!"),
        ):
            changed = json.loads(json.dumps(manifest))
            changed["packages"][0][field] = value
            with self.assertRaises(ValueError):
                self.builder._validate_manifest_value(changed)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            package = json.loads(json.dumps(manifest["packages"][0]))
            archive = cache / "archive-v0" / package["archiveId"]
            archive.mkdir(parents=True)
            link = cache / "wheels-v6/pypi" / package["normalizedName"] / (
                f"{package['version']}-py3-none-any"
            )
            link.parent.mkdir(parents=True)
            os.symlink(archive, link)
            self.assertEqual(
                archive,
                self.builder.resolve_cache_source(cache, package),
            )
            link.unlink()
            os.symlink(cache / "archive-v0/wrong", link)
            with self.assertRaises(ValueError):
                self.builder.resolve_cache_source(cache, package)


class Ceq1BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = _load_script("bootstrap_ceq1_runtime.py")
        cls.wrapper = _load_script("run_ceq1_env.py")

    def test_bootstrap_profile_is_portable_and_command_vectors_are_closed(self):
        template = self.bootstrap.BOOTSTRAP_SEATBELT_TEMPLATE
        self.assertIn("(deny default)", template)
        self.assertIn('(literal "/")', template)
        self.assertIn("(deny network*)", template)
        self.assertIn('(literal "/dev/null")', template)
        self.assertIn('(subpath "{JDK_ROOT}")', template)
        self.assertIn('(literal "{FIRESTORE_JAR}")', template)
        self.assertNotIn("/Users/", template)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            profile, receipt = self.bootstrap.render_bootstrap_profile(root)
            self.assertIn(str(root), profile)
            self.assertEqual(str(root), receipt["parameters"]["REPO"])
            self.assertRegex(receipt["renderedSha256"], r"^[0-9a-f]{64}$")
        outer_executable, outer_argv, outer_env = self.bootstrap.contained_launcher_contract(
            REPO_ROOT,
            REPO_ROOT / ".ceq1-runtime/bootstrap-contract",
            "prepare",
        )
        self.assertEqual(Path("/usr/bin/env"), outer_executable)
        self.assertEqual({}, outer_env)
        self.assertIn("/usr/bin/sandbox-exec", outer_argv)
        self.assertIn("--contained", outer_argv)
        commands = self.bootstrap.command_contract(sandboxed=False)
        self.assertTrue(commands)
        self.assertIn("/bin/cp", commands[0])
        self.assertIn("-cR", commands[0])
        for command in commands:
            self.assertEqual("/usr/bin/env", command[0])
            self.assertEqual("-i", command[1])
            self.assertNotIn("/usr/bin/sandbox-exec", command)
            joined = " ".join(command)
            self.assertNotIn("$PWD", joined)
            self.assertNotIn("http://", joined)
        for command in commands[1:]:
            cache_values = [item for item in command if item.startswith("UV_CACHE_DIR=")]
            self.assertEqual(1, len(cache_values))
            self.assertIn(".ceq1-runtime/bootstrap/uv-cache", cache_values[0])
        compile_command = next(c for c in commands if "compile" in c)
        for flag in ("--offline", "--no-config", "--no-python-downloads", "--generate-hashes"):
            self.assertIn(flag, compile_command)
        derived_install = next(c for c in commands if "--reinstall" in c)
        for flag in ("--offline", "--no-index", "--require-hashes", "--only-binary", "--link-mode"):
            self.assertIn(flag, derived_install)
        self.assertNotIn("--exact", derived_install)
        find_links = derived_install[derived_install.index("--find-links") + 1]
        self.assertTrue(find_links.endswith("/.ceq1-runtime/wheelhouse"))
        self.assertNotIn("/.ceq1-runtime/bootstrap/", find_links)

    def test_outer_policy_bytes_are_identical_and_outer_has_no_stateful_work(self):
        bootstrap = REPO_ROOT / ".ceq1-runtime/bootstrap-contract-red"
        expected_profile, _ = self.bootstrap.render_bootstrap_profile(REPO_ROOT)
        executable, argv, environment = self.bootstrap.contained_launcher_contract(
            REPO_ROOT,
            bootstrap,
            "prepare",
        )
        self.assertEqual(Path("/usr/bin/env"), executable)
        self.assertEqual({}, environment)
        profile_index = argv.index("-p") + 1
        self.assertEqual(expected_profile, argv[profile_index])
        channels = [
            item for item in argv
            if item.startswith("CEQ1_BOOTSTRAP_POLICY_B64=")
        ]
        self.assertEqual(1, len(channels))
        encoded = channels[0].split("=", 1)[1]
        self.assertEqual(expected_profile, base64.b64decode(encoded).decode("utf-8"))

        sentinel = RuntimeError("execve boundary reached")
        with (
            mock.patch.object(self.bootstrap.secrets, "token_hex", return_value="a" * 32),
            mock.patch.object(self.bootstrap, "_sha256_file") as hash_file,
            mock.patch.object(self.bootstrap, "cache_metadata_receipt") as cache_receipt,
            mock.patch.object(self.bootstrap, "_ensure_private_root") as ensure_root,
            mock.patch.object(self.bootstrap.os, "execve", side_effect=sentinel),
        ):
            with self.assertRaisesRegex(RuntimeError, "execve boundary reached"):
                self.bootstrap._launch_contained("prepare")
        hash_file.assert_not_called()
        cache_receipt.assert_not_called()
        ensure_root.assert_not_called()

    def test_direct_contained_invocation_refuses_before_mutation_and_sandbox_inherits(self):
        bootstrap_name = "bootstrap-direct-contained-red"
        bootstrap_path = REPO_ROOT / ".ceq1-runtime" / bootstrap_name
        self.assertFalse(bootstrap_path.exists())
        profile, _ = self.bootstrap.render_bootstrap_profile(REPO_ROOT)
        encoded = base64.b64encode(profile.encode("utf-8")).decode("ascii")
        environment = {
            "HOME": str(bootstrap_path / "home"),
            "TMPDIR": str(bootstrap_path / "tmp"),
            "XDG_CACHE_HOME": str(bootstrap_path / "cache"),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "CEQ1_BOOTSTRAP_POLICY_B64": encoded,
        }
        direct = subprocess.run(
            [
                str(PINNED_PYTHON),
                "-I",
                "-S",
                "-B",
                str(REPO_ROOT / "scripts/bootstrap_ceq1_runtime.py"),
                "prepare",
                "--contained",
                "--bootstrap",
                bootstrap_name,
            ],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, direct.returncode)
        self.assertIn("CE-Q1 bootstrap Seatbelt is not active", direct.stderr)
        self.assertFalse(bootstrap_path.exists())

        probe = (
            "import importlib.util,sys;"
            f"p={str(REPO_ROOT / 'scripts/bootstrap_ceq1_runtime.py')!r};"
            "s=importlib.util.spec_from_file_location('ceq1_probe',p);"
            "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;"
            "s.loader.exec_module(m);m.prove_active_seatbelt(m._repo_root())"
        )
        inherited = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                str(PINNED_PYTHON),
                "-I",
                "-S",
                "-B",
                "-c",
                probe,
            ],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, inherited.returncode, inherited.stderr)

    def test_task_cache_clone_rebases_only_internal_absolute_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source-cache"
            destination = root / "task-cache"
            archive = source / "archive-v0/exact-archive"
            archive.mkdir(parents=True)
            (archive / "payload.py").write_text("VALUE = 1\n", encoding="utf-8")
            link = source / "wheels-v6/pypi/demo/1.0-py3-none-any"
            link.parent.mkdir(parents=True)
            os.symlink(archive, link)
            before_identity = self.bootstrap.cache_identity_receipt(source)
            expected_topology = self.bootstrap.cache_logical_receipt(source)
            shutil.copytree(source, destination, symlinks=True)
            receipt = self.bootstrap.rebase_and_validate_cache_clone(
                source,
                destination,
                before_identity,
                expected_topology,
            )
            self.assertEqual(before_identity, self.bootstrap.cache_identity_receipt(source))
            self.assertEqual(
                destination / "archive-v0/exact-archive",
                Path(os.readlink(destination / link.relative_to(source))),
            )
            self.assertEqual(expected_topology["logicalDigest"], receipt["logicalDigest"])

            escaping = destination / "escape"
            os.symlink("/private/etc/passwd", escaping)
            with self.assertRaises(self.bootstrap.BootstrapBlocked):
                self.bootstrap.rebase_and_validate_cache_clone(
                    source,
                    destination,
                    before_identity,
                    expected_topology,
                )

    def test_derived_lock_is_canonical_and_uses_only_derived_hashes(self):
        manifest = json.loads(
            (REPO_ROOT / "docs/release-safety/ceq1-wheelhouse-manifest.json").read_text()
        )
        rendered = self.bootstrap.render_derived_lock(manifest)
        self.assertEqual((REPO_ROOT / "requirements-ceq1.lock").read_bytes(), rendered)
        self.assertEqual(5, rendered.count(b"--hash=sha256:"))

    def test_bundle_path_receipt_accepts_only_explicit_nonfilesystem_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp).resolve()
            extension = bundle / "python/lib/python3.12/lib-dynload/example.so"
            extension.parent.mkdir(parents=True)
            extension.write_bytes(b"fixture")
            receipt = {
                "executable": str(bundle / "venv/bin/python"),
                "prefix": str(bundle / "venv"),
                "basePrefix": str(bundle / "python"),
                "stdlib": str(bundle / "python/lib/python3.12"),
                "platstdlib": str(bundle / "python/lib/python3.12"),
                "extensions": ["built-in", "frozen", str(extension)],
            }
            self.bootstrap.validate_bundle_path_receipt(bundle, receipt)
            receipt["extensions"][0] = "relative-or-escaped"
            with self.assertRaises(self.bootstrap.BootstrapBlocked):
                self.bootstrap.validate_bundle_path_receipt(bundle, receipt)

    def test_canonical_wheelhouse_is_created_directly_then_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "stage"
            package_dir = stage / "demo"
            package_dir.mkdir(parents=True)
            payload = b"derived-wheel-fixture"
            filename = "demo-1.0-py3-none-any.whl"
            (package_dir / filename).write_bytes(payload)
            manifest = {
                "packages": [
                    {
                        "normalizedName": "demo",
                        "wheel": {
                            "filename": filename,
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        },
                    }
                ]
            }
            destination = root / "wheelhouse"
            self.bootstrap._ensure_canonical_wheelhouse(stage, destination, manifest)
            self.assertEqual(payload, (destination / filename).read_bytes())
            self.assertEqual(0o555, stat.S_IMODE(destination.stat().st_mode))

    def test_uv_install_metadata_is_normalized_and_record_rebound(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            dist_info = bundle / "venv/lib/python3.12/site-packages/demo-1.0.dist-info"
            dist_info.mkdir(parents=True)
            cache_path = dist_info / "uv_cache.json"
            cache_path.write_text(
                '{"timestamp":{"secs_since_epoch":123,"nanos_since_epoch":456},'
                '"commit":null,"tags":null,"env":{},"directories":{}}',
                encoding="utf-8",
            )
            old = cache_path.read_bytes()
            old_digest = base64.urlsafe_b64encode(hashlib.sha256(old).digest()).rstrip(b"=").decode()
            record_path = dist_info / "RECORD"
            record_path.write_text(
                "demo-1.0.dist-info/RECORD,,\n"
                f"demo-1.0.dist-info/uv_cache.json,sha256={old_digest},{len(old)}\n",
                encoding="utf-8",
            )
            manifest = {
                "packages": [
                    {
                        "members": [
                            {"path": "demo-1.0.dist-info/RECORD"},
                        ]
                    }
                ]
            }
            self.bootstrap.normalize_uv_install_metadata(bundle, manifest)
            canonical = (
                b'{"timestamp":{"secs_since_epoch":0,"nanos_since_epoch":0},'
                b'"commit":null,"tags":null,"env":{},"directories":{}}'
            )
            self.assertEqual(canonical, cache_path.read_bytes())
            digest = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=").decode()
            self.assertIn(
                f"demo-1.0.dist-info/uv_cache.json,sha256={digest},{len(canonical)}",
                record_path.read_text(encoding="utf-8"),
            )
            before = record_path.read_bytes()
            self.bootstrap.normalize_uv_install_metadata(bundle, manifest)
            self.assertEqual(before, record_path.read_bytes())

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
                "seatbeltTemplate",
                "sealedRuntime",
            },
            set(manifest),
        )
        self.assertNotIn("/Users/", json.dumps(manifest, sort_keys=True))
        self.assertNotIn("sandboxPolicyReceiptDigest", json.dumps(manifest, sort_keys=True))
        self.assertEqual(
            sorted(self.bootstrap.SEATBELT_PLACEHOLDERS),
            manifest["seatbeltTemplate"]["placeholders"],
        )
        self.bootstrap.validate_committed_toolchain(REPO_ROOT, manifest)

    def test_fast_path_requires_exact_ignored_receipt_and_wheelhouse(self):
        receipt_path = REPO_ROOT / ".ceq1-runtime/bootstrap-receipt.json"
        self.assertTrue(receipt_path.is_file())
        self.bootstrap.validate_runtime_receipt(REPO_ROOT, receipt_path)
        manifest = json.loads(
            (REPO_ROOT / "docs/release-safety/ceq1-wheelhouse-manifest.json").read_text()
        )
        self.bootstrap._validate_wheelhouse(
            REPO_ROOT / ".ceq1-runtime/wheelhouse",
            manifest,
            require_sealed=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            changed = Path(tmp) / "receipt.json"
            payload = json.loads(receipt_path.read_text())
            payload["renderedSha256"] = "0" * 64
            changed.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(self.bootstrap.BootstrapBlocked):
                self.bootstrap.validate_runtime_receipt(REPO_ROOT, changed)

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

    def test_wrapper_uses_a_true_execve_boundary_and_rejects_symlinked_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp) / "task"
            executable, argv, env = self.wrapper.build_execve_contract(
                REPO_ROOT, task, ["--inspect-runtime"]
            )
            self.assertEqual(REPO_ROOT / ".ceq1-venv/python/bin/python3.12", executable)
            self.assertEqual("-I", argv[1])
            self.assertEqual("-S", argv[2])
            self.assertEqual("-B", argv[3])
            self.assertIn("--ceq1-exec-child", argv)
            self.assertEqual(set(self.wrapper.CLOSED_ENV_KEYS), set(env))
            linked = Path(tmp) / "linked"
            os.symlink(task, linked)
            with self.assertRaises(self.wrapper.EnvironmentBoundaryError):
                self.wrapper.build_child_env(linked)

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
