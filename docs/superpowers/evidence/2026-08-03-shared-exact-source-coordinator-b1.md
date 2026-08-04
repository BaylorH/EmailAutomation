# Shared exact-source coordinator B1 verification evidence

**Recorded:** 2026-08-04
**Deliverable:** both (code and production-readiness findings)
**Status:** B1 verified and pushed; production remains **NO-GO** until B2-B4
are complete.

## Immutable candidate

- Baseline: `2b5e785bbc46754de16ca439e463793653e45f84`
- Verified code head: `a3fcdf51a9b721b4b61be857476942498a292495`
- Branch: `codex/sitesift-m3-b1-source-authority-20260803`
- Remote branch readback: exactly
  `a3fcdf51a9b721b4b61be857476942498a292495`
- Baseline delta: 31 files, 41,683 insertions, 1,287 deletions across
  33 commits.
- Sorted changed-file aggregate:
  `018fb05dd4bec033075c7cf9d70bf65aced9abd9f93ee21e79308d9e2b6ec5fe`

The aggregate used the plan's sorted-file procedure with
`/sbin/sha256sum`, the available equivalent of the unavailable `shasum`
binary.

## Verification gates

| Gate | Result | Runner duration |
|---|---:|---:|
| Complete B1 focused suite (10 modules, offline containment) | 606/606 | 23.779s |
| Retained M2 changed-surface suite (23 modules, offline containment) | 669/669 | 22.101s |
| Original source-loss and concurrency barriers | 3/3 | 0.053s |
| Final review-focused safety sample | 10/10 | pass |
| Compile every changed Python file | clean | exit 0 |
| `git diff --check 2b5e785` | clean | exit 0 |

Both suites ran with credentials removed, an empty OpenAI key, and an
unreachable local Firestore emulator address. The candidate run made no
provider or production-data calls. The retained command as originally written
in the plan could not import locally because it expected an absent
`service-account.json`; its authoritative rerun used the same offline
containment as the focused suite and passed 669/669.

GitHub contains the exact verified head. `gh run list` returned no workflow
runs for this branch, so the recorded local gates are the verification
authority; no CI result is being implied.

## Independent reviews

- Spec-compliance review: **APPROVED**, no open Critical or Important finding.
  The review initially found two Important gaps. The verified correction now
  runs retained Terminal A and legacy marker/replay-claim preflight before any
  fresh classifier or downstream call, and invalid coordinator modes fail
  safely to disabled with a non-secret configuration warning.
- Code-quality/security review: **APPROVED**, no open Critical or Important
  finding. It separately verified preflight order, B1 request recovery,
  invalid-mode containment, settled retry, and post-settlement crash recovery.

## Original source-loss barrier

The exact regression is
`SourceCoordinatorScannerTests.test_two_same_thread_sources_are_independently_settled`.
Its central assertion is:

> enforced scanner must process every exact same-thread source oldest-first

The test requires both same-thread sources to be dispatched, classified,
ledgered, and independently settled before cursor advancement. It passes on
the candidate together with the partial-marker/cursor barrier and the
two-worker single-classifier barrier.

Commit `8d98624` contains the Task 7 regression and its implementation in one
commit. A separate durable RED commit/transcript was not retained, so this
record does not invent a RED SHA. The exact GREEN barrier and assertion are
durably recorded here.

## Zero-effect and production boundary

- Runtime mode remains default-disabled; malformed or unknown values also
  resolve to disabled.
- Enforced production mode was not enabled.
- No deployment, merge, PR, campaign, external message, provider mutation, or
  production-data access occurred.
- No B2 stable-row ownership, B3 general execution/effect fence, or B4 real
  integration/rules cutover is claimed by B1.

## Production-clearance sequence

1. **B2:** stable row identity, immutable row bindings, and retained row owner.
2. **B3:** execution epochs, claims, effect intents/outcomes, reconciliation,
   and stale-worker fences.
3. **B4:** real datastore/rules integration, mutation inventories, frontend
   server-only boundaries, shadow-to-enforced cutover, and rollback proof.
4. **Production canaries:** only after B2-B4 gates, using a user-launched
   campaign or an explicitly named self-owned test recipient, with telemetry,
   rollback, and defect capture active.

B1 therefore closes a crucial source-loss class and moves the program to B2;
it does not by itself clear production campaigns.
