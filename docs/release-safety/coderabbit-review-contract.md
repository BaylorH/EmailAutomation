# CodeRabbit Review Contract

This contract turns CodeRabbit into a SiteSift-specific reviewer instead of a
generic Python reviewer. It is intentionally focused on the failure modes that
can cost the product trust: wrong sends, hidden failures, prompt drift, scheduler
scope, and feature-lane leakage.

## How To Use CodeRabbit In This Repo

1. Keep `.coderabbit.yaml` at the repository root.
2. Keep `AGENTS.md` current when production lanes or safety rules change.
3. Ask CodeRabbit for the resolved config on a pull request with
   `@coderabbitai configuration` when settings appear surprising.
4. Use CodeRabbit's path instructions for file-specific review focus.
5. Use custom pre-merge checks as a reviewer net, not as a replacement for tests.
6. Run normal backend tests/build checks before any production merge.
7. Treat CodeRabbit findings as review evidence; still verify with unit tests,
   Firestore readback, scheduler readback, and Baylor/BP21 proof where relevant.

## Feature Registry Contract

Every production-impacting pull request must be reviewed against:

- `AGENTS.md`
- `docs/release-safety/feature-registry.json`
- `docs/release-safety/adversarial-rubrics.json`
- `docs/release-safety/outbound-send-surface-inventory.json`
- `docs/release-safety/system-audit-matrix.json`

CodeRabbit should flag a PR when changed behavior is not mapped to a registry
feature with a lane, owner modules, dependencies, send risk, data writes, prompt
contracts, UI surfaces, fixtures, manual screenshot rubrics, and CodeRabbit
checks.

Production V1 currently means the `production_v1_core` and
`production_v1_admin` lanes only. The `dev_results`, `dev_tour`, and
`later_firebase_native` lanes are not normal-user production behavior. Changes
that expose those lanes to normal users, allow their side effects through
Firestore/Functions/backend paths, or mix their prompt contracts into core
campaign sending should be treated as release blockers.

Useful review prompt:

> Review this PR as a SiteSift release-safety change. Compare changed files to
> `AGENTS.md`, `docs/release-safety/feature-registry.json`,
> `docs/release-safety/adversarial-rubrics.json`, and
> `docs/release-safety/outbound-send-surface-inventory.json`, and
> `docs/release-safety/system-audit-matrix.json`. Flag missing
> feature rows, send-risk mismatches, Results/Tour leakage into Production V1,
> UI-only gates for backend-capable behavior, prompt changes without fixtures,
> and any production send path not tied to the outbound safety inventory.

## Production Entry Points

| Area | Files | What must stay true |
|---|---|---|
| Scheduled worker | `.github/workflows/email.yml`, `main.py` | The workflow runs reviewed `main.py`, keeps scope explicit, and uses the scheduler lease. |
| User processing order | `main.py::refresh_and_process_user` | Outbox send, inbox scan, Sent Items/manual scan, processing retry, pending response retry, follow-ups, cleanup, health. |
| Manual/debug scheduler | `app.py` | Must be development-scoped, authenticated/guarded, and unable to surprise-process live users. |
| Outbox sends | `email_automation/email.py` | Must keep recipient, placeholder, signature, opt-out, dedupe, audit, index, cancel, and dead-letter guards. |
| Dashboard replies | `email_automation/email.py` | Must preserve exact body, recipients, thread/row anchors, actionAudit, and pending/cancel state. |
| Pending responses | `email_automation/pending_responses.py` | Must not silently disappear or retry into duplicates. |
| Follow-ups | `email_automation/followup.py` | Must not send after inbound/manual reply, stop, completion, opt-out, or guard failure. |
| Inbox/classifier | `email_automation/processing.py`, `email_automation/ai_processing.py` | Must not drift event taxonomy, auto-schedule tours, hallucinate property facts, or leak client identity. |
| Sheets | `email_automation/sheet_operations.py`, `email_automation/sheets.py` | Must preserve row identity, formulas, custom fields, and provenance. |
| Graph provider | `email_automation/service_providers.py` | Must return enough message/conversation identity for audit and dedupe. |

## Custom Checks

### Outbound Safety Chokepoint

Fail if a changed send path bypasses any of:

- recipient validation,
- CC/reply-all preservation,
- opt-out guard,
- placeholder guard,
- selected signature,
- duplicate-send detection,
- cancellation after claim,
- actionAudit terminalization,
- message/thread indexing,
- dead-letter or visible recovery state.

### Scheduler Scope And Order

Fail if:

- a manual or scheduled run can process users outside the intended scope,
- follow-ups or pending retries run before reply/manual-continuation checks,
- the scheduler lease or GitHub concurrency protection is weakened,
- legacy sender scripts become production-active,
- per-user health no longer records token/Graph state.

### Feature Lane Isolation

Fail if:

- normal campaigns can trigger Tour Scheduling,
- Results/PDF/Map code sends email,
- admin/usage/debug routes are reachable by non-admin users,
- backend-capable features rely only on frontend hiding,
- experimental code shares prompt contracts with core campaign sending.

### AI Contract Drift

Fail if prompt/classifier/extractor changes:

- add or rename events without tests and downstream handling,
- blur property unavailable vs tour unavailable,
- introduce tour/LOI scheduling language into core outreach,
- allow fake CoStar-style facts when no source exists,
- remove first-name/placeholder safeguards,
- fail to update realistic broker-language fixtures.

### Recovery And Privacy Boundary

Fail if:

- dead-letter or pending-response retries can double-send,
- recovery deletes evidence before support can inspect it,
- code reads unrelated mailbox content instead of campaign-scoped records,
- Jill/live data can be mutated without an exact repair path,
- action notifications disappear before the underlying send/update is terminal.

## Current Known Review Gaps

CodeRabbit should keep calling these out until the tests exist:

- tight unit coverage for `refresh_and_process_user` ordering,
- focused tests for `process_pending_responses`,
- focused tests for `scan_sent_items_for_manual_replies` privacy and time-window boundaries,
- end-to-end proof that manual user replies suppress future autonomous sends,
- reply-all preservation across dashboard replies and follow-ups.

## Minimum Evidence Before Merge

For production-impacting changes, reviewers should expect:

- focused unit tests for changed lane,
- affected dependency tests from the release-safety map,
- `python3 -m py_compile` for changed Python files,
- `git diff --check`,
- GitHub Actions/scheduler readback for backend deploys,
- Baylor/BP21-only live proof for real email behavior,
- no Jill/live mutation unless explicitly approved.
