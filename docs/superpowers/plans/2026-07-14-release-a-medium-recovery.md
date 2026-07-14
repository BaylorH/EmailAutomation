# Release A Medium Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the four root causes behind the failed Release A medium proof and rerun the complete 10-row gate safely.

**Architecture:** Keep the existing backend pipeline and failure rails. Add deterministic helpers at the outbox and extraction boundaries, propagate Sheets transport failures to the scanner, and harden the single-worker Cloud Run process against warm-request memory retention.

**Tech Stack:** Python 3.12, unittest/pytest, Firestore, Google Sheets API, gunicorn, Cloud Run, Microsoft Graph.

---

### Task 1: Deterministic Campaign Outbox Order

**Files:**
- Modify: `email_automation/email.py`
- Test: `tests/test_outbox_safety.py`

- [ ] Add a test that gives `send_outboxes` same-timestamp exact campaign documents in shuffled document-ID order and expects row order 3, 4, 5.
- [ ] Run the focused test and verify it fails with document-ID order.
- [ ] Add a pure outbox sort key using `createdAt`, campaign `rowNumber`, and document ID.
- [ ] Run `tests/test_outbox_safety.py` and verify it passes.

### Task 2: Visible, Retryable Sheet Apply Failures

**Files:**
- Modify: `email_automation/ai_processing.py`
- Test: `tests/test_processing_retryability.py`

- [ ] Add a test whose real proposal apply reaches a fake batch update that raises a Google Sheets 429 and assert the exception escapes.
- [ ] Run the test and verify it fails because the exception is converted to an empty apply result.
- [ ] Remove the broad success-shaped exception return while retaining diagnostic logging.
- [ ] Run the retryability and proposal-apply tests and verify they pass.

### Task 3: Explicit Multi-Suite Total

**Files:**
- Modify: `email_automation/ai_processing.py`
- Test: `tests/test_battery_ai_processing.py`

- [ ] Add controls proving `Suite A is 5,200 SF and Suite C is 4,800 SF, 10,000 SF total` resolves to 10000 while ordinary single-suite extraction remains unchanged.
- [ ] Run the focused tests and verify the multi-suite case fails with 5200 or 4800.
- [ ] Add a conservative explicit-total matcher ahead of the generic first-area matcher.
- [ ] Run the extraction battery and verify it passes.

### Task 4: Request Memory Isolation and Headroom

**Files:**
- Modify: `scripts/deploy_process_user.sh`
- Modify: `deploy/cloudrun-service.yaml`
- Test: `tests/test_process_user_production_deploy_contract.py`
- Test: `tests/test_ws_b_cloudrun_service_spec.py`

- [ ] Add contract assertions for `--max-requests=1` and 2 GiB memory.
- [ ] Run the deploy/spec tests and verify they fail against the current configuration.
- [ ] Update the deploy command and service template without changing concurrency or timeout.
- [ ] Run deployment contract tests and the script dry-run.

### Task 5: Verification, Deploy, and Medium Rerun

**Files:**
- Modify: `docs/superpowers/plans/2026-07-14-release-a-medium-recovery.md` checkboxes only as work completes.
- Update via canonical vault helper: Mohr email automation project hub.

- [ ] Run all focused suites and then the full backend suite.
- [ ] Review the diff for recipient, scheduler-scope, failure-visibility, and retry regressions.
- [ ] Commit and deploy through `scripts/deploy_process_user.sh --apply`, then prove immutable digest, revision config, health, and no-traffic/traffic cutover state.
- [ ] Run a fresh Baylor-only 10-row campaign, process opt-out last, and capture Firestore, Sheet, Gmail, logs, and dashboard evidence.
- [ ] Launch the 22-row proof only if the medium verdict is clean; otherwise preserve the new veto and keep large blocked.
