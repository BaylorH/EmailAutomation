# Terminal Note Atomicity Implementation Plan

> **For agentic workers:** Use test-driven development and verification-before-completion. Stop at the first unexpected failing test.

**Goal:** Prevent terminal campaign transitions from succeeding without a durable, truthful Sheet note.

**Architecture:** Preserve the existing Firestore pre-stage that disables follow-ups, then extend the existing atomic Sheets row-move batch so it also writes the terminal note before deleting the source row. Checked Firestore state/event finalization follows that batch; a processing retry reads and reconciles an already-below row instead of duplicating the note.

**Tech Stack:** Python, unittest/pytest, Google Sheets API mocks, Firestore fakes.

**Plan deliverable:** code

---

### Task 1: Add failing full-path regressions

**Files:**
- Modify: `tests/test_compound_nonviable_processing.py`
- Read: `email_automation/processing.py`
- Read: `email_automation/sheets.py`

- [ ] Extend `_run_tour_invite_reply_processing` with a real Notes header plus observable Sheet mutation/finalization seams without weakening existing tests.
- [ ] Add an atomic Sheet-batch-failure test proving both row roots are staged but neither move nor note commits and stop/finalization, handled-event recording, and reply do not occur.
- [ ] Add a missing-notes-column test with the same fail-closed assertions.
- [ ] Add an existing-note retry test proving no duplicate write and successful transition.
- [ ] Add an already-below note-failure test proving no terminal finalization or handled event.
- [ ] Add a requirements-mismatch full-path assertion for truthful persisted note content.
- [ ] Run the new nodes and capture the expected failures before implementation.

### Task 2: Implement the atomic normal-path Sheet boundary

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `email_automation/sheet_operations.py`
- Reuse: `email_automation/sheets.py::_execute_with_retry`
- Reuse: `email_automation/sheets.py::_col_letter`

- [ ] Add a small timestamp helper that derives `MM/DD/YYYY` from the inbound Graph timestamp with a UTC-now legacy fallback.
- [ ] Read and merge the source row's existing notes value before any mutation; fail if the notes column/read is unavailable.
- [ ] Extend `move_row_below_divider` with the required notes column/value and add an `updateCells` request between its copy and delete requests in the same batch.
- [ ] Add an idempotent helper for the already-below recovery path that safely constructs the A1 range, reads the cell, no-ops when the exact note exists, and otherwise writes the merged value.
- [ ] Let missing-column and Sheets errors propagate; do not log-and-continue.
- [ ] Pass the required note into the atomic move batch and call the repair helper before the already-below terminalization path.
- [ ] Remove the old post-move swallowed comment block.

### Task 3: Check terminal finalization evidence

**Files:**
- Modify: `email_automation/processing.py`
- Modify only if the full-path regression requires it: `email_automation/sheet_operations.py`

- [ ] Ensure the staged row roots, final row number, stopped reason, non-viable evidence, and follow-up stop are reconciled before event handling.
- [ ] Treat swallowed/false finalization and handled-event results as retryable failures rather than success.
- [ ] Keep the notification dedupe key stable and write it only after durable note/state evidence.
- [ ] Preserve the no-send behavior on every failure path.

### Task 4: Verify adjacent behavior

**Files:**
- Test: `tests/test_compound_nonviable_processing.py`
- Test: `tests/test_split_thread_terminal_state.py`
- Test: `tests/test_jill_live_campaign_regressions.py`
- Test: `tests/test_processing_retryability.py`
- Test: `tests/test_followup_terminal_state.py`
- Test: `tests/test_sheet_operations.py` or the closest existing sheet-operation contract suite

- [ ] Run the focused new nodes.
- [ ] Run the complete compound/split-thread suites.
- [ ] Run the Jill live-regression, processing-retryability, and follow-up terminal-state suites fail-fast.
- [ ] Run the full deterministic suite if focused/adjacent verification is green.
- [ ] Inspect `git diff --check` and confirm no raw customer data, credentials, provider calls, or production effects were added.

### Task 5: Independent review

- [ ] Request a spec-compliance review against the design and this plan.
- [ ] Resolve all Critical/Important findings and rerun affected tests.
- [ ] Request a separate code-quality review.
- [ ] Resolve all Critical/Important findings and rerun affected tests.
- [ ] Commit the approved candidate but do not deploy or merge it into production.
