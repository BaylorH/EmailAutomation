# Workflow Provider Reliability Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:test-driven-development and execute the tasks in order.

> **Deliverable:** both code and findings. Add and prove a fixed no-effect
> reliability mode; stop before provider execution.

**Goal:** Create a low-call, fail-fast provider gate focused on workflow-intent
reliability without changing the existing smoke/final modes or spending the
new budget.

**Architecture:** Extend the existing capped CLI with an immutable mode
configuration that selects three current fixture cases, four repeats, and
mode-specific call/token/spend ceilings. Reuse the existing budget transport,
provider-quality oracle, deterministic policy grader, and privacy-safe report.

**Tech stack:** Python, `argparse`, immutable fixture catalogs, `unittest`, and
the existing pinned OpenAI transport.

---

### Task 1: Lock the fixed mode contract

**Files:**
- Modify: `tests/test_claim_pipeline_provider_policy_shadow_report.py`
- Modify: `scripts/run_claim_pipeline_provider_policy_shadow.py`

- [ ] Write a failing test that invokes recorded `workflow-reliability` mode
  and requires exactly 12 results, four repeats, and the three fixed case IDs.
- [ ] Write a failing test for exact mode-specific identity ceilings of 12
  calls, 400,000 reserved tokens, and 2,500,000 micro-USD.
- [ ] Run the focused tests and confirm the parser rejects the new mode.
- [ ] Add a closed mode configuration and derive selection, repeats, and caps
  from it.
- [ ] Keep smoke at 1 x 1 and final at 8 x 3 with their existing ceilings.
- [ ] Rerun focused tests and commit.

### Task 2: Prove pre-call safety and privacy

**Files:**
- Modify: `tests/test_claim_pipeline_provider_policy_shadow_report.py`

- [ ] Add assertions that the new schedule is selected before transport
  construction and that an unexpected planned-call count fails closed.
- [ ] Require OpenAI reliability mode to retain explicit opt-in, clean-tree,
  API-key, telemetry, and exact budget matching.
- [ ] Require the recorded report to contain no fixture values, evidence, raw
  output, addresses, emails, or recipients.
- [ ] Run report, provider-shadow, and isolation tests and commit any necessary
  test/code corrections.

### Task 3: Recorded reliability evidence

**Files:**
- Create: `docs/release-safety/workflow-provider-reliability-build-evidence-2026-07-22.md`

- [ ] Run the fixed recorded mode from a clean committed tree.
- [ ] Require 12/12 pass, zero gaps, zero semantic variance, zero provider
  calls/tokens/cost, and a privacy-safe report hash.
- [ ] Run compilation, claim-pipeline tests, and the full backend suite.
- [ ] Record exact revision, fixture/source/report hashes, test counts, and the
  no-provider/no-effects boundary.
- [ ] Commit evidence and update the canonical Active Experiment/backlog via
  the brain write helper.

### Task 4: Stop at the budget decision

- [ ] Do not use `--provider openai` or `--allow-provider-calls`.
- [ ] Present the exact proposed authorization: at most 12 calls, 400,000
  conservatively reserved tokens, and $2.50 worst-case reserved spend, with
  zero retries and fail-fast behavior.
- [ ] If approved later, run only from the committed clean evidence revision
  and stop immediately on the first mismatch.
