# Deterministic Policy and Action Evaluator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert validated broker claims into deterministic per-entity decisions and validated no-effect action plans across an executable safety lattice.

**Architecture:** Add one strict fixture loader and one pure policy module beneath `email_automation.claim_pipeline`. The evaluator reduces active claims, computes independent market/fit/completeness/conversation states, and builds stable action plans using the existing immutable contracts and validators. Nothing imports or calls production services.

**Tech Stack:** Python 3.12, frozen dataclasses, standard-library JSON/hash/date handling, `unittest`, existing claim-pipeline contracts and validators.

---

## File Structure

- Create `email_automation/claim_pipeline/policy_fixtures.py`: strict schema and loader for executable policy cases.
- Create `email_automation/claim_pipeline/policy.py`: request/result contracts, claim reduction, state evaluation, action planning, and stable digest.
- Create `tests/fixtures/claim_pipeline_policy_cases.json`: broad positive/opposed policy lattice.
- Create `tests/test_claim_pipeline_policy_fixtures.py`: fixture schema and coverage tests.
- Create `tests/test_claim_pipeline_policy.py`: decision, action, order-independence, and no-effect tests.
- Modify `email_automation/claim_pipeline/validation.py`: permit decision-supported terminal freezes while retaining scope and approval gates.
- Modify `tests/test_claim_pipeline_validation.py`: opposed validation tests for legitimate and illegitimate freezes.
- Modify `email_automation/claim_pipeline/__init__.py` and isolation tests: expose the pure API without service imports.

## Task 1: Strict Executable Policy Fixtures

**Files:**
- Create: `email_automation/claim_pipeline/policy_fixtures.py`
- Create: `tests/fixtures/claim_pipeline_policy_cases.json`
- Create: `tests/test_claim_pipeline_policy_fixtures.py`

- [ ] **Step 1: Write failing loader tests**

Require at least 19 cases, every governing dimension, a reproducible manifest hash, exact keys, known enums/reason codes, valid entity and claim references, required and forbidden action signatures, and `effectPolicy=no_side_effect`.

```python
catalog = load_policy_fixture_catalog(FIXTURE_PATH)
self.assertGreaterEqual(len(catalog.cases), 19)
self.assertTrue(REQUIRED_POLICY_DIMENSIONS <= catalog.covered_dimensions)
self.assertEqual(
    catalog.manifest_hash,
    load_policy_fixture_catalog(FIXTURE_PATH).manifest_hash,
)
```

Mutation tests must reject duplicate case IDs, unknown keys, missing dimensions, unknown reason codes, malformed current state, and any side-effect policy.

- [ ] **Step 2: Run tests and observe import failure**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy_fixtures`

Expected: FAIL because `policy_fixtures` does not exist.

- [ ] **Step 3: Implement the strict loader**

Define schema version 1, required dimensions, frozen `PolicyFixtureCase` and `PolicyFixtureCatalog`, exact-key validation, cross-reference validation, enum validation, and a canonical SHA-256 manifest.

- [ ] **Step 4: Add the broad lattice**

Cover available/missing facts, explicit unavailable, tour-only unavailable near miss, hard versus soft occupancy, hard term versus absent minimum, definite versus tentative remediation, accepting backups, split suites, alternate-property isolation, correction supersession, complete facts, OOO return, redirect approval, opt-out plus call, conflicting claims, unknown hard requirements, and order permutations.

Every expected entity result declares exact four-axis states, approval class, sorted reasons, missing fields, required actions, and forbidden actions.

- [ ] **Step 5: Run green and commit**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy_fixtures`

Commit: `git commit -m "Add executable policy safety lattice"`

## Task 2: Immutable Request and Claim Reduction

**Files:**
- Create: `email_automation/claim_pipeline/policy.py`
- Create: `tests/test_claim_pipeline_policy.py`

- [ ] **Step 1: Write failing scope and reduction tests**

Use this public shape:

```python
request = PolicyEvaluationRequest.create(
    contract=contract,
    scope=scope,
    entities=entities,
    claims=claims,
    snapshot_hash="snapshot-1",
    current_facts={entity.entity_id: {"rent": 12.0}},
    current_conversation_states={entity.entity_id: "active"},
    current_followup_states={entity.entity_id: "waiting"},
    authorized_recipients=("broker@example.test",),
)
```

Prove mismatched tenant/campaign scope, duplicate IDs, and bad cross-entity correction links fail. Prove exact correction supersession, agreeing claims, conflict preservation, and reversed input order are deterministic.

- [ ] **Step 2: Run tests and observe import failure**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy.PolicyReductionTests`

Expected: FAIL because the API is absent.

- [ ] **Step 3: Implement immutable support**

Add frozen `PolicyEvaluationRequest`, `ClaimConflict`, `EntityPolicyResult`, and `PolicyEvaluationResult`. Validate the claim bundle and all scope. Reduce corrections append-only, group active claims by entity/predicate, and preserve disagreements as conflicts rather than choosing a winner.

- [ ] **Step 4: Run green and commit**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy.PolicyReductionTests`

Commit: `git commit -m "Reduce policy claims deterministically"`

## Task 3: Four-Axis Decision Evaluation

**Files:**
- Modify: `email_automation/claim_pipeline/policy.py`
- Modify: `tests/test_claim_pipeline_policy.py`

- [ ] **Step 1: Write failing fixture-driven decision tests**

Build requests from every fixture and compare exact market, fit, completeness, conversation, approval class, reason codes, and missing fields. Add opposed checks proving tour-only wording cannot imply unavailable, soft preferences cannot create hard non-fit, one unavailable suite cannot terminalize its sibling, alternates cannot affect the target, and conflicts/unknown hard requirements create review.

- [ ] **Step 2: Observe semantic failures**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy.PolicyDecisionTests`

Expected: FAIL because evaluation is absent.

- [ ] **Step 3: Implement named pure evaluators**

Implement `_market_decision`, `_fit_decision`, `_completeness_decision`, `_conversation_decision`, and `_decision_approval_class` using the design precedence. Support hard `occupancy_by`, `minimum_term_months`, required fields, and named Base V1 policies. Unknown hard requirements must review, never allow. Create and validate one `DecisionSnapshot` per entity with sorted reason/evidence/missing-field tuples.

- [ ] **Step 4: Run green and commit**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy.PolicyDecisionTests`

Commit: `git commit -m "Evaluate campaign policy from accepted claims"`

## Task 4: Validated No-Effect Action Planning

**Files:**
- Modify: `email_automation/claim_pipeline/policy.py`
- Modify: `email_automation/claim_pipeline/validation.py`
- Modify: `tests/test_claim_pipeline_policy.py`
- Modify: `tests/test_claim_pipeline_validation.py`

- [ ] **Step 1: Write failing planner and validator tests**

For each fixture compare required and forbidden `(action_type, target, approval_class)` signatures. Prove every terminal-intent plan freezes follow-ups, no active/review plan auto-terminalizes, alternate facts cannot update the target row, recipients/calls remain human-required, every review has structured details, sequences/dependencies are valid, and all generated plans pass `validate_action_plan`.

Add opposed validator tests: an automatic redirect still fails; a freeze without a terminal/opt-out decision still fails.

- [ ] **Step 2: Observe failures**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy.PolicyActionTests tests.test_claim_pipeline_validation`

Expected: FAIL because plans are absent and validation lacks decision-supported freeze semantics.

- [ ] **Step 3: Implement deterministic planning**

Generate supported fact updates with exact current values as expected prior state; freeze every terminal-intent decision; transition changed conversation states; keep alternates, recipients, tours, and calls human-owned; and create one structured review item for every review decision. Use fixed action ordering and earlier-only dependencies. Generate no outbound drafts.

- [ ] **Step 4: Narrowly widen freeze validation**

Allow `FOLLOWUP_FREEZE` for opt-out or a bound `TERMINAL_INTENT` decision supported by same-entity availability, occupancy, term, or complete required-fact claims. Require the payload reason to match a terminal decision reason. Preserve all existing tenant, campaign, row, prior-state, target, and approval checks.

- [ ] **Step 5: Run green and commit**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy tests.test_claim_pipeline_validation`

Commit: `git commit -m "Plan validated no-effect campaign actions"`

## Task 5: Repeatability, Isolation, and Broad Verification

**Files:**
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`
- Modify: `tests/test_claim_pipeline_policy.py`
- Modify: this plan

- [ ] **Step 1: Write failing repeatability and import tests**

Evaluate the catalog three times with normal and reversed entity/claim order. Require identical decision dictionaries, plans, action IDs, idempotency keys, and result digests. Importing policy modules must not load Firebase, Firestore, OpenAI, requests, processing, messaging, Sheets, follow-up, email, or notifications.

- [ ] **Step 2: Observe export/isolation failures**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_policy tests.test_claim_pipeline_isolation`

- [ ] **Step 3: Export the API and finalize the digest**

Export policy request/result/fixture types and `evaluate_policy`. Hash canonical ordered decision and plan dictionaries only; exclude timing and diagnostics.

- [ ] **Step 4: Run the focused suite**

```bash
.venv/bin/python -m unittest   tests.test_claim_pipeline_policy_fixtures   tests.test_claim_pipeline_policy   tests.test_claim_pipeline_contracts   tests.test_claim_pipeline_validation   tests.test_claim_pipeline_claim_fixtures   tests.test_claim_pipeline_replay   tests.test_claim_pipeline_isolation
```

Expected: all focused tests pass.

- [ ] **Step 5: Run compilation, integrity, and full suite**

```bash
.venv/bin/python -m compileall -q email_automation/claim_pipeline
git diff --check
.venv/bin/python -m unittest discover -s tests -b
```

Expected: exit 0 and the full backend suite passes.

- [ ] **Step 6: Record exact evidence and commit**

Update this plan with exact counts and any justified oracle correction. Do not weaken expectations to fit implementation.

Commit: `git commit -m "Prove deterministic campaign policy planning"`

## Completion Gate

This phase is complete only when the lattice is strict and broad, every decision dimension is independent, terminal intent always freezes follow-ups, alternate entities cannot mutate the target row, sensitive actions stay human-owned, ambiguity creates actionable reasons, repeated evaluation is stable, package isolation remains intact, and the full suite passes. It does not authorize persistence, shadow integration, drafting, sending, Sheet writes, deployment, or production use.

