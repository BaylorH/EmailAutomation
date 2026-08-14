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


if __name__ == "__main__":
    unittest.main()
