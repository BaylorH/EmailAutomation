# Bounded Provider-to-Policy Shadow Implementation Plan

> **Deliverable:** both code and findings. Build a no-effect provider-to-policy
> composition harness and produce bounded, repeatable evidence without touching
> production.

**Goal:** Validate fresh provider-extracted claims against the existing claim
oracle and then against exact deterministic policy outcomes for eight sanitized
campaign boundaries.

**Architecture:** Add a strict cross-catalog fixture, a pure composition runner,
and a capped CLI. Reuse the pinned provider adapter, independent transport
telemetry, claim validator, and policy evaluator. Keep the report value-free.

**Tech stack:** Python dataclasses/enums, JSON fixtures, `unittest`, existing
claim-pipeline contracts, OpenAI Responses transport, and deterministic hashes.

---

### Task 1: Strict cross-catalog fixture

**Files:**
- Create: `email_automation/claim_pipeline/provider_policy_fixtures.py`
- Create: `tests/fixtures/claim_pipeline_provider_policy_cases.json`
- Create: `tests/test_claim_pipeline_provider_policy_fixtures.py`

1. Write failing tests for exact keys, safe IDs, unique provider references,
   strict entity selectors, controlled contract/current-state fields, exact
   decision/action enums, and named gap codes.
2. Prove the loader rejects raw messages, evidence, addresses, emails, recipient
   values, claim values, unknown provider cases, and duplicate selectors.
3. Implement immutable fixture contracts and canonical manifest hashing.
4. Add the eight-case bounded corpus and run fixture tests.
5. Commit.

### Task 2: Recorded composition runner

**Files:**
- Create: `email_automation/claim_pipeline/provider_policy_shadow.py`
- Create: `tests/test_claim_pipeline_provider_policy_shadow.py`

1. Write failing tests for normalize -> resolve -> propose -> validate -> policy
   composition using a recorded complete-proposal adapter.
2. Assert exact provider-quality grading before policy evaluation.
3. Assert prior claims accompany correction claims, entity selection is exact,
   and unselected contacts or historical alternates do not enter policy.
4. Assert exact decisions, required/forbidden action signatures, named gaps,
   repeat digests, reversed-order stability, and zero effects.
5. Implement the smallest pure runner that passes those tests and commit.

### Task 3: Provider budgets and privacy-safe report

**Files:**
- Modify: `email_automation/claim_pipeline/provider_policy_shadow.py`
- Create: `scripts/run_claim_pipeline_provider_policy_shadow.py`
- Create: `tests/test_claim_pipeline_provider_policy_shadow_report.py`
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`

1. Write failing tests for explicit provider opt-in, smoke/final modes, exact
   1/24/25 call bounds, zero retries, and pre-call token/spend reservations.
2. Assert provider reports reconcile observed telemetry and reject incomplete or
   mismatched usage.
3. Assert serialization contains no evidence text, addresses, emails,
   recipients, claim values, or raw output.
4. Implement source/runtime identity, hard budget enforcement, safe result
   digests, and dirty-tree gating.
5. Run recorded reports, isolation tests, and commit.

### Task 4: One-call provider smoke

1. Run focused tests and compilation from a committed clean tree.
2. Invoke exactly one pinned-provider call for
   `target-unavailable-stop-followups` with explicit opt-in.
3. Verify claim quality, terminal decision, follow-up freeze, no unsafe outbound
   action, complete telemetry, and report privacy.
4. Stop on any mismatch; otherwise record the smoke identity and usage.

### Task 5: Three-repeat final provider gate

1. Run eight cases for three repeats, capped at 24 calls with no retry.
2. Require exact policy outcome stability and allow only nonsemantic proposal
   wording variance.
3. Record named expected gaps separately from blockers.
4. Run the focused suite, full backend suite, compile checks, diff review, and
   branch cleanliness checks.
5. Commit only code and stable findings; do not commit local provider reports if
   they contain runtime-specific evidence not meant for source control.

### Task 6: Durable findings and downstream gate

1. Update the canonical Active Experiment and production-readiness backlog via
   the brain write helper with exact calls, tokens, cost, digests, gaps, and test
   counts.
2. Run the brain audit and commit only intended brain files.
3. Preserve the application branch without push, merge, deploy, or production
   interaction.
4. If no blocker remains, make the disabled effect adapter the next experiment;
   otherwise prioritize the observed extraction or policy gap before effects.
