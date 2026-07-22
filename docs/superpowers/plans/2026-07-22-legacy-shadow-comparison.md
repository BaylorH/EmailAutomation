# No-Effect Legacy Shadow Comparison Implementation Plan

> **Deliverable:** both code and findings. Build a pure comparison harness and
> produce deterministic evidence about where legacy attempted behavior agrees
> with, lags, or conflicts with the new policy planner.

**Goal:** Turn sanitized legacy proposal projections into entity-scoped action
attempts, compare them with deterministic policy decisions, and fail closed on
unclassified or safety-critical discrepancies.

**Architecture:** Add a strict fixture catalog, a pure adapter/grader, and a
read-only report script inside the isolated claim-pipeline surface. Reuse the
existing policy fixture catalog as the oracle. Never import or execute the live
worker path.

**Tech stack:** Python dataclasses/enums, JSON fixtures, `unittest`, existing
claim-pipeline contracts and policy evaluator.

---

### Task 1: Strict shadow fixture contract

**Files:**
- Create: `email_automation/claim_pipeline/legacy_shadow_fixtures.py`
- Create: `tests/fixtures/claim_pipeline_legacy_shadow_cases.json`
- Create: `tests/test_claim_pipeline_legacy_shadow_fixtures.py`

1. Write failing tests for exact root/case/provenance/binding/proposal/expected
   keys, supported enums, unique IDs, report-safe source references, controlled
   update/event values, and policy-case cross-references.
2. Prove the loader rejects response bodies, email values, raw addresses,
   missing event bindings, unknown policy cases, and synthetic cases mislabeled
   as historical.
3. Implement immutable fixture contracts and a canonical manifest hash.
4. Add a compact corpus spanning historical probes, legacy test contracts, and
   synthetic opposed boundaries.
5. Run fixture tests and commit.

### Task 2: Pure legacy proposal projection

**Files:**
- Create: `email_automation/claim_pipeline/legacy_shadow.py`
- Create: `tests/test_claim_pipeline_legacy_shadow.py`

1. Write failing tests for deterministic projection of every supported legacy
   event and update.
2. Assert that updates always bind to the declared current entity and events to
   their explicit event entity.
3. Assert that a present response becomes an automatic outbound draft only
   when `skip_response` is false.
4. Implement immutable, value-free `LegacyActionAttempt` contracts and the
   projection adapter.
5. Run focused projection tests and commit.

### Task 3: Discrepancy grading

**Files:**
- Modify: `email_automation/claim_pipeline/legacy_shadow.py`
- Modify: `tests/test_claim_pipeline_legacy_shadow.py`

1. Write failing opposed tests for wrong-row mutation, terminalization of an
   active row, fit/market conflation, missing terminal freeze, review bypass,
   unapproved recipient change, unsafe outbound during opt-out/review, and a
   policy obligation absent from legacy.
2. Write controls for aligned fact updates, aligned terminalization, safe
   closeout deferral, and out-of-office expected improvement.
3. Implement an exhaustive grader. Unknown combinations become
   `unclassified_difference` release blockers.
4. Compare exact expected case classifications from the strict fixture.
5. Run focused grading tests and commit.

### Task 4: Deterministic report and isolation

**Files:**
- Create: `scripts/run_claim_pipeline_legacy_shadow.py`
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`
- Create: `tests/test_claim_pipeline_legacy_shadow_report.py`

1. Write failing tests for three-repeat stability, reversed-order stability,
   exact discrepancy counts, dirty-tree identity, and privacy-safe serialization.
2. Implement report identity and digest contracts with code revision, source
   tree hash, both fixture hashes, repeat count, and case count.
3. Expose only pure APIs at the package boundary and prove the isolation test
   still rejects worker/service imports.
4. Run the script three times, scan its JSON for fixture-only values, and save
   the evidence summary without customer content.
5. Commit.

### Task 5: Verification and findings

**Files:**
- Modify: project evidence/backlog records only after verification.

1. Run fixture, projection, grading, report, policy, and isolation tests.
2. Run compile checks and the full backend suite.
3. Check the final diff, branch status, generated report digest, and absence of
   production imports or effects.
4. Record which discrepancies are blockers, expected improvements, and deferred
   surfaces; do not overstate synthetic evidence.
5. Update the Active Experiment and project backlog through the canonical brain
   write helper, then run the brain audit and commit only intended brain files.

### Task 6: Handoff to the next proof gate

1. If the no-effect report passes with no unaccepted blocker, preserve the
   branch without deploy/push/merge.
2. Define the bounded provider-backed shadow as the next experiment, including
   call cap, spend cap, repeat count, and stop conditions.
3. Keep the later effect adapter provisional until fresh provider outputs agree
   with the deterministic policy boundary.
