"""A manifest of the deployable source bytes baked into the image.

A suite run against a checkout proves something about the checkout. A stamp is
about the IMAGE. Those are the same bytes only if somebody checks -- and
``COPY . .`` behind a ``.dockerignore`` is exactly the thing that drifts
quietly: one ignore rule widened, and a file the reviewer read is no longer in
the artifact that ships, with nothing red anywhere.

So the image records its own deployable source at build time, and the verifier
recomputes the same set from the reviewed checkout under the same
``.dockerignore`` semantics. Added, omitted, or changed bytes each fail.

Digest, not size. A same-length edit is precisely the change a file listing
cannot show you.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from email_automation.certification.canonical_json import (
    canonical_bytes,
    digest_of_bytes,
    loads_strict,
)

SCHEMA_VERSION = "sitesift-image-source-manifest-v1"

# Written into the image at /app, so its own path is relative to /app.
MANIFEST_NAME = ".sitesift-source-manifest.json"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Never deployable source, regardless of .dockerignore: bytecode caches are
# produced by the interpreter, differ between runs, and describe nothing a
# reviewer read.
_CACHE_MARKERS = ("__pycache__/",)
_CACHE_SUFFIXES = (".pyc", ".pyo")


class ManifestError(RuntimeError):
    """A manifest that cannot be trusted. Never downgraded to a warning."""


@dataclass(frozen=True)
class Rule:
    pattern: str
    negated: bool


def load_dockerignore(path: Path) -> List[Rule]:
    """Parse .dockerignore into ordered rules. Later rules win, as Docker does."""
    rules: List[Rule] = []
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc.strerror}") from None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append(Rule(pattern=line[1:].strip() if negated else line,
                          negated=negated))
    return rules


def _rule_matches(relative_path: str, pattern: str) -> bool:
    # A trailing slash means "this directory and everything under it".
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return relative_path == prefix or relative_path.startswith(prefix + "/")
    if fnmatch.fnmatch(relative_path, pattern):
        return True
    # A bare name matches at any depth, and also as a directory prefix --
    # ".git" must exclude ".git/config", not merely a file called ".git".
    if "/" not in pattern:
        parts = relative_path.split("/")
        if any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True
        if fnmatch.fnmatch(parts[-1], pattern):
            return True
    return False


def is_excluded(relative_path: str, rules: Sequence[Rule]) -> bool:
    """Docker semantics: last matching rule decides."""
    excluded = False
    for rule in rules:
        if _rule_matches(relative_path, rule.pattern):
            excluded = not rule.negated
    return excluded


def _is_cache(relative_path: str) -> bool:
    return (any(marker in relative_path + "/" for marker in _CACHE_MARKERS)
            or relative_path.endswith(_CACHE_SUFFIXES))


def _validate(entry: Mapping[str, Any]) -> Dict[str, Any]:
    path = entry.get("path")
    size = entry.get("size")
    digest = entry.get("sha256")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path:
        raise ManifestError("every entry needs a relative non-empty path")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ManifestError(f"{path}: size must be a non-negative integer")
    if not isinstance(digest, str) or not _HEX64.match(digest):
        raise ManifestError(f"{path}: sha256 must be a lowercase 64-hex digest")
    return {"path": path, "size": size, "sha256": digest}


def build_manifest(entries: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Normalise, validate, and sort. Never trusts the caller's ordering."""
    kept: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        validated = _validate(entry)
        path = validated["path"]
        # A manifest listing itself cannot be computed, only fabricated: its own
        # digest would have to be known before it was written.
        if path == MANIFEST_NAME or _is_cache(path):
            continue
        if path in kept:
            raise ManifestError(f"duplicate path in manifest: {path}")
        kept[path] = validated
    return {
        "schemaVersion": SCHEMA_VERSION,
        "files": [kept[path] for path in sorted(kept)],
    }


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return digest_of_bytes(canonical_bytes(manifest))


def manifest_from_checkout(root: Path,
                           dockerignore: Path | None = None) -> Dict[str, Any]:
    """Recompute the deployable set from the reviewed checkout."""
    root = Path(root).resolve()
    rules = load_dockerignore(dockerignore or (root / ".dockerignore"))
    entries = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if is_excluded(relative, rules) or _is_cache(relative):
            continue
        raw = path.read_bytes()
        entries.append({"path": relative, "size": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()})
    return build_manifest(entries)


def load_manifest(path: Path) -> Dict[str, Any]:
    """Read a manifest. A missing one is a FAILURE, never an empty pass."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ManifestError(f"no image source manifest at {path}: {exc.strerror}") from None
    document = loads_strict(raw)
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise ManifestError(f"{path} is not a {SCHEMA_VERSION} manifest")
    files = document.get("files")
    if not isinstance(files, list):
        raise ManifestError(f"{path} declares no files array")
    return build_manifest(files)


def compare(checkout: Mapping[str, Any],
            image: Mapping[str, Any]) -> List[str]:
    """Differences between the reviewed checkout and the image, as text."""
    theirs = {e["path"]: e for e in image["files"]}
    ours = {e["path"]: e for e in checkout["files"]}
    differences: List[str] = []
    for path in sorted(set(ours) | set(theirs)):
        mine, yours = ours.get(path), theirs.get(path)
        if yours is None:
            differences.append(f"{path}: missing from image")
        elif mine is None:
            differences.append(f"{path}: only in image")
        elif mine["sha256"] != yours["sha256"] or mine["size"] != yours["size"]:
            differences.append(f"{path}: differs (checkout {mine['sha256'][:12]} "
                               f"vs image {yours['sha256'][:12]})")
    return differences
