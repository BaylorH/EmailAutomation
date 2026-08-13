#!/usr/bin/env python3
"""Build deterministic, locally derived CE-Q1 wheels from RECORD-closed caches."""

from __future__ import annotations

import argparse
import base64
import csv
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import zipfile


SCHEMA_KEYS = {"schemaVersion", "algorithmVersion", "python", "builderSha256", "packages"}
PACKAGE_KEYS = {
    "name",
    "normalizedName",
    "version",
    "archiveId",
    "recordSha256",
    "members",
    "excluded",
    "wheel",
}
MEMBER_KEYS = {"path", "size", "sha256"}
EXCLUDED_KEYS = MEMBER_KEYS | {"classification"}
WHEEL_KEYS = {"filename", "memberCount", "size", "sha256"}
PYTHON_KEYS = {"version", "launcherSha256", "zipfileSha256"}
ALGORITHM_VERSION = "ceq1-derived-wheel-v1"
_PYC_RE = re.compile(r"(?:^|/)__pycache__/[^/]+\.pyc\Z")
_NATIVE_SUFFIXES = (".so", ".dylib", ".dll", ".pyd", ".a", ".o")
_SIGNATURE_SUFFIXES = ("/RECORD.jws", "/RECORD.p7s")
_NORMALIZED_NAME_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*\Z")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:[a-z0-9]+)?\Z")
_ARCHIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_DISTRIBUTION_NAME_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*\Z")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_ZIP_MEMBER_MODE = stat.S_IFREG | 0o644
_ZIP_LOCAL_HEADER = struct.Struct("<4s5H3L2H")
_ZIP_CENTRAL_HEADER = struct.Struct("<4s6H3L5H2L")
_ZIP_END_HEADER = struct.Struct("<4s4H2LH")


class WheelBoundaryError(ValueError):
    """Raised when a cached distribution violates the closed wheel contract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _closed_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise WheelBoundaryError(f"{label}: closed keys mismatch: {actual}")
    return value


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WheelBoundaryError("unsafe empty/whitespace member path")
    if not value.isascii() or "\\" in value or any(ord(character) < 0x20 for character in value):
        raise WheelBoundaryError("member path must be ASCII POSIX")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise WheelBoundaryError("unsafe absolute/traversing member path")
    if any(part.lower().endswith(".data") for part in pure.parts):
        raise WheelBoundaryError("wheel .data trees are forbidden")
    lower = value.lower()
    if lower.endswith(_NATIVE_SUFFIXES):
        raise WheelBoundaryError("native wheel members are forbidden")
    if any(lower.endswith(suffix.lower()) for suffix in _SIGNATURE_SUFFIXES):
        raise WheelBoundaryError("wheel signature members are forbidden")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WheelBoundaryError(f"{label}: invalid SHA-256")
    return value


def _validate_package_value(value: object) -> dict[str, object]:
    package = _closed_keys(value, PACKAGE_KEYS, "package")
    for scalar in ("name", "normalizedName", "version", "archiveId"):
        if not isinstance(package[scalar], str) or not package[scalar]:
            raise WheelBoundaryError(f"package {scalar} must be nonempty")
    if not _DISTRIBUTION_NAME_RE.fullmatch(package["name"]):
        raise WheelBoundaryError("unsafe distribution name")
    normalized_name = re.sub(r"[-_.]+", "_", package["name"].lower())
    if (
        not _NORMALIZED_NAME_RE.fullmatch(package["normalizedName"])
        or package["normalizedName"] != normalized_name
    ):
        raise WheelBoundaryError("unsafe or noncanonical normalized package name")
    if not _VERSION_RE.fullmatch(package["version"]):
        raise WheelBoundaryError("unsafe package version")
    if not _ARCHIVE_ID_RE.fullmatch(package["archiveId"]):
        raise WheelBoundaryError("archive ID must be one opaque path component")
    _validate_digest(package["recordSha256"], "RECORD")
    members = package["members"]
    excluded = package["excluded"]
    if not isinstance(members, list) or not members:
        raise WheelBoundaryError("package members must be nonempty")
    if not isinstance(excluded, list):
        raise WheelBoundaryError("excluded members must be a list")
    paths: set[str] = set()
    for row_value in members:
        row = _closed_keys(row_value, MEMBER_KEYS, "member")
        path = _safe_relative(row["path"])
        if path in paths:
            raise WheelBoundaryError("duplicate member path")
        paths.add(path)
        if type(row["size"]) is not int or row["size"] < 0:
            raise WheelBoundaryError("member size must be a nonnegative integer")
        _validate_digest(row["sha256"], "member")
    for row_value in excluded:
        row = _closed_keys(row_value, EXCLUDED_KEYS, "excluded")
        path = _safe_relative(row["path"])
        if path in paths:
            raise WheelBoundaryError("excluded/member path overlap")
        paths.add(path)
        if row["classification"] != "generated-pyc" or not _PYC_RE.search(path):
            raise WheelBoundaryError("only manifested __pycache__ .pyc extras may be excluded")
        if type(row["size"]) is not int or row["size"] < 0:
            raise WheelBoundaryError("excluded size must be a nonnegative integer")
        _validate_digest(row["sha256"], "excluded")
    wheel = _closed_keys(package["wheel"], WHEEL_KEYS, "wheel")
    expected_filename = f"{package['normalizedName']}-{package['version']}-py3-none-any.whl"
    if wheel["filename"] != expected_filename:
        raise WheelBoundaryError("derived wheel filename mismatch")
    if type(wheel["memberCount"]) is not int or wheel["memberCount"] != len(members):
        raise WheelBoundaryError("wheel member count mismatch")
    if type(wheel["size"]) is not int or wheel["size"] < 0:
        raise WheelBoundaryError("wheel size must be nonnegative")
    _validate_digest(wheel["sha256"], "wheel")
    return package


def _validate_manifest_value(value: object) -> dict[str, object]:
    manifest = _closed_keys(value, SCHEMA_KEYS, "manifest")
    if manifest["schemaVersion"] != 1 or manifest["algorithmVersion"] != ALGORITHM_VERSION:
        raise WheelBoundaryError("manifest version mismatch")
    python = _closed_keys(manifest["python"], PYTHON_KEYS, "python")
    if python["version"] != "3.12.13":
        raise WheelBoundaryError("Python version mismatch")
    _validate_digest(python["launcherSha256"], "python launcher")
    _validate_digest(python["zipfileSha256"], "zipfile")
    _validate_digest(manifest["builderSha256"], "builder")
    packages = manifest["packages"]
    if not isinstance(packages, list) or len(packages) != 5:
        raise WheelBoundaryError("exactly five derived packages are required")
    names: set[str] = set()
    archives: set[str] = set()
    for package_value in packages:
        package = _validate_package_value(package_value)
        if package["normalizedName"] in names or package["archiveId"] in archives:
            raise WheelBoundaryError("duplicate package/archive identity")
        names.add(package["normalizedName"])
        archives.add(package["archiveId"])
    return manifest


def load_closed_manifest(path: Path) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise WheelBoundaryError(f"non-finite JSON value: {value}")

    def closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise WheelBoundaryError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        Path(path).read_bytes(), object_pairs_hook=closed_pairs, parse_constant=reject_constant
    )
    return _validate_manifest_value(value)


_STABLE_STAT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, name) == getattr(right, name) for name in _STABLE_STAT_FIELDS)


def _stable_stat_tuple(info: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(info, name) for name in _STABLE_STAT_FIELDS)


def _canonical_absolute_path(path: Path, label: str) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw.startswith("/") or os.path.normpath(raw) != raw:
        raise WheelBoundaryError(f"{label} must be a canonical absolute path")
    if "\x00" in raw:
        raise WheelBoundaryError(f"{label} contains NUL")
    return Path(raw)


def _open_absolute_directory(
    path: Path,
    *,
    label: str = "directory",
    leaf_mode: int | None = None,
) -> tuple[int, Path, os.stat_result]:
    canonical = _canonical_absolute_path(Path(path), label)
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in canonical.parts[1:]:
            parent_before = os.fstat(fd)
            child_link = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(child_link.st_mode) or not stat.S_ISDIR(child_link.st_mode):
                raise WheelBoundaryError(f"{label} component is not a real directory: {component}")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            child_open = os.fstat(child)
            if not _same_stat(child_link, child_open):
                os.close(child)
                raise WheelBoundaryError(f"{label} component changed while opening: {component}")
            if not _same_stat(os.fstat(fd), parent_before):
                os.close(child)
                raise WheelBoundaryError(f"{label} parent changed while opening: {component}")
            os.close(fd)
            fd = child
        opened = os.fstat(fd)
        if leaf_mode is not None and stat.S_IMODE(opened.st_mode) != leaf_mode:
            raise WheelBoundaryError(f"{label} mode drift")
        return fd, canonical, opened
    except BaseException:
        os.close(fd)
        raise


def _open_relative_directory(
    parent_fd: int,
    component: str,
    *,
    label: str,
    mode: int = 0o755,
) -> tuple[int, os.stat_result]:
    if not component or "/" in component or component in {".", ".."}:
        raise WheelBoundaryError(f"unsafe {label} component")
    parent_before = os.fstat(parent_fd)
    link_info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(link_info.st_mode) or not stat.S_ISDIR(link_info.st_mode):
        raise WheelBoundaryError(f"{label} must be a real directory")
    if stat.S_IMODE(link_info.st_mode) != mode:
        raise WheelBoundaryError(f"{label} mode drift")
    child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(child_fd)
    if not _same_stat(link_info, opened):
        os.close(child_fd)
        raise WheelBoundaryError(f"{label} changed while opening")
    if not _same_stat(os.fstat(parent_fd), parent_before):
        os.close(child_fd)
        raise WheelBoundaryError(f"{label} parent changed while opening")
    return child_fd, opened


def _create_private_directory(path: Path) -> tuple[int, Path, os.stat_result]:
    canonical = _canonical_absolute_path(Path(path), "output directory")
    parent_fd, _, _ = _open_absolute_directory(
        canonical.parent,
        label="output parent",
    )
    try:
        try:
            os.stat(canonical.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise WheelBoundaryError("output directory already exists")
        os.mkdir(canonical.name, mode=0o700, dir_fd=parent_fd)
        child_fd, opened = _open_relative_directory(
            parent_fd,
            canonical.name,
            label="output directory",
            mode=0o700,
        )
        return child_fd, canonical, opened
    finally:
        os.close(parent_fd)


def _scan_tree_fd(root_fd: int) -> tuple[dict[str, os.stat_result], dict[str, os.stat_result]]:
    files: dict[str, os.stat_result] = {}
    directories: dict[str, os.stat_result] = {}
    def walk(directory_fd: int, prefix: str) -> None:
        directory_before = os.fstat(directory_fd)
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{prefix}/{name}" if prefix else name
            _safe_relative(relative)
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise WheelBoundaryError(f"symlink forbidden: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o755:
                    raise WheelBoundaryError(f"directory mode drift: {relative}")
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if not _same_stat(opened, info):
                        raise WheelBoundaryError(f"directory changed before traversal: {relative}")
                    directories[relative] = opened
                    walk(child_fd, relative)
                    if not _same_stat(os.fstat(child_fd), opened):
                        raise WheelBoundaryError(f"directory changed during traversal: {relative}")
                    after_link = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _same_stat(after_link, opened):
                        raise WheelBoundaryError(f"directory identity changed during traversal: {relative}")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise WheelBoundaryError(f"hard link forbidden: {relative}")
                if stat.S_IMODE(info.st_mode) != 0o644:
                    raise WheelBoundaryError(f"member mode drift: {relative}")
                files[relative] = info
            else:
                raise WheelBoundaryError(f"special file forbidden: {relative}")
        if not _same_stat(os.fstat(directory_fd), directory_before):
            raise WheelBoundaryError(f"directory changed during scan: {prefix or '.'}")

    walk(root_fd, "")
    return files, directories


def _read_no_follow(
    root_fd: int,
    relative: str,
    expected: os.stat_result,
    directories: dict[str, os.stat_result],
) -> bytes:
    owned_fds: list[int] = []
    parent_fd = root_fd
    parts = PurePosixPath(relative).parts
    prefix: list[str] = []
    for component in parts[:-1]:
        prefix.append(component)
        try:
            expected_directory = directories["/".join(prefix)]
        except KeyError as error:
            raise WheelBoundaryError(f"unmanifested directory before read: {'/'.join(prefix)}") from error
        child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if not _same_stat(os.fstat(child_fd), expected_directory):
            os.close(child_fd)
            raise WheelBoundaryError(f"directory changed before read: {'/'.join(prefix)}")
        owned_fds.append(child_fd)
        parent_fd = child_fd
    try:
        fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent_fd)
    except BaseException:
        for owned in reversed(owned_fds):
            os.close(owned)
        raise
    try:
        before = os.fstat(fd)
        if (
            not _same_stat(before, expected)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o644
        ):
            raise WheelBoundaryError(f"source changed before read: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
        if not _same_stat(after, before):
            raise WheelBoundaryError(f"source changed during read: {relative}")
        for prefix_depth, owned in enumerate(owned_fds, start=1):
            expected_directory = directories["/".join(parts[:prefix_depth])]
            if not _same_stat(os.fstat(owned), expected_directory):
                raise WheelBoundaryError(f"directory changed during read: {relative}")
        return b"".join(chunks)
    finally:
        os.close(fd)
        for owned in reversed(owned_fds):
            os.close(owned)


def _urlsafe_sha256(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def inspect_source(source_root: Path, package_value: dict[str, object]) -> dict[str, object]:
    package = _validate_package_value(package_value)
    members = package.get("members")
    excluded = package.get("excluded")
    if Path(source_root).name != package["archiveId"]:
        raise WheelBoundaryError("cache archive identity mismatch")
    expected_rows: dict[str, dict[str, object]] = {}
    for row in members:
        _closed_keys(row, MEMBER_KEYS, "member")
        path = _safe_relative(row["path"])
        if path in expected_rows:
            raise WheelBoundaryError("duplicate member path")
        expected_rows[path] = row
    excluded_rows: dict[str, dict[str, object]] = {}
    for row in excluded:
        _closed_keys(row, EXCLUDED_KEYS, "excluded")
        path = _safe_relative(row["path"])
        if row["classification"] != "generated-pyc" or not _PYC_RE.search(path):
            raise WheelBoundaryError("invalid excluded extra")
        if path in expected_rows or path in excluded_rows:
            raise WheelBoundaryError("duplicate excluded path")
        excluded_rows[path] = row
    root_fd, _, root_before = _open_absolute_directory(
        Path(source_root),
        label="source archive",
        leaf_mode=0o755,
    )
    data_by_path: dict[str, bytes] = {}
    try:
        actual, directories = _scan_tree_fd(root_fd)
        if set(actual) != set(expected_rows) | set(excluded_rows):
            raise WheelBoundaryError("source tree differs from member/excluded closure")
        required_directories: set[str] = set()
        for path in actual:
            parts = PurePosixPath(path).parts[:-1]
            for depth in range(1, len(parts) + 1):
                required_directories.add("/".join(parts[:depth]))
        if set(directories) != required_directories:
            raise WheelBoundaryError("source directory topology differs from member closure")
        for path, row in sorted({**expected_rows, **excluded_rows}.items()):
            data = _read_no_follow(root_fd, path, actual[path], directories)
            if len(data) != row["size"] or sha256_bytes(data) != row["sha256"]:
                raise WheelBoundaryError(f"source member hash/size mismatch: {path}")
            data_by_path[path] = data
        actual_after, directories_after = _scan_tree_fd(root_fd)
        if (
            {path: _stable_stat_tuple(info) for path, info in actual.items()}
            != {path: _stable_stat_tuple(info) for path, info in actual_after.items()}
            or {path: _stable_stat_tuple(info) for path, info in directories.items()}
            != {path: _stable_stat_tuple(info) for path, info in directories_after.items()}
            or not _same_stat(os.fstat(root_fd), root_before)
        ):
            raise WheelBoundaryError("source tree changed while being read")
    finally:
        os.close(root_fd)
    dist_infos = {
        PurePosixPath(path).parts[0]
        for path in expected_rows
        if PurePosixPath(path).parts[0].endswith(".dist-info")
    }
    if len(dist_infos) != 1:
        raise WheelBoundaryError("exactly one .dist-info directory is required")
    dist_info = next(iter(dist_infos))
    expected_dist_info = f"{package['normalizedName']}-{package['version']}.dist-info"
    if dist_info != expected_dist_info:
        raise WheelBoundaryError("noncanonical .dist-info directory identity")
    record_path = f"{dist_info}/RECORD"
    metadata_path = f"{dist_info}/METADATA"
    wheel_path = f"{dist_info}/WHEEL"
    if not {record_path, metadata_path, wheel_path} <= set(expected_rows):
        raise WheelBoundaryError("METADATA/WHEEL/RECORD required")
    record = data_by_path[record_path]
    if sha256_bytes(record) != package["recordSha256"]:
        raise WheelBoundaryError("original RECORD byte hash mismatch")
    rows = list(csv.reader(record.decode("utf-8").splitlines()))
    seen: set[str] = set()
    self_rows = 0
    for row in rows:
        if len(row) != 3:
            raise WheelBoundaryError("RECORD row must have three fields")
        path, digest, size_text = row
        _safe_relative(path)
        if path in seen:
            raise WheelBoundaryError("duplicate RECORD path")
        seen.add(path)
        if path == record_path:
            self_rows += 1
            if digest or size_text:
                raise WheelBoundaryError("RECORD self row must be blank")
            continue
        if not digest.startswith("sha256=") or "=" in digest[len("sha256=") :]:
            raise WheelBoundaryError("RECORD must use unpadded sha256")
        if not size_text.isdecimal():
            raise WheelBoundaryError("RECORD size must be decimal")
        if path not in data_by_path:
            raise WheelBoundaryError("RECORD path missing")
        data = data_by_path[path]
        if digest != f"sha256={_urlsafe_sha256(data)}" or int(size_text) != len(data):
            raise WheelBoundaryError("RECORD content hash/size mismatch")
    if self_rows != 1 or seen != set(expected_rows):
        raise WheelBoundaryError("RECORD/member closure mismatch")
    metadata = BytesParser(policy=compat32).parsebytes(data_by_path[metadata_path])
    metadata_names = metadata.get_all("Name", [])
    metadata_versions = metadata.get_all("Version", [])
    if len(metadata_names) != 1 or len(metadata_versions) != 1:
        raise WheelBoundaryError("METADATA must contain one Name and Version")
    normalized_metadata_name = re.sub(r"[-_.]+", "_", str(metadata_names[0]).lower())
    if normalized_metadata_name != package["normalizedName"] or metadata_versions[0] != package["version"]:
        raise WheelBoundaryError("METADATA package identity mismatch")
    wheel_lines = data_by_path[wheel_path].decode("utf-8").splitlines()
    parsed: dict[str, list[str]] = {}
    for line in wheel_lines:
        if not line.strip():
            continue
        if ": " not in line:
            raise WheelBoundaryError("invalid WHEEL metadata")
        key, value = line.split(": ", 1)
        parsed.setdefault(key, []).append(value)
    if parsed.get("Wheel-Version") != ["1.0"]:
        raise WheelBoundaryError("Wheel-Version must be 1.0")
    if parsed.get("Root-Is-Purelib") != ["true"]:
        raise WheelBoundaryError("Root-Is-Purelib must be true")
    if parsed.get("Tag") != ["py3-none-any"]:
        raise WheelBoundaryError("exact py3-none-any tag required")
    return {
        "members": [dict(row) for row in members],
        "excluded": [dict(row) for row in excluded],
        "data": data_by_path,
        "recordPath": record_path,
        "sourceIdentity": _stable_stat_tuple(root_before),
        "fileIdentities": {
            path: _stable_stat_tuple(info) for path, info in sorted(actual.items())
        },
        "directoryIdentities": {
            path: _stable_stat_tuple(info) for path, info in sorted(directories.items())
        },
    }


def _read_regular_file_no_follow(path: Path, *, label: str) -> tuple[bytes, os.stat_result]:
    canonical = _canonical_absolute_path(Path(path), label)
    parent_fd, _, _ = _open_absolute_directory(canonical.parent, label=f"{label} parent")
    try:
        parent_before = os.fstat(parent_fd)
        link_info = os.stat(canonical.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(link_info.st_mode)
            or not stat.S_ISREG(link_info.st_mode)
            or link_info.st_nlink != 1
        ):
            raise WheelBoundaryError(f"{label} must be one real regular file")
        if stat.S_IMODE(link_info.st_mode) not in {0o444, 0o644}:
            raise WheelBoundaryError(f"{label} filesystem mode drift")
        fd = os.open(canonical.name, _FILE_FLAGS, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not _same_stat(before, link_info):
                raise WheelBoundaryError(f"{label} changed while opening")
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(fd)
            after_link = os.stat(canonical.name, dir_fd=parent_fd, follow_symlinks=False)
            if not _same_stat(before, after) or not _same_stat(after, after_link):
                raise WheelBoundaryError(f"{label} changed while reading")
            if not _same_stat(os.fstat(parent_fd), parent_before):
                raise WheelBoundaryError(f"{label} parent changed while reading")
            data = b"".join(chunks)
            if len(data) != before.st_size:
                raise WheelBoundaryError(f"{label} short read")
            return data, before
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _validate_raw_zip_layout(
    raw: bytes,
    infos: list[zipfile.ZipInfo],
    central_offset: int,
) -> None:
    cursor = 0
    for info in infos:
        if info.header_offset != cursor or cursor + _ZIP_LOCAL_HEADER.size > len(raw):
            raise WheelBoundaryError("ZIP local-header topology drift")
        (
            signature,
            extract_version,
            flag_bits,
            compression,
            dos_time,
            dos_date,
            crc,
            compressed_size,
            file_size,
            name_size,
            extra_size,
        ) = _ZIP_LOCAL_HEADER.unpack_from(raw, cursor)
        name_start = cursor + _ZIP_LOCAL_HEADER.size
        name_end = name_start + name_size
        data_start = name_end + extra_size
        data_end = data_start + compressed_size
        if (
            signature != b"PK\x03\x04"
            or extract_version != 20
            or flag_bits != 0
            or compression != zipfile.ZIP_STORED
            or dos_time != 0
            or dos_date != 33
            or crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or extra_size != 0
            or data_end > len(raw)
            or raw[name_start:name_end] != info.filename.encode("ascii")
        ):
            raise WheelBoundaryError("ZIP local-header metadata drift")
        cursor = data_end
    if cursor != central_offset:
        raise WheelBoundaryError("ZIP local/central boundary drift")

    central_start = cursor
    for info in infos:
        if cursor + _ZIP_CENTRAL_HEADER.size > len(raw):
            raise WheelBoundaryError("ZIP central directory is truncated")
        (
            signature,
            create_version,
            extract_version,
            flag_bits,
            compression,
            dos_time,
            dos_date,
            crc,
            compressed_size,
            file_size,
            name_size,
            extra_size,
            comment_size,
            disk_start,
            internal_attr,
            external_attr,
            local_offset,
        ) = _ZIP_CENTRAL_HEADER.unpack_from(raw, cursor)
        name_start = cursor + _ZIP_CENTRAL_HEADER.size
        name_end = name_start + name_size
        next_cursor = name_end + extra_size + comment_size
        if (
            signature != b"PK\x01\x02"
            or create_version != (3 << 8) | 20
            or extract_version != 20
            or flag_bits != 0
            or compression != zipfile.ZIP_STORED
            or dos_time != 0
            or dos_date != 33
            or crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or extra_size != 0
            or comment_size != 0
            or disk_start != 0
            or internal_attr != 0
            or external_attr != _ZIP_MEMBER_MODE << 16
            or local_offset != info.header_offset
            or next_cursor > len(raw)
            or raw[name_start:name_end] != info.filename.encode("ascii")
        ):
            raise WheelBoundaryError("ZIP central-directory metadata drift")
        cursor = next_cursor

    central_size = cursor - central_start
    if cursor + _ZIP_END_HEADER.size != len(raw):
        raise WheelBoundaryError("ZIP prefix, suffix, Zip64, or comment forbidden")
    (
        signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        recorded_central_size,
        recorded_central_offset,
        comment_size,
    ) = _ZIP_END_HEADER.unpack_from(raw, cursor)
    if (
        signature != b"PK\x05\x06"
        or disk_number != 0
        or central_disk != 0
        or disk_entries != len(infos)
        or total_entries != len(infos)
        or recorded_central_size != central_size
        or recorded_central_offset != central_start
        or comment_size != 0
    ):
        raise WheelBoundaryError("ZIP end-of-central-directory drift")


def _closed_expected_members(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise WheelBoundaryError("expected wheel members must be a nonempty list")
    expected: dict[str, dict[str, object]] = {}
    for row_value in value:
        row = _closed_keys(row_value, MEMBER_KEYS, "expected wheel member")
        path = _safe_relative(row["path"])
        if path in expected:
            raise WheelBoundaryError("duplicate expected wheel member")
        if type(row["size"]) is not int or row["size"] < 0:
            raise WheelBoundaryError("expected wheel member size drift")
        _validate_digest(row["sha256"], "expected wheel member")
        expected[path] = row
    return expected


def inspect_wheel(
    path: Path,
    *,
    validate_record: bool = True,
    expected_members: object | None = None,
) -> dict[str, object]:
    raw, file_info = _read_regular_file_no_follow(Path(path), label="finished wheel")
    with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
        if archive.comment:
            raise WheelBoundaryError("archive comment forbidden")
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or archive.testzip() is not None:
            raise WheelBoundaryError("duplicate or corrupt wheel member")
        for info in infos:
            _safe_relative(info.filename)
            if info.is_dir() or info.extra or info.comment:
                raise WheelBoundaryError("directory/extra/comment forbidden")
            if info.flag_bits != 0 or info.internal_attr != 0:
                raise WheelBoundaryError("ZIP flag/internal attribute drift")
            if info.compress_type != zipfile.ZIP_STORED or info.compress_size != info.file_size:
                raise WheelBoundaryError("wheel must be ZIP_STORED")
            if info.date_time != (1980, 1, 1, 0, 0, 0):
                raise WheelBoundaryError("wheel timestamp drift")
            if info.create_system != 3 or info.create_version != 20 or info.extract_version != 20:
                raise WheelBoundaryError("ZIP platform/version drift")
            if info.external_attr != _ZIP_MEMBER_MODE << 16:
                raise WheelBoundaryError("wheel mode/attribute drift")
        _validate_raw_zip_layout(raw, infos, archive.start_dir)
        member_receipts = [
            {
                "path": info.filename,
                "size": info.file_size,
                "sha256": sha256_bytes(archive.read(info.filename)),
            }
            for info in infos
        ]
        if expected_members is not None:
            expected = _closed_expected_members(expected_members)
            actual = {row["path"]: row for row in member_receipts}
            if actual != expected:
                raise WheelBoundaryError("finished wheel differs from manifested member bytes")
        if validate_record:
            record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(record_names) != 1:
                raise WheelBoundaryError("finished wheel must contain exactly one RECORD")
            record_name = record_names[0]
            if names[-1] != record_name or names[:-1] != sorted(names[:-1]):
                raise WheelBoundaryError("finished wheel member order drift")
            record_rows = list(csv.reader(archive.read(record_name).decode("utf-8").splitlines()))
            seen: set[str] = set()
            self_rows = 0
            for row in record_rows:
                if len(row) != 3:
                    raise WheelBoundaryError("finished RECORD row shape drift")
                member, digest, size_text = row
                _safe_relative(member)
                if member in seen:
                    raise WheelBoundaryError("finished RECORD duplicate path")
                seen.add(member)
                if member == record_name:
                    self_rows += 1
                    if digest or size_text:
                        raise WheelBoundaryError("finished RECORD self row must be blank")
                    continue
                if member not in names or not digest.startswith("sha256=") or not size_text.isdecimal():
                    raise WheelBoundaryError("finished RECORD member contract drift")
                data = archive.read(member)
                if digest != f"sha256={_urlsafe_sha256(data)}" or int(size_text) != len(data):
                    raise WheelBoundaryError("finished RECORD member hash/size mismatch")
            if self_rows != 1 or seen != set(names):
                raise WheelBoundaryError("finished RECORD/member closure mismatch")
        return {
            "names": names,
            "members": member_receipts,
            "size": len(raw),
            "sha256": sha256_bytes(raw),
            "fileMode": stat.S_IMODE(file_info.st_mode),
            "compressTypes": [info.compress_type for info in infos],
            "timestamps": [info.date_time for info in infos],
            "createSystems": [info.create_system for info in infos],
            "createVersions": [info.create_version for info in infos],
            "extractVersions": [info.extract_version for info in infos],
            "flagBits": [info.flag_bits for info in infos],
            "internalAttrs": [info.internal_attr for info in infos],
        }


def build_wheel(
    source_root: Path,
    package: dict[str, object],
    output_dir: Path,
    *,
    verify_output: bool = True,
) -> Path:
    package = _validate_package_value(package)
    first = inspect_source(Path(source_root), package)
    output_fd, output_dir, output_before = _create_private_directory(Path(output_dir))
    path = output_dir / package["wheel"]["filename"]
    record_path = first["recordPath"]
    order = sorted(item["path"] for item in package["members"] if item["path"] != record_path)
    order.append(record_path)
    create_flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_fd = os.open(path.name, create_flags, 0o600, dir_fd=output_fd)
    try:
        with os.fdopen(file_fd, "w+b", closefd=False) as handle:
            with zipfile.ZipFile(
                handle,
                "w",
                compression=zipfile.ZIP_STORED,
                allowZip64=False,
            ) as archive:
                archive.comment = b""
                for relative in order:
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.create_version = 20
                    info.extract_version = 20
                    info.flag_bits = 0
                    info.internal_attr = 0
                    info.external_attr = _ZIP_MEMBER_MODE << 16
                    info.extra = b""
                    info.comment = b""
                    archive.writestr(info, first["data"][relative])
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        created = os.fstat(file_fd)
        linked = os.stat(path.name, dir_fd=output_fd, follow_symlinks=False)
        if not _same_stat(created, linked) or created.st_nlink != 1:
            raise WheelBoundaryError("finished wheel identity changed after write")
    finally:
        os.close(file_fd)
    second = inspect_source(Path(source_root), package)
    if first != second:
        raise WheelBoundaryError("source changed across build")
    receipt = inspect_wheel(path, expected_members=package["members"])
    if receipt["names"] != order:
        raise WheelBoundaryError("wheel order mismatch")
    if verify_output:
        expected = package["wheel"]
        if receipt["size"] != expected["size"] or receipt["sha256"] != expected["sha256"]:
            raise WheelBoundaryError("derived wheel output mismatch")
    seal_fd = os.open(path.name, _FILE_FLAGS, dir_fd=output_fd)
    try:
        before_seal = os.fstat(seal_fd)
        if not _same_stat(before_seal, os.stat(path.name, dir_fd=output_fd, follow_symlinks=False)):
            raise WheelBoundaryError("finished wheel changed before seal")
        os.fchmod(seal_fd, 0o444)
        sealed = os.fstat(seal_fd)
        if stat.S_IMODE(sealed.st_mode) != 0o444 or sealed.st_nlink != 1:
            raise WheelBoundaryError("finished wheel seal failed")
    finally:
        os.close(seal_fd)
        os.close(output_fd)
    sealed_receipt = inspect_wheel(path, expected_members=package["members"])
    if sealed_receipt["sha256"] != receipt["sha256"] or sealed_receipt["size"] != receipt["size"]:
        raise WheelBoundaryError("finished wheel changed while sealing")
    return path


def resolve_cache_source(cache_root: Path, package: dict[str, object]) -> Path:
    package = _validate_package_value(package)
    cache_fd, cache, cache_info = _open_absolute_directory(
        Path(cache_root),
        label="uv cache root",
        leaf_mode=0o755,
    )
    opened: list[tuple[int, os.stat_result]] = [(cache_fd, cache_info)]
    try:
        archive_fd, archive_info = _open_relative_directory(
            cache_fd, "archive-v0", label="uv archive root"
        )
        opened.append((archive_fd, archive_info))
        wheels_fd, wheels_info = _open_relative_directory(
            cache_fd, "wheels-v6", label="uv wheels root"
        )
        opened.append((wheels_fd, wheels_info))
        pypi_fd, pypi_info = _open_relative_directory(wheels_fd, "pypi", label="uv pypi root")
        opened.append((pypi_fd, pypi_info))
        package_fd, package_info = _open_relative_directory(
            pypi_fd,
            package["normalizedName"],
            label="uv package wheel directory",
        )
        opened.append((package_fd, package_info))
        target_fd, target_info = _open_relative_directory(
            archive_fd,
            package["archiveId"],
            label="uv archive target",
        )
        opened.append((target_fd, target_info))
        link_name = f"{package['version']}-py3-none-any"
        link_before = os.stat(link_name, dir_fd=package_fd, follow_symlinks=False)
        if not stat.S_ISLNK(link_before.st_mode) or link_before.st_nlink != 1:
            raise WheelBoundaryError("expected cache wheel link is absent or not unique")
        expected = cache / "archive-v0" / package["archiveId"]
        target_text = os.readlink(link_name, dir_fd=package_fd)
        if target_text != str(expected):
            raise WheelBoundaryError("cache wheel link/archive identity mismatch")
        link_after = os.stat(link_name, dir_fd=package_fd, follow_symlinks=False)
        if (
            not _same_stat(link_before, link_after)
            or os.readlink(link_name, dir_fd=package_fd) != target_text
        ):
            raise WheelBoundaryError("cache wheel link changed while resolving")
        for fd, expected_info in opened:
            if not _same_stat(os.fstat(fd), expected_info):
                raise WheelBoundaryError("cache directory changed while resolving")
        return expected
    finally:
        for fd, _ in reversed(opened):
            os.close(fd)


def build_manifest_wheelhouse(manifest_path: Path, cache_root: Path, output: Path) -> list[Path]:
    manifest = load_closed_manifest(manifest_path)
    cache_root = _canonical_absolute_path(Path(cache_root), "uv cache root")
    output_fd, output, _ = _create_private_directory(Path(output))
    os.close(output_fd)
    results: list[Path] = []
    for package in manifest["packages"]:
        source = resolve_cache_source(cache_root, package)
        results.append(build_wheel(source, package, output / package["normalizedName"]))
        if resolve_cache_source(cache_root, package) != source:
            raise WheelBoundaryError("cache wheel link changed during build")
    return results


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = build_manifest_wheelhouse(args.manifest, args.cache_root, args.output)
    print(json.dumps({"wheels": [path.name for path in paths]}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
