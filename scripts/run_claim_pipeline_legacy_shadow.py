#!/usr/bin/env python3
"""Run the deterministic legacy-policy shadow with no provider or effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from email_automation.claim_pipeline.legacy_shadow import (
    LegacyShadowIdentity,
    run_legacy_shadow,
)
from email_automation.claim_pipeline.legacy_shadow_fixtures import (
    load_legacy_shadow_fixture_catalog,
)
from email_automation.claim_pipeline.policy_fixtures import (
    load_policy_fixture_catalog,
)


POLICY_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_policy_cases.json"
)
LEGACY_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_legacy_shadow_cases.json"
)
DEPENDENCY_LOCK_PATH = REPO_ROOT / "requirements.lock"


def _git(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_surface_paths() -> tuple[Path, ...]:
    paths = {
        *REPO_ROOT.joinpath("email_automation", "claim_pipeline").glob("*.py"),
        Path(__file__).resolve(),
        POLICY_FIXTURE_PATH,
        LEGACY_FIXTURE_PATH,
        DEPENDENCY_LOCK_PATH,
    }
    return tuple(sorted(paths, key=lambda path: path.relative_to(REPO_ROOT).as_posix()))


def _source_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in _source_surface_paths():
        relative = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare sanitized legacy proposal attempts with deterministic "
            "policy plans. No providers or effects are available."
        )
    )
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON report path. No report file is written by default.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    policy_catalog = load_policy_fixture_catalog(POLICY_FIXTURE_PATH)
    shadow_catalog = load_legacy_shadow_fixture_catalog(
        LEGACY_FIXTURE_PATH,
        policy_catalog=policy_catalog,
    )
    identity = LegacyShadowIdentity.create(
        code_revision=_git("rev-parse", "HEAD").decode("ascii").strip(),
        source_tree_hash=_source_tree_hash(),
        source_tree_dirty=bool(
            _git("status", "--porcelain=v1", "--untracked-files=all").strip()
        ),
        python_version=platform.python_version(),
        dependency_lock_hash=_file_hash(DEPENDENCY_LOCK_PATH),
        policy_fixture_hash=policy_catalog.manifest_hash,
        legacy_fixture_hash=shadow_catalog.manifest_hash,
        repeats=args.repeats,
        case_count=len(shadow_catalog.cases),
    )
    report = run_legacy_shadow(
        policy_catalog=policy_catalog,
        shadow_catalog=shadow_catalog,
        identity=identity,
    )
    serialized = json.dumps(
        report.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    if args.output is not None:
        args.output.write_text(f"{serialized}\n", encoding="utf-8")
    print(serialized)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
