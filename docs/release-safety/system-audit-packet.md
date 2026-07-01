# SiteSift System Audit Packet

This packet is the cross-repo release review contract for SiteSift. It tells
Codex, CodeRabbit, and future reviewers to evaluate one SiteSift product, not a
backend pull request here and a frontend pull request there.

## Plain-English Purpose

SiteSift failed its first broader-user moment because unfinished feature lanes
were able to affect the base email product:

- a normal user saw or triggered tour-scheduling behavior,
- a launch email reached production with `[NAME]`,
- and the team could not quickly answer every surface that could still send.

The fix is not only one patch. The fix is a repeatable release gate that proves
the whole product path before normal users are widened again.

## Current Release Rule

Normal users stay on Production V1. Production V1 is the base product:

- upload a campaign,
- map columns and verify variables,
- queue/send initial outreach,
- process broker replies,
- update the sheet,
- show dashboard actions and failures,
- preserve reply-all context,
- stop/cancel/dismiss/archive safely,
- and never leak Results/Tour/experimental behavior into core emailing.

Results, PDF exports, 3D Map, Tour Planner, Usage expansion, and
Firebase-native worker migration are separate development lanes until they pass
this packet too.

## What Reviewers Must Read

Use these files together:

- backend `AGENTS.md`
- frontend `AGENTS.md`
- backend `.coderabbit.yaml`
- frontend `.coderabbit.yaml`
- `docs/release-safety/feature-registry.json`
- `docs/release-safety/feature-gradebook.json`
- `docs/release-safety/adversarial-rubrics.json`
- `docs/release-safety/outbound-send-surface-inventory.json`
- `docs/release-safety/system-audit-matrix.json`

If a change touches a send-risk feature, the matrix entry for that feature must
be reviewed before it is considered production-safe. The gradebook entry for
that feature must also be used to pick fresh event classes, trigger variations,
combination playbooks, state permutations, negative controls, evidence, and
human grading roles; a narrow fixed happy path is not enough.

## Review Order

1. **Frontend surface:** identify who can see the control, warning, composer,
   result, or status.
2. **Backend surface:** identify every Python/Firebase Function path the control
   or worker can trigger.
3. **Firestore state:** identify collections written, terminal statuses, and
   visible failure states.
4. **Email / Graph state:** identify recipients, Cc/reply-all behavior, Graph
   IDs, Sent Items reconciliation, dedupe, and dead-letter behavior.
5. **Sheet/results state:** identify row anchors, formulas, source values,
   generated reports, and readback evidence.
6. **CodeRabbit review:** ask the SiteSift-specific questions in the matrix and
   require tests/evidence for every valid finding.

## Non-Negotiable Invariants

- Normal users cannot trigger Tour Scheduling.
- Raw placeholders never reach outbox.
- Every send has visible audit or recovery evidence.
- Retry checks Sent Items and manual user continuation before sending again.
- Frontend gates match backend entitlements; UI hiding is not enough.
- Reply-all Cc context is preserved while blocked/unrelated recipients are
  filtered.

## CodeRabbit Prompt

Use this prompt on recovery PRs and any future production-widening PR:

> Review this PR as a cross-repo SiteSift release-safety change. Treat the
> frontend and backend as one product. Compare the changed files against
> `AGENTS.md`, `.coderabbit.yaml`, `feature-registry.json`,
> `feature-gradebook.json`, `adversarial-rubrics.json`,
> `outbound-send-surface-inventory.json`, and `system-audit-matrix.json`. Flag
> any send-risk feature whose frontend surface, backend surface, Firestore
> writes, email behavior, user-visible evidence, source-of-truth readbacks,
> adversarial fixture classes, gradebook event/variation/combination coverage,
> or CodeRabbit review questions are incomplete. In particular, look for normal
> users triggering Tour Scheduling, raw placeholders reaching outbox, dropped
> Cc/reply-all recipients, duplicate sends after retry, hidden failed sends,
> UI-only entitlements, and Jill/MOHR identity leakage.

## Evidence Required Before Normal Users Return

No live-user email or data mutation is allowed as part of proving this packet.
Use clean branches, tests, emulator/local proof, read-only production readbacks,
and Baylor/BP21-only live proof when a real email send is explicitly needed.

The minimum evidence set is:

- all changed feature ids are named,
- all changed feature ids select fresh gradebook events, variations,
  combinations, state permutations, fixture classes, negative controls,
  evidence requirements, and human grading roles,
- all touched send-risk matrix entries pass their fixture classes,
- backend targeted safety tests pass,
- frontend denied-path and happy-path tests pass,
- production scheduler scope is read back,
- Firestore queues are clean,
- Baylor/BP21 base campaign proof passes without Results/Tour sends,
- CodeRabbit has reviewed the packet-aware PRs,
- and any valid CodeRabbit finding is either fixed with tests or explicitly
  parked outside Production V1.

## Stop Conditions

Stop and do not reopen normal users if:

- any path can send outside the intended recipient set,
- any normal user can trigger Tour Scheduling,
- any raw placeholder can reach outbox or Graph,
- any retry can double-send without Sent Items/manual-continuation checks,
- any send failure can be hidden from the operator,
- any Jill/live data mutation would be required without exact approval,
- or CodeRabbit finds a valid release-safety hole that lacks tests.

## How This Fits The Branch Strategy

Production V1 recovery uses this packet to prove the base product. The feature
development branch uses the same packet to grow Results/Tour safely, but those
features remain out of normal-user production until their own matrix rows pass.
The future Firebase-native migration must preserve this packet as the acceptance
test so the rewrite does not silently lose the existing product.
