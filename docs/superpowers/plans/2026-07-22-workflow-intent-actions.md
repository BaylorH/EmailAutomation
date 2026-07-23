# Workflow-Intent Action Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:test-driven-development and execute the tasks in order.

> **Deliverable:** both code and findings. Close the deterministic tour and
> information-request policy gaps, then record exact no-effect evidence.

**Goal:** Preserve broker tour and information requests as explicit,
human-required actions without weakening terminal suppression or creating
outbound effects.

**Architecture:** Extend the action enum and validator with one bounded
information-request action, then have the deterministic policy planner emit it
and the existing tour action from matching effective claims. Update exact
fixture oracles and run the recorded provider-to-policy composition gate.

**Tech stack:** Python enums/dataclasses, immutable policy contracts, JSON
fixtures, `unittest`, and deterministic shadow reports.

---

### Task 1: Lock the action contracts

**Files:**
- Modify: `tests/test_claim_pipeline_validation.py`
- Modify: `email_automation/claim_pipeline/contracts.py`
- Modify: `email_automation/claim_pipeline/validation.py`

- [ ] Add `INFORMATION_REQUEST` to test helpers and write tests proving tour
  and information actions reject automatic approval and unrelated source
  predicates.
- [ ] Run the focused validation tests and confirm the new information action
  fails because its enum/validation contract is absent.
- [ ] Add the enum value, approval gate, `notes` payload allowlist, and matching
  source predicate.
- [ ] Rerun validation tests and require a clean pass.
- [ ] Commit the contract change.

### Task 2: Plan human-owned workflow actions

**Files:**
- Modify: `tests/test_claim_pipeline_policy.py`
- Modify: `email_automation/claim_pipeline/policy.py`

- [ ] Write tests for pure tour, pure information, repeated information, and
  terminal mixed-intent behavior.
- [ ] Run the policy tests and confirm they fail on missing actions, approval,
  state, and reason codes.
- [ ] Mark tour/information claims human-required, add their reason codes, and
  emit one typed action per effective predicate.
- [ ] Preserve terminal-first conversation state and follow-up freeze while
  retaining the typed human-owned actions.
- [ ] Rerun policy tests and require a clean pass.
- [ ] Commit the policy behavior.

### Task 3: Close exact recorded-shadow gaps

**Files:**
- Modify: `tests/fixtures/claim_pipeline_provider_policy_cases.json`
- Modify: `email_automation/claim_pipeline/provider_policy_shadow.py`
- Modify: `tests/test_claim_pipeline_provider_policy_shadow.py`

- [ ] Update the repeated-information and mixed-workflow exact oracles to
  require typed human-owned actions and no named gaps.
- [ ] Update gap detection so a present information-request action closes the
  gap instead of reporting it unconditionally.
- [ ] Run the recorded three-repeat shadow and require 24/24 exact passes, zero
  gap codes, zero variance, zero provider calls, and zero effects.
- [ ] Commit the shadow closure.

### Task 4: Regression and evidence gate

**Files:**
- Create: `docs/release-safety/workflow-intent-actions-evidence-2026-07-22.md`

- [ ] Run focused claim-pipeline tests, isolation checks, compilation, and the
  complete backend suite.
- [ ] Review the diff for unexpected services, persistence, sends, recipients,
  or production imports.
- [ ] Record exact test counts, hashes, action semantics, and remaining scope
  boundaries in the evidence document.
- [ ] Commit the evidence, update the canonical Active Experiment/backlog via
  the brain write helper, run the brain audit, and commit only intended vault
  files.
- [ ] Stop before provider calls or effect-adapter work; both need fresh,
  separately bounded gates.
