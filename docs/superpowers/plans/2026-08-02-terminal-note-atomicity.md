# Terminal Note Atomicity Implementation Plan

> **For agentic workers:** Use test-driven development and verification-before-completion. Stop at the first unexpected failing test.

**Goal:** Prevent terminal campaign transitions from succeeding without a durable, truthful Sheet note.

**Architecture:** At entry to the ordered `property_unavailable` handler, persist an immutable, exact-source-message saga and a preflighted Firestore finalization plan before mutating the Sheet. A fenced, bounded Sheet-attempt lineage precedes the atomic note/move; exact-root Firestore finalization then creates durable notification and reply obligations. Cleanup appends one of at most eight immutable exact-source settlements with archived Sheet audit evidence and clears the active Sheet fields so a legitimate later generation starts clean. Only the exact source may resume or match its settlement; pending workers fail closed on every active terminal marker immediately before Graph.

**Tech Stack:** Python, unittest/pytest, Google Sheets API mocks, Firestore fakes.

**Plan deliverable:** code

---

### Task 1: Add failing full-path regressions

**Files:**
- Modify: `tests/test_compound_nonviable_processing.py`
- Read: `email_automation/processing.py`
- Read: `email_automation/sheets.py`

- [x] Extend `_run_tour_invite_reply_processing` with a real Notes header plus observable Sheet mutation/finalization seams without weakening existing tests.
- [x] Add an atomic Sheet-batch-failure test proving both row roots are staged but neither move nor note commits and stop/finalization, handled-event recording, and reply do not occur.
- [x] Add a missing-notes-column test with the same fail-closed assertions.
- [x] Add an existing-note retry test proving no duplicate write and successful transition.
- [x] Add an already-below note-failure test proving no terminal finalization or handled event.
- [x] Add a requirements-mismatch full-path assertion for truthful persisted note content.
- [x] Run the new nodes and capture the expected failures before implementation.

### Task 2: Implement the atomic normal-path Sheet boundary

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `email_automation/sheet_operations.py`
- Reuse: `email_automation/sheets.py::_execute_with_retry`
- Reuse: `email_automation/sheets.py::_col_letter`

- [x] Add a small timestamp helper that derives `MM/DD/YYYY` from the inbound Graph timestamp with a UTC-now legacy fallback.
- [x] Read and merge the source row's existing notes value before any mutation; fail if the notes column/read is unavailable.
- [x] Extend `move_row_below_divider` with the required notes column/value and add an `updateCells` request between its copy and delete requests in the same batch.
- [x] Add an idempotent helper for the already-below recovery path that safely constructs the A1 range, reads the cell, no-ops when the exact note exists, and otherwise writes the merged value.
- [x] Let missing-column and Sheets errors propagate; do not log-and-continue.
- [x] Pass the required note into the atomic move batch and call the repair helper before the already-below terminalization path.
- [x] Remove the old post-move swallowed comment block.

### Task 3: Check terminal finalization evidence

**Files:**
- Modify: `email_automation/processing.py`
- Modify only if the full-path regression requires it: `email_automation/sheet_operations.py`

- [x] Ensure the staged row roots, final row number, stopped reason, non-viable evidence, and follow-up stop are reconciled before event handling.
- [x] Treat swallowed/false finalization and handled-event results as retryable failures rather than success.
- [x] Keep the notification dedupe key stable and write it only after durable note/state evidence.
- [x] Preserve the no-send behavior on every failure path.

### Task 4: Verify adjacent behavior

**Files:**
- Test: `tests/test_compound_nonviable_processing.py`
- Test: `tests/test_split_thread_terminal_state.py`
- Test: `tests/test_jill_live_campaign_regressions.py`
- Test: `tests/test_processing_retryability.py`
- Test: `tests/test_followup_terminal_state.py`
- Test: `tests/test_sheet_operations.py` or the closest existing sheet-operation contract suite

- [x] Run the focused new nodes.
- [x] Run the complete compound/split-thread suites.
- [x] Run the Jill live-regression, processing-retryability, and follow-up terminal-state suites fail-fast.
- [x] Run the full deterministic suite if focused/adjacent verification is green.
- [x] Inspect `git diff --check` and confirm no raw customer data, credentials, provider calls, or production effects were added.

### Task 5: Independent review

- [x] Request a spec-compliance review against the design and this plan.
- [x] Resolve all Critical/Important findings and rerun affected tests.
- [x] Request a separate code-quality review.
- [x] Resolve all Critical/Important findings and rerun affected tests.
- [x] Leave the approved candidate uncommitted and unshipped under the resume boundary.

### Task 6: Remediate immutable-recovery review findings

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `tests/test_compound_nonviable_processing.py`
- Modify: this plan and its design

- [x] Add full-path red tests for no-event retry, changed reason, changed alternate context/note, ambiguous finalization, post-finalization notification recovery, and the 500-write preflight bound.
- [x] Persist the original source message, reason/note/event, source row/anchor, response scenario/body, and exact finalization plan in a hash-validated saga.
- [x] Resume only the exact source message without fresh model output or stopped-thread reactivation.
- [x] Finalize exact roots atomically with explicit notification and reply obligations.
- [x] Create notification and strict handled-event evidence only after terminal finalization.
- [x] Preserve the reply obligation until a durable send/retry/reconciliation/suppression outcome exists.
- [x] Reject more than 500 planned writes before any Sheet mutation.
- [x] Add a tamper regression for persisted immutable saga inputs.
- [x] Route blank-body exact recovery before attachment/link, order-log, fresh Sheet/client, identity, model, and generic-event work.
- [x] Freeze and validate Sheet/tab/Notes/anchor and recipient context; reject immutable layout drift without redirecting work.
- [x] Serialize split-root staging on the plan-derived canonical root and honor every unexpired foreign execution lease.
- [x] Carry an owner plus monotonic fencing token through normal and exact-recovery execution; reject missing, malformed, expired, or superseded leases.
- [x] Transactionally fence send/queue intents, notification/reply outcomes, and final cleanup; renew before provider/queue effects.
- [x] Prove stale owner A cannot queue, write an outcome, or clear newer owner B, including one accepted-send interleave reconciled with exactly one send.
- [x] Treat the initial thread-root read as authoritative and retry exact/different sources before generic effects when it is unavailable.
- [x] Reconcile provider-accepted send ambiguity through Sent Items without a duplicate send.
- [x] Persist campaign-suppression and definite-unsent queue intents before deterministic pending work.
- [x] Reconcile only exact `pendingResponses/{threadId}` evidence after an owed-clear failure; reject mismatched evidence and do not send/requeue an existing exact document.
- [x] Make different-source messages history-only and retryable while a terminal saga is pending, before reactivation, attachments, Sheet/model/events, notification, queue, or reply.
- [x] Stop same-thread scanner admission after an exact recovery failure and leave both exact/later processed markers absent.
- [x] Move different-source history-only admission ahead of campaign/status/client/follow-up decisions and prove only history/index/timestamp changes.
- [x] Reconcile provider-reachable attempts only by retained immutable draft ID and frozen envelope; require retained `settled_definitely_not_sent` proof for response-retry queues and no retained/active permit for pre-send campaign queues, both with zero Sent lookup and fail-closed malformed-state handling.
- [x] Fence pending-response Graph sends with a transactional thread-plus-pending claim; preserve while a terminal saga owns the thread, and prove a worker that loaded before Sent reconciliation cannot send after the exact pending document is atomically removed.
- [x] Persist one immutable, owner/fence-bound, non-stealable terminal Sheet mutation attempt before any Sheet write, with a provider deadline shorter than the execution lease.
- [x] Make takeover read-only for every `request_started` Sheet attempt; reconcile only exact row-plus-note evidence, and fail closed/operator-visible on absent, partial, malformed, or unreadable evidence with zero second mutation.
- [x] Bound the real Sheets transport and disable implicit mutation replay; record an explicit 429 as `definitely_not_applied`, deny same-owner reuse, and allow only a different higher-fenced owner to create one hash-linked next ordinal (eight-attempt cap).
- [x] Require `TerminalSagaExecution` for `_finalize_terminal_thread_roots` and replace its ownerless batch with one claim-validating, lease-renewing Firestore transaction.
- [x] Add a real stale-A/new-B barrier proving A cannot finalize after B owns a higher fence and B alone reconciles/finalizes the exact applied Sheet effect.
- [x] Add a second real barrier proving an early pending claim loses to terminal staging at the final pre-Graph token/envelope/lease/thread assertion, releases only its own claim, and sends zero Graph requests.
- [x] Apply the same fail-closed active-terminal marker union at both pending fences: saga key, reply/notification owed, pending reason, saga/claim dict, or reply-attempt dict; do not block on historical outcomes/settlements.
- [x] Persist a bounded collection of immutable exact-source terminal settlement projections before cleanup; archive Sheet attempt/history/review, cap retention at eight with no eviction, and route cleanup-success-before-processed-marker retry through its exact tombstone before replacement/generic work.
- [x] Clear active Sheet attempt/history/review atomically with saga pointers and add A-cleanup → B-fresh-attempt → both-exact-retries coverage.
- [x] Add strict apply-then-raise cleanup readback over every planned exact root, canonical claim, current obligation, active saga/Sheet pointer, and exact settlement projection.
- [x] Add RED/GREEN cases for ambiguous Sheet apply/readback, malformed attempt, zero second mutation, definite-429 lineage, stale finalizer, stale pending token/marker, cleanup ambiguity, cleanup-before-marker exact retry, and multi-generation settlement.
- [x] Repair pending-response expectations for the intentional CAS write and give the unknown-campaign fixture absent or expired well-formed ownership.
- [x] Run fresh focused, compound, and adjacent verification plus `py_compile` and `git diff --check`.
- [x] Complete independent scope/correctness review and resolve Critical/Important findings.
- [x] Leave the remediation uncommitted and unshipped; do not push, merge, deploy, contact anyone, or use production/provider/browser state.

### Task 7: Harden terminal Sheet attempt integrity with v2 exact state

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `tests/test_compound_nonviable_processing.py`
- Modify: this plan and its design

- [x] Set `TERMINAL_SHEET_MUTATION_VERSION` to 2 and persist `attemptImmutableHash` over exactly the existing immutable identity fields, including the v2 version.
- [x] Make `attemptHash` cover the complete persisted attempt state except itself, including immutable hash, status, and every exact outcome field.
- [x] Define one exact schema per status; reject missing, extra, cross-status, malformed-value, type, timestamp, and fence fields after hash validation.
- [x] Rebuild transitions from immutable identity so old outcome fields cannot leak. Permit only `request_started` to the four outcomes and `needs_operator_review` to `reconciled_applied`; make exact same-state replay a no-write no-op and all terminal states immutable.
- [x] Require exact integer `429` for the definite nonapply lane, rotate its final full-state hash, and link the next higher-fenced distinct-owner ordinal/history entry to that final hash only.
- [x] Require unique attempt IDs, contiguous ordinals, distinct successive owners, strictly increasing fencing tokens, exact final-hash links, and recursively v2-only active/history/settlement state.
- [x] Bind visible review evidence to the post-transition attempt ID/full hash; rotate once, preserve exact replay, and clear/strip review state only on legal reconciliation.
- [x] Make applied and reconciled execution idempotent with no provider or Sheet readback work; preserve read-only reconciliation for ambiguous/request-started/review lanes.
- [x] Accept ambiguous attempt-creation/outcome commits only when readback exactly matches the intended attempt, renewed claim, history, and review. Keep no-apply and a different internally valid fabricated state retryable.
- [x] Validate the complete Sheet attempt lineage and review before terminal root finalization.
- [x] Centrally derive mutation kind from immutable saga geometry and reject valid-enum kind flips even when attempt and settlement projection hashes are fully recomputed.
- [x] Reject caller-kind disagreement and staged/finalized saga row-shape drift before attempt persistence/provider work; build and branch only on the centrally derived kind.
- [x] Classify persisted state read-only after attempt-preparation failure and write malformed review evidence only for genuinely malformed state, leaving exact no-apply ambiguity and valid claim/owner errors unpoisoned.
- [x] Add focused controls for stale full-state tamper, exact schemas, the complete transition matrix, final full-hash lineage, review replay, applied/reconciled idempotency, v1/tampered nesting, exact transaction readback, exact integer 429, finalization history, rehashed mutation-kind drift, two-invocation no-apply recovery, and pre-provider caller/phase geometry rejection.
- [x] Run the approved stale-hash regression after the minimal hash fix: 1 test passed.
- [x] Run the consolidated focused v2 matrix: 23 tests passed, including fully rehashed mutation-kind drift, two-invocation no-apply recovery, and pre-provider geometry controls.
- [x] Run changed-Python compile and `git diff --check`.
- [x] Run the root-owned broad, adjacent-release, and release gates offline.
- [x] Leave the candidate uncommitted and unshipped; do not push, merge, deploy, contact anyone, or access provider/production state.

### Task 8: Close Graph-send and post-settlement production-readiness findings

**Files:**
- Modify: `email_automation/send_permits.py`
- Modify: `email_automation/pending_responses.py`
- Modify: `email_automation/processing.py`
- Modify: `email_automation/system_health.py`
- Test: `tests/test_send_permits.py`
- Test: `tests/test_graph_subject_binding.py`
- Test: `tests/test_graph_message_id_path_encoding.py`
- Test: `tests/test_post_settlement_completion_obligations.py`
- Test: `tests/test_pending_completion_health.py`

- [x] Require canonical same-path Firestore identities at every permit and issuer authority boundary.
- [x] Recover pending and terminal permit issuance across no-apply, apply-then-raise, repeated, malformed, and linked-capability crash boundaries without another provider request.
- [x] Preserve unresolved retained-draft review evidence in the canonical user-root queue and block claim, reissue, and completion until an authenticated local-only resolution settles it.
- [x] Bind every prepared and exact-Sent envelope to frozen case-sensitive subject, canonical visible body, recipient sets, Bcc absence, and exact attachment multiset evidence.
- [x] Encode every Graph message identifier exactly once in URL paths while retaining its raw identity in durable evidence.
- [x] Scope same-contact reactivation to exact source evidence and revalidate staging row geometry immediately before writes.
- [x] Replay exact terminal client completion from the immutable settlement projection; a false initial post-cleanup completion result remains retryable.
- [x] Atomically create one deterministic user-root pending-response completion obligation whenever exact Sent settlement deletes pending work, including direct, automatic, and operator-reconciled sends.
- [x] Replay only owed completion obligations through local Firestore/client state, require the exact pending issuer to be absent, and settle completed, ineligible, and not-required outcomes without Graph, Sent Items, or provider work.
- [x] Expose active or malformed completion obligations through a bounded, server-filtered health count without counting retained settled tombstones.
- [x] Run the frozen focused and broad deterministic matrices, compile every changed Python file, inspect `git diff --check`, and freeze a changed-file hash manifest.
- [x] Complete independent scope/correctness and code-quality review with zero unresolved Critical, Important, or Minor findings.
- [x] Leave the candidate uncommitted and unshipped under the resume boundary; do not push, merge, deploy, contact anyone, or access provider/production state.
