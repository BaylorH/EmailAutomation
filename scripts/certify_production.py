#!/usr/bin/env python3
"""Drive one certification run against the deployed private twin.

This is the agent-facing half of the instrument, and most of what it does is
REFUSE. The plan draws the agent's boundaries explicitly -- no public Git push,
no production traffic change, no shared Functions or Hosting deploy, no model
provider call, no raw captured message text -- and a CLI that could do any of
those would be the single easiest way to cross one by accident.

So those verbs are ABSENT from this file rather than guarded inside it. A test
reads the source and requires they never appear: a guard can be bypassed by the
next person who needs "just this once"; a missing capability cannot.

What it does do:

    prepare -> status -> run -> independent readbacks -> sanitized evidence

Anything it cannot verify becomes a refusal rather than a softer verdict. A
stamp that outruns its evidence is worse than no stamp, because it reads as
proof.

    python3 scripts/certify_production.py --scenario campaign-one-property \\
        --url https://process-user-certification-....run.app

Baylor-manual actions are printed as one exact command and never executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

# The operator whose identity token authenticates every call. Impersonated, so
# no key material exists locally.
OPERATOR_SERVICE_ACCOUNT = (
    "sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com"
)

# Operations an agent may invoke. review-input returns raw captured subjects and
# bodies for human naturalness review; it is Baylor's to read locally, and an
# agent that fetched it would be holding fixture text it must never hold.
AGENT_ALLOWED_OPERATIONS = frozenset({"prepare", "run", "status", "abort", "recover"})
HUMAN_ONLY_OPERATIONS = frozenset({"review-input", "review"})

# Counts that must be present before a verdict means anything. A PASS without a
# replay has not shown convergence; without a cleanup readback it has not shown
# zero residue.
REQUIRED_EVIDENCE_COUNTS = ("replay_delta", "cleanup_residue")

# Any nonzero value here contradicts a PASS regardless of what the service said.
FORBIDDEN_EFFECT_COUNTS = (
    "graph_network", "drive_call", "nonfixture_write", "bcc",
    "global_counter_effect", "cleanup_residue", "replay_delta",
)


class CertifyRefused(RuntimeError):
    """The run may not proceed, or its result may not be believed."""


# -- source state -----------------------------------------------------------


def assert_source_state(*, dirty: bool, local_sha: str, upstream_sha: str) -> None:
    """The reviewed source must be clean and already public.

    A stamp names a revision. With uncommitted changes the revision it names is
    not the code that ran, and with an unpushed commit nobody else can review,
    rebuild, or reproduce it.
    """
    if dirty:
        raise CertifyRefused(
            "checkout is dirty; a stamp would name a revision that is not what ran"
        )
    if local_sha != upstream_sha:
        raise CertifyRefused(
            "local and upstream revisions differ; the reviewed source is not public. "
            "Baylor pushes it; this tool never does."
        )


def read_source_state(repo: Path = REPO_ROOT) -> Tuple[bool, str, str]:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, check=False).stdout.strip()
    dirty = bool(git("status", "--porcelain"))
    local_sha = git("rev-parse", "HEAD")
    upstream_sha = git("rev-parse", "@{u}") or ""
    return dirty, local_sha, upstream_sha


# -- agent boundaries -------------------------------------------------------


def assert_agent_may_call(operation: str) -> None:
    if operation in HUMAN_ONLY_OPERATIONS:
        raise CertifyRefused(
            f"{operation} returns raw captured message text; it is a Baylor-manual "
            "command and an agent may not call it or capture its output"
        )
    if operation not in AGENT_ALLOWED_OPERATIONS:
        raise CertifyRefused(f"unknown certification operation: {operation}")


def agent_mode_precheck(scenario: Mapping[str, Any], *, run_id: str,
                        url: str) -> Tuple[Optional[str], Optional[str]]:
    """Stop BEFORE /run for a scenario that needs a real model call.

    The refusal has to precede the call rather than follow it: an agent must
    never submit fixture prompts to a model provider, and "we called it and then
    reported INSTRUMENT_BLOCKED" is not that.
    """
    if scenario.get("launchClass") == "user_runtime_launch_required":
        command = (
            f"python3 scripts/certify_production.py --url {url} "
            f"--scenario {scenario['scenarioId']} --run-id {run_id} --user-runtime-launch"
        )
        return "INSTRUMENT_BLOCKED:user_runtime_launch_required", command
    return None, None


# -- evidence ---------------------------------------------------------------


def assert_verdict_is_supported(result: Mapping[str, Any]) -> None:
    """A verdict is believed only when its own evidence carries it."""
    counts = dict(result.get("counts") or {})
    if result.get("verdict") != "PASS":
        return
    missing = [name for name in REQUIRED_EVIDENCE_COUNTS if name not in counts]
    if missing:
        raise CertifyRefused(
            f"PASS is unsupported: missing {', '.join(missing)}. A run that did not "
            "replay has not shown convergence, and one that did not read back has "
            "not shown zero residue."
        )
    nonzero = sorted(name for name in FORBIDDEN_EFFECT_COUNTS
                     if int(counts.get(name, 0)) != 0)
    if nonzero:
        raise CertifyRefused(
            f"PASS contradicted by its own evidence: {', '.join(nonzero)} nonzero"
        )


def new_run_id(scenario_id: str, *, nonce: str) -> str:
    """One run id per invocation. Run ids are single-use forever."""
    material = f"{scenario_id}\x1f{nonce}".encode("utf-8")
    return f"cert-{hashlib.sha256(material).hexdigest()[:24]}"


# -- transport --------------------------------------------------------------


def identity_token(audience: str) -> str:
    """Impersonated operator token. No key material exists locally."""
    completed = subprocess.run(
        ["gcloud", "auth", "print-identity-token", "--include-email",
         f"--impersonate-service-account={OPERATOR_SERVICE_ACCOUNT}",
         f"--audiences={audience}"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        raise CertifyRefused("could not mint an operator identity token")
    return completed.stdout.strip()


def call(url: str, operation: str, body: Mapping[str, Any], *,
         token: str) -> Tuple[Dict[str, Any], int]:
    assert_agent_may_call(operation)
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{url}/certification/{operation}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8")), response.status
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8")), exc.code
        except Exception:      # noqa: BLE001 - a body-less error is still a result
            return {"status": "error", "reason": "unreadable_response"}, exc.code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="the private twin's exact URL")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--revision", help="defaults to the local HEAD")
    parser.add_argument("--nonce", default="", help="makes the run id unique")
    parser.add_argument("--skip-source-check", action="store_true",
                        help="local dry runs only; never for a stamp")
    args = parser.parse_args(argv)

    dirty, local_sha, upstream_sha = read_source_state()
    if not args.skip_source_check:
        assert_source_state(dirty=dirty, local_sha=local_sha,
                            upstream_sha=upstream_sha)
    revision = args.revision or local_sha
    run_id = args.run_id or new_run_id(args.scenario, nonce=args.nonce or local_sha)

    sys.path.insert(0, str(REPO_ROOT))
    from email_automation.certification import scenarios

    try:
        scenario = dict(scenarios.get(args.scenario))
    except KeyError:
        print(f"REFUSED: {args.scenario} is not an approved scenario", file=sys.stderr)
        return 2
    scenario["launchClass"] = scenario.get("launchClass", "")

    blocked, command = agent_mode_precheck(scenario, run_id=run_id, url=args.url)
    if blocked:
        print(blocked)
        print("\nBaylor runs exactly this, and the agent resumes from sanitized "
              "/status afterwards:\n")
        print(f"  {command}")
        return 0

    token = identity_token(args.url)
    body = {"scenarioId": args.scenario, "runId": run_id,
            "expectedRevision": revision}

    prepared, code = call(args.url, "prepare", body, token=token)
    if code != 200:
        print(f"prepare refused ({code}): {prepared.get('reason')}", file=sys.stderr)
        return 1
    print(f"PREPARED {run_id}  authorization {prepared['authorizationDigest'][:12]}")

    result, code = call(args.url, "run", body, token=token)
    if code != 200:
        print(f"run refused ({code}): {result.get('reason')}", file=sys.stderr)
        # A failure before a proven /run aborts; an ambiguous one status-checks.
        call(args.url, "abort", {"runId": run_id, "expectedRevision": revision},
             token=token)
        return 1

    try:
        assert_verdict_is_supported(result)
    except CertifyRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    final, _ = call(args.url, "status",
                    {"runId": run_id, "expectedRevision": revision}, token=token)

    print(json.dumps({
        "runId": run_id,
        "scenarioId": args.scenario,
        "revision": revision,
        "verdict": result.get("verdict"),
        "counts": result.get("counts"),
        "evidenceDigest": result.get("evidenceDigest"),
        "finalState": final.get("state"),
    }, indent=2, sort_keys=True))
    return 0 if result.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
