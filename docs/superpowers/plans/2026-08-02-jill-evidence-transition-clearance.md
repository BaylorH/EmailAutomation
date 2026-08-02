# Jill Evidence-to-Transition Clearance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this evidence plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a dated, sanitized, executable production-readiness finding that maps recent target-user reports and historical campaign behavior to the exact SiteSift transition and test that proves or refutes each risk.

**Architecture:** Raw mailbox and production-history evidence stays in an uncommitted private working ledger. Only de-identified scenario records, transition/test mappings, commands, and aggregate results enter the repository. The run climbs L1 through L4 in order; every effectful lane is mechanically restricted to the test identities declared in `CLAUDE.md`, and every level stops on the first non-pass.

**Tech Stack:** Chrome read-only mailbox/SiteSift inspection, Python/pytest, existing EmailAutomation fixtures and production functions, OpenAI Responses API in dry-run mode, Firebase/Graph/Sheets readbacks, Markdown/JSON evidence artifacts.

**Plan deliverable:** finding. Any reproduced code defect exits into a separate exact TDD fix plan after the root cause and failing test are known.

---

### Task 1: Freeze the safety and candidate baseline

**Files:**
- Read: `AGENTS.md`
- Read: `CLAUDE.md`
- Read: `docs/superpowers/specs/2026-08-02-jill-evidence-transition-clearance-design.md`
- Create locally, never commit: `/Users/baylorharrison/Documents/Codex/2026-07-31/hey/work/sitesift-clearance-20260802/private-evidence-ledger.md`

- [ ] **Step 1: Prove the worktree and candidate identity**

Run:

```bash
git status --short
git rev-parse HEAD
git branch --show-current
```

Expected: clean status; HEAD is the candidate plus this plan/spec documentation; branch is `codex/sitesift-79-response-clearance-20260801`.

- [ ] **Step 2: Prove environment prerequisites without printing secrets**

Run a shell check that reports only `SET` or `UNSET` for `OPENAI_API_KEY`, Azure app/client/tenant variables, Firebase credentials, and Google credentials. Never print values or enumerate the environment.

Expected: OpenAI is available before L3; Graph/Firebase/Google availability is recorded but creates no authorization for effects.

- [ ] **Step 3: Reconfirm browser identities and the production safety state**

Read-only inspect the account controls or mailbox roots for the three exact test identities in `CLAUDE.md`, then inspect SiteSift Operations.

Expected: all identities readable; campaign creation and campaign automation remain closed globally; zero live campaigns before evidence collection. Record counts and timestamp in the private ledger.

- [ ] **Step 4: Establish the effect stop rule**

Write this exact rule at the top of the private ledger:

```text
STOP before any send, reply, draft, forward, support submission, customer campaign action,
unknown-recipient queue insertion, production flag widening, or destructive cleanup.
Only the three test identities in CLAUDE.md may participate in an agent-run L4 effect.
```

### Task 2: Build the recent report corpus

**Files:**
- Create locally, never commit: `/Users/baylorharrison/Documents/Codex/2026-07-31/hey/work/sitesift-clearance-20260802/private-evidence-ledger.md`
- Create: `docs/release-safety/jill-recent-transition-scenarios.json`

- [ ] **Step 1: Enumerate the newest fourteen days of target-user mail**

Use the already authenticated Manifold mailbox with a sender-scoped search. Read results newest-first and record each result's date, sanitized subject token, and classification. Expand only messages that can change the readiness decision.

Expected classifications are exactly:

```text
confirmed_defect
suspected_defect
workflow_confusion
feature_expectation
campaign_context
unrelated
```

- [ ] **Step 2: Trace every product-relevant report**

For each non-unrelated report, record privately:

```text
visible symptom
feature surface
previous state
trigger
observed state
expected state
date/version window
recurrence clue
source locator
```

Expected: no raw customer address, email, message ID, attachment, or verbatim body enters a committed file.

- [ ] **Step 3: Write the sanitized recent-scenario ledger**

Create `docs/release-safety/jill-recent-transition-scenarios.json` with top-level keys `sourceWindow`, `sanitization`, and `scenarios`. Every scenario must use the schema from the design and replace identities, addresses, and message text with synthetic equivalents that preserve only the tested mechanism.

- [ ] **Step 4: Validate de-identification before proceeding**

Run:

```bash
rg -n -i 'mohrpartners|jill\.ames|@[a-z0-9.-]+\.(com|ai|org|net)' docs/release-safety/jill-recent-transition-scenarios.json
python -m json.tool docs/release-safety/jill-recent-transition-scenarios.json >/dev/null
```

Expected: the identity scan returns no matches; JSON validation exits zero.

### Task 3: Add historical recurrence evidence

**Files:**
- Modify: `docs/release-safety/jill-recent-transition-scenarios.json`
- Read: `tests/fixtures/jill_readonly_replay_scenarios.json`
- Read: `tests/test_jill_readonly_replay_fixtures.py`
- Read: `tests/test_jill_live_campaign_regressions.py`
- Read: `tests/test_jill_june_regressions.py`

- [ ] **Step 1: Inspect SiteSift campaign/conversation history read-only**

From Operations, inspect the target user's historical campaign summaries and only the conversations needed to test recurrence. Do not use any reply, compose, retry, recover, stop, dismiss, or state-changing control.

- [ ] **Step 2: Connect old evidence to current mechanisms**

For each historical example, either link it to an existing recent scenario with `recurrence_count += 1` or add a sanitized historical scenario when it represents a distinct transition.

Expected: old behavior never changes a recent scenario from fail/unknown to pass.

- [ ] **Step 3: Reconcile existing regression fixtures**

Map each scenario to the exact existing test node that exercises the same trigger, transition, and observable outcome. A name-only or feature-only resemblance remains uncovered.

- [ ] **Step 4: Re-run the sanitization and JSON checks from Task 2**

Expected: no identity matches; valid JSON.

### Task 4: Produce the canonical transition and ownership matrix

**Files:**
- Create: `docs/release-safety/campaign-transition-ownership-matrix.md`
- Read: `docs/release-safety/system-audit-matrix.json`
- Read: `docs/release-safety/feature-registry.json`
- Read: `docs/release-safety/feature-gradebook.json`
- Read: `email_automation/email.py`
- Read: `email_automation/processing.py`
- Read: `email_automation/pending_responses.py`
- Read: `email_automation/followup.py`

- [ ] **Step 1: Write one row per canonical transition**

The matrix columns are exactly:

```text
transition_id | prior_state | trigger | next_state | owner | idempotency_key |
durable_evidence | UI_projection | retry_rule | forbidden_next_states |
recent_scenarios | historical_scenarios | L1 | L2 | L3 | L4 | status
```

- [ ] **Step 2: Trace each owner in code**

Use `rg` and targeted file reads to identify the exact function and data boundary for each transition. Record file and symbol, not guessed line numbers.

- [ ] **Step 3: Preserve unknowns honestly**

Any missing, stale, partial, or unexecutable proof is `UNKNOWN`. Do not infer a pass from a nearby test, a UI count of zero, or repository state.

- [ ] **Step 4: Review weak spots explicitly**

The matrix must include dedicated rows for upload/header rejection and dropped rows, accepted-fact persistence, terminal-decision citations, property/attachment binding, policy-blocked response visibility, extraction-crash retry, operator override, follow-up resumption, stale frontend/API compatibility, and processing-failure visibility.

### Task 5: Reconcile executable test coverage

**Files:**
- Create: `docs/release-safety/jill-transition-test-coverage.md`
- Modify only when evidence requires a missing sanitized fixture: `tests/fixtures/jill_readonly_replay_scenarios.json`
- Modify only when a current mechanism lacks a regression: `tests/test_jill_readonly_replay_fixtures.py`

- [ ] **Step 1: Collect the exact test inventory**

Run:

```bash
pytest --collect-only -q \
  tests/test_jill_readonly_replay_fixtures.py \
  tests/test_jill_live_campaign_regressions.py \
  tests/test_jill_june_regressions.py \
  tests/test_processing_reply_safety.py \
  tests/test_processing_retryability.py \
  tests/test_pending_responses.py \
  tests/test_followup_terminal_state.py \
  tests/test_outbox_safety.py \
  tests/test_action_audit_backend.py \
  tests/test_full_campaign_e2e.py
```

Expected: collection succeeds and every mapped node exists.

- [ ] **Step 2: Mark semantic coverage, not filename coverage**

For each scenario, record test node, test level, exact asserted outcome, and the remaining unproved boundary. Mark `GAP` when the test does not assert the reported failure mode.

- [ ] **Step 3: Add only sanitized deterministic regressions required by the corpus**

For each gap that can be reproduced without providers, first add one failing fixture/test, run that node to verify the expected failure, and stop the finding plan. Record the root-cause handoff needed for the separate TDD fix plan.

### Task 6: Run L1 and L2 in fail-fast order

**Files:**
- Create: `docs/release-safety/evidence/2026-08-02-jill-transition-l1-l2.md`

- [ ] **Step 1: Run the focused L1 corpus**

Run:

```bash
pytest -x -q \
  tests/test_jill_readonly_replay_fixtures.py \
  tests/test_jill_live_campaign_regressions.py \
  tests/test_jill_june_regressions.py \
  tests/test_processing_reply_safety.py \
  tests/test_processing_retryability.py \
  tests/test_pending_responses.py \
  tests/test_followup_terminal_state.py \
  tests/test_outbox_safety.py \
  tests/test_action_audit_backend.py
```

Expected: zero failures. Stop at the first non-pass.

- [ ] **Step 2: Run L2 integration and transaction coverage**

Run:

```bash
pytest -x -q \
  tests/test_full_campaign_e2e.py \
  tests/test_event_processing_order.py \
  tests/test_sheet_row_anchor_safety.py \
  tests/test_dead_letter_recovery.py \
  tests/test_system_health.py \
  tests/test_scheduler_scope.py \
  tests/test_scheduler_lease.py
```

Expected: zero failures. Stop at the first non-pass.

- [ ] **Step 3: Record reproducible evidence**

Record exact commit, interpreter, commands, selected/deselected/pass/fail counts, duration, and the first failure if any. Do not summarize an incomplete run as pass.

### Task 7: Run isolated L3 attempt 009

**Files:**
- Create locally, never commit raw model output: `/Users/baylorharrison/Documents/Codex/2026-07-31/hey/work/sitesift-clearance-20260802/l3-attempt-009-raw.jsonl`
- Create: `docs/release-safety/evidence/2026-08-02-jill-transition-l3-attempt-009.md`

- [ ] **Step 1: Build the L3 scenario shortlist**

Select no more than 25 sanitized scenarios, ordered by recent confirmed defects, recent suspected defects, recurring terminalization/property-binding risks, then coverage gaps. Record the selection order before the first call.

- [ ] **Step 2: Prove zero-effect execution**

Invoke only `email_automation.ai_processing.propose_sheet_updates(..., conversation=<synthetic>, dry_run=True)` with in-memory rows, synthetic attachments/text, a complete synthetic `column_config`, and no Firestore/Graph/Sheet mutation objects. Assert before each call that every embedded email is a testing identity declared in `CLAUDE.md` or a plus-address alias of one.

- [ ] **Step 3: Execute with a hard call cap and fail-fast behavior**

Run at most 25 OpenAI calls. Stop immediately when a scenario is `FAIL`, the response is unparsable, authentication fails, the wrong model is used, or any non-OpenAI provider boundary is attempted. Response-header organization labels are diagnostic metadata, not a release gate.

Expected: calls made `<= 25`; no email, campaign, customer, Firestore, Sheet, or Graph effects.

- [ ] **Step 4: Record only sanitized aggregate evidence**

The committed artifact records exact candidate, model, scenario IDs, pass/fail, call count, duration, and first non-pass. It excludes raw prompts/responses, headers, tokens, customer content, and credentials.

### Task 8: Run the controlled L4 self-owned flow only after all earlier gates pass

**Files:**
- Read only; do not copy into git: `/Users/baylorharrison/Documents/GitHub/EmailAutomation/test_pdfs/E2E_Real_World_Test.xlsx`
- Read: `tests/multi_turn_live_test.py`
- Create: `docs/release-safety/evidence/2026-08-02-jill-transition-l4.md`

- [ ] **Step 1: Preflight every effect target**

Inspect the standard workbook at the absolute path above and record its SHA-256 before upload, then inspect the launch preview. Assert that every recipient is one of the two test-broker identities in `CLAUDE.md`, the sender is the declared testing Outlook identity, no customer UID/client is selected, and campaign/user scoping cannot widen.

Expected: exact finite recipient set, exact test UID, exact row count, empty queues, and a named rollback/stop path. Any unknown blocks L4.

- [ ] **Step 2: Prove operations controls do not broaden users**

Do not flip global campaign creation or automation. Use only an existing Baylor-only exception or a test-only path whose authorization and scope are visible before launch. If no such path exists, record `L4 BLOCKED — safe admission path unavailable` and do not mutate production flags.

- [ ] **Step 3: Execute one standard self-owned campaign**

Launch at most one campaign from the test account, monitor every state transition, and stop on the first non-pass. All mail must remain among the three testing identities.

- [ ] **Step 4: Reconcile terminal state**

Require provider receipts to reconcile with outbox, action audit, thread/message index, campaign state, and Sheet state. At certification time require zero queued, pending, dead-letter, uncertain, processing-failure, and actionable work, plus released scheduler ownership.

- [ ] **Step 5: Preserve evidence without destructive cleanup**

Record the exact test campaign identifiers and terminal states. Do not delete mail, campaigns, Firestore evidence, or Sheets unless the user later explicitly requests cleanup.

### Task 9: Issue the clearance finding

**Files:**
- Create: `docs/release-safety/evidence/2026-08-02-jill-one-clearance-finding.md`
- Modify: `docs/release-safety/campaign-transition-ownership-matrix.md`
- Modify: `docs/release-safety/jill-transition-test-coverage.md`

- [ ] **Step 1: Decide PASS, FAIL, or BLOCKED from evidence**

`PASS` requires every rubric item in the design. `FAIL` names the first reproducible product non-pass and its exact transition. `BLOCKED` names the unavailable proof lane without substituting lower-level evidence.

- [ ] **Step 2: Separate the release decisions**

State independently:

```text
existing one-campaign clearance
observed test-campaign certification
second target-user campaign
broader-user cohort
formal Release A
```

- [ ] **Step 3: Write the next exact TDD fix plan if needed**

If a product defect is reproduced, stop this finding plan and create a separate plan containing the failing node, traced root cause, one minimal production change, adjacent regressions, and full verification commands. Do not bundle multiple speculative fixes.

- [ ] **Step 4: Verify and commit the evidence package**

Run JSON/Markdown checks, targeted tests referenced by the finding, `git diff --check`, and `git status --short`. Commit only sanitized artifacts and tests. Never commit the private ledger, raw model output, secrets, tokens, mailbox bodies, or customer identifiers.
