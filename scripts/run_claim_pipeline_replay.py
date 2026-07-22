#!/usr/bin/env python3
"""Run the current sanitized boundary corpus without provider or workflow effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from email_automation.claim_pipeline.claim_fixtures import load_claim_fixture_catalog
from email_automation.claim_pipeline.extraction import CLAIM_EXTRACTION_SCHEMA_VERSION
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.replay import (
    MAX_REPLAY_REPEATS,
    RecordedProposalAdapter,
    ReplayIdentity,
    run_claim_replay,
)
from email_automation.claim_pipeline.provider_replay import (
    PinnedProviderProposalAdapter,
)


INTERPRETATION_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_interpretation_cases.json"
)
CLAIM_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_claim_cases.json"
)
DEPENDENCY_LOCK_PATH = REPO_ROOT / "requirements.lock"
OPENAI_TRANSPORT_PATH = REPO_ROOT / "scripts" / "claim_pipeline_openai_transport.py"
MAX_PROVIDER_REPLAY_CALLS = 84


def _git(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replay_surface_paths() -> tuple[Path, ...]:
    paths = {
        *REPO_ROOT.joinpath("email_automation", "claim_pipeline").glob("*.py"),
        Path(__file__).resolve(),
        OPENAI_TRANSPORT_PATH,
        INTERPRETATION_FIXTURE_PATH,
        CLAIM_FIXTURE_PATH,
        DEPENDENCY_LOCK_PATH,
    }
    return tuple(sorted(paths, key=lambda path: path.relative_to(REPO_ROOT).as_posix()))


def _source_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in _replay_surface_paths():
        relative_bytes = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_tree_dirty() -> bool:
    return bool(_git("status", "--porcelain=v1", "--untracked-files=all").strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay the current sanitized SiteSift boundary fixtures with no "
            "effects. Recorded mode makes no provider calls."
        )
    )
    parser.add_argument(
        "--provider",
        choices=("recorded", "openai"),
        default="recorded",
    )
    parser.add_argument(
        "--allow-provider-calls",
        action="store_true",
        help="Required explicit opt-in for the bounded OpenAI replay.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        choices=range(1, MAX_REPLAY_REPEATS + 1),
        default=3,
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional local JSON report path. No report file is written by default.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    interpretation_catalog = load_interpretation_fixture_catalog(
        INTERPRETATION_FIXTURE_PATH
    )
    claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
    telemetry = None
    if args.provider == "recorded":
        adapter = RecordedProposalAdapter(claim_catalog)
    else:
        if not args.allow_provider_calls:
            parser.error("OpenAI replay requires --allow-provider-calls")
        planned_calls = args.repeats * len(claim_catalog.cases)
        if planned_calls > MAX_PROVIDER_REPLAY_CALLS:
            parser.error(
                f"OpenAI replay is capped at {MAX_PROVIDER_REPLAY_CALLS} calls"
            )
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            parser.error("OPENAI_API_KEY is required for OpenAI replay")
        from claim_pipeline_openai_transport import OpenAIClaimReplayTransport

        try:
            telemetry = OpenAIClaimReplayTransport(api_key=api_key)
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
        adapter = PinnedProviderProposalAdapter(telemetry)
    identity = ReplayIdentity.create(
        code_revision=_git("rev-parse", "HEAD").decode("ascii").strip(),
        source_tree_hash=_source_tree_hash(),
        source_tree_dirty=_source_tree_dirty(),
        python_version=platform.python_version(),
        dependency_lock_hash=_file_hash(DEPENDENCY_LOCK_PATH),
        interpretation_fixture_hash=interpretation_catalog.manifest_hash,
        claim_fixture_hash=claim_catalog.manifest_hash,
        extraction_schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
        provider_id=adapter.provider_id,
        model_id=adapter.model_id,
        prompt_id=adapter.prompt_id,
        prompt_hash=adapter.prompt_hash,
        evaluation_profile=(
            "provider_quality" if args.provider == "openai" else "candidate_validation"
        ),
        repeats=args.repeats,
        case_count=len(claim_catalog.cases),
        interpretation_case_count=len(interpretation_catalog.cases),
    )
    report = run_claim_replay(
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        adapter=adapter,
        identity=identity,
        telemetry=telemetry,
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
