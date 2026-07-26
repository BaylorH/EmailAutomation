#!/usr/bin/env python3
"""Run the bounded provider-to-policy shadow without workflow effects."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
from typing import NamedTuple, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_ROOT = Path(__file__).resolve().parent
for path in (REPO_ROOT, SCRIPT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from claim_pipeline_openai_transport import (
    INPUT_USD_PER_MILLION,
    MAX_OUTPUT_TOKENS,
    OUTPUT_USD_PER_MILLION,
    OpenAIClaimReplayTransport,
)
from email_automation.claim_pipeline.claim_fixtures import load_claim_fixture_catalog
from email_automation.claim_pipeline.extraction import CLAIM_EXTRACTION_SCHEMA_VERSION
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_policy_fixtures import (
    load_provider_policy_fixture_catalog,
)
from email_automation.claim_pipeline.provider_policy_shadow import (
    BudgetedProviderTransport,
    ProviderBudgetLimits,
    ProviderPolicyShadowIdentity,
    RecordedProviderQualityProposalAdapter,
    run_provider_policy_shadow,
    select_provider_policy_cases,
)
from email_automation.claim_pipeline.provider_quality_fixtures import (
    load_provider_quality_fixture_catalog,
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
PROVIDER_QUALITY_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_provider_quality_cases.json"
)
PROVIDER_POLICY_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_provider_policy_cases.json"
)
DEPENDENCY_LOCK_PATH = REPO_ROOT / "requirements.lock"
OPENAI_TRANSPORT_PATH = REPO_ROOT / "scripts" / "claim_pipeline_openai_transport.py"
SMOKE_CASE_ID = "unavailable-optout-suppression"
MAX_RESERVED_TOKENS = 1_500_000
MAX_RESERVED_COST_MICROUSD = 5_000_000
INPUT_TOKEN_OVERHEAD_PER_CALL = 4_096
WORKFLOW_RELIABILITY_CASE_IDS = (
    "workflow-intents-visible",
    "repeated-information-request",
    "unavailable-optout-suppression",
)


class _ModeSpec(NamedTuple):
    case_ids: tuple[str, ...]
    repeats: int
    planned_calls: int
    max_reserved_tokens: int
    max_reserved_cost_microusd: int


MODE_SPECS = {
    "smoke": _ModeSpec(
        case_ids=(SMOKE_CASE_ID,),
        repeats=1,
        planned_calls=1,
        max_reserved_tokens=MAX_RESERVED_TOKENS,
        max_reserved_cost_microusd=MAX_RESERVED_COST_MICROUSD,
    ),
    "final": _ModeSpec(
        case_ids=(),
        repeats=3,
        planned_calls=24,
        max_reserved_tokens=MAX_RESERVED_TOKENS,
        max_reserved_cost_microusd=MAX_RESERVED_COST_MICROUSD,
    ),
    "workflow-reliability": _ModeSpec(
        case_ids=WORKFLOW_RELIABILITY_CASE_IDS,
        repeats=4,
        planned_calls=12,
        max_reserved_tokens=400_000,
        max_reserved_cost_microusd=2_500_000,
    ),
}


def _git(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _surface_paths() -> tuple[Path, ...]:
    paths = {
        *REPO_ROOT.joinpath("email_automation", "claim_pipeline").glob("*.py"),
        Path(__file__).resolve(),
        OPENAI_TRANSPORT_PATH,
        INTERPRETATION_FIXTURE_PATH,
        CLAIM_FIXTURE_PATH,
        PROVIDER_QUALITY_FIXTURE_PATH,
        PROVIDER_POLICY_FIXTURE_PATH,
        DEPENDENCY_LOCK_PATH,
    }
    return tuple(sorted(paths, key=lambda path: path.relative_to(REPO_ROOT).as_posix()))


def _source_tree_hash() -> str:
    digest = hashlib.sha256()
    for path in _surface_paths():
        relative = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_tree_dirty() -> bool:
    return bool(_git("status", "--porcelain=v1", "--untracked-files=all").strip())


def _load_catalogs():
    interpretation_catalog = load_interpretation_fixture_catalog(
        INTERPRETATION_FIXTURE_PATH
    )
    claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
    provider_quality_catalog = load_provider_quality_fixture_catalog(
        PROVIDER_QUALITY_FIXTURE_PATH,
        claim_catalog=claim_catalog,
        interpretation_catalog=interpretation_catalog,
    )
    provider_policy_catalog = load_provider_policy_fixture_catalog(
        PROVIDER_POLICY_FIXTURE_PATH,
        provider_quality_catalog=provider_quality_catalog,
    )
    return (
        interpretation_catalog,
        claim_catalog,
        provider_quality_catalog,
        provider_policy_catalog,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed sanitized provider-to-policy smoke or final shadow. "
            "Neither mode executes workflow effects."
        )
    )
    parser.add_argument(
        "--provider",
        choices=("recorded", "openai"),
        default="recorded",
    )
    parser.add_argument("--mode", choices=tuple(MODE_SPECS), required=True)
    parser.add_argument(
        "--allow-provider-calls",
        action="store_true",
        help="Required explicit opt-in for pinned OpenAI calls.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional local JSON report path; no file is written by default.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.provider == "openai" and not args.allow_provider_calls:
        parser.error("OpenAI shadow requires --allow-provider-calls")

    (
        interpretation_catalog,
        claim_catalog,
        provider_quality_catalog,
        provider_policy_catalog,
    ) = _load_catalogs()
    mode = MODE_SPECS[args.mode]
    if mode.case_ids:
        provider_policy_catalog = select_provider_policy_cases(
            provider_policy_catalog,
            case_ids=mode.case_ids,
        )
    planned_calls = mode.repeats * len(provider_policy_catalog.cases)
    if planned_calls != mode.planned_calls:
        parser.error("provider-policy mode has an unexpected call plan")

    dirty = _source_tree_dirty()
    budget = None
    telemetry = None
    if args.provider == "recorded":
        adapter = RecordedProviderQualityProposalAdapter(
            provider_quality_catalog,
            claim_catalog,
        )
    else:
        if dirty:
            parser.error("OpenAI shadow requires a clean committed source tree")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            parser.error("OPENAI_API_KEY is required for OpenAI shadow")
        try:
            delegate = OpenAIClaimReplayTransport(api_key=api_key)
            budget = BudgetedProviderTransport(
                delegate,
                limits=ProviderBudgetLimits(
                    max_calls=planned_calls,
                    max_reserved_tokens=mode.max_reserved_tokens,
                    max_reserved_cost_microusd=mode.max_reserved_cost_microusd,
                ),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                input_token_overhead=INPUT_TOKEN_OVERHEAD_PER_CALL,
                input_cost_microusd_per_million=int(
                    INPUT_USD_PER_MILLION * 1_000_000
                ),
                output_cost_microusd_per_million=int(
                    OUTPUT_USD_PER_MILLION * 1_000_000
                ),
            )
            adapter = PinnedProviderProposalAdapter(budget)
            telemetry = budget
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))

    identity = ProviderPolicyShadowIdentity.create(
        code_revision=_git("rev-parse", "HEAD").decode("ascii").strip(),
        source_tree_hash=_source_tree_hash(),
        source_tree_dirty=dirty,
        python_version=platform.python_version(),
        dependency_lock_hash=_file_hash(DEPENDENCY_LOCK_PATH),
        interpretation_fixture_hash=interpretation_catalog.manifest_hash,
        claim_fixture_hash=claim_catalog.manifest_hash,
        provider_quality_fixture_hash=provider_quality_catalog.manifest_hash,
        provider_policy_fixture_hash=provider_policy_catalog.manifest_hash,
        extraction_schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
        provider_id=adapter.provider_id,
        model_id=adapter.model_id,
        prompt_id=adapter.prompt_id,
        prompt_hash=adapter.prompt_hash,
        call_mode=args.mode,
        max_provider_calls=planned_calls,
        max_reserved_tokens=mode.max_reserved_tokens,
        max_reserved_cost_microusd=mode.max_reserved_cost_microusd,
        repeats=mode.repeats,
        case_count=len(provider_policy_catalog.cases),
    )
    report = run_provider_policy_shadow(
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        provider_quality_catalog=provider_quality_catalog,
        provider_policy_catalog=provider_policy_catalog,
        adapter=adapter,
        identity=identity,
        telemetry=telemetry,
        budget=budget,
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
