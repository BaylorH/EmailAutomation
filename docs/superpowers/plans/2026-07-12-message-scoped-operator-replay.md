# Message-Scoped Operator Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely replay one exact failed Baylor inbox message without running any other user or campaign work.

**Architecture:** Add a narrow replay service function that validates UID, client, thread, Graph message ID, RFC message ID, sender, recipient, failure record, processed markers, and Sent Items state before invoking the existing single-message processor under the existing per-user lease. Recovery-artifact checks use exact source-message queries only. Completion requires fresh Sheet evidence stamped with the exact Graph ID, RFC ID, and replay attempt. Add a dry-run-first CLI that acquires the user's Graph token but never scans the inbox, outbox, pending responses, follow-ups, or any other failure.

**Tech Stack:** Python, Firestore, Microsoft Graph, MSAL, existing EmailAutomation processing and lease helpers.

---

### Task 1: Exact replay contract

**Files:**
- Create: `email_automation/operator_replay.py`
- Create: `tests/test_operator_message_replay.py`

- [ ] Write failing tests proving mismatched UID/client/thread/Graph/RFC/sender/recipient, missing failure, processed messages, existing sent artifacts, and held leases all refuse processing.
- [ ] Run `python -m unittest tests.test_operator_message_replay` and confirm the tests fail because the replay module does not exist.
- [ ] Implement the minimal identity validator and exact Firestore lookups.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Single-message execution

**Files:**
- Modify: `email_automation/operator_replay.py`
- Modify: `tests/test_operator_message_replay.py`

- [ ] Write failing tests proving one exact Graph fetch, exact-only artifact queries, one claim-consuming `process_inbox_message` call, fresh attempt-bound Sheet evidence, both processed markers, and atomic movement of only the exact failure into resolved replay history after success.
- [ ] Add a failure test proving processing exceptions preserve the active failure and both fail-closed replay preclaims.
- [ ] Implement the exact-message callback under `run_with_user_lease`; do not call `refresh_and_process_user`, `scan_inbox_against_index`, outbox, pending response, or follow-up functions.
- [ ] Run focused and adjacent processing/lease tests.

### Task 3: Dry-run-first operator CLI

**Files:**
- Create: `scripts/replay_exact_message.py`
- Modify: `tests/test_operator_message_replay.py`

- [ ] Write failing CLI tests for required identity arguments, default dry-run, explicit `--apply`, and refusal outside the approved Baylor/BP21 lane.
- [ ] Implement Graph token acquisition from the existing per-user MSAL cache without logging token contents.
- [ ] Print a concise preflight report in dry-run mode; require `--apply` for the mutation path.
- [ ] Run focused tests, the full backend suite, `py_compile`, and `git diff --check`.

### Task 4: Review and one-time production use

**Files:**
- No new files unless review finds a defect.

- [ ] Commit and push the branch only after the full suite passes.
- [ ] Run CodeRabbit and independent read-only review; resolve every valid safety finding.
- [ ] Deploy the underlying broken-asset fix first while the Cloud Tasks queue remains paused.
- [ ] Run the replay CLI in dry-run mode and compare exact Firestore, Graph, and Sheet state.
- [ ] Run `--apply` once only if every identity and Sent Items gate passes.
- [ ] Verify row 3 changes, warning/failure/processed records, Sent Items delta, unchanged rows 4-5, and unchanged other users.
