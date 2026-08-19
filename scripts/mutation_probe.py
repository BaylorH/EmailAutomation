#!/usr/bin/env python3
"""Mutation probe: break the shipped product, and see which tests notice.

WHY THIS EXISTS
---------------
A green suite is exactly the condition under which "this test never reaches the
code it claims to test" is invisible. Static reading does not find it -- a test
that never reaches its target looks perfectly normal. This project has paid for
that lesson five separate times:

  1. A guard's test built its own scenario dict carrying the guard's own
     spelling, so the guard was tested against itself. 91 scenarios were
     unguarded while the test was green.
  2. ``test_zero_drive_state_machine`` keyed a fixture ``"address"`` where the
     canonical field is ``property_address``. The column-contract validator
     refused before doing anything and returned ``None``, so all 21 methods
     asserted against ``None``. The module had never once run the pipeline.
  3. ``test_surface_d_6_``'s fake Firestore modelled no ``transaction``, so a
     send was refused by an unmodelled daily-cap counter and never reached the
     recipient filter the test existed to check.
  4. Two twin-contract rules asserted only that a message *contained*
     "image"/"traffic", which a generic residual message also satisfies. Both
     named rules could have been deleted outright with the suite green.
  5. ``scripts/deploy_certification_twin.sh`` was read by no test at all; three
     one-token edits each left the entire suite green.

The only instrument that finds these is mutation: change the SHIPPED source,
run the tests that plausibly cover it, and record which mutations NOTHING
caught. A mutation nothing catches is a regression nobody would catch either.

SAFETY -- read before adding a case
-----------------------------------
This tool edits tracked source files in place. Two agents in this project have
been burned by a mutation that silently no-op'd (reporting a false NOT CAUGHT
verdict, or worse a false CAUGHT one), and one harness killed mid-case left a
sabotaged control in the working tree that only luck kept out of a commit. So
every mutation here is guarded on both sides:

  * BEFORE writing: the anchor must occur in the file exactly ``occurrences``
    times. Not "at least once" -- exactly. An anchor that drifted to 0 or
    multiplied to 2 is a stale case, not a mutation.
  * The replacement must differ from the anchor. A no-op replacement is
    refused outright.
  * AFTER writing: the file is re-read FROM DISK and must have changed, and
    its SHA-256 must differ from the pre-mutation SHA.
  * AFTER restoring: the file is re-read FROM DISK and its SHA-256 must equal
    the pre-mutation SHA exactly. Byte equality, not "looks the same".
  * SIGINT / SIGTERM / atexit all restore every outstanding mutation. If this
    process dies at any point after the write, the tree comes back.

USAGE
-----
    python scripts/mutation_probe.py --list
    python scripts/mutation_probe.py --case workflow_dev_scoped_gate
    python scripts/mutation_probe.py --all
    python scripts/mutation_probe.py --all --json out.json

Exit code is 0 when every case ran and restored cleanly, regardless of whether
mutations were caught -- "nothing caught this" is a FINDING, not a tool error.
Exit code is non-zero only if a guard tripped or a restore failed, i.e. if the
report cannot be trusted.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]

# The interpreter that has this project's dependencies installed. Overridable
# because worktrees do not all resolve the same venv.
DEFAULT_PYTHON = os.environ.get("MUTATION_PROBE_PYTHON") or sys.executable

TEST_ENV_DEFAULTS = {
    "E2E_TEST_MODE": "true",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class MutationGuardError(RuntimeError):
    """A mutation could not be applied or restored in a way we can trust.

    Raised rather than returning a verdict, because a mutation whose
    application is uncertain produces a verdict that is worse than no verdict:
    a silent no-op reports NOT CAUGHT for a surface that may be perfectly
    well covered.
    """


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class MutationCase:
    """One mutation: an exact anchor in a shipped file, and what to replace it with.

    ``anchor`` is an exact substring, never a regex. Regexes drift silently;
    an exact substring plus an exact occurrence count does not.

    ``modules`` are the test modules run against the mutated tree. They are
    chosen deliberately per case (see ``rationale``) because a full-suite run
    per mutation is too slow to iterate on. Choosing them wrongly is the one
    way this tool can lie: a mutation reported NOT CAUGHT may simply have had
    its covering module left out. Every case therefore names why its modules
    are the plausible coverage.
    """

    name: str
    target: str  # repo-relative path
    anchor: str
    replacement: str
    occurrences: int
    modules: Sequence[str]
    blast_radius: str
    rationale: str


@dataclass
class _Live:
    path: Path
    original: bytes
    original_sha: str


_LIVE: Dict[str, _Live] = {}
_HANDLERS_INSTALLED = False


def _restore_all_live() -> None:
    """Put every outstanding mutation back. Safe to call repeatedly."""
    for key in list(_LIVE):
        live = _LIVE.pop(key)
        try:
            live.path.write_bytes(live.original)
        except Exception as exc:  # pragma: no cover - best effort on teardown
            print(
                f"CRITICAL: could not restore {live.path}: {exc}",
                file=sys.stderr,
                flush=True,
            )


def _signal_restore(signum, _frame):  # pragma: no cover - signal path
    _restore_all_live()
    # Re-raise as the default disposition so the exit status is honest.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_restore_handlers() -> None:
    """Install atexit + SIGINT/SIGTERM restore. Idempotent.

    A harness killed mid-case left a sabotaged control in this repo's working
    tree once. That must not be possible again.
    """
    global _HANDLERS_INSTALLED
    if _HANDLERS_INSTALLED:
        return
    atexit.register(_restore_all_live)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal_restore)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass
    _HANDLERS_INSTALLED = True


class Mutation:
    """Context manager applying one guarded mutation to a real file.

    Guards are asserted, not logged. Every failure path raises
    ``MutationGuardError`` so a case can never silently report a verdict it did
    not actually test.
    """

    def __init__(
        self,
        path: Path,
        anchor: str,
        replacement: str,
        occurrences: int = 1,
    ) -> None:
        self.path = Path(path)
        self.anchor = anchor
        self.replacement = replacement
        self.occurrences = occurrences
        self.original: Optional[bytes] = None
        self.original_sha: Optional[str] = None
        self.mutated_sha: Optional[str] = None
        self._key = f"{self.path}:{id(self)}"

    # -- application -----------------------------------------------------
    def apply(self) -> None:
        install_restore_handlers()

        if self.anchor == self.replacement:
            raise MutationGuardError(
                "replacement is identical to anchor; this mutation would be a "
                "no-op and its verdict would be meaningless"
            )
        if not self.path.is_file():
            raise MutationGuardError(f"target does not exist: {self.path}")

        original = self.path.read_bytes()
        text = original.decode("utf-8")
        found = text.count(self.anchor)
        if found != self.occurrences:
            raise MutationGuardError(
                f"anchor occurs {found}x in {self.path} but the case declares "
                f"{self.occurrences}x -- the case is stale, refusing to mutate. "
                f"anchor={self.anchor!r}"
            )

        self.original = original
        self.original_sha = hashlib.sha256(original).hexdigest()
        _LIVE[self._key] = _Live(self.path, original, self.original_sha)

        self.path.write_text(
            text.replace(self.anchor, self.replacement), encoding="utf-8"
        )

        # Re-read FROM DISK. Never trust the string we thought we wrote.
        after = self.path.read_bytes()
        if after == original:
            _restore_one(self._key)
            raise MutationGuardError(
                f"write to {self.path} left content unchanged -- the mutation "
                "silently no-op'd"
            )
        self.mutated_sha = hashlib.sha256(after).hexdigest()
        if self.mutated_sha == self.original_sha:
            _restore_one(self._key)
            raise MutationGuardError(
                f"SHA of {self.path} is unchanged after mutation ({self.mutated_sha})"
            )

    # -- restoration -----------------------------------------------------
    def restore(self) -> None:
        if self.original is None:
            return
        self.path.write_bytes(self.original)
        _LIVE.pop(self._key, None)
        # Verify by SHA equality against the pre-mutation bytes.
        restored_sha = sha256_of(self.path)
        if restored_sha != self.original_sha:
            raise MutationGuardError(
                f"RESTORE FAILED for {self.path}: sha {restored_sha} != "
                f"original {self.original_sha}. THE WORKING TREE IS DIRTY."
            )

    def __enter__(self) -> "Mutation":
        self.apply()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.restore()
        return False


def _restore_one(key: str) -> None:
    live = _LIVE.pop(key, None)
    if live is not None:
        live.path.write_bytes(live.original)


# ---------------------------------------------------------------------------
# Running tests
# ---------------------------------------------------------------------------

# SUBFAILED must be here. This suite leans heavily on subTest, and pytest
# reports a failed subtest as `SUBFAILED(param='x') tests/...::T::t` -- a line
# the FAILED/ERROR pattern alone does not match. Without it, a mutation caught
# ONLY by a subtest yields exit 1 with nothing parseable, which classify_run
# correctly refuses to read, so the case would be reported COULD NOT TELL when
# the suite in fact caught it perfectly. Found by exactly that happening.
_FAIL_LINE = re.compile(
    r"^(?:FAILED|ERROR|SUBFAILED(?:\([^)]*\))?)\s+(\S+)", re.MULTILINE
)


def run_modules(
    modules: Sequence[str],
    *,
    python: str = DEFAULT_PYTHON,
    repo_root: Path = REPO_ROOT,
    timeout: int = 900,
) -> Dict[str, object]:
    """Run the named test modules and report what failed."""
    env = dict(os.environ)
    env.update(TEST_ENV_DEFAULTS)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        python,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--no-header",
        "-x" if os.environ.get("MUTATION_PROBE_FAILFAST") else "--tb=no",
        *modules,
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = proc.stdout + proc.stderr
        code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - slow path
        raw = exc.stdout or ""
        out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        code = None  # deliberately NOT a number: there is no exit status
        timed_out = True
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "failures": sorted({m.group(1) for m in _FAIL_LINE.finditer(out or "")}),
        "seconds": round(time.time() - started, 1),
        "tail": "\n".join((out or "").strip().splitlines()[-12:]),
    }


# pytest's documented exit statuses. 1 is the only one that means "tests ran
# and some failed"; every other non-zero value means the run itself did not
# produce a usable answer.
PYTEST_OK = 0
PYTEST_TESTS_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_INTERNAL_ERROR = 3
PYTEST_USAGE_ERROR = 4
PYTEST_NO_TESTS_COLLECTED = 5

GREEN = "green"
RED = "red"
UNREADABLE = "unreadable"


def classify_run(run: Dict[str, object]) -> tuple:
    """Reduce a pytest run to one of three states, never two.

    This is the heart of the tool's honesty. "Caught", "not caught" and
    "could not tell" are THREE states, and collapsing the third into either of
    the other two produces the worst output this tool can produce:

      * fold it into NOT CAUGHT and the report invents a coverage hole. Someone
        then writes a test for a gap that does not exist while the real gaps
        stay hidden. In a tool whose entire output is "which mutations nothing
        noticed", that is the single most expensive way to be wrong.
      * fold it into CAUGHT and the report hides a real hole -- a mutation that
        broke collection outright would be scored as "the tests noticed", when
        in fact no test ever ran.

    A non-zero exit with no parseable FAILED/ERROR line is exactly the shape of
    an internal error, a usage error, a collection error, a timeout, or a
    crashed interpreter. None of those tell us anything about coverage.
    """
    code = run.get("exit_code")
    failures = run.get("failures") or []

    if run.get("timed_out") or code is None:
        return UNREADABLE, "the test run timed out; it produced no verdict"
    if code == PYTEST_OK:
        return GREEN, ""
    if code == PYTEST_TESTS_FAILED and failures:
        return RED, ""
    if code == PYTEST_TESTS_FAILED and not failures:
        return (
            UNREADABLE,
            "pytest reported failures but no FAILED/ERROR line could be "
            "parsed; the output format may have changed and the verdict "
            "cannot be trusted",
        )
    if code == PYTEST_NO_TESTS_COLLECTED:
        return (
            UNREADABLE,
            "no tests were collected -- a run in which nothing executed says "
            "nothing about whether the mutation would be caught",
        )
    if code == PYTEST_USAGE_ERROR:
        return UNREADABLE, "pytest usage error; the run never started"
    if code == PYTEST_INTERNAL_ERROR:
        return (
            UNREADABLE,
            "pytest internal error (often a collection failure caused by the "
            "mutation itself); no test outcome was produced",
        )
    if code == PYTEST_INTERRUPTED:
        return UNREADABLE, "the run was interrupted before finishing"
    return UNREADABLE, f"unrecognised pytest exit status {code!r}"


def probe(
    case: MutationCase,
    *,
    python: str = DEFAULT_PYTHON,
    repo_root: Path = REPO_ROOT,
    verify_baseline: bool = True,
    runner=None,
) -> Dict[str, object]:
    """Run one case end to end: baseline, mutate, run, restore, verify.

    The baseline run matters. A module that is ALREADY red would make every
    mutation look "caught", which is the exact false-confidence this tool
    exists to destroy.

    ``runner`` is injectable so the harness's own tests can drive the
    could-not-tell path without needing a test suite that misbehaves on demand.
    """
    run = runner or (
        lambda modules: run_modules(modules, python=python, repo_root=repo_root)
    )
    target = repo_root / case.target
    result: Dict[str, object] = {
        "case": case.name,
        "target": case.target,
        "modules": list(case.modules),
        "blast_radius": case.blast_radius,
        "rationale": case.rationale,
    }

    if verify_baseline:
        baseline = run_modules(case.modules, python=python, repo_root=repo_root) if runner is None else runner(case.modules)
        result["baseline"] = baseline
        state, why = classify_run(baseline)
        if state is not GREEN:
            result["verdict"] = "COULD NOT TELL"
            result["reason"] = (
                "the covering modules are not green BEFORE the mutation "
                f"({why or 'tests already failing'}); a verdict measured "
                "against a broken baseline is meaningless"
            )
            # Nothing was written, so nothing needed restoring. Saying so
            # explicitly matters: without it the report renders "restore:
            # FAILED" for a case that never touched the file, which trains the
            # reader to ignore the one line that must never be ignored.
            result["restored_ok"] = True
            result["mutation_applied"] = False
            return result

    mutation = Mutation(target, case.anchor, case.replacement, case.occurrences)
    try:
        mutation.apply()
        result["sha_before"] = mutation.original_sha
        result["sha_mutated"] = mutation.mutated_sha
        result["mutated"] = run(case.modules)
    finally:
        # Restoration is in `finally` so an exception anywhere above -- a
        # crashed subprocess, a KeyboardInterrupt, a bug in this file -- puts
        # the source back. The signal/atexit handlers cover the cases that
        # never reach Python at all.
        mutation.restore()
        result["sha_restored"] = sha256_of(target)
        result["restored_ok"] = result["sha_restored"] == mutation.original_sha

    state, why = classify_run(result["mutated"])  # type: ignore[arg-type]
    if state is RED:
        result["verdict"] = "CAUGHT"
    elif state is GREEN:
        result["verdict"] = "NOT CAUGHT"
    else:
        # Explicitly NOT "NOT CAUGHT". A run we cannot read is a harness
        # failure, and naming it as one is the difference between reporting a
        # coverage hole and inventing one.
        result["verdict"] = "COULD NOT TELL"
        result["reason"] = f"HARNESS FAILURE: {why}"
    return result


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
#
# Targets are chosen by BLAST RADIUS, not convenience. Priority order:
#   (a) guards on irreversible effects -- a send, a delete, a permission, a
#       traffic change, an IAM binding;
#   (b) fail-closed validators that return None/early on refusal, which is the
#       exact shape that silently swallows a whole test module;
#   (c) shipped artifacts nobody imports -- shell scripts, deploy manifests,
#       CI workflows -- since nothing forces a test to ever read them.

CASES: List[MutationCase] = [
    MutationCase(
        name="workflow_prod_dev_scoped_flag",
        target=".github/workflows/email.yml",
        anchor='SITESIFT_DEV_SCOPED_SCHEDULER: "1"',
        replacement='SITESIFT_DEV_SCOPED_SCHEDULER: "0"',
        occurrences=1,
        modules=[
            "tests/test_scheduler_scope.py",
            "tests/test_ws_b_secret_coverage_contract.py",
            "tests/test_ws_b_cutover_rollback_doc.py",
            "tests/test_release_feature_registry.py",
            "tests/test_ws_b_startup_env_validation.py",
            "tests/test_graph_send_inventory.py",
            "tests/test_unread_artifacts.py",
        ],
        blast_radius=(
            "CRITICAL. This flag is the emergency launch safety gate on the "
            "every-30-minutes production cron. With it off, the scheduler stops "
            "being scoped to Baylor's mailbox and processes every live beta "
            "user: autonomous sends, scans and follow-ups to real recipients. "
            "Irreversible and external."
        ),
        rationale=(
            "These five modules are every test file that mentions the workflow "
            "path or the scheduler-scope env names. If the gate has a pin, it "
            "is in one of them."
        ),
    ),
    MutationCase(
        name="workflow_prod_target_user_ids",
        target=".github/workflows/email.yml",
        anchor='SITESIFT_SCHEDULER_TARGET_USER_IDS: "NO7lVYVp6BaplKYEfMlWCgBnpdh2"',
        replacement='SITESIFT_SCHEDULER_TARGET_USER_IDS: ""',
        occurrences=1,
        modules=[
            "tests/test_scheduler_scope.py",
            "tests/test_ws_b_secret_coverage_contract.py",
            "tests/test_ws_b_cutover_rollback_doc.py",
            "tests/test_release_feature_registry.py",
            "tests/test_graph_send_inventory.py",
            "tests/test_unread_artifacts.py",
        ],
        blast_radius=(
            "CRITICAL. Same gate, the other half: emptying the target list "
            "widens the production cron from one mailbox to all of them."
        ),
        rationale="Same covering set as the flag it pairs with.",
    ),
    MutationCase(
        name="workflow_dev_scoped_allowlist",
        target=".github/workflows/email-dev-scoped.yml",
        anchor='SITESIFT_SCHEDULER_ALLOWED_USER_IDS: "NO7lVYVp6BaplKYEfMlWCgBnpdh2"',
        replacement='SITESIFT_SCHEDULER_ALLOWED_USER_IDS: "*"',
        occurrences=1,
        modules=[
            "tests/test_scheduler_scope.py",
            "tests/test_ws_b_secret_coverage_contract.py",
            "tests/test_release_feature_registry.py",
            "tests/test_graph_send_inventory.py",
            "tests/test_unread_artifacts.py",
        ],
        blast_radius=(
            "HIGH. The manually dispatched dev workflow runs main.py against "
            "PRODUCTION Firebase. Its only containment is the allowlist. "
            "Widening it turns a dev button into a full production run."
        ),
        rationale=(
            "No test names this file at all, so the covering set is the "
            "scheduler-scope pins, the registry that claims to own CI "
            "artifacts, and the one test that GLOBS .github/workflows/ "
            "without naming any file -- a name-based search would have missed "
            "that last one, and missing it is how this tool lies."
        ),
    ),
    MutationCase(
        name="production_reset_dry_run_default",
        target="scripts/production_reset.py",
        anchor="def delete_collection_batched(db, collection_ref, batch_size=50, dry_run=True):",
        replacement="def delete_collection_batched(db, collection_ref, batch_size=50, dry_run=False):",
        occurrences=1,
        modules=[
            "tests/test_release_feature_registry.py",
            "tests/test_backend_modules_are_tracked.py",
            "tests/test_unread_artifacts.py",
        ],
        blast_radius=(
            "HIGH. production_reset.py mass-deletes 13 Firestore collections "
            "per user plus nested subcollections. Flipping the fail-safe "
            "default turns every caller that omits dry_run into a live wipe. "
            "Irreversible data loss."
        ),
        rationale=(
            "No test references this script by name at all; these two are the "
            "repo-wide inventory pins, i.e. the only plausible catchers."
        ),
    ),
    MutationCase(
        name="replay_cli_apply_gate_inverted",
        target="scripts/replay_exact_message.py",
        anchor='        "--apply",\n        action="store_true",',
        replacement='        "--apply",\n        action="store_true",\n        default=True,',
        occurrences=1,
        modules=[
            "tests/test_operator_message_replay.py",
            "tests/test_external_effect_inventory.py",
            "tests/test_release_feature_registry.py",
            "tests/test_unread_artifacts.py",
        ],
        blast_radius=(
            "HIGH. This CLI replays one real inbox message through the live "
            "Graph path. --apply is the only thing between a read-only "
            "preflight and a real operator action; defaulting it True makes "
            "every invocation apply. email_automation/operator_replay.py is "
            "well tested, which is exactly why this survives: the LOGIC is "
            "pinned and the CLI WIRING that decides whether to invoke it is "
            "not."
        ),
        rationale=(
            "CONTROL CASE, and a correction. This script was first recorded as "
            "read by no test, because no test file contains the string "
            "'replay_exact_message.py'. It is in fact imported and driven by "
            "tests/test_operator_message_replay.py via `from scripts import "
            "replay_exact_message`, and mutation proved the --apply gate IS "
            "caught. A name-based coverage census cannot see an import; only "
            "running the mutation settled it."
        ),
    ),
    MutationCase(
        name="twin_deploy_public_ingress",
        target="scripts/deploy_certification_twin.sh",
        anchor="  --no-allow-unauthenticated",
        replacement="  --allow-unauthenticated",
        occurrences=1,
        modules=["tests/test_certification_mutation_controls.py"],
        blast_radius=(
            "CRITICAL if unpinned. This is defect #5 itself: the one-token "
            "edit that would put a public, unauthenticated route on the "
            "certification twin."
        ),
        rationale=(
            "CONTROL CASE. This gap was already closed by "
            "test_certification_mutation_controls.py. It is kept in the "
            "catalogue as a regression pin on the pin: if this ever reports "
            "NOT CAUGHT again, the fix for defect #5 has rotted. NOTE: the "
            "covering module imports PyYAML, so without it installed this "
            "case correctly reports COULD NOT TELL rather than guessing."
        ),
    ),
    MutationCase(
        name="outbound_placeholder_validator_neutered",
        target="email_automation/outbound_safety.py",
        anchor="def find_unresolved_placeholders(body: Optional[str]) -> List[str]:",
        replacement=(
            "def find_unresolved_placeholders(body: Optional[str]) -> List[str]:\n"
            "    return []  # MUTATION"
        ),
        occurrences=1,
        modules=["tests/test_outbound_body_safety.py"],
        blast_radius=(
            "HIGH. This is the guard that stops a draft containing "
            "'[Client Name]' from being sent to a real recipient. Neutered, "
            "unfilled template placeholders reach customers."
        ),
        rationale="The module named for the file under test.",
    ),
]

CASES_BY_NAME = {c.name: c for c in CASES}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render(result: Dict[str, object]) -> str:
    verdict = result.get("verdict")
    mark = {
        "CAUGHT": "caught",
        "NOT CAUGHT": "NOT CAUGHT",
        "COULD NOT TELL": "COULD NOT TELL",
        "GUARD REFUSED": "GUARD REFUSED",
    }.get(str(verdict), str(verdict))
    lines = [
        f"[{mark}] {result['case']}  ({result['target']})",
        f"    modules : {', '.join(result['modules'])}",  # type: ignore[arg-type]
    ]
    mutated = result.get("mutated")
    if isinstance(mutated, dict):
        fails = mutated.get("failures") or []
        lines.append(
            f"    mutated : exit={mutated['exit_code']} "
            f"failures={len(fails)} in {mutated['seconds']}s"
        )
        for f in list(fails)[:6]:
            lines.append(f"              - {f}")
    if result.get("reason"):
        lines.append(f"    reason  : {result['reason']}")
    if result.get("mutation_applied") is False:
        lines.append("    restore : n/a (nothing was written)")
    else:
        lines.append(
            f"    restore : {'OK' if result.get('restored_ok') else 'FAILED'} "
            f"(sha {str(result.get('sha_before'))[:12]})"
        )
    if verdict == "NOT CAUGHT":
        lines.append(f"    RADIUS  : {result['blast_radius']}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # __doc__ is None under `python -OO`; a mutation harness must not crash on
    # its own help text.
    summary = (__doc__ or "mutation probe").split("\n")[0]
    parser = argparse.ArgumentParser(description=summary)
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--case", action="append", default=[], help="run one case by name")
    parser.add_argument("--all", action="store_true", help="run every case")
    parser.add_argument("--json", help="write full results as JSON to this path")
    parser.add_argument(
        "--python",
        default=DEFAULT_PYTHON,
        help="interpreter used to run pytest (must have the project deps)",
    )
    parser.add_argument(
        "--modules",
        help=(
            "override the covering modules for the selected case(s), comma "
            "separated. Use this to CONFIRM a NOT CAUGHT verdict against a "
            "wider set -- the one way this tool can lie is by omitting the "
            "module that would have caught the mutation"
        ),
    )
    args = parser.parse_args(argv)

    if args.list:
        for case in CASES:
            print(f"{case.name}\n    {case.target}\n    {case.blast_radius}\n")
        return 0

    if args.all:
        selected = list(CASES)
    elif args.case:
        try:
            selected = [CASES_BY_NAME[n] for n in args.case]
        except KeyError as exc:
            print(f"unknown case: {exc}", file=sys.stderr)
            return 2
    else:
        parser.print_help()
        return 2

    if args.modules:
        override = tuple(m.strip() for m in args.modules.split(",") if m.strip())
        selected = [
            MutationCase(
                name=c.name,
                target=c.target,
                anchor=c.anchor,
                replacement=c.replacement,
                occurrences=c.occurrences,
                modules=override,
                blast_radius=c.blast_radius,
                rationale=f"MODULES OVERRIDDEN ON THE COMMAND LINE. {c.rationale}",
            )
            for c in selected
        ]

    install_restore_handlers()
    results = []
    trustworthy = True
    for case in selected:
        try:
            result = probe(case, python=args.python)
        except MutationGuardError as exc:
            trustworthy = False
            result = {
                "case": case.name,
                "target": case.target,
                "modules": list(case.modules),
                "blast_radius": case.blast_radius,
                "rationale": case.rationale,
                "verdict": "GUARD REFUSED",
                "reason": str(exc),
                "restored_ok": True,
            }
        results.append(result)
        print(_render(result), flush=True)
        if not result.get("restored_ok", False):
            trustworthy = False

    not_caught = [r for r in results if r.get("verdict") == "NOT CAUGHT"]
    # Kept separate from NOT CAUGHT on purpose. These are cases where the tool
    # failed, not surfaces where the tests failed, and merging them would put
    # phantom coverage holes into the report.
    unknown = [
        r for r in results if r.get("verdict") in ("COULD NOT TELL", "GUARD REFUSED")
    ]
    print("\n" + "=" * 70)
    print(
        f"{len(results)} case(s): {len(not_caught)} NOT CAUGHT, "
        f"{len(unknown)} COULD NOT TELL"
    )
    for r in not_caught:
        print(f"  NOT CAUGHT      {r['case']}  ->  {r['target']}")
    for r in unknown:
        print(f"  COULD NOT TELL  {r['case']}  ->  {r.get('reason')}")

    if unknown:
        trustworthy = False

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0 if trustworthy else 1


if __name__ == "__main__":
    sys.exit(main())
