#!/usr/bin/env python3
"""Prepare the sealed, offline CE-Q1 Python runtime and derived wheelhouse."""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import errno
import hashlib
import importlib.util
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import secrets
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile


PINNED_PYTHON = Path(
    "/Users/baylorharrison/.local/share/uv/python/"
    "cpython-3.12.13-macos-aarch64-none/bin/python3.12"
)
PINNED_PYTHON_ROOT = PINNED_PYTHON.parents[1]
PINNED_UV = Path("/Users/baylorharrison/.local/bin/uv")
UV_CACHE = Path("/Users/baylorharrison/.cache/uv")
UV_PYTHON_STORE = Path("/Users/baylorharrison/.local/share/uv/python")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PYTHON_SHA256 = "e2605291e058fdbe3102e8185d0ac5fe0e063398de617010a6af3a42a78f05e3"
UV_SHA256 = "4424f8430c3cb3990daaa68268af640bdc61190f2e5c276197e3473358b1e4e8"
JDK_ROOT = Path("/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home")
FIRESTORE_JAR = Path(
    "/Users/baylorharrison/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.8.jar"
)
FIRESTORE_JAR_SHA256 = "9d43599ed6151199e8d604dc87fac51218e49e5f3a48519b1ae560bbe5e3382d"


BOOTSTRAP_SEATBELT_TEMPLATE = r'''(version 1)
(deny default)
(allow process-fork)
(allow process-exec
  (literal "/usr/bin/env")
  (literal "{UV}")
  (literal "{PYTHON_SOURCE}/bin/python3.12")
  (literal "{BUNDLE}/python/bin/python3.12")
  (literal "{RELOCATION}/python/bin/python3.12")
  (literal "{JDK_ROOT}/bin/java"))
(allow signal)
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix*)
(allow file-read-metadata)
(allow file-read*
{READ_ANCESTOR_RULES}
  (literal "/")
  (subpath "/System")
  (subpath "/usr")
  (subpath "/bin")
  (subpath "/sbin")
  (subpath "/Library")
  (subpath "/private/etc")
  (subpath "/private/var/db/timezone")
  (subpath "/dev")
  (literal "{BOOTSTRAP_SCRIPT}")
  (literal "{BUILDER_SCRIPT}")
  (literal "{VERIFIER_SCRIPT}")
  (literal "{WRAPPER_SCRIPT}")
  (literal "{INPUT_MANIFEST}")
  (literal "{PRODUCT_LOCK}")
  (literal "{QUALIFICATION_INPUT}")
  (literal "{QUALIFICATION_LOCK}")
  (literal "{WHEELHOUSE_MANIFEST}")
  (literal "{TOOLCHAIN_MANIFEST}")
  (subpath "{PYTHON_SOURCE}")
  (literal "{UV}")
  (subpath "{UV_CACHE}")
  (subpath "{JDK_ROOT}")
  (literal "{FIRESTORE_JAR}")
  (subpath "{RUNTIME}")
  (subpath "{BUNDLE}"))
(allow file-write*
  (literal "/dev/null")
  (subpath "{RUNTIME}")
  (subpath "{BUNDLE}"))
(deny network*)
'''

SEATBELT_PLACEHOLDERS = (
    "BOOTSTRAP_SCRIPT",
    "BUNDLE",
    "BUILDER_SCRIPT",
    "FIRESTORE_JAR",
    "INPUT_MANIFEST",
    "JDK_ROOT",
    "PRODUCT_LOCK",
    "PYTHON_SOURCE",
    "QUALIFICATION_INPUT",
    "QUALIFICATION_LOCK",
    "READ_ANCESTOR_RULES",
    "RELOCATION",
    "REPO",
    "RUNTIME",
    "TOOLCHAIN_MANIFEST",
    "UV",
    "UV_CACHE",
    "VERIFIER_SCRIPT",
    "WHEELHOUSE_MANIFEST",
    "WRAPPER_SCRIPT",
)

_TOOLCHAIN_KEYS = {
    "schemaVersion",
    "algorithmVersion",
    "artifacts",
    "lockfiles",
    "wheelhouseManifestSha256",
    "inputManifestSha256",
    "bootstrapSha256",
    "builderSha256",
    "seatbeltTemplate",
    "sealedRuntime",
}


class BootstrapBlocked(RuntimeError):
    """A closed prerequisite is missing or has drifted."""


def validate_cf_user_text_encoding(value: str) -> None:
    """Accept only the two closed formats macOS injects at startup."""

    if not re.fullmatch(
        r"0x[0-9A-Fa-f]+:(?:0x[0-9A-Fa-f]+|0):(?:0x[0-9A-Fa-f]+|0)",
        value,
    ):
        raise BootstrapBlocked("unexpected OS-injected text encoding value")


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[1]
    if (root / "scripts/bootstrap_ceq1_runtime.py").resolve() != Path(__file__).resolve():
        raise BootstrapBlocked("bootstrap script escaped the worktree")
    return root


def _input_manifest_path(root: Path) -> Path:
    return Path(root) / "docs/release-safety/ceq1-input-manifest.json"


def validate_input_manifest(root: Path, manifest: dict[str, object]) -> None:
    required = {
        "schemaVersion",
        "algorithmVersion",
        "files",
        "trees",
        "portablePolicy",
        "platformTrust",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise BootstrapBlocked("input manifest closed keys mismatch")
    if manifest["schemaVersion"] != 1 or manifest["algorithmVersion"] != "ceq1-input-v1":
        raise BootstrapBlocked("input manifest version drift")
    serialized = json.dumps(manifest, sort_keys=True)
    if "/Users/" in serialized or "file://" in serialized:
        raise BootstrapBlocked("absolute path leaked into input manifest")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise BootstrapBlocked("input manifest files shape drift")
    required_files = {
        "scripts/verify_ceq1_entry.pl",
        "scripts/bootstrap_ceq1_runtime.py",
        "scripts/build_ceq1_wheelhouse.py",
        "scripts/run_ceq1_env.py",
        "requirements.lock",
        "requirements-ceq1.in",
        "requirements-ceq1.lock",
        "docs/release-safety/ceq1-wheelhouse-manifest.json",
    }
    if set(files) != required_files:
        raise BootstrapBlocked("input manifest file closure mismatch")
    for relative, record in files.items():
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "size"}
            or not isinstance(record["size"], int)
            or record["size"] < 0
            or not isinstance(record["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
        ):
            raise BootstrapBlocked("input manifest file record drift")
        path = root / PurePosixPath(relative)
        data = _read_regular_bytes(path)
        if len(data) != record["size"] or _sha256_bytes(data) != record["sha256"]:
            raise BootstrapBlocked(f"input manifest file drift: {relative}")
    trees = manifest["trees"]
    expected_trees = {
        "cpythonSource": {
            **tree_receipt(PINNED_PYTHON_ROOT),
            "launcherSha256": PYTHON_SHA256,
        },
        "openjdkSource": tree_receipt(JDK_ROOT),
    }
    if trees != expected_trees:
        raise BootstrapBlocked("input manifest tree drift")
    policy = manifest["portablePolicy"]
    if policy != {
        "templateSha256": _sha256_bytes(BOOTSTRAP_SEATBELT_TEMPLATE.encode("utf-8")),
        "placeholders": sorted(SEATBELT_PLACEHOLDERS),
    }:
        raise BootstrapBlocked("input manifest portable policy drift")
    trust = manifest["platformTrust"]
    expected_trust = {
        "perl": {
            "pathId": "APPLE_SYSTEM_PERL",
            "ownerUid": 0,
            "requiredModules": ["Digest::SHA", "Fcntl", "JSON::PP"],
        },
        "uv": {
            "pathId": "PINNED_UV",
            "sha256": UV_SHA256,
            "size": PINNED_UV.stat().st_size,
        },
        "firestoreJar": {
            "pathId": "FIRESTORE_JAR_1_19_8",
            "sha256": FIRESTORE_JAR_SHA256,
            "size": FIRESTORE_JAR.stat().st_size,
        },
    }
    if trust != expected_trust:
        raise BootstrapBlocked("input manifest platform trust drift")


def _lexical_repo_root() -> Path:
    path = Path(__file__)
    if not path.is_absolute():
        raise BootstrapBlocked("bootstrap script path must be absolute")
    return path.parent.parent


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    path = Path(path).absolute()
    parent_fd = _open_directory_chain(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.name, flags, dir_fd=parent_fd)
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise BootstrapBlocked(f"unsafe hashed file: {path}")
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        if not _same_tree_stat(os.fstat(fd), before):
            raise BootstrapBlocked(f"hashed file changed during read: {path}")
    finally:
        os.close(fd)
        os.close(parent_fd)
    return digest.hexdigest()


def _read_regular_bytes(path: Path, *, maximum: int = 16 * 1024 * 1024) -> bytes:
    source = Path(path).absolute()
    parent_fd = _open_directory_chain(source.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source.name, flags, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise BootstrapBlocked(f"unsafe bounded input file: {source}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise BootstrapBlocked(f"short bounded input read: {source}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise BootstrapBlocked(f"long bounded input read: {source}")
        if not _same_tree_stat(os.fstat(descriptor), before):
            raise BootstrapBlocked(f"bounded input changed during read: {source}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)
        os.close(parent_fd)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _logical_path_digest(path: Path) -> str:
    return _sha256_bytes(str(Path(path).absolute()).encode("utf-8"))


def _seatbelt_ancestor_paths(parameters: dict[str, Path]) -> tuple[Path, ...]:
    """Return only exact directory ancestors needed by no-follow root walks."""

    ancestors: set[Path] = set()
    for value in parameters.values():
        path = Path(value).absolute()
        for parent in path.parents:
            if parent != Path("/"):
                ancestors.add(parent)
    return tuple(sorted(ancestors, key=lambda item: str(item)))


def render_bootstrap_profile(
    repo_root: Path,
    *,
    bundle_path: Path | None = None,
    relocation_path: Path | None = None,
) -> tuple[str, dict[str, object]]:
    repo = Path(repo_root).absolute()
    bundle = Path(bundle_path).absolute() if bundle_path is not None else repo / ".ceq1-venv"
    relocation = (
        Path(relocation_path).absolute()
        if relocation_path is not None
        else repo / ".ceq1-runtime/bootstrap/relocation-proof"
    )
    parameters = {
        "REPO": repo,
        "BOOTSTRAP_SCRIPT": repo / "scripts/bootstrap_ceq1_runtime.py",
        "BUILDER_SCRIPT": repo / "scripts/build_ceq1_wheelhouse.py",
        "VERIFIER_SCRIPT": repo / "scripts/verify_ceq1_entry.pl",
        "WRAPPER_SCRIPT": repo / "scripts/run_ceq1_env.py",
        "INPUT_MANIFEST": repo / "docs/release-safety/ceq1-input-manifest.json",
        "PRODUCT_LOCK": repo / "requirements.lock",
        "QUALIFICATION_INPUT": repo / "requirements-ceq1.in",
        "QUALIFICATION_LOCK": repo / "requirements-ceq1.lock",
        "RELOCATION": relocation,
        "WHEELHOUSE_MANIFEST": repo / "docs/release-safety/ceq1-wheelhouse-manifest.json",
        "TOOLCHAIN_MANIFEST": repo / "docs/release-safety/ceq1-toolchain-manifest.json",
        "PYTHON_SOURCE": PINNED_PYTHON_ROOT.absolute(),
        "UV": PINNED_UV.absolute(),
        "UV_CACHE": UV_CACHE.absolute(),
        "JDK_ROOT": JDK_ROOT.absolute(),
        "FIRESTORE_JAR": FIRESTORE_JAR.absolute(),
        "RUNTIME": repo / ".ceq1-runtime",
        "BUNDLE": bundle,
    }
    ancestor_paths = _seatbelt_ancestor_paths(parameters)
    ancestor_rules: list[str] = []
    for path in ancestor_paths:
        value = str(path)
        if any(token in value for token in ('"', "\n", "{", "}")):
            raise BootstrapBlocked("unsafe Seatbelt ancestor path")
        ancestor_rules.append(f'  (literal "{value}")')
    render_parameters: dict[str, object] = {
        **parameters,
        "READ_ANCESTOR_RULES": "\n".join(ancestor_rules),
    }
    rendered = BOOTSTRAP_SEATBELT_TEMPLATE
    for key, path in render_parameters.items():
        value = str(path)
        if key != "READ_ANCESTOR_RULES" and ('"' in value or "\n" in value):
            raise BootstrapBlocked("unsafe Seatbelt path")
        rendered = rendered.replace("{" + key + "}", value)
    if "{" in rendered or "}" in rendered:
        raise BootstrapBlocked("unresolved Seatbelt template placeholder")
    if tuple(sorted(render_parameters)) != tuple(sorted(SEATBELT_PLACEHOLDERS)):
        raise BootstrapBlocked("Seatbelt placeholder schema drift")
    template_hash = _sha256_bytes(BOOTSTRAP_SEATBELT_TEMPLATE.encode("utf-8"))
    rendered_hash = _sha256_bytes(rendered.encode("utf-8"))
    parameter_digest = _sha256_bytes(
        _canonical(
            {
                **{
                    key: _logical_path_digest(value)
                    for key, value in sorted(parameters.items())
                },
                "READ_ANCESTOR_RULES": [
                    _logical_path_digest(value) for value in ancestor_paths
                ],
            }
        )
    )
    receipt_payload = {
        "schemaVersion": 1,
        "templateSha256": template_hash,
        "renderedSha256": rendered_hash,
        "parameterDigest": parameter_digest,
        "parameters": {
            **{key: str(value) for key, value in sorted(parameters.items())},
            "READ_ANCESTOR_RULES": [str(value) for value in ancestor_paths],
        },
    }
    return rendered, receipt_payload


def _encoded_policy(profile: str) -> str:
    return base64.b64encode(profile.encode("utf-8")).decode("ascii")


def prove_active_seatbelt(root: Path) -> None:
    """Prove the canonical repo-content denial is active before any mutation."""
    control_path = Path(root) / ".git"
    try:
        descriptor = os.open(
            control_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES):
            return
        raise BootstrapBlocked(f"active Seatbelt proof failed unexpectedly: {error.errno}") from error
    else:
        os.close(descriptor)
    raise BootstrapBlocked("CE-Q1 bootstrap Seatbelt is not active")


def _sandbox_env(root: Path, cache_dir: Path | None = None) -> list[str]:
    task_cache = cache_dir or root / ".ceq1-runtime/bootstrap/uv-cache"
    return [
        "/usr/bin/env",
        "-i",
        f"HOME={root / '.ceq1-runtime/bootstrap/home'}",
        f"TMPDIR={root / '.ceq1-runtime/bootstrap/tmp'}",
        f"XDG_CACHE_HOME={root / '.ceq1-runtime/bootstrap/cache'}",
        f"UV_CACHE_DIR={task_cache}",
        "UV_OFFLINE=true",
        "UV_PYTHON_DOWNLOADS=never",
        "PATH=/usr/bin:/bin",
        "LANG=C",
        "LC_ALL=C",
    ]


def _wrap(
    profile: Path,
    command: list[str],
    root: Path | None = None,
    cache_dir: Path | None = None,
    sandboxed: bool = True,
) -> list[str]:
    root = root or _repo_root()
    prefix = _sandbox_env(root, cache_dir)
    if sandboxed:
        prefix.extend((str(SANDBOX_EXEC), "-f", str(profile)))
    return [*prefix, *command]


def command_contract(
    repo_root: Path | None = None,
    *,
    sandboxed: bool = True,
) -> list[list[str]]:
    root = (repo_root or _repo_root()).resolve()
    runtime = root / ".ceq1-runtime/bootstrap"
    profile = runtime / "profile.sb"
    bundle = root / ".ceq1-venv"
    wheelhouse = root / ".ceq1-runtime/wheelhouse"
    task_cache = runtime / "uv-cache"
    compile_command = _wrap(
        profile,
        [
            str(PINNED_UV),
            "pip",
            "compile",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--generate-hashes",
            "--python",
            str(PINNED_PYTHON),
            "--constraint",
            str(root / "requirements.lock"),
            "--output-file",
            str(runtime / "diagnostic.lock"),
            str(root / "requirements-ceq1.in"),
        ],
        root,
        sandboxed=sandboxed,
    )
    builder_a = _wrap(
        profile,
        [
            str(PINNED_PYTHON),
            "-I",
            "-S",
            "-B",
            str(root / "scripts/build_ceq1_wheelhouse.py"),
            "--manifest",
            str(root / "docs/release-safety/ceq1-wheelhouse-manifest.json"),
            "--cache-root",
            str(task_cache),
            "--output",
            str(runtime / "stage-a"),
        ],
        root,
        sandboxed=sandboxed,
    )
    builder_b = builder_a[:-1] + [str(runtime / "stage-b")]
    venv = _wrap(
        profile,
        [
            str(PINNED_UV),
            "venv",
            "--offline",
            "--no-config",
            "--no-project",
            "--relocatable",
            "--no-python-downloads",
            "--python",
            str(bundle / "python/bin/python3.12"),
            str(bundle / "venv"),
        ],
        root,
        sandboxed=sandboxed,
    )
    product_install = _wrap(
        profile,
        [
            str(PINNED_UV),
            "pip",
            "install",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--link-mode",
            "copy",
            "--exact",
            "--python",
            str(bundle / "venv/bin/python"),
            "-r",
            str(root / "requirements.lock"),
        ],
        root,
        sandboxed=sandboxed,
    )
    derived_install = _wrap(
        profile,
        [
            str(PINNED_UV),
            "pip",
            "install",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--link-mode",
            "copy",
            "--reinstall",
            "--python",
            str(bundle / "venv/bin/python"),
            "-r",
            str(root / "requirements-ceq1.lock"),
        ],
        root,
        sandboxed=sandboxed,
    )
    return [compile_command, builder_a, builder_b, venv, product_install, derived_install]


def render_derived_lock(manifest: dict[str, object]) -> bytes:
    packages = sorted(manifest["packages"], key=lambda item: item["normalizedName"])
    lines = [
        "# Deterministic CE-Q1 derived-wheel lock.",
        "# These hashes identify local RECORD-closed reconstructions, not upstream wheels.",
    ]
    for package in packages:
        lines.append(f"{package['normalizedName']}=={package['version']} \\")
        lines.append(f"    --hash=sha256:{package['wheel']['sha256']}")
    return ("\n".join(lines) + "\n").encode("utf-8")


_STABLE_TREE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _same_tree_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in _STABLE_TREE_FIELDS
    )


def _open_directory_chain(path: Path) -> int:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BootstrapBlocked(f"tree root must be absolute and nonsymlinked: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _safe_tree_entries(root: Path, destination: Path | None = None) -> list[dict[str, object]]:
    source = Path(root)
    source_info = source.lstat()
    if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise BootstrapBlocked(f"tree root is not a real directory: {source}")
    source_fd = _open_directory_chain(source)
    source_root = source.absolute()
    if destination is not None:
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            os.close(source_fd)
            raise BootstrapBlocked(f"copy destination already exists: {destination}")
        destination.mkdir(mode=0o700)
    entries: list[dict[str, object]] = []
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def walk(directory_fd: int, relative_parent: str) -> None:
        before_directory = os.fstat(directory_fd)
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            relative = f"{relative_parent}/{child.name}" if relative_parent else child.name
            info = child.stat(follow_symlinks=False)
            item: dict[str, object] = {
                "relativePath": relative,
                "mode": stat.S_IMODE(info.st_mode),
                "uidClass": "owner" if info.st_uid == os.getuid() else "other",
                "gidClass": "owner" if info.st_gid == os.getgid() else "other",
                "symlinkTarget": None,
                "contentSha256": None,
            }
            output_path = destination / relative if destination is not None else None
            if stat.S_ISDIR(info.st_mode):
                opened = os.open(child.name, directory_flags, dir_fd=directory_fd)
                try:
                    if not _same_tree_stat(os.fstat(opened), info):
                        raise BootstrapBlocked(f"tree directory changed before read: {relative}")
                    item["type"] = "directory"
                    if output_path is not None:
                        output_path.mkdir(mode=0o700)
                    walk(opened, relative)
                    if not _same_tree_stat(os.fstat(opened), info):
                        raise BootstrapBlocked(f"tree directory changed during read: {relative}")
                    if output_path is not None:
                        output_path.chmod(stat.S_IMODE(info.st_mode))
                finally:
                    os.close(opened)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise BootstrapBlocked(f"hard link in runtime tree: {relative}")
                opened = os.open(child.name, file_flags, dir_fd=directory_fd)
                try:
                    opened_info = os.fstat(opened)
                    if not _same_tree_stat(opened_info, info):
                        raise BootstrapBlocked(f"tree file changed before read: {relative}")
                    chunks: list[bytes] = []
                    while chunk := os.read(opened, 1024 * 1024):
                        chunks.append(chunk)
                    if not _same_tree_stat(os.fstat(opened), opened_info):
                        raise BootstrapBlocked(f"tree file changed during read: {relative}")
                finally:
                    os.close(opened)
                data = b"".join(chunks)
                item["type"] = "file"
                item["contentSha256"] = _sha256_bytes(data)
                if output_path is not None:
                    fd = os.open(
                        output_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        stat.S_IMODE(info.st_mode),
                    )
                    try:
                        view = memoryview(data)
                        while view:
                            written = os.write(fd, view)
                            view = view[written:]
                        os.fchmod(fd, stat.S_IMODE(info.st_mode))
                    finally:
                        os.close(fd)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(child.name, dir_fd=directory_fd)
                after = os.stat(child.name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_tree_stat(after, info):
                    raise BootstrapBlocked(f"tree symlink changed during read: {relative}")
                parent_relative = Path(relative).parent
                if os.path.isabs(target):
                    normalized = Path(os.path.normpath(target))
                    if normalized != source_root and source_root not in normalized.parents:
                        raise BootstrapBlocked(f"escaping runtime symlink: {relative}")
                else:
                    combined = os.path.normpath(str(parent_relative / target))
                    if combined == ".." or combined.startswith("../") or os.path.isabs(combined):
                        raise BootstrapBlocked(f"escaping runtime symlink: {relative}")
                    logical = Path(combined)
                item["type"] = "symlink"
                item["symlinkTarget"] = target
                if output_path is not None:
                    parent_output_fd = _open_directory_chain(output_path.parent)
                    try:
                        os.symlink(target, output_path.name, dir_fd=parent_output_fd)
                        os.chmod(
                            output_path.name,
                            stat.S_IMODE(info.st_mode),
                            dir_fd=parent_output_fd,
                            follow_symlinks=False,
                        )
                    finally:
                        os.close(parent_output_fd)
            else:
                raise BootstrapBlocked(f"special runtime entry: {relative}")
            entries.append(item)
        if not _same_tree_stat(os.fstat(directory_fd), before_directory):
            raise BootstrapBlocked(f"tree directory changed during scan: {relative_parent or '.'}")

    try:
        walk(source_fd, "")
    finally:
        os.close(source_fd)
    if destination is not None:
        destination.chmod(stat.S_IMODE(source_info.st_mode))
    return sorted(entries, key=lambda item: item["relativePath"])


def tree_receipt(root: Path) -> dict[str, object]:
    entries = _safe_tree_entries(root)
    return {
        "entryCount": len(entries),
        "treeDigest": _sha256_bytes(_canonical(entries)),
        "algorithmVersion": "ceq1-tree-v1",
    }


def _cache_entries(root: Path, *, identity: bool) -> list[dict[str, object]]:
    """Walk cache metadata through held directory descriptors without payload reads."""
    cache_root = Path(root).absolute()
    root_fd = _open_directory_chain(cache_root)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    entries: list[dict[str, object]] = []

    def walk(directory_fd: int, parent: str) -> None:
        before = os.fstat(directory_fd)
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            relative = f"{parent}/{child.name}" if parent else child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                kind = "directory"
            elif stat.S_ISREG(info.st_mode):
                kind = "file"
                if info.st_nlink != 1:
                    raise BootstrapBlocked(f"hard link in uv cache: {relative}")
            elif stat.S_ISLNK(info.st_mode):
                kind = "symlink"
            else:
                raise BootstrapBlocked(f"special entry in uv cache: {relative}")
            target = os.readlink(child.name, dir_fd=directory_fd) if kind == "symlink" else None
            item: dict[str, object] = {
                "relativePath": relative,
                "type": kind,
                "mode": stat.S_IMODE(info.st_mode),
                "size": info.st_size,
                "symlinkTarget": target,
            }
            if identity:
                item.update(
                    {
                        "device": info.st_dev,
                        "inode": info.st_ino,
                        "links": info.st_nlink,
                        "mtimeNs": info.st_mtime_ns,
                        "ctimeNs": info.st_ctime_ns,
                    }
                )
            entries.append(item)
            if kind == "directory":
                child_fd = os.open(child.name, directory_flags, dir_fd=directory_fd)
                try:
                    if not _same_tree_stat(os.fstat(child_fd), info):
                        raise BootstrapBlocked(f"uv cache directory changed: {relative}")
                    walk(child_fd, relative)
                    if not _same_tree_stat(os.fstat(child_fd), info):
                        raise BootstrapBlocked(f"uv cache directory changed during walk: {relative}")
                finally:
                    os.close(child_fd)
        if not _same_tree_stat(os.fstat(directory_fd), before):
            raise BootstrapBlocked(f"uv cache directory changed during enumeration: {parent or '.'}")

    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    return entries


def _cache_identity_index(root: Path) -> dict[str, dict[str, object]]:
    return {
        str(item["relativePath"]): item
        for item in _cache_entries(root, identity=True)
    }


def _matches_cache_identity(
    info: os.stat_result,
    relative: str,
    expected: dict[str, dict[str, object]],
) -> bool:
    item = expected.get(relative)
    if item is None:
        return False
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    else:
        return False
    return item == {
        "relativePath": relative,
        "type": kind,
        "mode": stat.S_IMODE(info.st_mode),
        "size": info.st_size,
        "symlinkTarget": item["symlinkTarget"] if kind == "symlink" else None,
        "device": info.st_dev,
        "inode": info.st_ino,
        "links": info.st_nlink,
        "mtimeNs": info.st_mtime_ns,
        "ctimeNs": info.st_ctime_ns,
    }


def cache_identity_receipt(root: Path) -> dict[str, object]:
    root_fd = _open_directory_chain(Path(root).absolute())
    try:
        root_info = os.fstat(root_fd)
    finally:
        os.close(root_fd)
    entries = _cache_entries(root, identity=True)
    return {
        "entryCount": len(entries),
        "identityDigest": _sha256_bytes(_canonical(entries)),
        "rootIdentity": {
            "device": root_info.st_dev,
            "inode": root_info.st_ino,
            "mode": root_info.st_mode,
            "links": root_info.st_nlink,
            "mtimeNs": root_info.st_mtime_ns,
            "ctimeNs": root_info.st_ctime_ns,
        },
        "algorithmVersion": "ceq1-cache-identity-v1",
    }


def _logical_cache_target(cache_root: Path, relative: str, target: str | None) -> str | None:
    if target is None:
        return None
    if os.path.isabs(target):
        target_path = Path(os.path.normpath(target))
        try:
            within = target_path.relative_to(cache_root)
        except ValueError:
            try:
                pinned_text = target_path.relative_to(PINNED_PYTHON_ROOT)
            except ValueError:
                pass
            else:
                try:
                    resolved = target_path.resolve(strict=True)
                    pinned = resolved.relative_to(PINNED_PYTHON_ROOT)
                except (OSError, ValueError):
                    return "@DENIED_EXTERNAL_PYTHON/" + pinned_text.as_posix()
                return "@PINNED_PYTHON/" + pinned.as_posix()
            try:
                denied = target_path.relative_to(UV_PYTHON_STORE)
            except ValueError as error:
                raise BootstrapBlocked(f"escaping absolute uv-cache link: {relative}") from error
            try:
                resolved = target_path.resolve(strict=True)
                pinned_alias = resolved.relative_to(PINNED_PYTHON_ROOT)
            except (OSError, ValueError):
                pass
            else:
                return "@PINNED_PYTHON/" + pinned_alias.as_posix()
            return "@DENIED_EXTERNAL_PYTHON/" + denied.as_posix()
        return "@CACHE/" + within.as_posix()
    normalized = os.path.normpath(str(Path(relative).parent / target))
    if normalized == ".." or normalized.startswith("../"):
        raise BootstrapBlocked(f"escaping relative uv-cache link: {relative}")
    return target


def cache_logical_receipt(root: Path) -> dict[str, object]:
    cache_root = Path(root).absolute()
    entries = _cache_entries(cache_root, identity=False)
    for item in entries:
        logical_target = _logical_cache_target(
            cache_root,
            str(item["relativePath"]),
            item["symlinkTarget"] if isinstance(item["symlinkTarget"], str) else None,
        )
        item["symlinkTarget"] = logical_target
        if item["type"] == "symlink" and logical_target is not None:
            item["size"] = len(logical_target.encode("utf-8"))
    return {
        "entryCount": len(entries),
        "logicalDigest": _sha256_bytes(_canonical(entries)),
        "algorithmVersion": "ceq1-cache-logical-v1",
    }


def require_source_cache_logical_stability(
    root: Path, expected: dict[str, object]
) -> None:
    if cache_logical_receipt(root) != expected:
        raise BootstrapBlocked("source uv cache logical topology changed during bootstrap")


def rebase_and_validate_cache_clone(
    source: Path,
    destination: Path,
    expected_identity: dict[str, object],
    expected_logical: dict[str, object],
) -> dict[str, object]:
    source_root = Path(source).absolute()
    destination_root = Path(destination).absolute()
    if cache_identity_receipt(source_root) != expected_identity:
        raise BootstrapBlocked("source uv cache changed before clone validation")
    destination_fd = _open_directory_chain(destination_root)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def walk(directory_fd: int, parent: str) -> None:
        before = os.fstat(directory_fd)
        intentionally_modified = False
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            relative = f"{parent}/{child.name}" if parent else child.name
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(child.name, directory_flags, dir_fd=directory_fd)
                try:
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(child.name, dir_fd=directory_fd)
                if os.path.isabs(target):
                    target_path = Path(os.path.normpath(target))
                    try:
                        suffix = target_path.relative_to(source_root)
                    except ValueError:
                        try:
                            target_path.relative_to(destination_root)
                        except ValueError:
                            logical = _logical_cache_target(source_root, relative, target)
                            if not isinstance(logical, str) or not logical.startswith(
                                ("@PINNED_PYTHON/", "@DENIED_EXTERNAL_PYTHON/")
                            ):
                                raise BootstrapBlocked(
                                    f"escaping absolute cloned-cache link: {relative}"
                                )
                    else:
                        new_target = str(destination_root / suffix)
                        os.unlink(child.name, dir_fd=directory_fd)
                        os.symlink(new_target, child.name, dir_fd=directory_fd)
                        os.chmod(
                            child.name,
                            stat.S_IMODE(info.st_mode),
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        intentionally_modified = True
            elif not stat.S_ISREG(info.st_mode):
                raise BootstrapBlocked(f"special entry in cloned uv cache: {relative}")
        after = os.fstat(directory_fd)
        if intentionally_modified:
            stable_identity = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid")
            if any(getattr(after, field) != getattr(before, field) for field in stable_identity):
                raise BootstrapBlocked(f"cloned uv cache directory identity changed: {parent or '.'}")
        elif not _same_tree_stat(after, before):
            raise BootstrapBlocked(f"cloned uv cache changed during rebase: {parent or '.'}")

    try:
        walk(destination_fd, "")
    finally:
        os.close(destination_fd)
    actual = cache_logical_receipt(destination_root)
    if actual != expected_logical:
        raise BootstrapBlocked("task uv cache topology differs from source")
    if cache_identity_receipt(source_root) != expected_identity:
        raise BootstrapBlocked("source uv cache changed during clone validation")
    return actual


_CLONE_NOOWNERCOPY = 0x0002
_CLONE_NOFOLLOW_ANY = 0x0008
_CLONE_RESOLVE_BENEATH = 0x0010
_FCLONE_FLAGS = _CLONE_NOOWNERCOPY | _CLONE_NOFOLLOW_ANY | _CLONE_RESOLVE_BENEATH


def _same_cache_root_identity(info: os.stat_result, receipt: dict[str, object]) -> bool:
    expected = receipt.get("rootIdentity")
    if not isinstance(expected, dict):
        return False
    return expected == {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "links": info.st_nlink,
        "mtimeNs": info.st_mtime_ns,
        "ctimeNs": info.st_ctime_ns,
    }


def _create_absolute_directory(path: Path, mode: int) -> int:
    destination = Path(path).absolute()
    parent_fd = _open_directory_chain(destination.parent)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        try:
            os.mkdir(destination.name, mode, dir_fd=parent_fd)
        except FileExistsError as error:
            raise BootstrapBlocked(f"cache clone destination already exists: {destination}") from error
        return os.open(destination.name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def clone_cache_to_task(
    source: Path,
    destination: Path,
    expected_identity: dict[str, object],
    expected_logical: dict[str, object],
) -> dict[str, object]:
    """Clone a cache through held dirfds, then rebase and validate its links."""
    source_root = Path(source).absolute()
    expected_entries = _cache_identity_index(source_root)
    if _sha256_bytes(_canonical(list(expected_entries.values()))) != expected_identity.get(
        "identityDigest"
    ):
        raise BootstrapBlocked("source uv cache identity index drift")
    source_fd = _open_directory_chain(source_root)
    source_before = os.fstat(source_fd)
    if not _same_cache_root_identity(source_before, expected_identity):
        os.close(source_fd)
        raise BootstrapBlocked("source uv cache root identity drift")
    destination_fd = _create_absolute_directory(
        Path(destination), stat.S_IMODE(source_before.st_mode)
    )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    fclonefileat = libc.fclonefileat
    fclonefileat.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    fclonefileat.restype = ctypes.c_int

    def copy_file(source_file_fd: int, destination_directory_fd: int, name: str, mode: int) -> None:
        result = fclonefileat(
            source_file_fd,
            destination_directory_fd,
            os.fsencode(name),
            _FCLONE_FLAGS,
        )
        if result == 0:
            cloned = os.open(name, file_flags, dir_fd=destination_directory_fd)
            try:
                os.fchmod(cloned, mode)
            finally:
                os.close(cloned)
            return
        clone_error = ctypes.get_errno()
        if clone_error not in (errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL):
            raise OSError(clone_error, os.strerror(clone_error), name)
        destination_file_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=destination_directory_fd,
        )
        try:
            os.lseek(source_file_fd, 0, os.SEEK_SET)
            while data := os.read(source_file_fd, 1024 * 1024):
                view = memoryview(data)
                while view:
                    written = os.write(destination_file_fd, view)
                    view = view[written:]
            os.fchmod(destination_file_fd, mode)
        finally:
            os.close(destination_file_fd)

    def copy_directory(
        source_directory_fd: int,
        destination_directory_fd: int,
        relative_parent: str,
    ) -> None:
        directory_before = os.fstat(source_directory_fd)
        with os.scandir(source_directory_fd) as iterator:
            children = sorted(iterator, key=lambda item: item.name)
        for child in children:
            relative = (
                f"{relative_parent}/{child.name}" if relative_parent else child.name
            )
            info = child.stat(follow_symlinks=False)
            if not _matches_cache_identity(info, relative, expected_entries):
                raise BootstrapBlocked(f"source cache identity drift before open: {relative}")
            if stat.S_ISDIR(info.st_mode):
                source_child_fd = os.open(
                    child.name, directory_flags, dir_fd=source_directory_fd
                )
                if not _same_tree_stat(os.fstat(source_child_fd), info):
                    os.close(source_child_fd)
                    raise BootstrapBlocked("source cache directory raced during clone")
                os.mkdir(child.name, stat.S_IMODE(info.st_mode), dir_fd=destination_directory_fd)
                destination_child_fd = os.open(
                    child.name, directory_flags, dir_fd=destination_directory_fd
                )
                try:
                    copy_directory(source_child_fd, destination_child_fd, relative)
                    os.fchmod(destination_child_fd, stat.S_IMODE(info.st_mode))
                finally:
                    os.close(destination_child_fd)
                    os.close(source_child_fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise BootstrapBlocked("hard link in source cache clone")
                source_file_fd = os.open(child.name, file_flags, dir_fd=source_directory_fd)
                try:
                    opened_info = os.fstat(source_file_fd)
                    if not _matches_cache_identity(opened_info, relative, expected_entries):
                        raise BootstrapBlocked(
                            f"source cache identity drift after file open: {relative}"
                        )
                    if not _same_tree_stat(opened_info, info):
                        raise BootstrapBlocked("source cache file raced before clone")
                    copy_file(
                        source_file_fd,
                        destination_directory_fd,
                        child.name,
                        stat.S_IMODE(info.st_mode),
                    )
                    if not _same_tree_stat(os.fstat(source_file_fd), opened_info):
                        raise BootstrapBlocked("source cache file raced during clone")
                finally:
                    os.close(source_file_fd)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(child.name, dir_fd=source_directory_fd)
                after = os.stat(
                    child.name, dir_fd=source_directory_fd, follow_symlinks=False
                )
                if not _same_tree_stat(after, info):
                    raise BootstrapBlocked("source cache symlink raced during clone")
                os.symlink(target, child.name, dir_fd=destination_directory_fd)
                os.chmod(
                    child.name,
                    stat.S_IMODE(info.st_mode),
                    dir_fd=destination_directory_fd,
                    follow_symlinks=False,
                )
            else:
                raise BootstrapBlocked("special entry in source cache clone")
        if not _same_tree_stat(os.fstat(source_directory_fd), directory_before):
            raise BootstrapBlocked("source cache directory raced during clone")

    try:
        copy_directory(source_fd, destination_fd, "")
        os.fchmod(destination_fd, stat.S_IMODE(source_before.st_mode))
        if not _same_tree_stat(os.fstat(source_fd), source_before):
            raise BootstrapBlocked("source uv cache root changed during clone")
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    return rebase_and_validate_cache_clone(
        source_root,
        Path(destination),
        expected_identity,
        expected_logical,
    )


def denied_external_cache_link_targets(root: Path) -> list[str]:
    targets: list[str] = []
    for item in _cache_entries(Path(root).absolute(), identity=False):
        target = item["symlinkTarget"]
        if not isinstance(target, str) or not os.path.isabs(target):
            continue
        logical = _logical_cache_target(Path(root).absolute(), str(item["relativePath"]), target)
        if isinstance(logical, str) and logical.startswith("@DENIED_EXTERNAL_PYTHON/"):
            targets.append(target)
    return sorted(set(targets))


def prove_denied_cache_link_targets(root: Path) -> None:
    targets = denied_external_cache_link_targets(root)
    if not targets:
        return
    for target in targets:
        try:
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES):
                continue
            raise BootstrapBlocked(
                f"denied external cache link had unexpected result: {error.errno}"
            ) from error
        else:
            os.close(descriptor)
        raise BootstrapBlocked("denied external cache link target was readable")


def cache_metadata_receipt(root: Path) -> dict[str, object]:
    """Compatibility name for the stronger identity receipt."""
    return cache_identity_receipt(root)


def _load_builder(root: Path):
    path = root / "scripts/build_ceq1_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("ceq1_bootstrap_builder", path)
    if spec is None or spec.loader is None:
        raise BootstrapBlocked("cannot load wheel builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _validate_static_inputs(root: Path) -> dict[str, object]:
    if sys.version_info[:3] != (3, 12, 13):
        raise BootstrapBlocked("wrong bootstrap Python")
    input_manifest = json.loads(
        _read_regular_bytes(_input_manifest_path(root)).decode("utf-8")
    )
    validate_input_manifest(root, input_manifest)
    if _sha256_file(PINNED_PYTHON) != PYTHON_SHA256:
        raise BootstrapBlocked("pinned Python launcher drift")
    if _sha256_file(PINNED_UV) != UV_SHA256:
        raise BootstrapBlocked("pinned uv drift")
    manifest_path = root / "docs/release-safety/ceq1-wheelhouse-manifest.json"
    raw_manifest = json.loads(_read_regular_bytes(manifest_path).decode("utf-8"))
    if not isinstance(raw_manifest, dict):
        raise BootstrapBlocked("wheel manifest shape drift")
    expected_builder_hash = raw_manifest.get("builderSha256")
    builder_hash = _sha256_file(root / "scripts/build_ceq1_wheelhouse.py")
    if expected_builder_hash != builder_hash:
        raise BootstrapBlocked("builder source drift")
    builder = _load_builder(root)
    manifest = builder.load_closed_manifest(manifest_path)
    if manifest["python"]["launcherSha256"] != PYTHON_SHA256:
        raise BootstrapBlocked("wheel manifest Python drift")
    zipfile_path = Path(__import__("zipfile").__file__)
    if manifest["python"]["zipfileSha256"] != _sha256_file(zipfile_path):
        raise BootstrapBlocked("wheel manifest zipfile.py drift")
    return manifest


def _close_non_stdio_fds() -> None:
    try:
        candidates = [int(name) for name in os.listdir("/dev/fd") if name.isdecimal()]
    except OSError:
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        candidates = list(range(3, min(int(soft), 65536)))
    for descriptor in candidates:
        if descriptor <= 2:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass


def _make_private_dir(path: Path) -> None:
    if path.is_symlink() or path.exists():
        raise BootstrapBlocked(f"task directory already exists: {path}")
    parent_info = path.parent.lstat()
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise BootstrapBlocked(f"unsafe task directory parent: {path.parent}")
    path.mkdir(mode=0o700, exist_ok=False)
    info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        raise BootstrapBlocked(f"unsafe task directory: {path}")


def create_runtime_target(mode: str, target: Path) -> None:
    """Create the one exact, empty private runtime root for this build mode."""

    target = Path(target)
    if mode == "derive-review-candidate":
        _make_private_dir(target.parent)
    elif mode != "prepare":
        raise BootstrapBlocked("unsupported runtime target mode")
    _make_private_dir(target)


def review_candidate_artifact_paths(bootstrap: Path) -> tuple[Path, Path]:
    review_root = Path(bootstrap) / "review-candidate"
    return (
        review_root / "requirements-ceq1.lock",
        review_root / "ceq1-toolchain-manifest.json",
    )


def _ensure_private_root(path: Path) -> None:
    if path.is_symlink():
        raise BootstrapBlocked(f"symlinked task root: {path}")
    if not path.exists():
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise BootstrapBlocked(f"unsafe task-root parent: {path.parent}")
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
    ):
        raise BootstrapBlocked(f"unsafe task root: {path}")


def _run(command: list[str], *, cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise BootstrapBlocked(
            f"contained command failed ({result.returncode}): {command[-1]}\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )


def _copy_python(source: Path, destination: Path) -> None:
    expected_entries = _safe_tree_entries(source, destination)
    expected = {
        "entryCount": len(expected_entries),
        "treeDigest": _sha256_bytes(_canonical(expected_entries)),
        "algorithmVersion": "ceq1-tree-v1",
    }
    actual = tree_receipt(destination)
    if expected != actual:
        raise BootstrapBlocked("copied Python tree differs from source")


def _flatten_wheels(stage: Path, destination: Path, manifest: dict[str, object]) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for package in manifest["packages"]:
        source = stage / package["normalizedName"] / package["wheel"]["filename"]
        target = destination / package["wheel"]["filename"]
        shutil.copy2(source, target)
        if target.stat().st_size != package["wheel"]["size"] or _sha256_file(target) != package["wheel"]["sha256"]:
            raise BootstrapBlocked("promoted derived wheel mismatch")


def _validate_wheelhouse(
    destination: Path,
    manifest: dict[str, object],
    *,
    require_sealed: bool = False,
) -> None:
    info = destination.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise BootstrapBlocked("canonical wheelhouse is not a real directory")
    if require_sealed and stat.S_IMODE(info.st_mode) != 0o555:
        raise BootstrapBlocked("canonical wheelhouse is not sealed")
    expected = {
        package["wheel"]["filename"]: package["wheel"]
        for package in manifest["packages"]
    }
    actual = {entry.name: entry for entry in destination.iterdir()}
    if set(actual) != set(expected):
        raise BootstrapBlocked("canonical wheelhouse closure mismatch")
    for name, path in actual.items():
        entry = path.lstat()
        wheel = expected[name]
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            raise BootstrapBlocked("unsafe canonical wheelhouse member")
        if require_sealed and stat.S_IMODE(entry.st_mode) != 0o444:
            raise BootstrapBlocked("canonical wheelhouse member is not sealed")
        if entry.st_size != wheel["size"] or _sha256_file(path) != wheel["sha256"]:
            raise BootstrapBlocked("canonical wheelhouse member drift")


def _ensure_canonical_wheelhouse(
    stage: Path,
    destination: Path,
    manifest: dict[str, object],
) -> None:
    if destination.exists() or destination.is_symlink():
        _validate_wheelhouse(destination, manifest, require_sealed=True)
        return
    # macOS refuses to rename a directory once its contents are sealed read-only.
    # Build at the canonical path, close and verify every member, then seal it.
    _flatten_wheels(stage, destination, manifest)
    _validate_wheelhouse(destination, manifest)
    _make_read_only(destination)
    _validate_wheelhouse(destination, manifest, require_sealed=True)


def _rewrite_venv_links(bundle: Path) -> None:
    bin_dir = bundle / "venv/bin"
    for name in ("python", "python3", "python3.12"):
        path = bin_dir / name
        if path.exists() or path.is_symlink():
            path.unlink()
    os.symlink("../../python/bin/python3.12", bin_dir / "python")
    os.symlink("python", bin_dir / "python3")
    os.symlink("python", bin_dir / "python3.12")
    (bundle / "venv/pyvenv.cfg").write_text(
        "home = ../python/bin\n"
        "implementation = CPython\n"
        "uv = 0.11.3\n"
        "version_info = 3.12.13\n"
        "include-system-site-packages = false\n"
        "relocatable = true\n",
        encoding="utf-8",
    )


def normalize_uv_install_metadata(bundle: Path, manifest: dict[str, object]) -> None:
    canonical_cache = (
        b'{"timestamp":{"secs_since_epoch":0,"nanos_since_epoch":0},'
        b'"commit":null,"tags":null,"env":{},"directories":{}}'
    )
    site_packages = bundle / "venv/lib/python3.12/site-packages"
    for package in manifest["packages"]:
        record_paths = [
            row["path"]
            for row in package["members"]
            if row["path"].endswith(".dist-info/RECORD")
        ]
        if len(record_paths) != 1:
            raise BootstrapBlocked("derived distribution RECORD identity mismatch")
        record_relative = record_paths[0]
        if record_relative.startswith("/") or ".." in Path(record_relative).parts:
            raise BootstrapBlocked("unsafe installed RECORD path")
        dist_info = record_relative.rsplit("/", 1)[0]
        record_path = site_packages / record_relative
        cache_relative = f"{dist_info}/uv_cache.json"
        cache_path = site_packages / cache_relative
        cache_value = json.loads(cache_path.read_text(encoding="utf-8"))
        if set(cache_value) != {"timestamp", "commit", "tags", "env", "directories"}:
            raise BootstrapBlocked("uv install metadata shape drift")
        timestamp = cache_value["timestamp"]
        if (
            not isinstance(timestamp, dict)
            or set(timestamp) != {"secs_since_epoch", "nanos_since_epoch"}
            or any(
                isinstance(timestamp[key], bool)
                or not isinstance(timestamp[key], int)
                or timestamp[key] < 0
                for key in timestamp
            )
            or cache_value["commit"] is not None
            or cache_value["tags"] is not None
            or cache_value["env"] != {}
            or cache_value["directories"] != {}
        ):
            raise BootstrapBlocked("unexpected uv install metadata")
        cache_path.write_bytes(canonical_cache)
        digest = base64.urlsafe_b64encode(hashlib.sha256(canonical_cache).digest()).rstrip(b"=").decode("ascii")
        rows = list(csv.reader(record_path.read_text(encoding="utf-8").splitlines()))
        target_count = 0
        self_count = 0
        for row in rows:
            if len(row) != 3:
                raise BootstrapBlocked("installed RECORD row shape drift")
            if row[0] == cache_relative:
                row[1] = f"sha256={digest}"
                row[2] = str(len(canonical_cache))
                target_count += 1
            if row[0] == record_relative:
                if row[1:] != ["", ""]:
                    raise BootstrapBlocked("installed RECORD self row must be blank")
                self_count += 1
        if target_count != 1 or self_count != 1:
            raise BootstrapBlocked("installed uv metadata RECORD closure mismatch")
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        record_path.write_text(output.getvalue(), encoding="utf-8", newline="")


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_pinned_requirements(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^ \\]+)(?: \\)?$")
    for line in _read_regular_bytes(Path(path)).decode("utf-8").splitlines():
        match = pattern.fullmatch(line)
        if not match:
            continue
        name = _normalized_distribution_name(match.group(1))
        version = match.group(2)
        if name in result and result[name] != version:
            raise BootstrapBlocked("requirement version conflict")
        result[name] = version
    if not result:
        raise BootstrapBlocked("pinned requirements are empty")
    return result


def validate_installed_environment(bundle: Path, root: Path) -> dict[str, object]:
    expected = parse_pinned_requirements(root / "requirements.lock")
    for name, version in parse_pinned_requirements(root / "requirements-ceq1.lock").items():
        if name in expected and expected[name] != version:
            raise BootstrapBlocked("product/qualification requirement conflict")
        expected[name] = version
    site = bundle / "venv/lib/python3.12/site-packages"
    distributions = list(importlib.metadata.distributions(path=[str(site)]))
    actual: dict[str, importlib.metadata.Distribution] = {}
    for distribution in distributions:
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise BootstrapBlocked("installed distribution is missing Name")
        name = _normalized_distribution_name(raw_name)
        if name in actual:
            raise BootstrapBlocked("duplicate installed distribution")
        actual[name] = distribution
    versions = {name: distribution.version for name, distribution in actual.items()}
    if versions != expected:
        raise BootstrapBlocked("installed distribution name/version closure mismatch")
    venv_root = (bundle / "venv").resolve()
    owned_paths: set[str] = set()
    member_count = 0
    for name, distribution in actual.items():
        dist_info = Path(distribution._path)
        if dist_info.is_symlink() or not dist_info.is_dir():
            raise BootstrapBlocked("installed dist-info root is unsafe")
        record = dist_info / "RECORD"
        rows = list(csv.reader(record.read_text(encoding="utf-8").splitlines()))
        seen: set[str] = set()
        self_rows = 0
        for row in rows:
            if len(row) != 3:
                raise BootstrapBlocked("installed RECORD row shape mismatch")
            relative, digest, size_text = row
            if not relative or relative in seen or "\\" in relative:
                raise BootstrapBlocked("installed RECORD path closure mismatch")
            seen.add(relative)
            lexical_candidate = site / relative
            candidate = lexical_candidate.resolve()
            if candidate != venv_root and venv_root not in candidate.parents:
                raise BootstrapBlocked("installed RECORD path escaped venv")
            if lexical_candidate.is_symlink():
                raise BootstrapBlocked("installed RECORD member is a symlink")
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise BootstrapBlocked("installed RECORD member is unsafe")
            record_relative = record.relative_to(site).as_posix()
            if relative == record_relative:
                self_rows += 1
                if digest or size_text:
                    raise BootstrapBlocked("installed RECORD self row must be blank")
                continue
            if not digest.startswith("sha256=") or not size_text.isdecimal():
                raise BootstrapBlocked("installed RECORD digest/size missing")
            data = candidate.read_bytes()
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            if digest != f"sha256={encoded}" or int(size_text) != len(data):
                raise BootstrapBlocked("installed RECORD member hash/size mismatch")
            member_count += 1
            owned_paths.add(candidate.relative_to(venv_root).as_posix())
        files = {str(path) for path in (distribution.files or [])}
        if self_rows != 1 or seen != files:
            raise BootstrapBlocked("installed RECORD/importlib member closure mismatch")
        owned_paths.add(record.resolve().relative_to(venv_root).as_posix())
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
    symlink_scaffold = {
        "bin/python": "../../python/bin/python3.12",
        "bin/python3": "python",
        "bin/python3.12": "python",
    }
    actual_paths: set[str] = set()
    actual_directories: set[str] = set()
    for path in venv_root.rglob("*"):
        entry = path.lstat()
        relative = path.relative_to(venv_root).as_posix()
        if stat.S_ISREG(entry.st_mode):
            actual_paths.add(relative)
        elif stat.S_ISLNK(entry.st_mode):
            actual_paths.add(relative)
            if symlink_scaffold.get(relative) != os.readlink(path):
                raise BootstrapBlocked("installed scaffold symlink drift")
        elif stat.S_ISDIR(entry.st_mode):
            actual_directories.add(relative)
        else:
            raise BootstrapBlocked("special entry in installed environment")
    expected_paths = owned_paths | regular_scaffold | set(symlink_scaffold)
    if actual_paths != expected_paths:
        raise BootstrapBlocked("installed environment contains unowned paths")
    expected_directories: set[str] = set()
    for relative in expected_paths:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    if actual_directories != expected_directories:
        raise BootstrapBlocked("installed environment directory closure mismatch")
    return {
        "distributionCount": len(actual),
        "memberCount": member_count,
        "versionsDigest": _sha256_bytes(_canonical(versions)),
    }


def validate_bundle_path_receipt(bundle: Path, receipt: dict[str, object]) -> None:
    required = {"executable", "prefix", "basePrefix", "stdlib", "platstdlib", "extensions"}
    if set(receipt) != required or not isinstance(receipt["extensions"], list):
        raise BootstrapBlocked("bundle path receipt shape mismatch")
    root = bundle.resolve()
    filesystem_values = [
        receipt["executable"],
        receipt["prefix"],
        receipt["basePrefix"],
        receipt["stdlib"],
        receipt["platstdlib"],
    ]
    for value in filesystem_values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise BootstrapBlocked(f"invalid bundle filesystem path: {value}")
        resolved = Path(value).resolve()
        if resolved != root and root not in resolved.parents:
            raise BootstrapBlocked(f"bundle path escaped: {value}")
    for value in receipt["extensions"]:
        if value in {"built-in", "frozen"}:
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise BootstrapBlocked(f"invalid extension origin: {value}")
        resolved = Path(value).resolve()
        if resolved != root and root not in resolved.parents:
            raise BootstrapBlocked(f"bundle path escaped: {value}")


def _bundle_path_receipt(bundle: Path) -> dict[str, object]:
    code = (
        "import importlib.util,json,sys,sysconfig;"
        "mods=['_ssl','_hashlib','_sqlite3'];"
        "print(json.dumps({'executable':sys.executable,'prefix':sys.prefix,"
        "'basePrefix':sys.base_prefix,'stdlib':sysconfig.get_path('stdlib'),"
        "'platstdlib':sysconfig.get_path('platstdlib'),'extensions':"
        "[importlib.util.find_spec(x).origin for x in mods]},sort_keys=True))"
    )
    result = subprocess.run(
        [str(bundle / "venv/bin/python"), "-I", "-S", "-B", "-c", code],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise BootstrapBlocked(f"bundle path probe failed: {result.stderr}")
    receipt = json.loads(result.stdout)
    validate_bundle_path_receipt(bundle, receipt)
    return receipt


def _make_read_only(root: Path) -> None:
    root_fd = _open_directory_chain(Path(root))
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    def seal(directory_fd: int) -> None:
        with os.scandir(directory_fd) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name)
        for child in children:
            info = child.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                opened = os.open(child.name, directory_flags, dir_fd=directory_fd)
                try:
                    if not _same_tree_stat(os.fstat(opened), info):
                        raise BootstrapBlocked("directory changed before seal")
                    seal(opened)
                    os.fchmod(opened, 0o555)
                finally:
                    os.close(opened)
            elif stat.S_ISREG(info.st_mode):
                opened = os.open(child.name, file_flags, dir_fd=directory_fd)
                try:
                    if not _same_tree_stat(os.fstat(opened), info):
                        raise BootstrapBlocked("file changed before seal")
                    os.fchmod(opened, 0o555 if info.st_mode & 0o111 else 0o444)
                finally:
                    os.close(opened)
            else:
                raise BootstrapBlocked("special entry encountered during seal")
    try:
        seal(root_fd)
        os.fchmod(root_fd, 0o555)
    finally:
        os.close(root_fd)


def _version_output_sha(argv: list[str]) -> str:
    result = subprocess.run(
        argv,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise BootstrapBlocked(f"version probe failed: {argv[0]}")
    return _sha256_bytes(result.stdout)


def validate_runtime_receipt(root: Path, receipt_path: Path) -> dict[str, object]:
    value = json.loads(_read_regular_bytes(Path(receipt_path)).decode("utf-8"))
    expected_keys = {
        "schemaVersion",
        "templateSha256",
        "renderedSha256",
        "parameterDigest",
        "parameters",
        "inputManifestSha256",
        "toolchainManifestSha256",
        "verifierSha256",
        "wheelhouseManifestSha256",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise BootstrapBlocked("runtime receipt closed keys mismatch")
    parameters = value.get("parameters")
    relocation_value = parameters.get("RELOCATION") if isinstance(parameters, dict) else None
    if not isinstance(relocation_value, str):
        raise BootstrapBlocked("runtime receipt relocation binding is missing")
    relocation = Path(relocation_value)
    runtime_root = Path(root) / ".ceq1-runtime"
    if (
        not relocation.is_absolute()
        or relocation.name != "relocation-proof"
        or relocation.parent.parent != runtime_root
        or not relocation.parent.name.startswith("bootstrap-")
    ):
        raise BootstrapBlocked("runtime receipt relocation binding escaped")
    expected = _runtime_receipt_payload(
        Path(root),
        render_bootstrap_profile(
            Path(root),
            bundle_path=Path(root) / ".ceq1-venv",
            relocation_path=relocation,
        )[1],
    )
    if value != expected:
        raise BootstrapBlocked("runtime receipt does not match current checkout")
    return value


def _runtime_receipt_payload(root: Path, profile_receipt: dict[str, object]) -> dict[str, object]:
    value = dict(profile_receipt)
    value.update(
        {
            "inputManifestSha256": _sha256_file(
                root / "docs/release-safety/ceq1-input-manifest.json"
            ),
            "toolchainManifestSha256": _sha256_file(
                root / "docs/release-safety/ceq1-toolchain-manifest.json"
            ),
            "verifierSha256": _sha256_file(root / "scripts/verify_ceq1_entry.pl"),
            "wheelhouseManifestSha256": _sha256_file(
                root / "docs/release-safety/ceq1-wheelhouse-manifest.json"
            ),
        }
    )
    return value


def _atomic_write_bytes(path: Path, data: bytes, mode: int) -> None:
    destination = Path(path).absolute()
    parent_fd = _open_directory_chain(destination.parent)
    temporary = f".{destination.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise BootstrapBlocked("short atomic receipt write")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _write_runtime_receipt(root: Path, receipt: dict[str, object]) -> Path:
    path = root / ".ceq1-runtime/bootstrap-receipt.json"
    _atomic_write_bytes(path, _canonical(receipt) + b"\n", 0o400)
    return path


_VERSION_OUTPUT_ARTIFACTS = frozenset(
    {"cpythonSource", "openjdkSource", "firestoreJar", "uv"}
)


def _recorded_version_output_hashes(toolchain: dict[str, object]) -> dict[str, str]:
    artifacts = toolchain.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BootstrapBlocked("toolchain artifact records are absent")
    result: dict[str, str] = {}
    for name in sorted(_VERSION_OUTPUT_ARTIFACTS):
        record = artifacts.get(name)
        value = record.get("versionOutputSha256") if isinstance(record, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise BootstrapBlocked(f"toolchain {name} version receipt is invalid")
        result[name] = value
    return result


def _expected_toolchain(
    root: Path,
    runtime: Path,
    manifest: dict[str, object],
    *,
    version_output_hashes: dict[str, str] | None = None,
) -> dict[str, object]:
    if version_output_hashes is None:
        version_output_hashes = {
            "cpythonSource": _version_output_sha([str(PINNED_PYTHON), "--version"]),
            "openjdkSource": _version_output_sha([str(JDK_ROOT / "bin/java"), "-version"]),
            "firestoreJar": _version_output_sha(
                [str(JDK_ROOT / "bin/java"), "-jar", str(FIRESTORE_JAR), "--version"]
            ),
            "uv": _version_output_sha([str(PINNED_UV), "--version"]),
        }
    if set(version_output_hashes) != _VERSION_OUTPUT_ARTIFACTS or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in version_output_hashes.values()
    ):
        raise BootstrapBlocked("toolchain version receipt mapping drift")
    zipfile_path = Path(__import__("zipfile").__file__)
    artifacts = {
        "entryVerifier": {
            "sha256": _sha256_file(root / "scripts/verify_ceq1_entry.pl"),
        },
        "directWrapper": {
            "sha256": _sha256_file(root / "scripts/run_ceq1_env.py"),
        },
        "cpythonSource": {
            **tree_receipt(PINNED_PYTHON_ROOT),
            "launcherSha256": PYTHON_SHA256,
            "version": "3.12.13",
            "versionOutputSha256": version_output_hashes["cpythonSource"],
        },
        "openjdkSource": {
            **tree_receipt(JDK_ROOT),
            "version": "25.0.2",
            "versionOutputSha256": version_output_hashes["openjdkSource"],
        },
        "firestoreJar": {
            "version": "1.19.8",
            "sha256": _sha256_file(FIRESTORE_JAR),
            "size": FIRESTORE_JAR.stat().st_size,
            "versionOutputSha256": version_output_hashes["firestoreJar"],
        },
        "uv": {
            "version": "0.11.3",
            "sha256": UV_SHA256,
            "versionOutputSha256": version_output_hashes["uv"],
        },
        "zipfile": {"sha256": _sha256_file(zipfile_path)},
    }
    return {
        "schemaVersion": 1,
        "algorithmVersion": "ceq1-toolchain-v1",
        "artifacts": artifacts,
        "lockfiles": {
            "product": _sha256_file(root / "requirements.lock"),
            "qualification": _sha256_file(root / "requirements-ceq1.lock"),
        },
        "wheelhouseManifestSha256": _sha256_file(root / "docs/release-safety/ceq1-wheelhouse-manifest.json"),
        "inputManifestSha256": _sha256_file(
            root / "docs/release-safety/ceq1-input-manifest.json"
        ),
        "bootstrapSha256": _sha256_file(root / "scripts/bootstrap_ceq1_runtime.py"),
        "builderSha256": _sha256_file(root / "scripts/build_ceq1_wheelhouse.py"),
        "seatbeltTemplate": {
            "sha256": _sha256_bytes(BOOTSTRAP_SEATBELT_TEMPLATE.encode("utf-8")),
            "placeholders": sorted(SEATBELT_PLACEHOLDERS),
        },
        "sealedRuntime": tree_receipt(runtime),
    }


def _validate_toolchain_envelope(manifest: dict[str, object]) -> None:
    if not isinstance(manifest, dict) or set(manifest) != _TOOLCHAIN_KEYS:
        raise BootstrapBlocked("toolchain manifest closed keys mismatch")
    serialized = json.dumps(manifest, sort_keys=True)
    if "/Users/" in serialized or "file://" in serialized:
        raise BootstrapBlocked("absolute path leaked into toolchain manifest")


def validate_committed_toolchain(root: Path, manifest: dict[str, object]) -> None:
    _validate_toolchain_envelope(manifest)
    runtime = root / ".ceq1-venv"
    expected = _expected_toolchain(root, runtime, _validate_static_inputs(root))
    if expected != manifest:
        raise BootstrapBlocked("committed toolchain manifest mismatch")


def validate_committed_toolchain_without_probes(
    root: Path, manifest: dict[str, object]
) -> None:
    """Recompute static/runtime bindings using already verified version receipts."""

    _validate_toolchain_envelope(manifest)
    runtime = root / ".ceq1-venv"
    expected = _expected_toolchain(
        root,
        runtime,
        _validate_static_inputs(root),
        version_output_hashes=_recorded_version_output_hashes(manifest),
    )
    if expected != manifest:
        raise BootstrapBlocked("committed toolchain manifest mismatch")


def contained_launcher_contract(
    root: Path,
    bootstrap: Path,
    mode: str,
) -> tuple[Path, list[str], dict[str, str]]:
    bundle = (
        root / ".ceq1-venv"
        if mode == "prepare"
        else bootstrap / "review-candidate/runtime"
    )
    profile_text, _ = render_bootstrap_profile(
        root,
        bundle_path=bundle,
        relocation_path=bootstrap / "relocation-proof",
    )
    encoded_policy = _encoded_policy(profile_text)
    argv = [
        "/usr/bin/env",
        "-i",
        f"HOME={bootstrap / 'home'}",
        f"TMPDIR={bootstrap / 'tmp'}",
        f"XDG_CACHE_HOME={bootstrap / 'cache'}",
        "PATH=/usr/bin:/bin",
        "LANG=C",
        "LC_ALL=C",
        f"CEQ1_BOOTSTRAP_POLICY_B64={encoded_policy}",
        str(SANDBOX_EXEC),
        "-p",
        profile_text,
        str(PINNED_PYTHON),
        "-I",
        "-S",
        "-B",
        str(root / "scripts/bootstrap_ceq1_runtime.py"),
        mode,
        "--contained",
        "--bootstrap",
        bootstrap.name,
    ]
    return Path("/usr/bin/env"), argv, {}


def _launch_contained(mode: str) -> None:
    root = _lexical_repo_root()
    runtime_root = root / ".ceq1-runtime"
    bootstrap = runtime_root / f"bootstrap-{secrets.token_hex(16)}"
    executable, argv, environment = contained_launcher_contract(root, bootstrap, mode)
    _close_non_stdio_fds()
    os.execve(executable, argv, environment)
    raise BootstrapBlocked("contained bootstrap exec unexpectedly returned")


def _prepare(mode: str, bootstrap_name: str) -> dict[str, object]:
    root = _lexical_repo_root()
    os.umask(0o077)
    cf_encoding = os.environ.pop("__CF_USER_TEXT_ENCODING", None)
    if cf_encoding is not None:
        validate_cf_user_text_encoding(cf_encoding)
    encoded_policy = os.environ.pop("CEQ1_BOOTSTRAP_POLICY_B64", None)
    if not isinstance(encoded_policy, str):
        raise BootstrapBlocked("contained bootstrap policy channel is missing")
    try:
        supplied_policy = base64.b64decode(encoded_policy, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise BootstrapBlocked("contained bootstrap policy channel is malformed") from error
    bundle = (
        root / ".ceq1-venv"
        if mode == "prepare"
        else root / ".ceq1-runtime" / bootstrap_name / "review-candidate/runtime"
    )
    relocation = root / ".ceq1-runtime" / bootstrap_name / "relocation-proof"
    expected_policy, _ = render_bootstrap_profile(
        root,
        bundle_path=bundle,
        relocation_path=relocation,
    )
    if supplied_policy != expected_policy:
        raise BootstrapBlocked("contained bootstrap policy bytes drift")
    prove_active_seatbelt(root)
    validated_root = _repo_root()
    if validated_root != root:
        raise BootstrapBlocked("contained bootstrap root identity drift")
    expected_environment = {
        "HOME": str(root / ".ceq1-runtime" / bootstrap_name / "home"),
        "TMPDIR": str(root / ".ceq1-runtime" / bootstrap_name / "tmp"),
        "XDG_CACHE_HOME": str(root / ".ceq1-runtime" / bootstrap_name / "cache"),
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if dict(os.environ) != expected_environment:
        raise BootstrapBlocked("contained bootstrap environment drift")
    runtime_root = root / ".ceq1-runtime"
    _ensure_private_root(runtime_root)
    if not re.fullmatch(r"bootstrap-[A-Za-z0-9_-]+", bootstrap_name):
        raise BootstrapBlocked("unsafe contained bootstrap identity")
    bootstrap = runtime_root / bootstrap_name
    _make_private_dir(bootstrap)
    for name in ("home", "tmp", "cache"):
        _make_private_dir(bootstrap / name)
    profile_text, profile_receipt = render_bootstrap_profile(
        root,
        bundle_path=bundle,
        relocation_path=relocation,
    )
    profile = bootstrap / "profile.sb"
    _atomic_write_bytes(profile, profile_text.encode("utf-8"), 0o400)
    receipt_path = bootstrap / "sandbox-policy-receipt.json"
    _atomic_write_bytes(receipt_path, _canonical(profile_receipt) + b"\n", 0o400)
    manifest = _validate_static_inputs(root)

    target = root / ".ceq1-venv" if mode == "prepare" else bootstrap / "review-candidate/runtime"
    if target.is_symlink():
        raise BootstrapBlocked("runtime target must not be a symlink")
    if mode == "prepare" and target.exists():
        committed = json.loads(
            _read_regular_bytes(
                root / "docs/release-safety/ceq1-toolchain-manifest.json"
            ).decode("utf-8")
        )
        validate_committed_toolchain(root, committed)
        validate_runtime_receipt(root, root / ".ceq1-runtime/bootstrap-receipt.json")
        _validate_wheelhouse(
            root / ".ceq1-runtime/wheelhouse",
            manifest,
            require_sealed=True,
        )
        return {"status": "PASS", "runtime": tree_receipt(target)}

    commands = command_contract(root, sandboxed=False)
    # Commands are rebuilt for this unique bootstrap root without using parent PWD.
    def retarget(command: list[str]) -> list[str]:
        replacements = {
            str(root / ".ceq1-runtime/bootstrap"): str(bootstrap),
            str(root / ".ceq1-runtime/bootstrap/profile.sb"): str(profile),
            str(root / ".ceq1-venv"): str(target),
        }
        return [replacements.get(item, item.replace(str(root / ".ceq1-runtime/bootstrap"), str(bootstrap)).replace(str(root / ".ceq1-venv"), str(target))) for item in command]

    commands = [retarget(command) for command in commands]
    source_cache_before = cache_identity_receipt(UV_CACHE)
    source_cache_logical = cache_logical_receipt(UV_CACHE)
    clone_cache_to_task(
        UV_CACHE,
        bootstrap / "uv-cache",
        source_cache_before,
        source_cache_logical,
    )
    prove_denied_cache_link_targets(bootstrap / "uv-cache")
    _run(commands[0], cwd=root)
    diagnostic_versions = parse_pinned_requirements(bootstrap / "diagnostic.lock")
    expected_diagnostic = {
        "pytest": "9.1.1",
        "pluggy": "1.6.0",
        "iniconfig": "2.3.0",
        "pygments": "2.20.0",
        "packaging": "26.2",
    }
    if diagnostic_versions != expected_diagnostic:
        raise BootstrapBlocked("diagnostic qualification resolution drift")
    _run(commands[1], cwd=root)
    _run(commands[2], cwd=root)
    stage_a = bootstrap / "stage-a"
    stage_b = bootstrap / "stage-b"
    for package in manifest["packages"]:
        a = stage_a / package["normalizedName"] / package["wheel"]["filename"]
        b = stage_b / package["normalizedName"] / package["wheel"]["filename"]
        if a.read_bytes() != b.read_bytes():
            raise BootstrapBlocked("independent wheel builds differ")
    wheelhouse = root / ".ceq1-runtime/wheelhouse"
    _ensure_canonical_wheelhouse(stage_a, wheelhouse, manifest)
    candidate_lock = render_derived_lock(manifest)
    if candidate_lock != _read_regular_bytes(root / "requirements-ceq1.lock"):
        raise BootstrapBlocked("committed derived lock differs from canonical rendering")
    create_runtime_target(mode, target)
    candidate_lock_path, candidate_toolchain_path = review_candidate_artifact_paths(
        bootstrap
    )
    if mode == "derive-review-candidate":
        _atomic_write_bytes(candidate_lock_path, candidate_lock, 0o400)
    _copy_python(PINNED_PYTHON_ROOT, target / "python")
    _run(commands[3], cwd=root)
    _run(commands[4], cwd=root)
    _run(commands[5], cwd=root)
    source_cache_after = cache_identity_receipt(UV_CACHE)
    if source_cache_after != source_cache_before:
        raise BootstrapBlocked("source uv cache changed during contained bootstrap")
    require_source_cache_logical_stability(UV_CACHE, source_cache_logical)
    normalize_uv_install_metadata(target, manifest)
    _rewrite_venv_links(target)
    validate_installed_environment(target, root)
    _bundle_path_receipt(target)
    relocation = bootstrap / "relocation-proof"
    relocation_entries = _safe_tree_entries(target, relocation)
    if _sha256_bytes(_canonical(relocation_entries)) != tree_receipt(target)["treeDigest"]:
        raise BootstrapBlocked("relocation copy differs before launch proof")
    _bundle_path_receipt(relocation)
    _make_read_only(relocation)
    _make_read_only(wheelhouse)
    _make_read_only(target)
    toolchain = _expected_toolchain(root, target, manifest)
    candidate_path: Path | None = None
    if mode == "derive-review-candidate":
        candidate_path = candidate_toolchain_path
        _atomic_write_bytes(
            candidate_path,
            json.dumps(toolchain, sort_keys=True, indent=2).encode("utf-8") + b"\n",
            0o400,
        )
    if mode == "prepare":
        committed = json.loads(
            _read_regular_bytes(
                root / "docs/release-safety/ceq1-toolchain-manifest.json"
            ).decode("utf-8")
        )
        if committed != toolchain:
            raise BootstrapBlocked("prepared runtime differs from committed toolchain manifest")
        _write_runtime_receipt(root, _runtime_receipt_payload(root, profile_receipt))
    result = {
        "status": "PASS" if mode == "prepare" else "REVIEW_CANDIDATE",
        "bootstrap": bootstrap.name,
        "runtime": tree_receipt(target),
        "renderedSandboxSha256": profile_receipt["renderedSha256"],
    }
    if candidate_path is not None:
        result["toolchainCandidate"] = candidate_path.relative_to(root).as_posix()
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "derive-review-candidate"))
    parser.add_argument("--contained", action="store_true")
    parser.add_argument("--bootstrap")
    args = parser.parse_args(argv)
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise BootstrapBlocked("bootstrap requires -I -S -B")
    if not args.contained:
        if args.bootstrap is not None:
            raise BootstrapBlocked("outer bootstrap must not accept task identity")
        _launch_contained(args.mode)
        raise BootstrapBlocked("contained launch unexpectedly returned")
    if not args.bootstrap:
        raise BootstrapBlocked("contained bootstrap identity is required")
    result = _prepare(args.mode, args.bootstrap)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
