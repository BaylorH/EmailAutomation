"""Task 1 contracts for the sealed CE-Q1 qualification runtime."""

from __future__ import annotations

import ast
import base64
import csv
import ctypes
import errno
import hashlib
import importlib.util
import io
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile
import zlib

from tests.ceq1.contracts import (
    EffectAttempt,
    EvidenceResult,
    EventRecord,
    ExecutionResult,
    FactRecord,
    FutureGate,
    GateReport,
    GateVerdict,
    Layer,
    NextGateEligibility,
    PromotionClass,
    SatisfiedDiagnostic,
    ScoreRecord,
    StateSnapshot,
    canonical_json,
    classify_gate,
    sha256_json,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_PYTHON = Path(
    "/Users/baylorharrison/.local/share/uv/python/"
    "cpython-3.12.13-macos-aarch64-none/bin/python3.12"
)
TASK1_FILES = (
    ".gitignore",
    "requirements-ceq1.in",
    "requirements-ceq1.lock",
    "docs/release-safety/ceq1-input-manifest.json",
    "docs/release-safety/ceq1-wheelhouse-manifest.json",
    "docs/release-safety/ceq1-toolchain-manifest.json",
    "docs/release-safety/evidence/ceq1/README.md",
    "scripts/bootstrap_ceq1_runtime.py",
    "scripts/build_ceq1_wheelhouse.py",
    "scripts/run_ceq1_env.py",
    "scripts/verify_ceq1_entry.pl",
    "tests/ceq1/__init__.py",
)
EXPECTED_PACKAGES = {
    "pytest": ("9.1.1", "omI8AR3KdOUNlTla9oxPI"),
    "pluggy": ("1.6.0", "EHZJDR6pWsFsWQYAMFTP_"),
    "iniconfig": ("2.3.0", "5Ebt4q15_KqBu2iptD-ep"),
    "pygments": ("2.20.0", "pFcp15BRXIJdISPErAGLj"),
    "packaging": ("26.2", "U8I70fV3E7adQmQckZ_fB"),
}
PERL_TRAMPOLINE = (
    'my($p,$h,@a)=@ARGV;sysopen(my $f,$p,O_RDONLY|O_NOFOLLOW)or die;'
    'my @b=stat($f);die unless -f _&&$b[3]==1&&$b[7]<=262144;'
    'my($s,$n)=("",$b[7]);while($n){my $r=sysread($f,my $c,$n);'
    'die unless defined($r)&&$r>0;$s.=$c;$n-=$r}'
    'my $r=sysread($f,my $x,1);die unless defined($r)&&$r==0;'
    'my @e=stat($f);die unless @b==@e&&!grep{$b[$_]!=$e[$_]}(0,1,2,3,7,9,10);'
    'die unless sha256_hex($s)eq$h;@ARGV=@a;'
    'eval "package CEQ1::VerifiedEntry;\\n$s";die $@ if $@'
)


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


def _set_fixture_modes(root: Path, *, regular_files: bool = False) -> None:
    """Make synthetic source/cache topology independent of the caller's umask."""
    root.chmod(0o755)
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o755)
        for name in directories:
            child = current_path / name
            if not child.is_symlink():
                child.chmod(0o755)
        if regular_files:
            for name in files:
                child = current_path / name
                if not child.is_symlink():
                    child.chmod(0o644)


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
    _set_fixture_modes(source, regular_files=True)
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

    def test_static_entry_manifest_and_verifier_are_closed(self):
        verifier = REPO_ROOT / "scripts/verify_ceq1_entry.pl"
        manifest_path = REPO_ROOT / "docs/release-safety/ceq1-input-manifest.json"
        self.assertTrue(verifier.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schemaVersion",
                "algorithmVersion",
                "files",
                "trees",
                "portablePolicy",
                "platformTrust",
            },
            set(manifest),
        )
        serialized = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("file://", serialized)
        files = manifest["files"]
        for relative in (
            "scripts/verify_ceq1_entry.pl",
            "scripts/bootstrap_ceq1_runtime.py",
            "scripts/build_ceq1_wheelhouse.py",
            "scripts/run_ceq1_env.py",
            "requirements.lock",
            "requirements-ceq1.in",
            "requirements-ceq1.lock",
            "docs/release-safety/ceq1-wheelhouse-manifest.json",
        ):
            self.assertIn(relative, files)
            self.assertEqual(
                hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest(),
                files[relative]["sha256"],
            )
        source = verifier.read_text(encoding="utf-8")
        self.assertIn("sub verify_inputs", source)
        self.assertIn("sub run_verified_python", source)
        self.assertIn("__name__", source)
        self.assertIn("__file__", source)
        self.assertIn("sys.argv", source)
        self.assertIn("waitpid", source)
        self.assertNotIn("system(", source)
        self.assertNotIn("qx/", source)

    def _synthetic_verifier_root(self, parent: Path, *, exit_code: int = 0) -> tuple[Path, str, str]:
        root = parent / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "docs/release-safety").mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "scripts/verify_ceq1_entry.pl", root / "scripts/verify_ceq1_entry.pl")
        policy = "(version 1)\n(deny default)\n"
        bootstrap = (
            "import json,os,sys\n"
            f"BOOTSTRAP_SEATBELT_TEMPLATE = r'''{policy}'''\n\n"
            "fds=[]\n"
            "for fd in range(256):\n"
            "    try: os.fstat(fd)\n"
            "    except OSError: continue\n"
            "    fds.append(fd)\n"
            "print(json.dumps({'name':__name__,'file':__file__,'package':__package__,"
            "'spec':None if __spec__ is None else str(__spec__),'argv':sys.argv,'fds':fds},sort_keys=True))\n"
            f"raise SystemExit({exit_code})\n"
        )
        files = {
            "docs/release-safety/ceq1-wheelhouse-manifest.json": b"{}\n",
            "requirements-ceq1.in": b"pytest==9.1.1\n",
            "requirements-ceq1.lock": b"fixture\n",
            "requirements.lock": b"fixture\n",
            "scripts/bootstrap_ceq1_runtime.py": bootstrap.encode(),
            "scripts/build_ceq1_wheelhouse.py": b"# fixture\n",
            "scripts/run_ceq1_env.py": b"# fixture\n",
            "scripts/verify_ceq1_entry.pl": (root / "scripts/verify_ceq1_entry.pl").read_bytes(),
        }
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        reference = json.loads(
            (REPO_ROOT / "docs/release-safety/ceq1-input-manifest.json").read_text()
        )
        manifest = {
            "schemaVersion": 1,
            "algorithmVersion": "ceq1-input-v1",
            "files": {
                relative: {"sha256": _sha256(data), "size": len(data)}
                for relative, data in files.items()
            },
            "trees": reference["trees"],
            "portablePolicy": {
                "templateSha256": _sha256(policy.encode()),
                "placeholders": reference["portablePolicy"]["placeholders"],
            },
            "platformTrust": reference["platformTrust"],
        }
        input_path = root / "docs/release-safety/ceq1-input-manifest.json"
        input_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return root, _sha256(files["scripts/verify_ceq1_entry.pl"]), _sha256(input_path.read_bytes())

    def test_real_trampoline_restores_script_context_and_closes_inherited_fds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, verifier_hash, input_hash = self._synthetic_verifier_root(Path(tmp).resolve())
            inherited = os.open(root / "requirements.lock", os.O_RDONLY)
            os.set_inheritable(inherited, True)
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/perl",
                        "-MDigest::SHA=sha256_hex",
                        "-MFcntl=:DEFAULT",
                        "-e",
                        PERL_TRAMPOLINE,
                        "scripts/verify_ceq1_entry.pl",
                        verifier_hash,
                        "bootstrap",
                        input_hash,
                        "--",
                        "derive-review-candidate",
                    ],
                    cwd=root,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                    pass_fds=(inherited,),
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                os.close(inherited)
            self.assertEqual(0, result.returncode, result.stderr)
            receipt = json.loads(result.stdout)
            expected_file = str(root / "scripts/bootstrap_ceq1_runtime.py")
            self.assertEqual("__main__", receipt["name"])
            self.assertEqual(expected_file, receipt["file"])
            self.assertIsNone(receipt["package"])
            self.assertIsNone(receipt["spec"])
            self.assertEqual([expected_file, "derive-review-candidate"], receipt["argv"])
            self.assertEqual([0, 1, 2], receipt["fds"])

    def test_real_trampoline_rejects_target_drift_and_propagates_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, verifier_hash, input_hash = self._synthetic_verifier_root(
                Path(tmp).resolve(), exit_code=7
            )
            command = [
                "/usr/bin/perl",
                "-MDigest::SHA=sha256_hex",
                "-MFcntl=:DEFAULT",
                "-e",
                PERL_TRAMPOLINE,
                "scripts/verify_ceq1_entry.pl",
                verifier_hash,
                "bootstrap",
                input_hash,
                "--",
                "prepare",
            ]
            exited = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
            self.assertEqual(7, exited.returncode, exited.stderr)
            target = root / "scripts/bootstrap_ceq1_runtime.py"
            target.write_text(target.read_text() + "# drift\n", encoding="utf-8")
            blocked = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn("input file", blocked.stderr)

    def test_real_trampoline_rejects_symlinked_entries_and_manifest_hash_drift(self):
        for relative in (
            "scripts/verify_ceq1_entry.pl",
            "scripts/bootstrap_ceq1_runtime.py",
            "scripts/run_ceq1_env.py",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root, verifier_hash, input_hash = self._synthetic_verifier_root(
                    Path(tmp).resolve()
                )
                target = root / relative
                alternate = target.with_name(f"{target.name}.reviewed-copy")
                alternate.write_bytes(target.read_bytes())
                target.unlink()
                os.symlink(alternate.name, target)
                result = subprocess.run(
                    [
                        "/usr/bin/perl",
                        "-MDigest::SHA=sha256_hex",
                        "-MFcntl=:DEFAULT",
                        "-e",
                        PERL_TRAMPOLINE,
                        "scripts/verify_ceq1_entry.pl",
                        verifier_hash,
                        "bootstrap",
                        input_hash,
                        "--",
                        "prepare",
                    ],
                    cwd=root,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode)

        with tempfile.TemporaryDirectory() as tmp:
            root, verifier_hash, _input_hash = self._synthetic_verifier_root(
                Path(tmp).resolve()
            )
            result = subprocess.run(
                [
                    "/usr/bin/perl",
                    "-MDigest::SHA=sha256_hex",
                    "-MFcntl=:DEFAULT",
                    "-e",
                    PERL_TRAMPOLINE,
                    "scripts/verify_ceq1_entry.pl",
                    verifier_hash,
                    "bootstrap",
                    "0" * 64,
                    "--",
                    "prepare",
                ],
                cwd=root,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)

    def test_real_run_verifier_rejects_output_manifest_hash_drift_before_wrapper(self):
        verifier = REPO_ROOT / "scripts/verify_ceq1_entry.pl"
        input_manifest = REPO_ROOT / "docs/release-safety/ceq1-input-manifest.json"
        result = subprocess.run(
            [
                "/usr/bin/perl",
                "-MDigest::SHA=sha256_hex",
                "-MFcntl=:DEFAULT",
                "-e",
                PERL_TRAMPOLINE,
                "scripts/verify_ceq1_entry.pl",
                _sha256(verifier.read_bytes()),
                "run",
                _sha256(input_manifest.read_bytes()),
                "0" * 64,
                "--",
                "./.ceq1-venv/python/bin/python3.12",
                "-I",
                "-S",
                "-B",
                "scripts/run_ceq1_env.py",
                "--inspect-runtime",
            ],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("environmentKeys", result.stdout)

    def test_plan_canonical_commands_have_no_direct_bootstrap_or_wrapper_bypass(self):
        plan = (
            REPO_ROOT
            / "docs/superpowers/plans/2026-08-13-ceq1-conversation-extraction-qualification.md"
        ).read_text(encoding="utf-8")
        lines = plan.splitlines()
        wrapper_lines = [
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("./.ceq1-venv/python/bin/python3.12 ")
            and "scripts/run_ceq1_env.py" in line
        ]
        self.assertEqual(13, len(wrapper_lines))
        for index in wrapper_lines:
            command_prefix = "\n".join(lines[max(0, index - 12) : index + 1])
            self.assertIn("/usr/bin/perl -MDigest::SHA=sha256_hex", command_prefix)
            self.assertIn("scripts/verify_ceq1_entry.pl", command_prefix)
            self.assertIn(" run ", command_prefix)
        self.assertEqual(14, plan.count("/usr/bin/perl -MDigest::SHA=sha256_hex"))
        self.assertNotRegex(
            plan,
            r"python(?:3(?:\.12)?)?\s+scripts/bootstrap_ceq1_runtime\.py\s+"
            r"(?:prepare|derive-review-candidate)",
        )


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
            root = Path(tmp).resolve()
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
                root = Path(tmp).resolve()
                source, package = _mini_distribution(root)
                if mutation == "payload":
                    (source / "demo_pkg/__init__.py").write_bytes(b"VALUE = 8\n")
                elif mutation == "extra":
                    (source / "unexpected.py").write_text("bad = True\n")
                    (source / "unexpected.py").chmod(0o644)
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
                    (source / "empty-directory").chmod(0o755)
                with self.assertRaises((ValueError, OSError)):
                    self.builder.inspect_source(source, package)

    def test_finished_wheel_payload_and_record_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
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
                    changed.chmod(0o644)
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
                source, package = _mini_distribution(Path(tmp).resolve())
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
            cache = Path(tmp).resolve()
            cache.chmod(0o755)
            package = json.loads(json.dumps(manifest["packages"][0]))
            archive = cache / "archive-v0" / package["archiveId"]
            archive.mkdir(parents=True)
            link = cache / "wheels-v6/pypi" / package["normalizedName"] / (
                f"{package['version']}-py3-none-any"
            )
            link.parent.mkdir(parents=True)
            os.symlink(archive, link)
            _set_fixture_modes(cache)
            self.assertEqual(
                archive,
                self.builder.resolve_cache_source(cache, package),
            )
            link.unlink()
            os.symlink(cache / "archive-v0/wrong", link)
            with self.assertRaises(ValueError):
                self.builder.resolve_cache_source(cache, package)

    def test_every_source_and_cache_component_is_opened_without_following_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            real_root = root / "real"
            source, package = _mini_distribution(real_root)
            alias = root / "archive-alias"
            os.symlink(real_root / "archive-v0", alias)
            with self.assertRaises((ValueError, OSError)):
                self.builder.inspect_source(alias / source.name, package)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cache = root / "cache"
            cache.mkdir(mode=0o755)
            archive = cache / "archive-v0" / "mini-archive"
            archive.mkdir(parents=True)
            real_wheels = cache / "real-wheels"
            package_dir = real_wheels / "pypi/demo_pkg"
            package_dir.mkdir(parents=True)
            os.symlink(real_wheels, cache / "wheels-v6")
            link = package_dir / "1.2.3-py3-none-any"
            os.symlink(archive, link)
            package = _mini_distribution(root / "fixture")[1]
            _set_fixture_modes(cache)
            with self.assertRaises((ValueError, OSError)):
                self.builder.resolve_cache_source(cache, package)

    def test_source_and_cache_tree_modes_are_canonical_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, package = _mini_distribution(root)
            source.chmod(0o700)
            with self.assertRaises(ValueError):
                self.builder.inspect_source(source, package)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, package = _mini_distribution(root)
            original_read = self.builder._read_no_follow
            changed = False

            def read_then_mutate(*args, **kwargs):
                nonlocal changed
                data = original_read(*args, **kwargs)
                if not changed:
                    (source / "demo_pkg").chmod(0o700)
                    changed = True
                return data

            with mock.patch.object(self.builder, "_read_no_follow", side_effect=read_then_mutate):
                with self.assertRaises(ValueError):
                    self.builder.inspect_source(source, package)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cache = root / "cache"
            cache.mkdir(mode=0o755)
            archive = cache / "archive-v0" / "mini-archive"
            archive.mkdir(parents=True)
            package_dir = cache / "wheels-v6/pypi/demo_pkg"
            package_dir.mkdir(parents=True)
            os.symlink(archive, package_dir / "1.2.3-py3-none-any")
            _set_fixture_modes(cache)
            (cache / "wheels-v6/pypi").chmod(0o700)
            package = _mini_distribution(root / "fixture")[1]
            with self.assertRaises(ValueError):
                self.builder.resolve_cache_source(cache, package)

    def test_cache_link_identity_is_rechecked_after_readlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cache = root / "cache"
            cache.mkdir(mode=0o755)
            archive = cache / "archive-v0" / "mini-archive"
            archive.mkdir(parents=True)
            wrong = cache / "archive-v0" / "wrong-archive"
            wrong.mkdir()
            package_dir = cache / "wheels-v6/pypi/demo_pkg"
            package_dir.mkdir(parents=True)
            link = package_dir / "1.2.3-py3-none-any"
            os.symlink(archive, link)
            package = _mini_distribution(root / "fixture")[1]
            _set_fixture_modes(cache)
            real_readlink = os.readlink
            swapped = False

            def racing_readlink(*args, **kwargs):
                nonlocal swapped
                value = real_readlink(*args, **kwargs)
                if not swapped:
                    link.unlink()
                    os.symlink(wrong, link)
                    swapped = True
                return value

            with mock.patch.object(self.builder.os, "readlink", side_effect=racing_readlink):
                with self.assertRaises((ValueError, OSError)):
                    self.builder.resolve_cache_source(cache, package)

    def test_package_and_dist_info_identities_are_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, package = _mini_distribution(Path(tmp).resolve())
            package["name"] = "../not-a-package"
            with self.assertRaises(ValueError):
                self.builder.inspect_source(source, package)

        with tempfile.TemporaryDirectory() as tmp:
            source, package = _mini_distribution(Path(tmp).resolve())
            old_dist_info = source / "demo_pkg-1.2.3.dist-info"
            new_dist_info = source / "alternate.dist-info"
            old_dist_info.rename(new_dist_info)
            record_path = "alternate.dist-info/RECORD"
            payload_paths = sorted(
                path.relative_to(source).as_posix()
                for path in source.rglob("*")
                if path.is_file() and path.name != "RECORD"
            )
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            for relative in payload_paths:
                data = (source / relative).read_bytes()
                writer.writerow((relative, f"sha256={_record_hash(data)}", str(len(data))))
            writer.writerow((record_path, "", ""))
            record = output.getvalue().encode("utf-8")
            (source / record_path).write_bytes(record)
            package["recordSha256"] = _sha256(record)
            package["members"] = [
                {
                    "path": relative,
                    "size": (source / relative).stat().st_size,
                    "sha256": _sha256((source / relative).read_bytes()),
                }
                for relative in sorted((*payload_paths, record_path))
            ]
            package["wheel"]["memberCount"] = len(package["members"])
            with self.assertRaises(ValueError):
                self.builder.inspect_source(source, package)

    def test_finished_wheel_is_sealed_and_path_identity_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, package = _mini_distribution(root)
            wheel = self.builder.build_wheel(source, package, root / "wheel", verify_output=False)
            self.assertEqual(0o444, stat.S_IMODE(wheel.stat().st_mode))
            symlink = root / "wheel-link.whl"
            os.symlink(wheel, symlink)
            with self.assertRaises((ValueError, OSError)):
                self.builder.inspect_wheel(symlink)
            hardlink = root / "wheel-hardlink.whl"
            os.link(wheel, hardlink)
            with self.assertRaises(ValueError):
                self.builder.inspect_wheel(hardlink)

    def test_finished_zip_rejects_prefix_suffix_and_noncanonical_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, package = _mini_distribution(root)
            wheel = self.builder.build_wheel(source, package, root / "wheel", verify_output=False)
            original = wheel.read_bytes()
            for label, data in (("prefix", b"junk" + original), ("suffix", original + b"junk")):
                with self.subTest(label=label):
                    changed = root / f"{label}.whl"
                    changed.write_bytes(data)
                    changed.chmod(0o644)
                    with self.assertRaises(ValueError):
                        self.builder.inspect_wheel(changed)

            changed = root / "low-external-attributes.whl"
            with zipfile.ZipFile(wheel, "r") as source_archive:
                members = [
                    (info, source_archive.read(info.filename))
                    for info in source_archive.infolist()
                ]
            with zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_STORED) as output_archive:
                for info, data in members:
                    info.external_attr |= 1
                    output_archive.writestr(info, data)
            changed.chmod(0o644)
            with self.assertRaises(ValueError):
                self.builder.inspect_wheel(changed)

    def test_finished_wheel_must_match_the_manifested_member_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, package = _mini_distribution(root)
            wheel = self.builder.build_wheel(source, package, root / "wheel", verify_output=False)
            with zipfile.ZipFile(wheel, "r") as archive:
                infos = archive.infolist()
                payloads = {info.filename: archive.read(info.filename) for info in infos}
            target = "demo_pkg/__init__.py"
            payloads[target] = b"VALUE = 99\n"
            record_name = "demo_pkg-1.2.3.dist-info/RECORD"
            record_rows = list(csv.reader(payloads[record_name].decode("utf-8").splitlines()))
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\n")
            for member, digest, size_text in record_rows:
                if member == target:
                    data = payloads[target]
                    digest = f"sha256={_record_hash(data)}"
                    size_text = str(len(data))
                writer.writerow((member, digest, size_text))
            payloads[record_name] = output.getvalue().encode("utf-8")
            changed = root / "self-consistent-but-wrong.whl"
            with zipfile.ZipFile(changed, "w", compression=zipfile.ZIP_STORED) as archive:
                for info in infos:
                    archive.writestr(info, payloads[info.filename])
            changed.chmod(0o644)
            with self.assertRaises(ValueError):
                self.builder.inspect_wheel(changed, expected_members=package["members"])


class Ceq1BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = _load_script("bootstrap_ceq1_runtime.py")
        cls.wrapper = _load_script("run_ceq1_env.py")

    def test_os_injected_text_encoding_forms_are_closed(self):
        for module in (self.bootstrap, self.wrapper):
            with self.subTest(module=module.__name__, form="ordinary"):
                module.validate_cf_user_text_encoding("0x1F5:0x0:0x0")
            with self.subTest(module=module.__name__, form="seatbelt"):
                module.validate_cf_user_text_encoding("0x1F5:0:0")
            for invalid in ("1F5:0:0", "0x1F5:1:0", "0x1F5:0", "garbage"):
                with self.subTest(module=module.__name__, invalid=invalid):
                    with self.assertRaises(Exception):
                        module.validate_cf_user_text_encoding(invalid)

    def test_bootstrap_profile_is_portable_and_command_vectors_are_closed(self):
        template = self.bootstrap.BOOTSTRAP_SEATBELT_TEMPLATE
        self.assertIn("(deny default)", template)
        self.assertIn('(literal "/")', template)
        self.assertIn("(deny network*)", template)
        self.assertIn('(literal "/dev/null")', template)
        self.assertIn('(subpath "{JDK_ROOT}")', template)
        self.assertIn('(literal "{FIRESTORE_JAR}")', template)
        self.assertIn('(literal "{RELOCATION}/python/bin/python3.12")', template)
        self.assertNotIn('(literal "{RELOCATION}/venv/bin/python")', template)
        for required_input in ("INPUT_MANIFEST", "VERIFIER_SCRIPT", "WRAPPER_SCRIPT"):
            self.assertIn(f'(literal "{{{required_input}}}")', template)
        self.assertIn("{READ_ANCESTOR_RULES}", template)
        self.assertNotIn("/Users/", template)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            relocation = root / ".ceq1-runtime/bootstrap-contract/relocation-proof"
            profile, receipt = self.bootstrap.render_bootstrap_profile(
                root, relocation_path=relocation
            )
            self.assertIn(str(root), profile)
            self.assertEqual(str(root), receipt["parameters"]["REPO"])
            ancestors = receipt["parameters"]["READ_ANCESTOR_RULES"]
            authorized = {
                Path(value)
                for key, value in receipt["parameters"].items()
                if key != "READ_ANCESTOR_RULES"
            }
            expected_ancestors = sorted(
                {
                    str(parent)
                    for path in authorized
                    for parent in path.parents
                    if parent != Path("/")
                }
            )
            self.assertEqual(expected_ancestors, ancestors)
            self.assertIn(str(root.parent), ancestors)
            self.assertNotIn(str(root / ".git"), ancestors)
            recursive_roots = {
                receipt["parameters"][key]
                for key in ("PYTHON_SOURCE", "UV_CACHE", "JDK_ROOT", "RUNTIME", "BUNDLE")
            }
            for ancestor in ancestors:
                self.assertIn(f'(literal "{ancestor}")', profile)
                if ancestor not in recursive_roots:
                    self.assertNotIn(f'(subpath "{ancestor}")', profile)
            self.assertNotIn(f'(subpath "{root}")', profile)
            self.assertNotIn(f'(subpath "{Path.home()}")', profile)
            self.assertEqual(
                str(relocation),
                receipt["parameters"]["RELOCATION"],
            )
            self.assertRegex(receipt["renderedSha256"], r"^[0-9a-f]{64}$")
            alternate_bundle = root.parent / "alternate-ceq1-bundle/nested"
            _, alternate_receipt = self.bootstrap.render_bootstrap_profile(
                root,
                bundle_path=alternate_bundle,
                relocation_path=relocation,
            )
            self.assertNotEqual(
                receipt["parameters"]["READ_ANCESTOR_RULES"],
                alternate_receipt["parameters"]["READ_ANCESTOR_RULES"],
            )
            self.assertNotEqual(receipt["renderedSha256"], alternate_receipt["renderedSha256"])
            self.assertNotEqual(receipt["parameterDigest"], alternate_receipt["parameterDigest"])
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
        self.assertTrue(
            all(str(self.bootstrap.UV_CACHE) not in item for command in commands for item in command)
        )
        for builder in (commands[1], commands[2]):
            cache_root = builder[builder.index("--cache-root") + 1]
            self.assertTrue(cache_root.endswith("/.ceq1-runtime/bootstrap/uv-cache"))
            self.assertNotEqual(str(self.bootstrap.UV_CACHE), cache_root)
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
        expected_profile, _ = self.bootstrap.render_bootstrap_profile(
            REPO_ROOT, relocation_path=bootstrap / "relocation-proof"
        )
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
            mock.patch.object(self.bootstrap, "_close_non_stdio_fds") as close_fds,
            mock.patch.object(self.bootstrap.os, "execve", side_effect=sentinel),
        ):
            with self.assertRaisesRegex(RuntimeError, "execve boundary reached"):
                self.bootstrap._launch_contained("prepare")
        hash_file.assert_not_called()
        cache_receipt.assert_not_called()
        ensure_root.assert_not_called()
        close_fds.assert_called_once_with()

    def test_outer_closes_inherited_descriptors_before_contained_exec(self):
        sentinel = RuntimeError("execve boundary reached")
        with (
            mock.patch.object(self.bootstrap.secrets, "token_hex", return_value="b" * 32),
            mock.patch.object(self.bootstrap, "_close_non_stdio_fds") as close_fds,
            mock.patch.object(self.bootstrap.os, "execve", side_effect=sentinel),
        ):
            with self.assertRaisesRegex(RuntimeError, "execve boundary reached"):
                self.bootstrap._launch_contained("prepare")
        close_fds.assert_called_once_with()

    def test_direct_contained_invocation_refuses_before_mutation_and_sandbox_inherits(self):
        bootstrap_name = "bootstrap-direct-contained-red"
        bootstrap_path = REPO_ROOT / ".ceq1-runtime" / bootstrap_name
        self.assertFalse(bootstrap_path.exists())
        profile, _ = self.bootstrap.render_bootstrap_profile(
            REPO_ROOT, relocation_path=bootstrap_path / "relocation-proof"
        )
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

        ancestor_probe = (
            "import errno,importlib.util,os,sys;"
            f"p={str(REPO_ROOT / 'scripts/bootstrap_ceq1_runtime.py')!r};"
            "s=importlib.util.spec_from_file_location('ceq1_ancestor_probe',p);"
            "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;"
            "s.loader.exec_module(m);"
            "targets=(m._repo_root()/'.ceq1-runtime',m.PINNED_PYTHON_ROOT,"
            "m.UV_CACHE,m.JDK_ROOT);"
            "fds=[m._open_directory_chain(x) for x in targets];"
            "[os.close(fd) for fd in fds];"
            "denied=(m._repo_root()/'.git',m._repo_root()/'.coderabbit.yaml');"
            "exec(\"for x in denied:\\n try: os.open(x,os.O_RDONLY)\\n except OSError as e:\\n  assert e.errno in (errno.EPERM,errno.EACCES)\\n else: raise RuntimeError('unlisted read allowed')\")"
        )
        opened = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                str(PINNED_PYTHON),
                "-I",
                "-S",
                "-B",
                "-c",
                ancestor_probe,
            ],
            cwd=REPO_ROOT,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, opened.returncode, opened.stderr)

    def test_task_cache_clone_rebases_only_internal_absolute_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source-cache"
            destination = root / "task-cache"
            archive = source / "archive-v0/exact-archive"
            archive.mkdir(parents=True)
            (archive / "payload.py").write_text("VALUE = 1\n", encoding="utf-8")
            link = source / "wheels-v6/pypi/demo/1.0-py3-none-any"
            link.parent.mkdir(parents=True)
            os.symlink(archive, link)
            pinned_link = source / "builds-v0/pinned-python"
            pinned_link.parent.mkdir(parents=True)
            os.symlink(self.bootstrap.PINNED_PYTHON, pinned_link)
            before_identity = self.bootstrap.cache_identity_receipt(source)
            expected_topology = self.bootstrap.cache_logical_receipt(source)
            previous_umask = os.umask(0o077)
            try:
                receipt = self.bootstrap.clone_cache_to_task(
                    source,
                    destination,
                    before_identity,
                    expected_topology,
                )
            finally:
                os.umask(previous_umask)
            self.assertEqual(before_identity, self.bootstrap.cache_identity_receipt(source))
            self.assertEqual(
                destination / "archive-v0/exact-archive",
                Path(os.readlink(destination / link.relative_to(source))),
            )
            self.assertEqual(
                self.bootstrap.PINNED_PYTHON,
                Path(os.readlink(destination / pinned_link.relative_to(source))),
            )
            self.assertEqual(
                stat.S_IMODE(pinned_link.lstat().st_mode),
                stat.S_IMODE((destination / pinned_link.relative_to(source)).lstat().st_mode),
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

    def test_task_cache_clone_byte_fallback_preserves_manifested_mode(self):
        class FallbackClone:
            argtypes = None
            restype = None

            def __call__(self, *_args):
                ctypes.set_errno(errno.ENOTSUP)
                return -1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source-cache"
            destination = root / "task-cache"
            archive = source / "archive-v0/exact-archive"
            archive.mkdir(parents=True)
            payload = archive / "payload.py"
            payload.write_text("VALUE = 1\n", encoding="utf-8")
            _set_fixture_modes(source, regular_files=True)
            before_identity = self.bootstrap.cache_identity_receipt(source)
            expected_topology = self.bootstrap.cache_logical_receipt(source)
            previous_umask = os.umask(0o077)
            try:
                with mock.patch.object(
                    self.bootstrap.ctypes,
                    "CDLL",
                    return_value=SimpleNamespace(fclonefileat=FallbackClone()),
                ):
                    self.bootstrap.clone_cache_to_task(
                        source,
                        destination,
                        before_identity,
                        expected_topology,
                    )
            finally:
                os.umask(previous_umask)
            self.assertEqual(
                0o644,
                stat.S_IMODE((destination / payload.relative_to(source)).stat().st_mode),
            )

    def test_task_cache_clone_refuses_preexisting_or_symlinked_destination(self):
        for kind in ("directory", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = root / "source-cache"
                source.mkdir()
                _set_fixture_modes(source)
                destination = root / "task-cache"
                if kind == "directory":
                    destination.mkdir()
                else:
                    outside = root / "outside"
                    outside.mkdir()
                    os.symlink(outside, destination)
                with self.assertRaisesRegex(
                    self.bootstrap.BootstrapBlocked,
                    "destination already exists",
                ):
                    self.bootstrap.clone_cache_to_task(
                        source,
                        destination,
                        self.bootstrap.cache_identity_receipt(source),
                        self.bootstrap.cache_logical_receipt(source),
                    )

    def test_real_denied_python_cache_link_is_blocked_by_canonical_seatbelt(self):
        with tempfile.TemporaryDirectory(
            dir=REPO_ROOT / ".ceq1-runtime",
            prefix="denied-link-test-",
        ) as tmp:
            cache = Path(tmp) / "cache"
            link = cache / "archive-v0/example/bin/python"
            link.parent.mkdir(parents=True)
            denied_target = (
                self.bootstrap.UV_PYTHON_STORE
                / "cpython-3.11.15-macos-aarch64-none/bin/python3.11"
            )
            self.assertTrue(denied_target.is_file())
            os.symlink(denied_target, link)
            profile, _ = self.bootstrap.render_bootstrap_profile(REPO_ROOT)
            probe = (
                "import importlib.util,sys;"
                f"p={str(REPO_ROOT / 'scripts/bootstrap_ceq1_runtime.py')!r};"
                "s=importlib.util.spec_from_file_location('ceq1_denied_link_probe',p);"
                "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;"
                f"s.loader.exec_module(m);m.prove_denied_cache_link_targets(m.Path({str(cache)!r}))"
            )
            result = subprocess.run(
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
            self.assertEqual(0, result.returncode, result.stderr)

    def test_task_cache_clone_rejects_swap_restore_before_open(self):
        for kind in ("directory", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source = root / "source-cache"
                source.mkdir()
                victim = source / ("victim" if kind == "directory" else "victim.txt")
                replacement = root / "replacement"
                if kind == "directory":
                    victim.mkdir()
                    replacement.mkdir()
                else:
                    victim.write_text("reviewed\n", encoding="utf-8")
                    replacement.write_text("substituted\n", encoding="utf-8")
                _set_fixture_modes(source, regular_files=True)
                _set_fixture_modes(replacement, regular_files=True) if replacement.is_dir() else replacement.chmod(0o644)
                original_info = victim.lstat()
                expected_identity = self.bootstrap.cache_identity_receipt(source)
                expected_logical = self.bootstrap.cache_logical_receipt(source)
                expected_entries = self.bootstrap._cache_identity_index(source)
                real_open = os.open
                swapped = False
                saved = root / "reviewed-saved"

                def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal swapped
                    if not swapped and dir_fd is not None and os.fspath(path) == victim.name:
                        victim.rename(saved)
                        replacement.rename(victim)
                        try:
                            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                        finally:
                            victim.rename(replacement)
                            saved.rename(victim)
                        swapped = True
                        return descriptor
                    return real_open(path, flags, mode, dir_fd=dir_fd)

                with (
                    mock.patch.object(
                        self.bootstrap,
                        "_cache_identity_index",
                        return_value=expected_entries,
                    ),
                    mock.patch.object(self.bootstrap.os, "open", side_effect=racing_open),
                ):
                    with self.assertRaisesRegex(
                        self.bootstrap.BootstrapBlocked,
                        "raced|identity drift",
                    ):
                        self.bootstrap.clone_cache_to_task(
                            source,
                            root / "task-cache",
                            expected_identity,
                            expected_logical,
                        )
                self.assertTrue(swapped)
                self.assertEqual(original_info.st_ino, victim.lstat().st_ino)
                self.assertFalse(saved.exists())

    def test_external_python_alias_is_pinned_only_by_manifest_bound_realpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = root / "python-store"
            pinned = store / "cpython-3.12.13-test"
            executable = pinned / "bin/python3.12"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"manifest-bound-python")
            alias = store / "cpython-3.12-test"
            os.symlink(pinned, alias)
            textual_target = str(alias / "bin/python3.12")
            with (
                mock.patch.object(self.bootstrap, "UV_PYTHON_STORE", store),
                mock.patch.object(self.bootstrap, "PINNED_PYTHON_ROOT", pinned),
            ):
                self.assertEqual(
                    "@PINNED_PYTHON/bin/python3.12",
                    self.bootstrap._logical_cache_target(
                        root / "cache",
                        "archive-v0/example/bin/python",
                        textual_target,
                    ),
                )
                alias.unlink()
                other = store / "cpython-3.12.99-unreviewed"
                (other / "bin").mkdir(parents=True)
                (other / "bin/python3.12").write_bytes(b"unreviewed-python")
                os.symlink(other, alias)
                self.assertEqual(
                    "@DENIED_EXTERNAL_PYTHON/cpython-3.12-test/bin/python3.12",
                    self.bootstrap._logical_cache_target(
                        root / "cache",
                        "archive-v0/example/bin/python",
                        textual_target,
                    ),
                )
                outside = root / "outside-alias"
                os.symlink(pinned, outside)
                with self.assertRaises(self.bootstrap.BootstrapBlocked):
                    self.bootstrap._logical_cache_target(
                        root / "cache",
                        "archive-v0/example/bin/python",
                        str(outside / "bin/python3.12"),
                    )
                dangling = pinned / "missing/bin/python3.12"
                self.assertEqual(
                    "@DENIED_EXTERNAL_PYTHON/"
                    "missing/bin/python3.12",
                    self.bootstrap._logical_cache_target(
                        root / "cache",
                        "archive-v0/example/bin/python",
                        str(dangling),
                    ),
                )
                escaped_root = root / "escaped-python"
                (escaped_root / "bin").mkdir(parents=True)
                (escaped_root / "bin/python3.12").write_bytes(b"outside-pinned-tree")
                os.symlink(escaped_root, pinned / "escape")
                self.assertEqual(
                    "@DENIED_EXTERNAL_PYTHON/"
                    "escape/bin/python3.12",
                    self.bootstrap._logical_cache_target(
                        root / "cache",
                        "archive-v0/example/bin/python",
                        str(pinned / "escape/bin/python3.12"),
                    ),
                )

    def test_source_cache_logical_classification_is_rechecked_after_uv_work(self):
        expected = {"algorithmVersion": "ceq1-cache-logical-v1", "logicalDigest": "a"}
        with mock.patch.object(
            self.bootstrap,
            "cache_logical_receipt",
            side_effect=(expected, {**expected, "logicalDigest": "b"}),
        ):
            initial = self.bootstrap.cache_logical_receipt(Path("/unused"))
            with self.assertRaisesRegex(
                self.bootstrap.BootstrapBlocked,
                "logical topology changed",
            ):
                self.bootstrap.require_source_cache_logical_stability(
                    Path("/unused"), initial
                )

        source = inspect.getsource(self.bootstrap._prepare)
        last_uv = source.index("_run(commands[5]")
        logical_recheck = source.index("require_source_cache_logical_stability")
        normalize = source.index("normalize_uv_install_metadata")
        self.assertLess(last_uv, logical_recheck)
        self.assertLess(logical_recheck, normalize)

    def test_review_candidate_artifacts_stay_below_review_candidate_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap = Path(tmp).resolve() / "bootstrap-exact"
            lock, toolchain = self.bootstrap.review_candidate_artifact_paths(bootstrap)
            review_root = bootstrap / "review-candidate"
            self.assertEqual(review_root / "requirements-ceq1.lock", lock)
            self.assertEqual(review_root / "ceq1-toolchain-manifest.json", toolchain)

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

    def test_runtime_target_lifecycle_is_private_and_nonpreexisting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidate = root / "bootstrap/review-candidate/runtime"
            (root / "bootstrap").mkdir()
            self.bootstrap.create_runtime_target("derive-review-candidate", candidate)
            self.assertEqual(0o700, stat.S_IMODE(candidate.parent.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(candidate.stat().st_mode))
            with self.assertRaises(self.bootstrap.BootstrapBlocked):
                self.bootstrap.create_runtime_target("derive-review-candidate", candidate)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical = root / ".ceq1-venv"
            self.bootstrap.create_runtime_target("prepare", canonical)
            self.assertEqual(0o700, stat.S_IMODE(canonical.stat().st_mode))

    def test_runtime_tree_copy_preserves_exact_modes_under_private_umask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            source.mkdir(mode=0o755)
            regular = source / "regular.py"
            regular.write_bytes(b"VALUE = 1\n")
            regular.chmod(0o644)
            executable = source / "tool"
            executable.write_bytes(b"#!/bin/sh\n")
            executable.chmod(0o755)
            link = source / "python"
            os.symlink("tool", link)
            os.chmod(link, 0o755, follow_symlinks=False)
            destination = root / "destination"
            previous_umask = os.umask(0o077)
            try:
                expected = self.bootstrap._safe_tree_entries(source, destination)
            finally:
                os.umask(previous_umask)
            self.assertEqual(expected, self.bootstrap._safe_tree_entries(destination))

    def test_tree_receipt_rejects_relative_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inside = root / "inside"
            inside.mkdir()
            os.symlink("../outside", inside / "escape")
            with self.assertRaises(self.bootstrap.BootstrapBlocked):
                self.bootstrap.tree_receipt(inside)

    def test_canonical_wheelhouse_is_created_directly_then_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
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
                "inputManifestSha256",
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
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("toolchain recomputation executed a probe"),
        ):
            self.bootstrap.validate_committed_toolchain_without_probes(REPO_ROOT, manifest)
        call_sites = [
            line.strip()
            for line in inspect.getsource(self.bootstrap).splitlines()
            if "validate_committed_toolchain(root" in line
        ]
        self.assertEqual(
            [
                "def validate_committed_toolchain(root: Path, manifest: dict[str, object]) -> None:",
                "validate_committed_toolchain(root, committed)",
            ],
            call_sites,
        )

    def test_tree_receipt_detects_byte_mode_path_and_symlink_target_mutation(self):
        for mutation in ("byte", "mode", "path", "symlink-target"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve() / "runtime"
                root.mkdir()
                first = root / "first.py"
                second = root / "second.py"
                first.write_bytes(b"FIRST = 1\n")
                second.write_bytes(b"SECOND = 2\n")
                first.chmod(0o644)
                second.chmod(0o644)
                link = root / "python"
                os.symlink("first.py", link)
                baseline = self.bootstrap.tree_receipt(root)
                if mutation == "byte":
                    first.write_bytes(b"FIRST = 9\n")
                    first.chmod(0o644)
                elif mutation == "mode":
                    first.chmod(0o600)
                elif mutation == "path":
                    first.rename(root / "renamed.py")
                else:
                    link.unlink()
                    os.symlink("second.py", link)
                self.assertNotEqual(baseline, self.bootstrap.tree_receipt(root))

    def test_static_input_validation_hashes_builder_before_import(self):
        events: list[str] = []
        original_hash = self.bootstrap._sha256_file

        def hash_file(path):
            if Path(path).name == "build_ceq1_wheelhouse.py":
                events.append("builder-hash")
            return original_hash(path)

        original_load = self.bootstrap._load_builder

        def load_builder(root):
            events.append("builder-import")
            return original_load(root)

        with (
            mock.patch.object(self.bootstrap, "_sha256_file", side_effect=hash_file),
            mock.patch.object(self.bootstrap, "_load_builder", side_effect=load_builder),
        ):
            self.bootstrap._validate_static_inputs(REPO_ROOT)
        self.assertLess(events.index("builder-hash"), events.index("builder-import"))

    def _minimal_installed_bundle(self, root: Path) -> tuple[Path, object]:
        bundle = root / "bundle"
        venv = bundle / "venv"
        site = venv / "lib/python3.12/site-packages"
        dist_info = site / "demo-1.0.dist-info"
        dist_info.mkdir(parents=True)
        payload = site / "demo.py"
        payload.write_bytes(b"VALUE = 1\n")
        payload_digest = base64.urlsafe_b64encode(
            hashlib.sha256(payload.read_bytes()).digest()
        ).rstrip(b"=").decode("ascii")
        record_relative = "demo-1.0.dist-info/RECORD"
        record = dist_info / "RECORD"
        record.write_text(
            f"demo.py,sha256={payload_digest},{payload.stat().st_size}\n"
            f"{record_relative},,\n",
            encoding="utf-8",
        )
        files = ("demo.py", record_relative)
        distribution = SimpleNamespace(
            metadata={"Name": "demo"},
            version="1.0",
            _path=dist_info,
            files=files,
        )
        regular_scaffold = {
            ".gitignore",
            ".lock",
            "CACHEDIR.TAG",
            "bin/activate",
            "bin/activate.bat",
            "bin/activate.fish",
            "bin/activate.nu",
            "bin/activate.ps1",
            "bin/activate_this.py",
            "bin/deactivate.bat",
            "bin/pydoc.bat",
            "lib/python3.12/site-packages/_virtualenv.pth",
            "lib/python3.12/site-packages/_virtualenv.py",
            "pyvenv.cfg",
        }
        for relative in regular_scaffold:
            target = venv / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
        python = venv / "bin/python"
        python.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../../python/bin/python3.12", python)
        os.symlink("python", venv / "bin/python3")
        os.symlink("python", venv / "bin/python3.12")
        return bundle, distribution

    def test_installed_environment_rejects_scaffold_link_and_empty_directory_drift(self):
        def requirements(path):
            return {"demo": "1.0"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bundle, distribution = self._minimal_installed_bundle(root)
            python = bundle / "venv/bin/python"
            python.unlink()
            os.symlink("/private/etc/passwd", python)
            with (
                mock.patch.object(self.bootstrap, "parse_pinned_requirements", side_effect=requirements),
                mock.patch.object(
                    self.bootstrap.importlib.metadata,
                    "distributions",
                    return_value=[distribution],
                ),
            ):
                with self.assertRaises(self.bootstrap.BootstrapBlocked):
                    self.bootstrap.validate_installed_environment(bundle, root)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            bundle, distribution = self._minimal_installed_bundle(root)
            (bundle / "venv/lib/python3.12/site-packages/undeclared-empty").mkdir()
            with (
                mock.patch.object(self.bootstrap, "parse_pinned_requirements", side_effect=requirements),
                mock.patch.object(
                    self.bootstrap.importlib.metadata,
                    "distributions",
                    return_value=[distribution],
                ),
            ):
                with self.assertRaises(self.bootstrap.BootstrapBlocked):
                    self.bootstrap.validate_installed_environment(bundle, root)

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

    def test_direct_wrapper_validation_never_executes_a_probe(self):
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("direct wrapper executed a subprocess probe"),
        ):
            self.wrapper._validate_sealed_boundary(REPO_ROOT)

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

        sentinel = RuntimeError("wrapper execve reached")
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        try:
            with (
                mock.patch.object(self.wrapper, "require_isolated_flags"),
                mock.patch.object(
                    self.wrapper.sys,
                    "flags",
                    SimpleNamespace(isolated=1, no_site=1, dont_write_bytecode=1),
                ),
                mock.patch.object(self.wrapper, "_close_non_stdio_fds") as close_fds,
                mock.patch.object(self.wrapper.os, "execve", side_effect=sentinel) as execve,
            ):
                with self.assertRaisesRegex(RuntimeError, "wrapper execve reached"):
                    self.wrapper.main(["--inspect-runtime"])
        finally:
            os.umask(previous_umask)
        close_fds.assert_called_once_with()
        self.assertEqual(REPO_ROOT / ".ceq1-venv/python/bin/python3.12", Path(execve.call_args.args[0]))
        self.assertEqual(set(self.wrapper.CLOSED_ENV_KEYS), set(execve.call_args.args[2]))

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
        self.assertNotIn("execBoundary", receipt)
        self.assertEqual(sorted(self.wrapper.CLOSED_ENV_KEYS), receipt["environmentKeys"])
        root = str((REPO_ROOT / ".ceq1-venv").resolve())
        for key in ("executable", "prefix", "basePrefix", "stdlib", "platstdlib"):
            self.assertTrue(receipt[key].startswith(root), (key, receipt[key]))
        self.assertTrue(all(path.startswith(root) for path in receipt["loadedPaths"]))


class Ceq1ClosedContractTests(unittest.TestCase):
    def _effect(self) -> EffectAttempt:
        return EffectAttempt.from_mapping(
            {
                "operationId": "op-001",
                "attemptOrdinal": 0,
                "effectClass": "SHEET_WRITE",
                "method": "set",
                "target": "row:target-001",
                "outcome": "BLOCKED",
                "succeeded": False,
            }
        )

    def _fact(self) -> FactRecord:
        return FactRecord.from_mapping(
            {
                "field": "opex",
                "value": 4,
                "unit": "USD_PER_SF",
                "basis": "ANNUAL",
                "sourceMessageId": "msg-003",
                "sourceSpan": [11, 17],
                "targetPropertyId": "property-001",
                "targetSuiteId": "suite-100",
                "freshness": "CURRENT",
                "evidenceRef": "segment-msg-003-01",
            }
        )

    def _snapshot(self, reverse: bool = False) -> StateSnapshot:
        pairs = (
            ("targetRow", {"id": "target-001", "status": "OPEN"}),
            ("siblingRows", []),
            ("formulas", []),
            ("threads", []),
            ("conversations", []),
            ("messages", []),
            ("indexes", {}),
            ("reviews", []),
            ("terminalActions", []),
            ("pendingResponses", []),
            ("audit", []),
            ("outbox", []),
            ("sends", []),
            ("followups", []),
            ("providerLedger", []),
            ("effectLedger", []),
            ("actionOrder", []),
        )
        return StateSnapshot.from_mapping({"state": dict(reversed(pairs) if reverse else pairs)})

    def _score(
        self,
        result: EvidenceResult,
        *,
        scenario_id: str = "CEQ-MEM-01",
        variant_id: str = "explicit-decline",
        promotion_class: PromotionClass = PromotionClass.REQUIRED,
        non_claims: list[str] | None = None,
        layer: Layer = Layer.L2,
    ) -> ScoreRecord:
        before = self._snapshot()
        return ScoreRecord.from_mapping(
            {
                "scenarioId": scenario_id,
                "variantId": variant_id,
                "layer": layer.value,
                "promotionClass": promotion_class.value,
                "evidenceResult": result.value,
                "failureReasons": [] if result is EvidenceResult.VERIFIED else ["FACT_PROVENANCE_MISSING"],
                "diff": {"facts": []},
                "stateBeforeDigest": before.digest,
                "stateAfterDigest": before.digest,
                "stateReplayDigest": before.digest,
                "nonClaims": non_claims or ["NO_LIVE_PROVIDER_EVIDENCE"],
            }
        )

    def test_closed_enums_have_only_approved_values(self):
        self.assertEqual(["L1", "L2", "L3"], [item.value for item in Layer])
        self.assertEqual(
            ["BLOCKED", "INSTRUMENT_FAILURE", "FAIL", "UNVERIFIED", "PASS_OFFLINE"],
            [item.value for item in GateVerdict],
        )
        self.assertEqual(
            ["VERIFIED", "REFUTED", "UNVERIFIED"],
            [item.value for item in EvidenceResult],
        )
        self.assertEqual(["required", "diagnostic"], [item.value for item in PromotionClass])
        self.assertEqual(
            ["CE-Q1B-TEXT", "CE-Q1B-VOICE"], [item.value for item in FutureGate]
        )

    def test_canonical_json_and_hash_are_order_independent_and_finite(self):
        from enum import Enum

        class ForeignEnum(str, Enum):
            VALUE = "VALUE"

        left = {"z": [3, 2, 1], "a": {"two": 2, "one": 1}}
        right = {"a": {"one": 1, "two": 2}, "z": [3, 2, 1]}
        expected = b'{"a":{"one":1,"two":2},"z":[3,2,1]}'
        self.assertEqual(expected, canonical_json(left))
        self.assertEqual(expected, canonical_json(right))
        self.assertEqual(sha256_json(left), sha256_json(right))
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                canonical_json({"value": value})
        with self.assertRaises(TypeError):
            canonical_json(GateVerdict.PASS_OFFLINE)
        class ForeignRecord:
            def to_mapping(self):
                return {"forged": True}

        for value in ((1, 2), ForeignEnum.VALUE, ForeignRecord()):
            with self.subTest(raw_type=type(value).__name__), self.assertRaises(TypeError):
                canonical_json(value)
        self.assertNotEqual(sha256_json({"values": [1, 2]}), sha256_json({"values": [2, 1]}))
        self.assertNotEqual(sha256_json(True), sha256_json(1))
        self.assertNotEqual(sha256_json(1), sha256_json(1.0))

    def test_every_record_rejects_missing_and_extra_keys(self):
        valid_records = (
            (EffectAttempt, self._effect().to_mapping()),
            (FactRecord, self._fact().to_mapping()),
            (StateSnapshot, self._snapshot().to_mapping()),
            (
                ExecutionResult,
                {
                    "scenarioId": "CEQ-MEM-01",
                    "variantId": "explicit-decline",
                    "layer": "L2",
                    "sourceIdentity": "source-001",
                    "facts": [self._fact().to_mapping()],
                    "events": [
                        {"kind": "PROPOSAL", "ordinal": 0, "payload": {"safe": True}}
                    ],
                    "draft": None,
                    "stateBefore": self._snapshot().to_mapping(),
                    "stateAfter": self._snapshot(reverse=True).to_mapping(),
                    "effectLedger": [self._effect().to_mapping()],
                    "providerLedger": [],
                    "runtimeProjectionDigest": "1" * 64,
                    "nonClaims": ["NO_LIVE_PROVIDER_EVIDENCE"],
                },
            ),
            (ScoreRecord, self._score(EvidenceResult.VERIFIED).to_mapping()),
            (
                NextGateEligibility,
                {
                    "gateId": "CE-Q1B-TEXT",
                    "ceq1aVerdict": "PASS_OFFLINE",
                    "eligible": True,
                    "blockingDiagnostics": [],
                    "satisfiedDiagnostics": [],
                    "nonClaims": ["NO_MODEL_CALL_AUTHORIZED", "SEPARATE_AUTHORIZATION_REQUIRED"],
                },
            ),
            (
                GateReport,
                GateReport.from_scores(
                    required_scores=(self._score(EvidenceResult.VERIFIED),),
                    diagnostic_scores=(),
                ).to_mapping(),
            ),
        )
        for record_type, mapping in valid_records:
            with self.subTest(record=record_type.__name__, case="extra"):
                with self.assertRaises(ValueError):
                    record_type.from_mapping({**mapping, "unexpected": True})
            with self.subTest(record=record_type.__name__, case="missing"):
                missing = dict(mapping)
                missing.pop(next(iter(missing)))
                with self.assertRaises(ValueError):
                    record_type.from_mapping(missing)

    def test_state_and_execution_digests_are_stable_and_records_are_deeply_frozen(self):
        before = self._snapshot()
        after = self._snapshot(reverse=True)
        self.assertEqual(before.digest, after.digest)
        result = ExecutionResult.from_mapping(
            {
                "scenarioId": "CEQ-MEM-01",
                "variantId": "explicit-decline",
                "layer": "L2",
                "sourceIdentity": "source-001",
                "facts": [self._fact().to_mapping()],
                "events": [
                    {"kind": "PROPOSAL", "ordinal": 0, "payload": {"safe": True}}
                ],
                "draft": {"plainBody": "Synthetic draft."},
                "stateBefore": before.to_mapping(),
                "stateAfter": after.to_mapping(),
                "effectLedger": [self._effect().to_mapping()],
                "providerLedger": [],
                "runtimeProjectionDigest": "1" * 64,
                "nonClaims": ["NOT_FINAL_DRAFT_UNVERIFIED", "NO_LIVE_PROVIDER_EVIDENCE"],
            }
        )
        wire = result.to_mapping()
        roundtrip = ExecutionResult.from_mapping(wire)
        self.assertEqual(wire, roundtrip.to_mapping())
        self.assertEqual(sha256_json(result), sha256_json(roundtrip))
        wire["facts"][0]["value"] = 99
        wire["events"][0]["kind"] = "MUTATED"
        self.assertEqual(4, result.facts[0].value)
        self.assertEqual("PROPOSAL", result.events[0].kind)
        with self.assertRaises(TypeError):
            result.draft["plainBody"] = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            result.nonClaims += ("changed",)

    def test_event_record_is_closed_and_ordinal_is_exact(self):
        event = EventRecord.from_mapping(
            {"kind": "PROPOSAL", "ordinal": 0, "payload": {"safe": True}}
        )
        self.assertEqual(
            {"kind": "PROPOSAL", "ordinal": 0, "payload": {"safe": True}},
            event.to_mapping(),
        )
        for key, value in (("ordinal", True), ("ordinal", -1), ("payload", [])):
            wire = event.to_mapping()
            wire[key] = value
            with self.subTest(key=key, value=value), self.assertRaises((TypeError, ValueError)):
                EventRecord.from_mapping(wire)

    def test_nested_json_and_scalar_fields_reject_ambiguous_or_unsafe_values(self):
        from decimal import Decimal

        class DictSubclass(dict):
            pass

        class ListSubclass(list):
            pass

        nested_cycle: list[object] = []
        nested_cycle.append(nested_cycle)
        invalid_json = (
            {"nested": [float("nan")]},
            {1: "non-string-key"},
            {"value": b"bytes"},
            {"value": {"set"}},
            {"value": Decimal("1.5")},
            {"value": nested_cycle},
            DictSubclass(value=1),
            {"value": ListSubclass([1])},
        )
        for value in invalid_json:
            with self.subTest(value_type=type(next(iter(value.values()))).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    canonical_json(value)
        with self.assertRaises(TypeError):
            EffectAttempt.from_mapping(DictSubclass(self._effect().to_mapping()))
        tuple_fact = self._fact().to_mapping()
        tuple_fact["value"] = (1, 2)
        with self.assertRaises(TypeError):
            FactRecord.from_mapping(tuple_fact)
        subclass_state = self._snapshot().to_mapping()
        subclass_state["state"]["messages"] = ListSubclass([])
        with self.assertRaises(TypeError):
            StateSnapshot.from_mapping(subclass_state)
        malformed_effect = self._effect().to_mapping()
        malformed_effect["attemptOrdinal"] = True
        with self.assertRaises(TypeError):
            EffectAttempt.from_mapping(malformed_effect)
        for key, value in (
            ("attemptOrdinal", -1),
            ("succeeded", 1),
            ("outcome", "UNKNOWN"),
        ):
            malformed_effect = self._effect().to_mapping()
            malformed_effect[key] = value
            with self.subTest(key=key), self.assertRaises((TypeError, ValueError)):
                EffectAttempt.from_mapping(malformed_effect)
        malformed_fact = self._fact().to_mapping()
        malformed_fact["sourceSpan"] = [11, 11]
        with self.assertRaises(ValueError):
            FactRecord.from_mapping(malformed_fact)
        nullable_fact = self._fact().to_mapping()
        for key in (
            "unit",
            "basis",
            "sourceMessageId",
            "sourceSpan",
            "targetPropertyId",
            "targetSuiteId",
            "freshness",
            "evidenceRef",
        ):
            nullable_fact[key] = None
        self.assertIsNone(FactRecord.from_mapping(nullable_fact).evidenceRef)

    def test_snapshot_requires_complete_surface_and_detaches_mutable_aliases(self):
        state = self._snapshot().to_mapping()["state"]
        original_digest = StateSnapshot.from_mapping({"state": state}).digest
        state["messages"].append({"id": "late-mutation"})
        snapshot = self._snapshot()
        alias = snapshot.to_mapping()["state"]
        detached = StateSnapshot.from_mapping({"state": alias})
        alias["messages"].append({"id": "mutated-after-construction"})
        self.assertEqual(snapshot.digest, detached.digest)
        self.assertNotEqual(original_digest, StateSnapshot.from_mapping({"state": state}).digest)
        for case in ("missing", "extra"):
            invalid = self._snapshot().to_mapping()["state"]
            if case == "missing":
                invalid.pop("actionOrder")
            else:
                invalid["unexpected"] = []
            with self.subTest(case=case), self.assertRaises(ValueError):
                StateSnapshot.from_mapping({"state": invalid})
        for key, invalid_value in (("targetRow", []), ("indexes", []), ("messages", {})):
            invalid = self._snapshot().to_mapping()["state"]
            invalid[key] = invalid_value
            with self.subTest(key=key), self.assertRaises(TypeError):
                StateSnapshot.from_mapping({"state": invalid})

    def test_gate_verdict_precedence_is_closed(self):
        self.assertEqual(
            GateVerdict.BLOCKED,
            classify_gate(execution_started=False, prerequisite_missing=True),
        )
        self.assertEqual(
            GateVerdict.INSTRUMENT_FAILURE,
            classify_gate(instrument_faults=["guard_identity"], required_refutations=["wrong_value"]),
        )
        self.assertEqual(
            GateVerdict.FAIL,
            classify_gate(
                required_refutations=["wrong_value"],
                missing_required_evidence=["fact_provenance"],
            ),
        )
        self.assertEqual(
            GateVerdict.UNVERIFIED,
            classify_gate(missing_required_evidence=["fact_provenance"]),
        )
        self.assertEqual(GateVerdict.PASS_OFFLINE, classify_gate())
        with self.assertRaises(TypeError):
            classify_gate(False)
        with self.assertRaises(ValueError):
            classify_gate(prerequisite_missing=True, execution_started=True)
        with self.assertRaises(ValueError):
            classify_gate(execution_started=False)
        with self.assertRaises(TypeError):
            classify_gate(prerequisite_missing=True, instrument_faults=[1])
        with self.assertRaises(TypeError):
            classify_gate(execution_started=1)
        with self.assertRaises(TypeError):
            classify_gate(instrument_faults="guard_identity")
        with self.assertRaises(ValueError):
            classify_gate(instrument_faults=["same", "same"])
        with self.assertRaises(ValueError):
            classify_gate(execution_started=False, prerequisite_missing=True, instrument_faults=["late"])

    def test_diagnostic_unverified_does_not_downgrade_hard_gate(self):
        required = self._score(EvidenceResult.VERIFIED)
        diagnostic = self._score(
            EvidenceResult.UNVERIFIED,
            scenario_id="VOICE-LAUNCH",
            variant_id="launch",
            promotion_class=PromotionClass.DIAGNOSTIC,
            non_claims=["UNVERIFIED_NO_SHARED_FINALIZER"],
            layer=Layer.L1,
        )
        report = GateReport.from_scores(
            required_scores=(required,),
            diagnostic_scores=(diagnostic,),
        )
        self.assertIs(GateVerdict.PASS_OFFLINE, report.verdict)
        eligibility = {item.gateId: item for item in report.nextGateEligibility}
        self.assertEqual({"CE-Q1B-TEXT", "CE-Q1B-VOICE"}, set(eligibility))
        self.assertTrue(eligibility["CE-Q1B-TEXT"].eligible)
        self.assertFalse(eligibility["CE-Q1B-VOICE"].eligible)
        observed = next(
            item
            for item in eligibility["CE-Q1B-VOICE"].blockingDiagnostics
            if item.scenarioId == "VOICE-LAUNCH"
        )
        self.assertIn("UNVERIFIED_NO_SHARED_FINALIZER", observed.nonClaims)

    def test_score_and_report_reject_reason_and_promotion_laundering(self):
        verified = self._score(EvidenceResult.VERIFIED).to_mapping()
        verified["failureReasons"] = ["IMPOSSIBLE_REASON"]
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(verified)
        refuted = self._score(EvidenceResult.REFUTED).to_mapping()
        refuted["failureReasons"] = []
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(refuted)
        unverified = self._score(EvidenceResult.UNVERIFIED).to_mapping()
        unverified["nonClaims"] = []
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(unverified)
        verified_nonclaim = self._score(EvidenceResult.VERIFIED).to_mapping()
        verified_nonclaim.update(
            {
                "scenarioId": "VOICE-LAUNCH",
                "variantId": "launch",
                "promotionClass": "diagnostic",
                "layer": "L1",
                "nonClaims": ["UNVERIFIED_NO_SHARED_FINALIZER"],
            }
        )
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(verified_nonclaim)
        invalid_digest = self._score(EvidenceResult.VERIFIED).to_mapping()
        invalid_digest["stateReplayDigest"] = "A" * 64
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(invalid_digest)
        missing_replay = self._score(EvidenceResult.VERIFIED).to_mapping()
        missing_replay["stateReplayDigest"] = None
        with self.assertRaises(ValueError):
            ScoreRecord.from_mapping(missing_replay)

        required = self._score(EvidenceResult.VERIFIED)
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(), diagnostic_scores=(required,))
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(required, required), diagnostic_scores=())
        diagnostic = self._score(
            EvidenceResult.VERIFIED,
            promotion_class=PromotionClass.DIAGNOSTIC,
        )
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(diagnostic,), diagnostic_scores=())
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(required,), diagnostic_scores=(diagnostic,))

    def test_required_unknown_and_refuted_scores_reduce_hard_gate(self):
        for result, expected in (
            (EvidenceResult.UNVERIFIED, GateVerdict.UNVERIFIED),
            (EvidenceResult.REFUTED, GateVerdict.FAIL),
        ):
            with self.subTest(result=result.value):
                report = GateReport.from_scores(
                    required_scores=(self._score(result),), diagnostic_scores=()
                )
                self.assertIs(expected, report.verdict)
        diagnostic = self._score(
            EvidenceResult.REFUTED,
            promotion_class=PromotionClass.DIAGNOSTIC,
        )
        report = GateReport.from_scores(required_scores=(), diagnostic_scores=(diagnostic,))
        self.assertIs(GateVerdict.INSTRUMENT_FAILURE, report.verdict)
        self.assertEqual(
            ("DIAGNOSTIC_CONTRACT_MISMATCH:CEQ-MEM-01/explicit-decline/L2",),
            report.instrumentFaults,
        )

    def test_gate_report_refuses_vacuous_or_incomplete_score_partition(self):
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(), diagnostic_scores=())
        required = self._score(EvidenceResult.VERIFIED)
        diagnostic_wire = required.to_mapping()
        diagnostic_wire["scenarioId"] = "CEQ-PDF-01"
        diagnostic_wire["variantId"] = "image-only-explicitly-unverified"
        diagnostic_wire["promotionClass"] = "diagnostic"
        diagnostic = ScoreRecord.from_mapping(diagnostic_wire)
        report = GateReport.from_scores(
            required_scores=(required,), diagnostic_scores=(diagnostic,)
        )
        forged = report.to_mapping()
        forged["requiredScores"], forged["diagnosticScores"] = (
            forged["diagnosticScores"],
            forged["requiredScores"],
        )
        with self.assertRaises(ValueError):
            GateReport.from_mapping(forged)
        with self.assertRaises(ValueError):
            GateReport.from_scores(required_scores=(), diagnostic_scores=(), instrument_faults=())
        blocked = GateReport.from_scores(
            required_scores=(),
            diagnostic_scores=(),
            execution_started=False,
            missing_prerequisites=("EMULATOR_MISSING",),
        )
        self.assertIs(GateVerdict.BLOCKED, blocked.verdict)
        for score_kind in ("required", "diagnostic"):
            with self.subTest(score_kind=score_kind), self.assertRaises(ValueError):
                GateReport.from_scores(
                    required_scores=(required,) if score_kind == "required" else (),
                    diagnostic_scores=(diagnostic,) if score_kind == "diagnostic" else (),
                    execution_started=False,
                    missing_prerequisites=("EMULATOR_MISSING",),
                )
        instrument_failure = GateReport.from_scores(
            required_scores=(),
            diagnostic_scores=(),
            instrument_faults=("GUARD_IDENTITY",),
        )
        self.assertIs(GateVerdict.INSTRUMENT_FAILURE, instrument_failure.verdict)
        with self.assertRaises(ValueError):
            GateReport.from_scores(
                required_scores=(required,),
                diagnostic_scores=(),
                instrument_faults=("same", "same"),
            )

    def test_standalone_next_gate_projection_is_semantically_closed(self):
        report = GateReport.from_scores(
            required_scores=(self._score(EvidenceResult.VERIFIED),),
            diagnostic_scores=(),
        )
        text, voice = report.nextGateEligibility
        for mutation in ("text-blocker", "voice-verified", "voice-resolution"):
            wire = (text if mutation == "text-blocker" else voice).to_mapping()
            blocker = voice.to_mapping()["blockingDiagnostics"][0]
            if mutation == "text-blocker":
                wire["blockingDiagnostics"] = [blocker]
                wire["eligible"] = False
            elif mutation == "voice-verified":
                blocker["observedResult"] = "VERIFIED"
                wire["blockingDiagnostics"][0] = blocker
            else:
                blocker["requiredResolution"] = "ARBITRARY_RESOLUTION"
                wire["blockingDiagnostics"][0] = blocker
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                NextGateEligibility.from_mapping(wire)
        pass_with_refutation = voice.to_mapping()
        pass_with_refutation["ceq1aVerdict"] = "PASS_OFFLINE"
        pass_with_refutation["blockingDiagnostics"][0]["observedResult"] = "REFUTED"
        with self.assertRaises(ValueError):
            NextGateEligibility.from_mapping(pass_with_refutation)
        forged_voice = voice.to_mapping()
        forged_voice["blockingDiagnostics"] = []
        forged_voice["satisfiedDiagnostics"] = []
        forged_voice["eligible"] = True
        with self.assertRaises(ValueError):
            NextGateEligibility.from_mapping(forged_voice)
        all_satisfied = GateReport.from_scores(
            required_scores=(self._score(EvidenceResult.VERIFIED),),
            diagnostic_scores=tuple(
                self._score(
                    EvidenceResult.VERIFIED,
                    scenario_id=scenario_id,
                    variant_id=variant_id,
                    promotion_class=PromotionClass.DIAGNOSTIC,
                    layer=Layer.L1,
                )
                for scenario_id, variant_id in (
                    ("VOICE-CONTINUATION", "continuation"),
                    ("VOICE-CORRECTION-CLOSE", "correction-close"),
                    ("VOICE-FOLLOWUP", "followup"),
                    ("VOICE-LAUNCH", "launch"),
                    ("VOICE-MISSING", "missing-field"),
                )
            ),
        ).nextGateEligibility[1].to_mapping()
        all_satisfied["satisfiedDiagnostics"][0]["nonClaims"] = [
            "UNVERIFIED_NO_SHARED_FINALIZER"
        ]
        with self.assertRaises(ValueError):
            NextGateEligibility.from_mapping(all_satisfied)

    def test_all_verified_voice_dependencies_make_voice_gate_eligible(self):
        dependencies = (
            ("VOICE-CONTINUATION", "continuation"),
            ("VOICE-CORRECTION-CLOSE", "correction-close"),
            ("VOICE-FOLLOWUP", "followup"),
            ("VOICE-LAUNCH", "launch"),
            ("VOICE-MISSING", "missing-field"),
        )
        diagnostics = tuple(
            self._score(
                EvidenceResult.VERIFIED,
                scenario_id=scenario_id,
                variant_id=variant_id,
                promotion_class=PromotionClass.DIAGNOSTIC,
                layer=Layer.L1,
            )
            for scenario_id, variant_id in dependencies
        )
        report = GateReport.from_scores(
            required_scores=(self._score(EvidenceResult.VERIFIED),),
            diagnostic_scores=diagnostics,
        )
        voice = next(
            item for item in report.nextGateEligibility if item.gateId is FutureGate.CE_Q1B_VOICE
        )
        self.assertTrue(voice.eligible)
        self.assertEqual((), voice.blockingDiagnostics)
        self.assertEqual(
            dependencies,
            tuple((item.scenarioId, item.variantId) for item in voice.satisfiedDiagnostics),
        )
        self.assertTrue(all(type(item) is SatisfiedDiagnostic for item in voice.satisfiedDiagnostics))

    def test_gate_report_recomputes_verdict_and_next_gate_projection(self):
        required = self._score(EvidenceResult.REFUTED)
        diagnostic_mapping = self._score(
            EvidenceResult.UNVERIFIED,
            scenario_id="VOICE-LAUNCH",
            variant_id="launch",
            promotion_class=PromotionClass.DIAGNOSTIC,
            non_claims=["UNVERIFIED_NO_SHARED_FINALIZER"],
            layer=Layer.L1,
        ).to_mapping()
        diagnostic = ScoreRecord.from_mapping(diagnostic_mapping)
        ocr_diagnostic = self._score(
            EvidenceResult.UNVERIFIED,
            scenario_id="CEQ-PDF-01",
            variant_id="image-only-explicitly-unverified",
            promotion_class=PromotionClass.DIAGNOSTIC,
            non_claims=["UNVERIFIED_NO_EFFECT_FREE_OCR"],
        )
        report = GateReport.from_scores(
            required_scores=(required,), diagnostic_scores=(ocr_diagnostic, diagnostic)
        )
        self.assertIs(GateVerdict.FAIL, report.verdict)
        self.assertEqual((ocr_diagnostic, diagnostic), report.diagnosticScores)
        eligibility = {item.gateId: item for item in report.nextGateEligibility}
        self.assertFalse(eligibility["CE-Q1B-TEXT"].eligible)
        self.assertFalse(eligibility["CE-Q1B-VOICE"].eligible)
        self.assertTrue(
            all(item.ceq1aVerdict is GateVerdict.FAIL for item in eligibility.values())
        )
        voice_blockers = eligibility["CE-Q1B-VOICE"].blockingDiagnostics
        self.assertEqual(5, len(voice_blockers))
        self.assertEqual(
            {
                "VOICE-LAUNCH/launch/L1",
                "VOICE-MISSING/missing-field/L1",
                "VOICE-CORRECTION-CLOSE/correction-close/L1",
                "VOICE-FOLLOWUP/followup/L1",
                "VOICE-CONTINUATION/continuation/L1",
            },
            {
                f"{item.scenarioId}/{item.variantId}/{item.layer.value}"
                for item in voice_blockers
            },
        )
        observed = next(item for item in voice_blockers if item.scenarioId == "VOICE-LAUNCH")
        self.assertIs(EvidenceResult.UNVERIFIED, observed.observedResult)
        self.assertEqual("SHARED_PRODUCTION_FINALIZER_REQUIRED", observed.requiredResolution)
        missing = [item for item in voice_blockers if item.scenarioId != "VOICE-LAUNCH"]
        self.assertTrue(
            all(item.nonClaims == ("MISSING_DIAGNOSTIC_EVIDENCE",) for item in missing)
        )
        self.assertNotIn(
            "UNVERIFIED_NO_EFFECT_FREE_OCR", eligibility["CE-Q1B-TEXT"].nonClaims
        )
        serialized = report.to_mapping()
        serialized["verdict"] = "PASS_OFFLINE"
        with self.assertRaises(ValueError):
            GateReport.from_mapping(serialized)
        serialized = report.to_mapping()
        serialized["nextGateEligibility"] = []
        with self.assertRaises(ValueError):
            GateReport.from_mapping(serialized)
        serialized = report.to_mapping()
        serialized["nextGateEligibility"].append(serialized["nextGateEligibility"][0])
        with self.assertRaises(ValueError):
            GateReport.from_mapping(serialized)


TASK3_SCENARIO_IDS = (
    "CEQ-LONG-01",
    "CEQ-MEM-01",
    "CEQ-TERM-01",
    "CEQ-TERM-02",
    "CEQ-SUITE-01",
    "CEQ-PDF-01",
    "CEQ-OPEX-01",
    "CEQ-OPEX-02",
    "CEQ-ALT-01",
    "CEQ-IN-09",
    "CEQ-IN-10",
    "CEQ-WRONG-01",
    "CEQ-OOO-01",
    "CEQ-AUDIENCE-01",
    "VOICE-LAUNCH",
    "VOICE-MISSING",
    "VOICE-CORRECTION-CLOSE",
    "VOICE-FOLLOWUP",
    "VOICE-CONTINUATION",
)

TASK3_PRIMARY_FAMILIES = {
    "CEQ-LONG-01": "chronology",
    "CEQ-MEM-01": "EXT-01",
    "CEQ-TERM-01": "EXT-05",
    "CEQ-TERM-02": "EXT-02",
    "CEQ-SUITE-01": "EXT-03",
    "CEQ-PDF-01": "PDF layout",
    "CEQ-OPEX-01": "EXT-04",
    "CEQ-OPEX-02": "EXT-04",
    "CEQ-ALT-01": "EXT-06",
    "CEQ-IN-09": "IN-09",
    "CEQ-IN-10": "IN-10",
    "CEQ-WRONG-01": "EXT-02",
    "CEQ-OOO-01": "autoresponse",
    "CEQ-AUDIENCE-01": "audience",
    "VOICE-LAUNCH": "voice",
    "VOICE-MISSING": "voice",
    "VOICE-CORRECTION-CLOSE": "voice",
    "VOICE-FOLLOWUP": "voice",
    "VOICE-CONTINUATION": "voice",
}

TASK3_PRIVACY_RULE_IDS = frozenset(
    {
        "CEQ_PRIV_ABSOLUTE_PATH",
        "CEQ_PRIV_FILE_URI",
        "CEQ_PRIV_PRODUCTION_ID",
        "CEQ_PRIV_RAW_MESSAGE_ID",
        "CEQ_PRIV_CREDENTIAL",
        "CEQ_PRIV_JSON_SECRET_FIELD",
        "CEQ_PRIV_CLOCK_RANGE",
        "CEQ_PRIV_NON_INVALID_MAILBOX",
        "CEQ_PRIV_UNDECLARED_IDENTITY",
        "CEQ_PRIV_OBFUSCATED_IDENTITY",
        "CEQ_PRIV_FORBIDDEN_TOKEN",
        "CEQ_PRIV_TREE_LINK",
        "CEQ_PRIV_TREE_SPECIAL",
        "CEQ_PRIV_OPAQUE_BINARY",
        "CEQ_PRIV_ARTIFACT_ID",
    }
)

TASK3_MATRIX = (
    ("known-filled", "CEQ-MEM-01", ("L1", "L2", "L3")),
    ("explicit-decline", "CEQ-MEM-01", ("L1", "L2", "L3")),
    ("correction-after-window", "CEQ-LONG-01", ("L1", "L2", "L3")),
    ("acknowledgement-not-question", "CEQ-MEM-01", ("L1", "L2", "L3")),
    ("fresh-target-terminal", "CEQ-TERM-01", ("L1", "L2", "L3")),
    ("stale-quoted-terminal", "CEQ-TERM-02", ("L1", "L2", "L3")),
    ("wrong-property-terminal", "CEQ-WRONG-01", ("L1", "L2", "L3")),
    ("addressless-terminal", "CEQ-TERM-02", ("L1", "L2", "L3")),
    ("ambiguous-terminal", "CEQ-TERM-02", ("L1", "L2", "L3")),
    ("same-address-two-suites", "CEQ-SUITE-01", ("L1", "L2", "L3")),
    ("mixed-property-pdf", "CEQ-PDF-01", ("L1", "L2", "L3")),
    ("mixed-suite-pdf", "CEQ-SUITE-01", ("L1", "L2", "L3")),
    ("exact-target-attachment", "CEQ-PDF-01", ("L1", "L2", "L3")),
    ("rent14-opex4", "CEQ-OPEX-01", ("L1", "L2", "L3")),
    ("monthly-annual", "CEQ-OPEX-01", ("L1", "L2", "L3")),
    ("latest-correction", "CEQ-OPEX-01", ("L1", "L2", "L3")),
    ("numeric-range", "CEQ-OPEX-01", ("L1", "L2", "L3")),
    ("digit-decoy", "CEQ-OPEX-01", ("L1", "L2", "L3")),
    ("unsupported-opex", "CEQ-OPEX-02", ("L1", "L2", "L3")),
    ("ordered-success", "CEQ-TERM-01", ("L2", "L3")),
    ("move-failure", "CEQ-TERM-01", ("L2", "L3")),
    ("comment-failure", "CEQ-TERM-01", ("L2", "L3")),
    ("highlight-failure", "CEQ-TERM-01", ("L2", "L3")),
    ("audit-write-failure", "CEQ-TERM-01", ("L2", "L3")),
    ("terminal-state-failure", "CEQ-TERM-01", ("L2", "L3")),
    ("column-beyond-z", "CEQ-TERM-01", ("L2", "L3")),
    ("retry-after-partial-attempt", "CEQ-TERM-01", ("L2", "L3")),
    ("viable-alternate", "CEQ-ALT-01", ("L1", "L2", "L3")),
    ("alternate-unavailable", "CEQ-ALT-01", ("L1", "L2", "L3")),
    ("two-alternates", "CEQ-ALT-01", ("L1", "L2", "L3")),
    ("same-event-replay", "CEQ-ALT-01", ("L1", "L2", "L3")),
    ("direct-broker-question", "CEQ-IN-09", ("L1", "L2", "L3")),
    ("confidential-identity-question", "CEQ-IN-09", ("L1", "L2", "L3")),
    ("question-plus-partial-specs", "CEQ-IN-09", ("L1", "L2", "L3")),
    ("unrelated-mail", "CEQ-IN-10", ("L2", "L3")),
    ("quoted-cre-nearmiss", "CEQ-IN-10", ("L2", "L3")),
    ("tracked-reply-nearmiss", "CEQ-IN-10", ("L2", "L3")),
    ("thirteen-message-window", "CEQ-LONG-01", ("L1", "L2", "L3")),
    ("delayed-inbound-order", "CEQ-LONG-01", ("L1", "L2", "L3")),
    ("pause-hold", "CEQ-LONG-01", ("L2", "L3")),
    ("monitored-resume", "CEQ-LONG-01", ("L2", "L3")),
    ("settled-replay", "CEQ-LONG-01", ("L2", "L3")),
    ("dated-ooo", "CEQ-OOO-01", ("L2", "L3")),
    ("generic-auto-ack", "CEQ-OOO-01", ("L2", "L3")),
    ("quoted-cre-ooo", "CEQ-OOO-01", ("L2", "L3")),
    ("copied-party-reply-all", "CEQ-AUDIENCE-01", ("L2", "L3")),
    ("display-name-ambiguity", "CEQ-AUDIENCE-01", ("L2", "L3")),
    ("wrong-tenant-signature-decoy", "CEQ-AUDIENCE-01", ("L2", "L3")),
    ("native-text-three-page", "CEQ-PDF-01", ("L1", "L2", "L3")),
    ("image-only-explicitly-unverified", "CEQ-PDF-01", ("L1",)),
    ("launch", "VOICE-LAUNCH", ("L1",)),
    ("missing-field", "VOICE-MISSING", ("L1",)),
    ("correction-close", "VOICE-CORRECTION-CLOSE", ("L1",)),
    ("followup", "VOICE-FOLLOWUP", ("L1",)),
    ("continuation", "VOICE-CONTINUATION", ("L1",)),
)

# Independent transcription of every reviewed Task 7 row. The first field is
# the variant-level reporting family; it is intentionally not derived from the
# public scenario's descriptive primary family because LONG/TERM/PDF scenarios
# participate in more than one pressure family.
TASK3_SEALED_METADATA = {
    "known-filled": ("EXT-01", "SAB-EXT01-01", "KNOWN_FACT_REASKED", "missing_field_reply", "required", "FAIL", ()),
    "explicit-decline": ("EXT-01", "SAB-EXT01-02", "DECLINED_FACT_REASKED", "missing_field_reply", "required", "FAIL", ()),
    "correction-after-window": ("EXT-01", "SAB-EXT01-03", "STALE_CORRECTION_WON", "correction_close_reply", "required", "FAIL", ()),
    "acknowledgement-not-question": ("EXT-01", "SAB-EXT01-04", "ACK_MISCLASSIFIED_AS_QUESTION", "missing_field_reply", "required", "FAIL", ()),
    "fresh-target-terminal": ("EXT-02", "SAB-EXT02-01", "CITED_TERMINAL_NOT_APPLIED", "terminal_reply", "required", "FAIL", ()),
    "stale-quoted-terminal": ("EXT-02", "SAB-EXT02-02", "QUOTED_ONLY_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    "wrong-property-terminal": ("EXT-02", "SAB-EXT02-03", "CROSS_ENTITY_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    "addressless-terminal": ("EXT-02", "SAB-EXT02-04", "UNCITED_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    "ambiguous-terminal": ("EXT-02", "SAB-EXT02-05", "AMBIGUOUS_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    "same-address-two-suites": ("EXT-03", "SAB-EXT03-01", "CROSS_SUITE_FACT_ACCEPTED", "review_no_reply", "required", "FAIL", ()),
    "mixed-property-pdf": ("EXT-03", "SAB-EXT03-02", "CROSS_PROPERTY_PDF_FACT_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    "mixed-suite-pdf": ("EXT-03", "SAB-EXT03-03", "CROSS_SUITE_PDF_FACT_ACCEPTED", "review_no_reply", "required", "FAIL", ()),
    "exact-target-attachment": ("EXT-03", "SAB-EXT03-04", "SUPPORTED_TARGET_FACT_DROPPED", "missing_field_reply", "required", "FAIL", ()),
    "rent14-opex4": ("EXT-04", "SAB-EXT04-01", "RENT_OPEX_CONFLATED", "missing_field_reply", "required", "FAIL", ()),
    "monthly-annual": ("EXT-04", "SAB-EXT04-02", "BASIS_CONVERSION_WRONG", "missing_field_reply", "required", "FAIL", ()),
    "latest-correction": ("EXT-04", "SAB-EXT04-03", "STALE_NUMERIC_VALUE_WON", "missing_field_reply", "required", "FAIL", ()),
    "numeric-range": ("EXT-04", "SAB-EXT04-04", "NUMERIC_RANGE_TRANSFORM_WRONG", "missing_field_reply", "required", "FAIL", ()),
    "digit-decoy": ("EXT-04", "SAB-EXT04-05", "DIGIT_DECOY_ACCEPTED", "missing_field_reply", "required", "FAIL", ()),
    "unsupported-opex": ("EXT-04", "SAB-EXT04-06", "INVENTED_OPEX_ACCEPTED", "missing_field_reply", "required", "FAIL", ()),
    "ordered-success": ("EXT-05", "SAB-EXT05-01", "TERMINAL_OPERATION_ORDER_WRONG", "terminal_reply", "required", "FAIL", ()),
    "move-failure": ("EXT-05", "SAB-EXT05-02", "MOVE_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    "comment-failure": ("EXT-05", "SAB-EXT05-03", "COMMENT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    "highlight-failure": ("EXT-05", "SAB-EXT05-04", "HIGHLIGHT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    "audit-write-failure": ("EXT-05", "SAB-EXT05-05", "AUDIT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    "terminal-state-failure": ("EXT-05", "SAB-EXT05-06", "FALSE_TERMINAL_COMPLETION", "terminal_reply", "required", "FAIL", ()),
    "column-beyond-z": ("EXT-05", "SAB-EXT05-07", "COMMENT_COLUMN_ADDRESS_TRUNCATED", "terminal_reply", "required", "FAIL", ()),
    "retry-after-partial-attempt": ("EXT-05", "SAB-EXT05-08", "PARTIAL_RETRY_DUPLICATED_EFFECT", "terminal_reply", "required", "FAIL", ()),
    "viable-alternate": ("EXT-06", "SAB-EXT06-01", "ALTERNATE_ACTION_MISSING", "alternate_reply", "required", "FAIL", ()),
    "alternate-unavailable": ("EXT-06", "SAB-EXT06-02", "UNAVAILABLE_ALTERNATE_ACTIONED", "terminal_reply", "required", "FAIL", ()),
    "two-alternates": ("EXT-06", "SAB-EXT06-03", "ALTERNATE_CARDINALITY_WRONG", "alternate_reply", "required", "FAIL", ()),
    "same-event-replay": ("EXT-06", "SAB-EXT06-04", "DUPLICATE_ALTERNATE_ACTION", "alternate_reply", "required", "FAIL", ()),
    "direct-broker-question": ("IN-09", "SAB-IN09-01", "UNSAFE_BROKER_QUESTION_ANSWERED", "review_no_reply", "required", "VERIFY", ()),
    "confidential-identity-question": ("IN-09", "SAB-IN09-02", "CONFIDENTIAL_IDENTITY_DISCLOSED", "review_no_reply", "required", "VERIFY", ()),
    "question-plus-partial-specs": ("IN-09", "SAB-IN09-03", "SAFE_FACTS_DROPPED_ON_REVIEW", "review_no_reply", "required", "FAIL", ()),
    "unrelated-mail": ("IN-10", "SAB-IN10-01", "UNTRACKED_MAIL_MUTATED_STATE", "no_reply", "required", "VERIFY", ()),
    "quoted-cre-nearmiss": ("IN-10", "SAB-IN10-02", "QUOTED_CRE_NEARMISS_PROCESSED", "no_reply", "required", "VERIFY", ()),
    "tracked-reply-nearmiss": ("IN-10", "SAB-IN10-03", "TRACKED_NEARMISS_PROCESSED", "no_reply", "required", "VERIFY", ()),
    "thirteen-message-window": ("chronology", "SAB-CHR-01", "HISTORY_WINDOW_BYPASSED", "correction_close_reply", "required", "FAIL", ()),
    "delayed-inbound-order": ("chronology", "SAB-CHR-02", "DELAYED_INBOUND_ORDER_WRONG", "missing_field_reply", "required", "FAIL", ()),
    "pause-hold": ("chronology", "SAB-CHR-03", "PAUSED_THREAD_CONTINUED", "no_reply", "required", "VERIFY", ()),
    "monitored-resume": ("chronology", "SAB-CHR-04", "UNSUPPORTED_RESUME", "monitored_continuation_reply", "required", "FAIL", ()),
    "settled-replay": ("chronology", "SAB-CHR-05", "SETTLED_REPLAY_STATE_DELTA", "no_reply", "required", "FAIL", ()),
    "dated-ooo": ("autoresponse", "SAB-AUTO-01", "OOO_EXTRACTED_OR_REPLIED", "no_reply", "required", "VERIFY", ()),
    "generic-auto-ack": ("autoresponse", "SAB-AUTO-02", "AUTOACK_EXTRACTED_OR_REPLIED", "no_reply", "required", "VERIFY", ()),
    "quoted-cre-ooo": ("autoresponse", "SAB-AUTO-03", "QUOTED_CRE_OOO_PROCESSED", "no_reply", "required", "VERIFY", ()),
    "copied-party-reply-all": ("audience", "SAB-AUD-01", "CC_DROPPED_OR_MISROUTED", "reply_all_draft", "required", "UNVERIFIED", ()),
    "display-name-ambiguity": ("audience", "SAB-AUD-02", "AMBIGUOUS_AUDIENCE_GUESSED", "reply_all_draft", "required", "UNVERIFIED", ()),
    "wrong-tenant-signature-decoy": ("audience", "SAB-AUD-03", "SIGNATURE_IDENTITY_DRIFT", "reply_all_draft", "required", "UNVERIFIED", ()),
    "native-text-three-page": ("PDF layout", "SAB-PDF-01", "NATIVE_PDF_PAGE_BINDING_WRONG", "review_no_reply", "required", "VERIFY", ()),
    "image-only-explicitly-unverified": ("PDF layout", "SAB-PDF-02", "OCR_CAPABILITY_OVERCLAIMED", "no_reply", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_EFFECT_FREE_OCR",)),
    "launch": ("voice", "SAB-VOICE-01", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "launch_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    "missing-field": ("voice", "SAB-VOICE-02", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "missing_field_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    "correction-close": ("voice", "SAB-VOICE-03", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "correction_close_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    "followup": ("voice", "SAB-VOICE-04", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "followup_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    "continuation": ("voice", "SAB-VOICE-05", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "continuation_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
}


class Ceq1ManifestPrivacyTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        from tests.ceq1 import manifest, privacy

        self.manifest = manifest
        self.privacy = privacy

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")

    def _provenance(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "syntheticTemplateVersion": "ceq1-synthetic-v1",
            "generationMethod": "newly_authored_synthetic_template",
            "rawCustomerSourcesAccessed": False,
            "fictionalPeople": ["Avery Example"],
            "fictionalProperties": ["100 Example Plaza"],
            "fictionalDomains": ["example.invalid"],
            "fictionalMailboxes": ["avery@example.invalid"],
            "syntheticClock": {
                "start": "2040-01-01T00:00:00Z",
                "end": "2040-12-31T23:59:59Z",
            },
            "scannerRules": [
                {"ruleId": rule_id, "sha256": digest}
                for rule_id, digest in sorted(self.privacy.SCANNER_RULE_HASHES.items())
            ],
            "scannerNonClaim": self.privacy.SCANNER_NONCLAIM,
            "independentReviewStatus": "pending",
            "independentReviewerRole": None,
            "reviewedArtifactSetSha256": None,
            "reviewedCommit": None,
        }

    def test_task3_validator_modules_are_product_free(self):
        for name in ("manifest.py", "privacy.py"):
            source = (REPO_ROOT / "tests/ceq1" / name).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=name)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(
                any(item == "email_automation" or item.startswith("email_automation.") for item in imports),
                name,
            )

    def test_privacy_rule_receipts_are_exact_and_recomputed_from_closed_specs(self):
        self.assertEqual(TASK3_PRIVACY_RULE_IDS, frozenset(self.privacy.SCANNER_RULE_SPECS))
        self.assertEqual(TASK3_PRIVACY_RULE_IDS, frozenset(self.privacy.SCANNER_RULE_HASHES))
        self.assertEqual(
            {
                rule_id: sha256_json(self.privacy.SCANNER_RULE_SPECS[rule_id])
                for rule_id in sorted(TASK3_PRIVACY_RULE_IDS)
            },
            self.privacy.SCANNER_RULE_HASHES,
        )

    def _fixture_contracts(self, root: Path):
        public_root = root / "public"
        input_root = public_root / "inputs"
        response_root = public_root / "responses"
        owner_root = root / "production-owners"
        oracle_root = root / "sealed-oracles"
        input_root.mkdir(parents=True)
        response_root.mkdir()
        owner_root.mkdir()
        oracle_root.mkdir()
        refs = {
            "schemaVersion": 1,
            "productionAncestor": "6caa8ec14cc525299cfb8ed13bdd219f35c4322b",
            "implementationBase": "b400ee5ad55ac75203da6a53730c4a134cad79e5",
        }
        scenarios = []
        expected_owner_paths = {}
        scenario_hashes = {}
        for index, scenario_id in enumerate(TASK3_SCENARIO_IDS):
            stem = scenario_id.lower()
            input_bytes = self._json_bytes(
                {
                    "scenarioId": scenario_id,
                    "senderName": "Avery Example",
                    "senderEmail": "avery@example.invalid",
                    "propertyAddress": "100 Example Plaza",
                    "receivedAt": "2040-02-03T04:05:06Z",
                }
            )
            response_bytes = self._json_bytes(
                {"scenarioId": scenario_id, "response": f"synthetic response {index}"}
            )
            owner_bytes = f"# synthetic owner {scenario_id}\nOWNER = 'ceq1'\n".encode("ascii")
            input_path = f"{stem}-input.json"
            response_path = f"{stem}-response.json"
            owner_path = f"{stem}.py"
            (input_root / input_path).write_bytes(input_bytes)
            (response_root / response_path).write_bytes(response_bytes)
            (owner_root / owner_path).write_bytes(owner_bytes)
            input_hash = _sha256(input_bytes)
            response_hash = _sha256(response_bytes)
            scenario_hashes[scenario_id] = (input_hash, response_hash)
            expected_owner_paths[scenario_id] = frozenset({owner_root / owner_path})
            scenarios.append(
                {
                    "id": scenario_id,
                    "family": TASK3_PRIMARY_FAMILIES[scenario_id],
                    "purpose": f"Synthetic pressure case {index}",
                    "provenanceLabel": "newly-authored-synthetic",
                    "inputBundle": input_path,
                    "inputHash": input_hash,
                    "responseBundle": response_path,
                    "responseHash": response_hash,
                    "ownerModuleHashes": {owner_path: _sha256(owner_bytes)},
                }
            )
        manifest = {**refs, "scenarios": scenarios}
        schedule_entries = []
        coverage_records = []
        for ordinal, (variant_id, scenario_id, layers) in enumerate(TASK3_MATRIX):
            input_hash, response_hash = scenario_hashes[scenario_id]
            oracle_bytes = self._json_bytes(
                {"scenarioId": scenario_id, "variantId": variant_id, "synthetic": True}
            )
            (oracle_root / f"{variant_id}.json").write_bytes(oracle_bytes)
            schedule_entries.append(
                {
                    "ordinal": ordinal,
                    "scenarioId": scenario_id,
                    "variantId": variant_id,
                    "layers": list(layers),
                    "inputHash": input_hash,
                    "responseHash": response_hash,
                }
            )
            (
                _variant_family,
                sabotage_id,
                _sabotage_reason,
                response_class,
                promotion_class,
                _baseline,
                non_claims,
            ) = TASK3_SEALED_METADATA[variant_id]
            coverage_records.append(
                {
                    "variantId": variant_id,
                    "scenarioId": scenario_id,
                    "layers": list(layers),
                    "responseClass": response_class,
                    "voiceEligibility": False,
                    "oracleHash": _sha256(oracle_bytes),
                    "sabotageId": sabotage_id,
                    "promotionClass": promotion_class,
                    "expectedVerdict": (
                        "UNVERIFIED" if promotion_class == "diagnostic" else "PASS_OFFLINE"
                    ),
                    "nonClaims": list(non_claims),
                }
            )
        schedule = {**refs, "entries": schedule_entries}
        coverage = {**refs, "records": coverage_records}
        return SimpleNamespace(
            public_root=public_root,
            input_root=input_root,
            response_root=response_root,
            owner_root=owner_root,
            oracle_root=oracle_root,
            manifest=manifest,
            schedule=schedule,
            coverage=coverage,
            provenance=self._provenance(),
            expected_owner_paths=expected_owner_paths,
        )

    def _validate(self, root: Path):
        fixture = self._fixture_contracts(root)
        controls = root / "controls"
        controls.mkdir()
        document_paths = {}
        for name in ("manifest", "schedule", "coverage", "provenance"):
            path = controls / f"{name}.json"
            path.write_bytes(self._json_bytes(getattr(fixture, name)))
            document_paths[name] = path
        return self.manifest.validate_fixture_contract_files(
            manifest_path=document_paths["manifest"],
            schedule_path=document_paths["schedule"],
            coverage_path=document_paths["coverage"],
            provenance_path=document_paths["provenance"],
            input_root=fixture.input_root,
            response_root=fixture.response_root,
            owner_root=fixture.owner_root,
            oracle_root=fixture.oracle_root,
            expected_owner_paths=fixture.expected_owner_paths,
        )

    def test_exact_mandatory_scenario_and_variant_sets_are_independent_constants(self):
        self.assertEqual(frozenset(TASK3_SCENARIO_IDS), self.manifest.MANDATORY_SCENARIO_IDS)
        self.assertEqual(
            frozenset(item[0] for item in TASK3_MATRIX),
            self.manifest.MANDATORY_VARIANT_IDS,
        )
        self.assertEqual(19, len(self.manifest.MANDATORY_SCENARIO_IDS))
        self.assertEqual(55, len(self.manifest.MANDATORY_VARIANT_IDS))
        self.assertEqual(TASK3_PRIMARY_FAMILIES, self.manifest.SCENARIO_PRIMARY_FAMILIES)

    def test_reviewed_matrix_freezes_every_row_and_variant_reporting_family(self):
        expected = []
        for ordinal, (variant_id, scenario_id, layers) in enumerate(TASK3_MATRIX):
            (
                variant_family,
                sabotage_id,
                sabotage_reason,
                response_class,
                promotion_class,
                baseline,
                non_claims,
            ) = TASK3_SEALED_METADATA[variant_id]
            expected.append(
                {
                    "ordinal": ordinal,
                    "variantFamily": variant_family,
                    "variantId": variant_id,
                    "scenarioId": scenario_id,
                    "layers": list(layers),
                    "sabotageId": sabotage_id,
                    "sabotageReason": sabotage_reason,
                    "responseClass": response_class,
                    "voiceEligibility": False,
                    "promotionClass": promotion_class,
                    "expectedVerdict": (
                        "UNVERIFIED" if promotion_class == "diagnostic" else "PASS_OFFLINE"
                    ),
                    "nonClaims": list(non_claims),
                    "baseline": baseline,
                }
            )
        self.assertEqual(expected, [row.to_mapping() for row in self.manifest.REVIEWED_VARIANT_MATRIX])
        self.assertEqual(55, len({row.sabotageId for row in self.manifest.REVIEWED_VARIANT_MATRIX}))
        self.assertEqual(
            6,
            sum(row.promotionClass == "diagnostic" for row in self.manifest.REVIEWED_VARIANT_MATRIX),
        )
        self.assertEqual(
            {1: 6, 2: 20, 3: 29},
            {
                width: sum(len(row.layers) == width for row in self.manifest.REVIEWED_VARIANT_MATRIX)
                for width in (1, 2, 3)
            },
        )
        by_variant = {row.variantId: row for row in self.manifest.REVIEWED_VARIANT_MATRIX}
        self.assertEqual("EXT-01", by_variant["correction-after-window"].variantFamily)
        self.assertEqual("chronology", by_variant["thirteen-message-window"].variantFamily)
        self.assertEqual("EXT-02", by_variant["fresh-target-terminal"].variantFamily)
        self.assertEqual("EXT-05", by_variant["ordered-success"].variantFamily)
        self.assertEqual("EXT-03", by_variant["mixed-property-pdf"].variantFamily)
        self.assertEqual("PDF layout", by_variant["native-text-three-page"].variantFamily)

    def test_schema_version_refs_and_integer_ordinals_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            mutations = []
            for surface in ("manifest", "schedule", "coverage"):
                for field, value in (
                    ("schemaVersion", True),
                    ("schemaVersion", 2),
                    ("productionAncestor", "0" * 40),
                    ("implementationBase", "f" * 40),
                ):
                    documents = {
                        name: json.loads(json.dumps(getattr(fixture, name)))
                        for name in ("manifest", "schedule", "coverage")
                    }
                    documents[surface][field] = value
                    mutations.append((f"{surface}:{field}:{value}", documents))
            ordinal_bool = {
                name: json.loads(json.dumps(getattr(fixture, name)))
                for name in ("manifest", "schedule", "coverage")
            }
            ordinal_bool["schedule"]["entries"][0]["ordinal"] = True
            mutations.append(("ordinal-bool", ordinal_bool))
            for label, documents in mutations:
                with self.subTest(label=label), self.assertRaises((TypeError, ValueError)):
                    self.manifest.validate_fixture_contracts(
                        documents["manifest"],
                        documents["schedule"],
                        documents["coverage"],
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        oracle_root=fixture.oracle_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )

    def test_strict_raw_json_loader_rejects_ambiguous_or_noncanonical_inputs(self):
        invalid = (
            ("duplicate", b'{"safe":1,"safe":2}'),
            ("nan", b'{"value":NaN}'),
            ("positive-infinity", b'{"value":Infinity}'),
            ("negative-infinity", b'{"value":-Infinity}'),
            ("bom", b'\xef\xbb\xbf{"safe":1}'),
            ("utf8", b'{"safe":"\xff"}'),
        )
        for label, payload in invalid:
            with self.subTest(label=label), self.assertRaises(ValueError) as raised:
                self.manifest.load_json_bytes(payload, artifact_id="control-json")
            self.assertEqual(("CEQ_JSON_INVALID", "control-json"), raised.exception.args)
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
        self.assertEqual(
            {"safe": [1, True, None]},
            self.manifest.load_json_bytes(
                b'{"safe":[1,true,null]}', artifact_id="control-json"
            ),
        )

    def test_canonical_file_entry_rejects_control_document_links_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture_contracts(root)
            controls = root / "controls"
            controls.mkdir()
            paths = {}
            for name in ("manifest", "schedule", "coverage", "provenance"):
                paths[name] = controls / f"{name}.json"
                paths[name].write_bytes(self._json_bytes(getattr(fixture, name)))

            real_manifest = controls / "real-manifest.json"
            paths["manifest"].rename(real_manifest)
            os.symlink(real_manifest.name, paths["manifest"])
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contract_files(
                    manifest_path=paths["manifest"],
                    schedule_path=paths["schedule"],
                    coverage_path=paths["coverage"],
                    provenance_path=paths["provenance"],
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                )
            paths["manifest"].unlink()
            paths["manifest"].write_bytes(
                real_manifest.read_bytes()[:-1] + b',"schemaVersion":1}'
            )
            with self.assertRaises(ValueError) as raised:
                self.manifest.validate_fixture_contract_files(
                    manifest_path=paths["manifest"],
                    schedule_path=paths["schedule"],
                    coverage_path=paths["coverage"],
                    provenance_path=paths["provenance"],
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                )
            self.assertEqual(("CEQ_JSON_INVALID", "public-manifest"), raised.exception.args)

    def test_escaped_or_nested_sealed_fields_never_enter_public_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            secret = hashlib.sha256(os.urandom(32)).hexdigest()
            base = self._json_bytes(fixture.manifest)
            raw = base[:-1] + b',"or\\u0061cleHash":"' + secret.encode("ascii") + b'"}'
            with self.assertRaises(ValueError) as raised:
                self.manifest.validate_public_manifest_bytes(
                    raw,
                    artifact_id="public-manifest",
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(secret, repr(raised.exception.args))
            for location in (
                ("top", lambda doc, field: doc.__setitem__(field, secret)),
                (
                    "nested",
                    lambda doc, field: doc["scenarios"][0]["ownerModuleHashes"].__setitem__(
                        field, secret
                    ),
                ),
            ):
                for field in (
                    "oracleHash",
                    "expectedVerdict",
                    "expectedState",
                    "sabotageId",
                    "promotionClass",
                    "nonClaims",
                    "baseline",
                    "variantFamily",
                    "sabotageReason",
                ):
                    changed = json.loads(json.dumps(fixture.manifest))
                    location[1](changed, field)
                    with self.subTest(location=location[0], field=field), self.assertRaises(ValueError) as leak:
                        self.manifest.validate_public_manifest(
                            changed,
                            input_root=fixture.input_root,
                            response_root=fixture.response_root,
                            owner_root=fixture.owner_root,
                            expected_owner_paths=fixture.expected_owner_paths,
                            provenance=fixture.provenance,
                        )
                    self.assertNotIn(secret, str(leak.exception))

    def test_valid_temporary_contracts_return_capability_separated_typed_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            validated_manifest, validated_schedule, validated_coverage = self._validate(
                Path(tmp)
            )
        self.assertIs(type(validated_manifest), self.manifest.ValidatedManifest)
        self.assertIs(
            type(validated_schedule), self.manifest.ValidatedExecutionSchedule
        )
        self.assertIs(type(validated_coverage), self.manifest.ValidatedCoverage)
        self.assertFalse(hasattr(validated_manifest, "oracleHash"))
        self.assertFalse(hasattr(validated_schedule, "coverage"))
        self.assertFalse(hasattr(validated_schedule, "oracle"))
        descriptors = self.manifest.emit_child_descriptors(validated_schedule)
        expected_order = [
            (scenario_id, variant_id, layer)
            for variant_id, scenario_id, layers in TASK3_MATRIX
            for layer in layers
        ]
        self.assertEqual(133, len(descriptors))
        self.assertEqual(
            expected_order,
            [
                (item["scenarioId"], item["variantId"], item["layer"])
                for item in descriptors
            ],
        )
        expected_keys = {
            "scenarioId",
            "variantId",
            "layer",
            "inputHash",
            "responseHash",
        }
        self.assertTrue(all(set(descriptor) == expected_keys for descriptor in descriptors))
        serialized = json.dumps(descriptors, sort_keys=True)
        for forbidden in (
            "expectedVerdict",
            "oracleHash",
            "sabotageId",
            "responseClass",
            "promotionClass",
            "nonClaims",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertIn("semantic", self.manifest.DESCRIPTOR_SEPARATION_NONCLAIM.lower())
        self.assertIn("Task 7", self.manifest.DESCRIPTOR_SEPARATION_NONCLAIM)
        with self.assertRaises(TypeError):
            self.manifest.emit_child_descriptors(validated_coverage)

    def test_public_manifest_and_schedule_schemas_are_recursively_closed(self):
        public_top = {
            "schemaVersion",
            "productionAncestor",
            "implementationBase",
            "scenarios",
        }
        scenario_keys = {
            "id",
            "family",
            "purpose",
            "provenanceLabel",
            "inputBundle",
            "inputHash",
            "responseBundle",
            "responseHash",
            "ownerModuleHashes",
        }
        schedule_top = {
            "schemaVersion",
            "productionAncestor",
            "implementationBase",
            "entries",
        }
        schedule_keys = {
            "ordinal",
            "scenarioId",
            "variantId",
            "layers",
            "inputHash",
            "responseHash",
        }
        coverage_keys = {
            "variantId",
            "scenarioId",
            "layers",
            "responseClass",
            "voiceEligibility",
            "oracleHash",
            "sabotageId",
            "promotionClass",
            "expectedVerdict",
            "nonClaims",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture_contracts(root)
            self.assertEqual(public_top, set(fixture.manifest))
            self.assertTrue(all(set(item) == scenario_keys for item in fixture.manifest["scenarios"]))
            self.assertEqual(schedule_top, set(fixture.schedule))
            self.assertTrue(all(set(item) == schedule_keys for item in fixture.schedule["entries"]))
            self.assertTrue(all(set(item) == coverage_keys for item in fixture.coverage["records"]))
            forbidden_fields = (
                "expectedVerdict",
                "oracleHash",
                "expectedState",
                "sabotageId",
            )
            for field in forbidden_fields:
                changed = json.loads(json.dumps(fixture.manifest))
                changed["scenarios"][0][field] = "forbidden"
                with self.subTest(surface="manifest", field=field), self.assertRaises(ValueError):
                    self.manifest.validate_public_manifest(
                        changed,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )
            schedule_forbidden = (
                "responseClass",
                "voiceEligibility",
                "oracleHash",
                "sabotageId",
                "expectedVerdict",
                "expectedOutcome",
                "promotionClass",
                "nonClaims",
            )
            for field in schedule_forbidden:
                changed = json.loads(json.dumps(fixture.schedule))
                changed["entries"][0][field] = "forbidden"
                with self.subTest(surface="schedule", field=field), self.assertRaises(ValueError):
                    self.manifest.validate_execution_schedule(changed)
            for field in ("skip", "filter", "xfail"):
                changed = json.loads(json.dumps(fixture.coverage))
                changed["records"][0][field] = True
                with self.subTest(surface="coverage", field=field), self.assertRaises(ValueError):
                    self.manifest.validate_coverage(
                        changed,
                        oracle_root=fixture.oracle_root,
                        provenance=fixture.provenance,
                    )

    def test_exact_sets_duplicates_row_order_layers_and_public_sealed_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture_contracts(root)
            manifest, schedule, coverage = fixture.manifest, fixture.schedule, fixture.coverage
            mutations = []
            missing_scenario = json.loads(json.dumps(manifest))
            missing_scenario["scenarios"].pop()
            mutations.append(("scenario-missing", missing_scenario, schedule, coverage))
            duplicate_scenario = json.loads(json.dumps(manifest))
            duplicate_scenario["scenarios"][-1] = duplicate_scenario["scenarios"][0]
            mutations.append(("scenario-duplicate", duplicate_scenario, schedule, coverage))
            wrong_family = json.loads(json.dumps(manifest))
            wrong_family["scenarios"][0]["family"] = "voice"
            mutations.append(("scenario-primary-family", wrong_family, schedule, coverage))
            missing_variant = json.loads(json.dumps(schedule))
            missing_variant["entries"].pop()
            mutations.append(("variant-missing", manifest, missing_variant, coverage))
            duplicate_variant = json.loads(json.dumps(schedule))
            duplicate_variant["entries"][-1]["variantId"] = duplicate_variant["entries"][0]["variantId"]
            mutations.append(("variant-duplicate", manifest, duplicate_variant, coverage))
            wrong_order = json.loads(json.dumps(schedule))
            wrong_order["entries"][0], wrong_order["entries"][1] = (
                wrong_order["entries"][1], wrong_order["entries"][0]
            )
            mutations.append(("row-order", manifest, wrong_order, coverage))
            wrong_ordinal = json.loads(json.dumps(schedule))
            wrong_ordinal["entries"][1]["ordinal"] = 7
            mutations.append(("ordinal", manifest, wrong_ordinal, coverage))
            wrong_mapping = json.loads(json.dumps(schedule))
            wrong_mapping["entries"][0]["scenarioId"] = "CEQ-LONG-01"
            mutations.append(("mapping", manifest, wrong_mapping, coverage))
            wrong_layers = json.loads(json.dumps(schedule))
            wrong_layers["entries"][0]["layers"] = ["L3", "L1", "L2"]
            mutations.append(("layers", manifest, wrong_layers, coverage))
            coverage_drift = json.loads(json.dumps(coverage))
            coverage_drift["records"][0]["layers"] = ["L1"]
            mutations.append(("sealed-public-drift", manifest, schedule, coverage_drift))
            for label, changed_manifest, changed_schedule, changed_coverage in mutations:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_fixture_contracts(
                        changed_manifest,
                        changed_schedule,
                        changed_coverage,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        oracle_root=fixture.oracle_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )

    def test_reviewed_matrix_rejects_public_and_sealed_collusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))

            def copies():
                return (
                    json.loads(json.dumps(fixture.schedule)),
                    json.loads(json.dumps(fixture.coverage)),
                )

            collusions = []
            schedule, coverage = copies()
            schedule["entries"][0], schedule["entries"][1] = (
                schedule["entries"][1],
                schedule["entries"][0],
            )
            schedule["entries"][0]["ordinal"] = 0
            schedule["entries"][1]["ordinal"] = 1
            coverage["records"][0], coverage["records"][1] = (
                coverage["records"][1],
                coverage["records"][0],
            )
            collusions.append(("swap-renumber", schedule, coverage))

            schedule, coverage = copies()
            schedule["entries"][0]["scenarioId"] = "CEQ-LONG-01"
            long_scenario = next(
                item for item in fixture.manifest["scenarios"] if item["id"] == "CEQ-LONG-01"
            )
            schedule["entries"][0]["inputHash"] = long_scenario["inputHash"]
            schedule["entries"][0]["responseHash"] = long_scenario["responseHash"]
            coverage["records"][0]["scenarioId"] = "CEQ-LONG-01"
            collusions.append(("scenario-both", schedule, coverage))

            schedule, coverage = copies()
            schedule["entries"][0]["layers"] = ["L1", "L2"]
            coverage["records"][0]["layers"] = ["L1", "L2"]
            collusions.append(("layer-downgrade-both", schedule, coverage))

            schedule, coverage = copies()
            first_schedule = schedule["entries"][0]
            second_schedule = schedule["entries"][4]
            for field in ("variantId", "scenarioId", "layers", "inputHash", "responseHash"):
                first_schedule[field], second_schedule[field] = (
                    second_schedule[field],
                    first_schedule[field],
                )
            first_coverage = coverage["records"][0]
            second_coverage = coverage["records"][4]
            for field in set(first_coverage):
                first_coverage[field], second_coverage[field] = (
                    second_coverage[field],
                    first_coverage[field],
                )
            collusions.append(("move-full-variant-metadata", schedule, coverage))

            schedule, coverage = copies()
            for field in ("sabotageId", "responseClass"):
                coverage["records"][0][field], coverage["records"][4][field] = (
                    coverage["records"][4][field],
                    coverage["records"][0][field],
                )
            collusions.append(("swap-sealed-metadata", schedule, coverage))

            schedule, coverage = copies()
            coverage["records"][0].update(
                {
                    "promotionClass": "diagnostic",
                    "expectedVerdict": "UNVERIFIED",
                    "nonClaims": ["UNVERIFIED_NO_SHARED_FINALIZER"],
                }
            )
            collusions.append(("promotion-verdict-together", schedule, coverage))

            schedule, coverage = copies()
            schedule["entries"][0]["layers"] = ["L3", "L2", "L1"]
            coverage["records"][0]["layers"] = ["L3", "L2", "L1"]
            collusions.append(("layer-permutation-both", schedule, coverage))

            for label, schedule, coverage in collusions:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_fixture_contracts(
                        fixture.manifest,
                        schedule,
                        coverage,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        oracle_root=fixture.oracle_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )

    def test_sealed_metadata_invariants_and_sorted_contract_digest_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            original = self.manifest.validate_coverage(
                fixture.coverage,
                oracle_root=fixture.oracle_root,
                provenance=fixture.provenance,
            )
            reordered_document = json.loads(json.dumps(fixture.coverage))
            reordered_document["records"] = list(reversed(reordered_document["records"]))
            reordered = self.manifest.validate_coverage(
                reordered_document,
                oracle_root=fixture.oracle_root,
                provenance=fixture.provenance,
            )
            self.assertEqual(55, original.count)
            self.assertEqual(55, reordered.count)
            self.assertEqual(original.contractDigest, reordered.contractDigest)
            self.assertRegex(original.contractDigest, r"^[0-9a-f]{64}$")
            self.assertEqual(
                [item[0] for item in TASK3_MATRIX],
                [record.variantId for record in reordered.records],
            )
            controls = []
            mutations = (
                ("sabotageId", "SAB-EXT02-01"),
                ("responseClass", "terminal_reply"),
                ("voiceEligibility", True),
                ("promotionClass", "diagnostic"),
                ("expectedVerdict", "UNVERIFIED"),
                ("nonClaims", ["UNVERIFIED_NO_SHARED_FINALIZER"]),
                ("layers", ["L1"]),
            )
            for field, value in mutations:
                changed = json.loads(json.dumps(fixture.coverage))
                changed["records"][0][field] = value
                controls.append((field, changed))
            for extra_field in ("baseline", "variantFamily", "sabotageReason"):
                changed = json.loads(json.dumps(fixture.coverage))
                changed["records"][0][extra_field] = "plan-only"
                controls.append((extra_field, changed))
            for label, changed in controls:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_coverage(
                        changed,
                        oracle_root=fixture.oracle_root,
                        provenance=fixture.provenance,
                    )

    def test_byte_owner_and_oracle_hashes_are_lowercase_exact_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture_contracts(root)
            manifest, schedule, coverage = fixture.manifest, fixture.schedule, fixture.coverage
            mutations = []
            for field in ("inputHash", "responseHash"):
                changed = json.loads(json.dumps(manifest))
                changed["scenarios"][0][field] = "A" * 64
                mutations.append((field, changed, schedule, coverage))
            changed = json.loads(json.dumps(manifest))
            owner_path = next(iter(changed["scenarios"][0]["ownerModuleHashes"]))
            changed["scenarios"][0]["ownerModuleHashes"][owner_path] = "0" * 64
            mutations.append(("owner-hash", changed, schedule, coverage))
            changed_coverage = json.loads(json.dumps(coverage))
            changed_coverage["records"][0]["oracleHash"] = "0" * 64
            mutations.append(("oracle-hash", manifest, schedule, changed_coverage))
            for label, changed_manifest, changed_schedule, changed_coverage in mutations:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_fixture_contracts(
                        changed_manifest,
                        changed_schedule,
                        changed_coverage,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        oracle_root=fixture.oracle_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )
            first_input = fixture.input_root / manifest["scenarios"][0]["inputBundle"]
            first_input.write_bytes(b"{}")
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    manifest,
                    schedule,
                    coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

    def test_hash_correct_public_and_sealed_bundles_still_require_privacy_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            nonce = hashlib.sha256(os.urandom(32)).hexdigest()
            first_scenario = fixture.manifest["scenarios"][0]
            input_path = fixture.input_root / first_scenario["inputBundle"]
            unsafe = self._json_bytes({"senderEmail": f"{nonce[:12]}@example.com"})
            input_path.write_bytes(unsafe)
            first_scenario["inputHash"] = _sha256(unsafe)
            for entry in fixture.schedule["entries"]:
                if entry["scenarioId"] == first_scenario["id"]:
                    entry["inputHash"] = first_scenario["inputHash"]
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    fixture.schedule,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            nonce = hashlib.sha256(os.urandom(32)).hexdigest()
            oracle_path = fixture.oracle_root / "known-filled.json"
            unsafe = self._json_bytes({"senderEmail": f"{nonce[:12]}@example.com"})
            oracle_path.write_bytes(unsafe)
            fixture.coverage["records"][0]["oracleHash"] = _sha256(unsafe)
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    fixture.schedule,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

    def test_paths_are_relative_contained_regular_files_and_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._fixture_contracts(root)
            manifest, provenance = fixture.manifest, fixture.provenance
            outside = root / "outside.json"
            first_path = manifest["scenarios"][0]["inputBundle"]
            outside.write_bytes((fixture.input_root / first_path).read_bytes())
            cases = (
                ("", "empty"),
                (".", "dot"),
                ("./input.json", "dot-prefix"),
                ("/Users/private/customer.json", "absolute"),
                ("C:/private/customer.json", "windows-drive"),
                (r"C:\private\customer.json", "backslash"),
                ("file:///Users/private/customer.json", "file-uri"),
                ("https://example.invalid/input.json", "uri"),
                ("../outside.json", "traversal"),
                ("nested/../outside.json", "embedded-dotdot"),
                ("nested//input.json", "empty-component"),
                ("%2e%2e/outside.json", "encoded-dotdot"),
                ("input%2falias.json", "encoded-separator"),
                ("input.json\x00suffix", "nul"),
            )
            for value, label in cases:
                changed = json.loads(json.dumps(manifest))
                changed["scenarios"][0]["inputBundle"] = value
                with self.subTest(label=label), self.assertRaises(ValueError) as raised:
                    self.manifest.validate_public_manifest(
                        changed,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=provenance,
                    )
                if value:
                    self.assertNotIn(value, str(raised.exception))
            link = fixture.input_root / "input-link.json"
            os.symlink(first_path, link)
            changed = json.loads(json.dumps(manifest))
            changed["scenarios"][0]["inputBundle"] = "input-link.json"
            with self.assertRaises(ValueError):
                self.manifest.validate_public_manifest(
                    changed,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=provenance,
                )

            outside_dir = root / "outside-dir"
            outside_dir.mkdir()
            (outside_dir / "escaped.json").write_bytes(
                (fixture.input_root / first_path).read_bytes()
            )
            os.symlink(outside_dir, fixture.input_root / "ancestor-link")
            changed = json.loads(json.dumps(manifest))
            changed["scenarios"][0]["inputBundle"] = "ancestor-link/escaped.json"
            with self.assertRaises(ValueError):
                self.manifest.validate_public_manifest(
                    changed,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=provenance,
                )

    def test_bound_file_reads_reject_special_hardlinked_oversize_and_cross_surface_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            first = fixture.manifest["scenarios"][0]
            original_path = fixture.input_root / first["inputBundle"]
            controls = []

            fifo = fixture.input_root / "fixture.fifo"
            os.mkfifo(fifo)
            changed = json.loads(json.dumps(fixture.manifest))
            changed["scenarios"][0]["inputBundle"] = fifo.name
            controls.append(("special", changed))

            hardlink = fixture.input_root / "hardlink.json"
            try:
                os.link(original_path, hardlink)
            except OSError as error:
                self.skipTest(f"filesystem does not support hard links: {error.errno}")
            changed = json.loads(json.dumps(fixture.manifest))
            changed["scenarios"][0]["inputBundle"] = hardlink.name
            controls.append(("hardlink", changed))

            oversize = fixture.input_root / "oversize.json"
            oversize.write_bytes(b"x" * (self.manifest.MAX_ARTIFACT_BYTES + 1))
            changed = json.loads(json.dumps(fixture.manifest))
            changed["scenarios"][0]["inputBundle"] = oversize.name
            controls.append(("oversize", changed))

            for label, manifest in controls:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_public_manifest(
                        manifest,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )

            response_alias = fixture.response_root / first["responseBundle"]
            response_alias.unlink()
            os.link(original_path, response_alias)
            first["responseHash"] = first["inputHash"]
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    fixture.schedule,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

    def test_oracle_lookup_is_derived_and_input_cannot_alias_sealed_bytes(self):
        self.assertEqual(
            PurePosixPath("known-filled.json"),
            self.manifest.oracle_path_for_variant("known-filled"),
        )
        with self.assertRaises(ValueError):
            self.manifest.oracle_path_for_variant("caller/controlled")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            first = fixture.manifest["scenarios"][0]
            oracle = fixture.oracle_root / "known-filled.json"
            source = fixture.input_root / first["inputBundle"]
            source.unlink()
            os.link(oracle, source)
            first["inputHash"] = _sha256(oracle.read_bytes())
            for entry in fixture.schedule["entries"]:
                if entry["scenarioId"] == first["id"]:
                    entry["inputHash"] = first["inputHash"]
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    fixture.schedule,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

    def test_owner_hashes_are_nonempty_and_match_a_trusted_external_allowlist(self):
        self.assertIn("Task 7", self.manifest.OWNER_COMPLETENESS_NONCLAIM)
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            controls = []
            omitted = json.loads(json.dumps(fixture.manifest))
            omitted["scenarios"][0]["ownerModuleHashes"] = {}
            controls.append(("empty", omitted, fixture.owner_root, fixture.expected_owner_paths))
            missing_expected = dict(fixture.expected_owner_paths)
            missing_expected[fixture.manifest["scenarios"][0]["id"]] = frozenset()
            controls.append(("trusted-allowlist-empty", fixture.manifest, fixture.owner_root, missing_expected))
            wrong_root = Path(tmp) / "wrong-owner-root"
            wrong_root.mkdir()
            for owner in fixture.owner_root.iterdir():
                (wrong_root / owner.name).write_bytes(owner.read_bytes())
            controls.append(("wrong-root-capability", fixture.manifest, wrong_root, fixture.expected_owner_paths))
            for label, manifest, owner_root, expected in controls:
                with self.subTest(label=label), self.assertRaises(ValueError):
                    self.manifest.validate_public_manifest(
                        manifest,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=owner_root,
                        expected_owner_paths=expected,
                        provenance=fixture.provenance,
                    )

    def test_schedule_hashes_are_bound_to_unique_scenario_manifest_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            changed = json.loads(json.dumps(fixture.schedule))
            first = changed["entries"][0]
            other = next(
                item
                for item in fixture.manifest["scenarios"]
                if item["id"] != first["scenarioId"]
            )
            first["inputHash"] = other["inputHash"]
            first["responseHash"] = other["responseHash"]
            with self.assertRaises(ValueError):
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    changed,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )

    def test_component_openat_holds_ancestor_fd_across_swap_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            scenario = fixture.manifest["scenarios"][0]
            original = fixture.input_root / scenario["inputBundle"]
            slot = fixture.input_root / "slot"
            slot.mkdir()
            payload = slot / "payload.json"
            payload.write_bytes(original.read_bytes())
            scenario["inputBundle"] = "slot/payload.json"
            scenario["inputHash"] = _sha256(payload.read_bytes())
            outside = Path(tmp) / "swap-outside"
            outside.mkdir()
            (outside / "payload.json").write_bytes(b'{"escaped":true}')
            held = fixture.input_root / "held-slot"
            real_open = self.manifest.os.open
            swapped = False

            def swapping_open(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == "payload.json" and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    slot.rename(held)
                    os.symlink(outside, slot)
                    try:
                        return real_open(path, flags, *args, **kwargs)
                    finally:
                        slot.unlink()
                        held.rename(slot)
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(self.manifest.os, "open", side_effect=swapping_open):
                validated = self.manifest.validate_public_manifest(
                    fixture.manifest,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )
            self.assertTrue(swapped)
            self.assertIs(type(validated), self.manifest.ValidatedManifest)

    def test_same_byte_swap_restore_is_rejected_by_pre_post_fstat(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            scenario = fixture.manifest["scenarios"][0]
            path = fixture.input_root / scenario["inputBundle"]
            original = path.read_bytes()
            altered = bytes([original[0] ^ 1]) + original[1:]
            real_read = self.manifest.os.read
            mutated = False

            def mutating_read(fd, count):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    path.write_bytes(altered)
                    path.write_bytes(original)
                return real_read(fd, count)

            with mock.patch.object(self.manifest.os, "read", side_effect=mutating_read):
                with self.assertRaises(ValueError):
                    self.manifest.validate_public_manifest(
                        fixture.manifest,
                        input_root=fixture.input_root,
                        response_root=fixture.response_root,
                        owner_root=fixture.owner_root,
                        expected_owner_paths=fixture.expected_owner_paths,
                        provenance=fixture.provenance,
                    )
            self.assertTrue(mutated)

    def test_validated_records_detach_from_caller_mutation_and_retain_no_private_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = self._fixture_contracts(Path(tmp))
            validated_manifest, validated_schedule, validated_coverage = (
                self.manifest.validate_fixture_contracts(
                    fixture.manifest,
                    fixture.schedule,
                    fixture.coverage,
                    input_root=fixture.input_root,
                    response_root=fixture.response_root,
                    owner_root=fixture.owner_root,
                    oracle_root=fixture.oracle_root,
                    expected_owner_paths=fixture.expected_owner_paths,
                    provenance=fixture.provenance,
                )
            )
            before = (
                validated_manifest.to_mapping(),
                validated_schedule.to_mapping(),
                validated_coverage.to_mapping(),
                self.manifest.emit_child_descriptors(validated_schedule),
            )
            fixture.manifest["scenarios"][0]["ownerModuleHashes"].clear()
            fixture.schedule["entries"].reverse()
            fixture.coverage["records"][0]["layers"].clear()
            fixture.provenance["fictionalPeople"].append("Caller Mutation")
            after = (
                validated_manifest.to_mapping(),
                validated_schedule.to_mapping(),
                validated_coverage.to_mapping(),
                self.manifest.emit_child_descriptors(validated_schedule),
            )
            self.assertEqual(before, after)
            temp_prefix = str(Path(tmp))
            for public in (validated_manifest, validated_schedule):
                names = set(dir(public))
                self.assertTrue(
                    names.isdisjoint(
                        {
                            "coverage",
                            "oracle",
                            "provenance",
                            "inputRoot",
                            "responseRoot",
                            "ownerRoot",
                            "_coverage",
                            "_oracle",
                            "_provenance",
                        }
                    )
                )
                self.assertNotIn(temp_prefix, repr(public))

    def test_typed_schedule_constructor_and_emitter_reject_forged_row_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _validated_manifest, schedule, _validated_coverage = self._validate(Path(tmp))
            forged_entries = (schedule.entries[1], schedule.entries[0], *schedule.entries[2:])
            with self.assertRaises(ValueError):
                self.manifest.ValidatedExecutionSchedule(
                    schemaVersion=schedule.schemaVersion,
                    productionAncestor=schedule.productionAncestor,
                    implementationBase=schedule.implementationBase,
                    entries=forged_entries,
                )
            forged = object.__new__(self.manifest.ValidatedExecutionSchedule)
            for name, value in (
                ("schemaVersion", schedule.schemaVersion),
                ("productionAncestor", schedule.productionAncestor),
                ("implementationBase", schedule.implementationBase),
                ("entries", forged_entries),
            ):
                object.__setattr__(forged, name, value)
            with self.assertRaises(ValueError):
                self.manifest.emit_child_descriptors(forged)

    def test_privacy_scanners_accept_only_declared_synthetic_identity_and_clock(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        safe = self._json_bytes(
            {
                "name": "Avery Example",
                "email": "avery@example.invalid",
                "propertyAddress": "100 Example Plaza",
                "receivedAt": "2040-06-01T12:00:00Z",
            }
        )
        self.assertEqual((), self.privacy.scan_bytes(safe, artifact_id="safe", provenance=provenance))
        self.assertEqual(
            (),
            self.privacy.scan_json(json.loads(safe), artifact_id="safe-json", provenance=provenance),
        )
        digest = hashlib.sha256(os.urandom(32)).hexdigest().encode("ascii")
        cases = (
            ("CEQ_PRIV_ABSOLUTE_PATH", b"/" + b"Users/fixture/customer.json"),
            ("CEQ_PRIV_FILE_URI", b"file:" + b"///fixture/customer.json"),
            ("CEQ_PRIV_PRODUCTION_ID", b"projects/" + digest[:20] + b"/databases/(default)"),
            ("CEQ_PRIV_RAW_MESSAGE_ID", b"graph-message-id:" + digest),
            ("CEQ_PRIV_CREDENTIAL", b"sk-" + digest),
            ("CEQ_PRIV_CLOCK_RANGE", b"2032-01-01T00:00:00Z"),
            ("CEQ_PRIV_NON_INVALID_MAILBOX", digest[:12] + b"@example.com"),
            ("CEQ_PRIV_UNDECLARED_IDENTITY", digest[:12] + b"@other.invalid"),
        )
        for rule_id, token in cases:
            with self.subTest(rule_id=rule_id), self.assertRaises(ValueError) as raised:
                self.privacy.scan_bytes(token, artifact_id="bundle-01", provenance=provenance)
            message = str(raised.exception)
            self.assertEqual((rule_id, "bundle-01"), raised.exception.args)
            self.assertNotIn(token.decode("ascii"), message)
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
        self.assertEqual(
            (),
            self.privacy.scan_bytes(
                b"ordinary words 14000 " + digest,
                artifact_id="safe-nonclaim",
                provenance=provenance,
            ),
        )
        self.assertIn("arbitrary copied prose", self.privacy.SCANNER_NONCLAIM)
        self.assertIn("numbers", self.privacy.SCANNER_NONCLAIM)
        unsafe_artifact_id = hashlib.sha256(os.urandom(32)).hexdigest() + "@example.com"
        with self.assertRaises(ValueError) as raised:
            self.privacy.scan_bytes(
                b"safe",
                artifact_id=unsafe_artifact_id,
                provenance=provenance,
            )
        self.assertEqual(
            ("CEQ_PRIV_ARTIFACT_ID", "invalid-artifact-id"), raised.exception.args
        )
        self.assertNotIn(unsafe_artifact_id, str(raised.exception))

    def test_colon_labeled_absolute_path_does_not_bypass_uri_handling(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        with self.assertRaises(ValueError) as raised:
            self.privacy.scan_bytes(
                b"label:/Users/private/customer.json",
                artifact_id="colon-path",
                provenance=provenance,
            )
        self.assertEqual(
            ("CEQ_PRIV_ABSOLUTE_PATH", "colon-path"), raised.exception.args
        )
        self.assertEqual(
            (),
            self.privacy.scan_bytes(
                b"https://example.invalid/synthetic/input.json",
                artifact_id="https-uri",
                provenance=provenance,
            ),
        )
        with self.assertRaises(ValueError) as raised:
            self.privacy.scan_bytes(
                b"file:///Users/private/customer.json",
                artifact_id="file-uri",
                provenance=provenance,
            )
        self.assertEqual(("CEQ_PRIV_FILE_URI", "file-uri"), raised.exception.args)

    def test_text_scanner_distinguishes_sentence_punctuation_and_slash_operator(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        for payload in (
            b"Contact avery@example.invalid.",
            b"Contact avery@example.invalid...",
            "Contact avery@example.invalid\u2026".encode("utf-8"),
            b"rent / opex",
        ):
            with self.subTest(payload=payload):
                self.assertEqual(
                    (),
                    self.privacy.scan_bytes(
                        payload,
                        artifact_id="text-boundary-safe",
                        provenance=provenance,
                    ),
                )
        for terminator in (
            b".",
            b"/",
            b"]",
            b"#",
            b"=",
            b"...",
            "\u2026".encode("utf-8"),
        ):
            with self.subTest(terminator=terminator):
                with self.assertRaises(ValueError) as raised:
                    self.privacy.scan_bytes(
                        b"Contact broker@outside.example" + terminator,
                        artifact_id="punctuated-mailbox",
                        provenance=provenance,
                    )
                self.assertEqual(
                    ("CEQ_PRIV_NON_INVALID_MAILBOX", "punctuated-mailbox"),
                    raised.exception.args,
                )
        for payload in (
            b"/",
            b"label:/Users",
            b"/Users",
            b"//server",
            b"///usr",
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError) as raised:
                self.privacy.scan_bytes(
                    payload,
                    artifact_id="absolute-path-control",
                    provenance=provenance,
                )
            self.assertEqual(
                ("CEQ_PRIV_ABSOLUTE_PATH", "absolute-path-control"),
                raised.exception.args,
            )

    def test_seeded_forbidden_token_is_quarantined_without_committing_or_echoing_it(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        forbidden = hashlib.sha256(os.urandom(32)).hexdigest().encode("ascii")
        with tempfile.TemporaryDirectory() as tmp:
            seeded = Path(tmp) / "seeded.json"
            seeded.write_bytes(b'{"value":"' + forbidden + b'"}')
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(
                    Path(tmp),
                    artifact_id="seed-control",
                    provenance=provenance,
                    forbidden_tokens={"CEQ_TEST_FORBIDDEN": forbidden},
                )
        self.assertEqual(
            ("CEQ_PRIV_FORBIDDEN_TOKEN", "seed-control"), raised.exception.args
        )
        self.assertNotIn(forbidden.decode("ascii"), str(raised.exception))
        self.assertNotIn(
            forbidden.decode("ascii"),
            (REPO_ROOT / "tests/fixtures/ceq1/inputs/provenance.json").read_text(),
        )

    def test_json_identity_keys_and_tree_symlinks_are_quarantined(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        for key, value in (
            ("brokerName", "Undeclared Person"),
            ("propertyAddress", "999 Undeclared Street"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError) as raised:
                self.privacy.scan_json(
                    {key: value}, artifact_id="identity-control", provenance=provenance
                )
            self.assertIn("CEQ_PRIV_UNDECLARED_IDENTITY", str(raised.exception))
            self.assertNotIn(value, str(raised.exception))
        safe_metadata = (
            ("file-name", {"fileName": "synthetic.pdf"}),
            ("campaign-name", {"campaignName": "Synthetic Campaign"}),
            ("email-address", {"emailAddress": "avery@example.invalid"}),
            (
                "nested-attachment",
                {
                    "attachments": [
                        {"name": "synthetic.pdf", "fileName": "synthetic.pdf"}
                    ]
                },
            ),
            (
                "nested-mailbox",
                {
                    "mailbox": {
                        "name": "Synthetic Mailbox",
                        "address": "avery@example.invalid",
                    }
                },
            ),
        )
        for label, value in safe_metadata:
            with self.subTest(label=label):
                self.assertEqual(
                    (),
                    self.privacy.scan_json(
                        value,
                        artifact_id="metadata-control",
                        provenance=provenance,
                    ),
                )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe.json").write_bytes(self._json_bytes({"name": "Avery Example"}))
            self.assertEqual((), self.privacy.scan_tree(root, artifact_id="tree", provenance=provenance))
            os.symlink("safe.json", root / "alias.json")
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(root, artifact_id="tree", provenance=provenance)
            self.assertIn("CEQ_PRIV_TREE_LINK", str(raised.exception))

    def test_privacy_scanner_closes_encoded_identity_and_secret_key_evasions(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        raw_cases = (
            b'{"senderEmail":"' + nonce[:12].encode() + b'\\u0040other.invalid"}',
            json.dumps({"senderEmail": f"{nonce[:12]}\u200b@other.invalid"}).encode(),
            json.dumps({"senderEmail": f"{nonce[:12]}＠other.invalid"}).encode(),
            json.dumps({"senderEmail": f"{nonce[:12]}%40other.invalid"}).encode(),
            b'{"api\\u004bey":"' + nonce.encode() + b'"}',
            b'{"accessToken":"ordinary-looking-value"}',
            b'{"privateKey":"ordinary-looking-value"}',
            b'{"messageId":"graph-message-id:' + nonce.encode() + b'"}',
        )
        for index, payload in enumerate(raw_cases):
            with self.subTest(index=index), self.assertRaises(ValueError) as raised:
                self.privacy.scan_bytes(
                    payload, artifact_id=f"evasion-{index}", provenance=provenance
                )
            self.assertNotIn(nonce, str(raised.exception))
        safe_encoded = b'{"senderEmail":"avery\\u0040example.invalid"}'
        self.assertEqual(
            (),
            self.privacy.scan_bytes(
                safe_encoded, artifact_id="encoded-safe", provenance=provenance
            ),
        )

    def test_synthetic_clock_is_strict_valid_utc_z_and_range_bounded(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        for value in (
            "2040-01-01T00:00:00Z",
            "2040-12-31T23:59:59Z",
        ):
            self.assertEqual(
                (),
                self.privacy.scan_json(
                    {"receivedAt": value}, artifact_id="clock-safe", provenance=provenance
                ),
            )
        for value in (
            "2039-12-31T23:59:59Z",
            "2041-01-01T00:00:00Z",
            "2040-01-01T00:00:00+00:00",
            "2040-01-01T00:00:00.000Z",
            "2040-02-30T00:00:00Z",
            "2040-1-01T00:00:00Z",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                self.privacy.scan_json(
                    {"receivedAt": value}, artifact_id="clock-control", provenance=provenance
                )
            self.assertEqual(
                ("CEQ_PRIV_CLOCK_RANGE", "clock-control"), raised.exception.args
            )
            self.assertNotIn(value, str(raised.exception))

    def test_tree_scans_filenames_rejects_specials_and_quarantines_opaque_pdf(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        nonce = hashlib.sha256(os.urandom(32)).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsafe_name = f"{nonce[:12]}@example.com.json"
            (root / unsafe_name).write_bytes(b"{}")
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(root, artifact_id="filename-tree", provenance=provenance)
            self.assertNotIn(unsafe_name, str(raised.exception))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fifo = root / "special"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(root, artifact_id="special-tree", provenance=provenance)
            self.assertEqual(("CEQ_PRIV_TREE_SPECIAL", "special-tree"), raised.exception.args)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "synthetic.pdf"
            pdf.write_bytes(b"%PDF-1.7\x00synthetic")
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(root, artifact_id="pdf-tree", provenance=provenance)
            self.assertEqual(("CEQ_PRIV_OPAQUE_BINARY", "pdf-tree"), raised.exception.args)
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(
                    root,
                    artifact_id="pdf-tree",
                    provenance=provenance,
                    decoded_text_by_path={"synthetic.pdf": "Avery Example at 100 Example Plaza"},
                )
            self.assertEqual(("CEQ_PRIV_OPAQUE_BINARY", "pdf-tree"), raised.exception.args)

    def test_pdf_raw_metadata_is_scanned_before_opaque_tolerance(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "synthetic.pdf").write_bytes(
                b"%PDF-1.7\x00 1 0 obj << /Type /Catalog >>"
            )
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(
                    root,
                    artifact_id="pdf-standard-names",
                    provenance=provenance,
                    decoded_text_by_path={
                        "synthetic.pdf": "Avery Example at 100 Example Plaza"
                    },
                )
            self.assertEqual(
                ("CEQ_PRIV_OPAQUE_BINARY", "pdf-standard-names"),
                raised.exception.args,
            )
        cases = (
            ("CEQ_PRIV_NON_INVALID_MAILBOX", b"broker@outside.example"),
            ("CEQ_PRIV_ABSOLUTE_PATH", b"/Users/private/customer.json"),
            (
                "CEQ_PRIV_PRODUCTION_ID",
                b"projects/production-project/databases/(default)",
            ),
            (
                "CEQ_PRIV_RAW_MESSAGE_ID",
                b"graph-message-id:0123456789abcdef0123456789abcdef",
            ),
            ("CEQ_PRIV_CLOCK_RANGE", b"2032-01-01T00:00:00Z"),
            ("CEQ_PRIV_OBFUSCATED_IDENTITY", b"broker%40outside.example"),
            (
                "CEQ_PRIV_OBFUSCATED_IDENTITY",
                "broker\uff20outside.example".encode("utf-8"),
            ),
            (
                "CEQ_PRIV_OBFUSCATED_IDENTITY",
                "broker\u200b@outside.example".encode("utf-8"),
            ),
        )
        for rule_id, raw_metadata in cases:
            with self.subTest(rule_id=rule_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "synthetic.pdf").write_bytes(
                    b"%PDF-1.7\x00" + raw_metadata
                )
                with self.assertRaises(ValueError) as raised:
                    self.privacy.scan_tree(
                        root,
                        artifact_id="pdf-raw-metadata",
                        provenance=provenance,
                        decoded_text_by_path={
                            "synthetic.pdf": "Avery Example at 100 Example Plaza"
                        },
                    )
                self.assertEqual(
                    (rule_id, "pdf-raw-metadata"), raised.exception.args
                )
                self.assertNotIn(raw_metadata.decode("utf-8"), str(raised.exception))

    def test_opaque_pdf_caller_decoded_map_cannot_forge_privacy_pass(self):
        provenance = self.privacy.validate_generation_provenance(self._provenance())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "synthetic.pdf").write_bytes(
                b"%PDF-1.7\x00stream\n" + zlib.compress(b"broker@outside.example")
            )
            with self.assertRaises(ValueError) as raised:
                self.privacy.scan_tree(
                    root,
                    artifact_id="pdf-compressed-forgery",
                    provenance=provenance,
                    decoded_text_by_path={
                        "synthetic.pdf": "Avery Example at 100 Example Plaza"
                    },
                )
            self.assertEqual(
                ("CEQ_PRIV_OPAQUE_BINARY", "pdf-compressed-forgery"),
                raised.exception.args,
            )
        compressed = zlib.compress(b"broker@outside.example")
        for label, encoded in (
            ("base64-zlib", base64.b64encode(compressed)),
            ("ascii85-zlib", base64.a85encode(compressed)),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "synthetic.pdf").write_bytes(
                    b"%PDF-1.7\nstream\n" + encoded + b"\nendstream\n%%EOF\n"
                )
                with self.assertRaises(ValueError) as raised:
                    self.privacy.scan_tree(
                        root,
                        artifact_id="pdf-ascii-encoded-forgery",
                        provenance=provenance,
                        decoded_text_by_path={
                            "synthetic.pdf": "Avery Example at 100 Example Plaza"
                        },
                    )
                self.assertEqual(
                    ("CEQ_PRIV_OPAQUE_BINARY", "pdf-ascii-encoded-forgery"),
                    raised.exception.args,
                )

    def test_generation_provenance_is_closed_newly_authored_and_review_gating_is_explicit(self):
        valid = self._provenance()
        parsed = self.privacy.validate_generation_provenance(valid)
        self.assertEqual("newly_authored_synthetic_template", parsed.generationMethod)
        self.assertFalse(parsed.rawCustomerSourcesAccessed)
        self.assertEqual("pending", parsed.independentReviewStatus)
        self.assertFalse(parsed.gateApproved)
        self.assertIsNone(parsed.independentReviewerRole)
        self.assertIsNone(parsed.reviewedArtifactSetSha256)
        self.assertIsNone(parsed.reviewedCommit)
        self.assertEqual(self.privacy.SCANNER_NONCLAIM, parsed.scannerNonClaim)
        self.assertEqual(self.privacy.SCANNER_NONCLAIM, parsed.scannerNonClaim)
        self.assertIn(
            "PDF decoded-text privacy remains unverified", parsed.scannerNonClaim
        )
        self.assertIn("Task 7 verified parser receipt", parsed.scannerNonClaim)
        self.assertIn(
            "caller-supplied decoded-text map cannot produce a privacy gate pass",
            parsed.scannerNonClaim,
        )
        artifact_digest = hashlib.sha256(os.urandom(32)).hexdigest()
        reviewed_commit = "3" * 40
        approved = json.loads(json.dumps(valid))
        approved.update(
            {
                "independentReviewStatus": "approved",
                "independentReviewerRole": "independent_fixture_privacy_reviewer",
                "reviewedArtifactSetSha256": artifact_digest,
                "reviewedCommit": reviewed_commit,
            }
        )
        self.assertTrue(
            self.privacy.validate_generation_provenance(
                approved,
                artifact_set_sha256=artifact_digest,
                current_commit=reviewed_commit,
            ).gateApproved
        )
        self.assertFalse(
            self.privacy.validate_generation_provenance(
                approved,
                artifact_set_sha256="4" * 64,
                current_commit=reviewed_commit,
            ).gateApproved
        )
        status_only = json.loads(json.dumps(valid))
        status_only["independentReviewStatus"] = "approved"
        with self.assertRaises(ValueError):
            self.privacy.validate_generation_provenance(status_only)
        mutations = []
        for key, value in (
            ("generationMethod", "adapted_customer_fixture"),
            ("rawCustomerSourcesAccessed", True),
            ("independentReviewStatus", "self-approved"),
            ("scannerNonClaim", "overclaim"),
        ):
            changed = json.loads(json.dumps(valid))
            changed[key] = value
            mutations.append((key, changed))
        extra = json.loads(json.dumps(valid))
        extra["reviewerName"] = "not allowed"
        mutations.append(("extra-key", extra))
        missing = json.loads(json.dumps(valid))
        missing.pop("scannerRules")
        mutations.append(("missing-key", missing))
        bad_hash = json.loads(json.dumps(valid))
        bad_hash["scannerRules"][0]["sha256"] = "0" * 64
        mutations.append(("scanner-rule-hash", bad_hash))
        duplicate_rule = json.loads(json.dumps(valid))
        duplicate_rule["scannerRules"].append(duplicate_rule["scannerRules"][0])
        mutations.append(("scanner-rule-duplicate", duplicate_rule))
        missing_rule = json.loads(json.dumps(valid))
        missing_rule["scannerRules"].pop()
        mutations.append(("scanner-rule-missing", missing_rule))
        extra_rule = json.loads(json.dumps(valid))
        extra_rule["scannerRules"].append({"ruleId": "CEQ_PRIV_UNKNOWN", "sha256": "0" * 64})
        mutations.append(("scanner-rule-extra", extra_rule))
        unsorted_rules = json.loads(json.dumps(valid))
        unsorted_rules["scannerRules"].reverse()
        mutations.append(("scanner-rule-order", unsorted_rules))
        uppercase_mailbox = json.loads(json.dumps(valid))
        uppercase_mailbox["fictionalMailboxes"] = ["Avery@example.invalid"]
        mutations.append(("mailbox-lowercase", uppercase_mailbox))
        noninvalid_domain = json.loads(json.dumps(valid))
        noninvalid_domain["fictionalDomains"] = ["example.com"]
        noninvalid_domain["fictionalMailboxes"] = [artifact_digest[:12] + "@example.com"]
        mutations.append(("domain-invalid", noninvalid_domain))
        wrong_role = json.loads(json.dumps(approved))
        wrong_role["independentReviewerRole"] = "author"
        mutations.append(("reviewer-role", wrong_role))
        for label, changed in mutations:
            with self.subTest(label=label), self.assertRaises((TypeError, ValueError)):
                self.privacy.validate_generation_provenance(changed)
        before = parsed.to_mapping()
        valid["fictionalPeople"].append("caller mutation")
        valid["scannerRules"].clear()
        self.assertEqual(before, parsed.to_mapping())

    def test_generation_provenance_raw_bytes_use_strict_loader(self):
        valid = self._provenance()
        raw = self._json_bytes(valid)
        parsed = self.privacy.validate_generation_provenance_bytes(
            raw, artifact_id="provenance"
        )
        self.assertEqual("pending", parsed.independentReviewStatus)
        duplicate = raw[:-1] + b',"schemaVersion":1}'
        with self.assertRaises(ValueError) as raised:
            self.privacy.validate_generation_provenance_bytes(
                duplicate, artifact_id="provenance"
            )
        self.assertEqual(("CEQ_JSON_INVALID", "provenance"), raised.exception.args)

    def test_committed_provenance_is_synthetic_closed_and_pending(self):
        path = REPO_ROOT / "tests/fixtures/ceq1/inputs/provenance.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        parsed = self.privacy.validate_generation_provenance(record)
        self.assertEqual("newly_authored_synthetic_template", parsed.generationMethod)
        self.assertFalse(parsed.rawCustomerSourcesAccessed)
        self.assertEqual("pending", parsed.independentReviewStatus)
        self.assertFalse(parsed.gateApproved)


if __name__ == "__main__":
    unittest.main()
