# Claim Pipeline Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first no-side-effect foundation for the approved broker claim pipeline: stable contracts, cross-reference validation, mode gating, and versioned boundary fixtures.

**Architecture:** Add an isolated `email_automation.claim_pipeline` package containing only immutable Python contracts and pure validation. It must not import Graph, Firebase, Sheets, OpenAI, messaging, processing, or follow-up modules. A versioned fixture loader supplies deterministic boundary cases for later extractor and policy work without yet interpreting or committing live messages.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `enum`, `hashlib`, `json`, `pathlib`), pytest/unittest-compatible tests, JSON fixtures.

---

## File Structure

- Create `email_automation/claim_pipeline/__init__.py`: public no-effect contract exports.
- Create `email_automation/claim_pipeline/contracts.py`: enums, immutable data contracts, canonical serialization, and stable identifiers.
- Create `email_automation/claim_pipeline/validation.py`: bundle-level provenance, entity, contract, decision, and action-plan invariants.
- Create `email_automation/claim_pipeline/mode.py`: fail-closed `off/replay/shadow/enforce` mode parsing and allowlist decisions.
- Create `email_automation/claim_pipeline/fixtures.py`: strict versioned fixture loading with deterministic manifest hashes.
- Create `tests/fixtures/claim_pipeline_boundary_cases.json`: broad FDR-004 boundary cases, not model outputs.
- Create `tests/test_claim_pipeline_contracts.py`: contract construction and serialization tests.
- Create `tests/test_claim_pipeline_validation.py`: cross-reference and safety invariant tests.
- Create `tests/test_claim_pipeline_mode.py`: fail-closed mode and allowlist tests.
- Create `tests/test_claim_pipeline_fixtures.py`: fixture schema, coverage, and reproducibility tests.

## Task 1: Immutable Contract Vocabulary

**Files:**
- Create: `email_automation/claim_pipeline/__init__.py`
- Create: `email_automation/claim_pipeline/contracts.py`
- Test: `tests/test_claim_pipeline_contracts.py`

- [x] **Step 1: Write failing tests for stable evidence and claim identities**

```python
def test_evidence_identity_is_stable_for_the_same_source():
    first = EvidenceEnvelope.create(
        tenant_id="uid-1",
        message_id="graph-1",
        source_kind=EvidenceSource.FRESH_BODY,
        location="body:0-18",
        content="Suite B is open.",
        direction=Direction.INBOUND,
        actor=Actor(name="Alex", email="alex@example.com", role=ActorRole.BROKER),
        observed_at="2026-07-22T12:00:00Z",
    )
    second = EvidenceEnvelope.create(**same_values)
    assert first.evidence_id == second.evidence_id
```

- [x] **Step 2: Run the tests and verify import failure**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_contracts`
Expected: FAIL because `email_automation.claim_pipeline` does not exist.

- [x] **Step 3: Implement string enums and frozen contracts**

Define `EvidenceSource`, `EvidenceFreshness`, `Direction`, `ActorRole`, `EntityType`, `ClaimPredicate`, `ClaimPolarity`, `ClaimModality`, `MarketState`, `FitState`, `CompletenessState`, `ConversationState`, `ActionType`, `ApprovalClass`, and `EffectStatus` as `str, Enum` classes. Define frozen `Actor`, `EvidenceEnvelope`, `EntityRef`, `Claim`, `CampaignContract`, `DecisionSnapshot`, `PlannedAction`, `ActionPlan`, `EffectReceipt`, and `CommitReceipt` dataclasses.

Every identity-producing `create()` method must use canonical JSON plus SHA-256 and include tenant scope. Every contract must expose `to_dict()` returning JSON-compatible enum values.

- [x] **Step 4: Run contract tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_contracts`
Expected: all contract tests pass.

- [x] **Step 5: Commit the contract vocabulary as part of the combined foundation commit**

```bash
git add email_automation/claim_pipeline tests/test_claim_pipeline_contracts.py
git commit -m "Add claim pipeline contracts"
```

## Task 2: Cross-Reference and Safety Validation

**Files:**
- Create: `email_automation/claim_pipeline/validation.py`
- Test: `tests/test_claim_pipeline_validation.py`

- [x] **Step 1: Write failing tests for unsupported and cross-tenant claims**

```python
def test_claim_bundle_rejects_missing_evidence(self):
    with self.assertRaisesRegex(ContractViolation, "unknown evidence"):
        validate_claim_bundle(
            tenant_id="uid-1",
            evidence=[],
            entities=[target_entity],
            claims=[claim_for_missing_evidence],
        )

def test_action_plan_rejects_automatic_new_recipient(self):
    with self.assertRaisesRegex(ContractViolation, "requires approval"):
        validate_action_plan(plan_with_automatic_recipient_change, decision)
```

- [x] **Step 2: Run the tests and verify missing validator failures**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_validation`
Expected: FAIL because the validation API does not exist.

- [x] **Step 3: Implement pure fail-closed validators**

Implement:

```python
class ContractViolation(ValueError):
    pass

def validate_claim_bundle(*, tenant_id, evidence, entities, claims) -> None: ...
def validate_decision(decision, *, entities, contract) -> None: ...
def validate_action_plan(
    plan,
    decision,
    *,
    scope,
    entities,
    claims,
    authorized_recipients,
) -> None: ...
```

The bundle validator requires tenant equality, unique IDs, existing evidence/entity references, exact non-empty evidence excerpts, and confidence within `[0, 1]`. The decision validator requires exact client, campaign, contract, and entity scope. The action validator requires an authoritative thread/Sheet/row scope, real target-matching source claims, action-specific payload schemas, semantic claim-to-field support, stable effect identity, expected prior state, dependency order, and human approval for recipient changes, alternate-property proposals, tours, calls, and LOIs.

- [x] **Step 4: Run validator tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_validation`
Expected: all validation tests pass.

- [x] **Step 5: Commit validation as part of the combined foundation commit**

```bash
git add email_automation/claim_pipeline/validation.py tests/test_claim_pipeline_validation.py
git commit -m "Validate claim pipeline provenance and actions"
```

## Task 3: Fail-Closed Runtime Mode

**Files:**
- Create: `email_automation/claim_pipeline/mode.py`
- Test: `tests/test_claim_pipeline_mode.py`

- [x] **Step 1: Write failing mode tests**

```python
def test_unknown_mode_falls_back_to_off():
    assert parse_pipeline_mode("surprise") is ClaimPipelineMode.OFF

def test_enforce_requires_explicit_tenant_and_campaign_allowlist():
    gate = PipelineGate(mode=ClaimPipelineMode.ENFORCE, allowed_tenants=("uid-1",))
    assert not gate.allows_enforcement("uid-1", "campaign-1")
```

- [x] **Step 2: Verify the tests fail because mode API is absent**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_mode`
Expected: FAIL on import.

- [x] **Step 3: Implement immutable mode gate**

Define `ClaimPipelineMode` with `off`, `replay`, `shadow`, and `enforce`. Unknown or blank values parse to `off`. `PipelineGate.allows_replay`, `allows_shadow`, and `allows_enforcement` require exact tenant and campaign membership; enforcement is never implied by shadow permission.

- [x] **Step 4: Run mode tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_mode`
Expected: all mode tests pass.

- [x] **Step 5: Commit mode gating as part of the combined foundation commit**

```bash
git add email_automation/claim_pipeline/mode.py tests/test_claim_pipeline_mode.py
git commit -m "Add fail-closed claim pipeline modes"
```

## Task 4: Versioned Case-Lattice Fixtures

**Files:**
- Create: `email_automation/claim_pipeline/fixtures.py`
- Create: `tests/fixtures/claim_pipeline_boundary_cases.json`
- Test: `tests/test_claim_pipeline_fixtures.py`

- [x] **Step 1: Write failing fixture tests**

```python
def test_boundary_catalog_covers_each_governing_dimension():
    catalog = load_fixture_catalog(FIXTURE_PATH)
    assert REQUIRED_DIMENSIONS <= catalog.covered_dimensions
    assert len(catalog.cases) >= 12

def test_manifest_hash_is_reproducible():
    assert load_fixture_catalog(FIXTURE_PATH).manifest_hash == load_fixture_catalog(FIXTURE_PATH).manifest_hash
```

- [x] **Step 2: Run the tests and verify fixture API failure**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_fixtures`
Expected: FAIL because the loader/catalog does not exist.

- [x] **Step 3: Add the strict fixture loader**

The root JSON object must contain `schemaVersion`, `catalogId`, and `cases`. Every case must contain `caseId`, `dimensions`, `contract`, `evidence`, and `expected`. Duplicate case IDs, unknown root keys, missing required dimensions, or invalid shapes raise `FixtureValidationError`. The manifest hash is SHA-256 over canonical parsed JSON.

- [x] **Step 4: Add at least 12 broad boundary cases**

Include explicit cases for late availability, short term, definite funded remediation, tentative remediation, under-contract backups, split-suite state, OOO return date, OOO backup contact, stop-emailing-plus-call, alternate-property attachment binding, correction supersession, and later user requirement revision. Expected values describe claims/decisions/review class, not exact model prose.

- [x] **Step 5: Run fixture tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_fixtures`
Expected: all fixture tests pass.

- [x] **Step 6: Commit fixture foundation as part of the combined foundation commit**

```bash
git add email_automation/claim_pipeline/fixtures.py tests/fixtures/claim_pipeline_boundary_cases.json tests/test_claim_pipeline_fixtures.py
git commit -m "Add claim pipeline boundary catalog"
```

## Task 5: Foundation Isolation and Broad Verification

**Files:**
- Modify: `tests/test_claim_pipeline_contracts.py`
- Modify: `tests/test_claim_pipeline_validation.py`
- Modify: `tests/test_claim_pipeline_mode.py`
- Modify: `tests/test_claim_pipeline_fixtures.py`

- [x] **Step 1: Add an import-isolation test**

Import `email_automation.claim_pipeline`, then assert that package modules do not import `firebase_admin`, `google.cloud.firestore`, `openai`, `requests`, `email_automation.processing`, `email_automation.messaging`, `email_automation.sheets`, or `email_automation.followup`.

- [x] **Step 2: Run the focused foundation suite**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_contracts tests.test_claim_pipeline_validation tests.test_claim_pipeline_mode tests.test_claim_pipeline_fixtures tests.test_claim_pipeline_isolation`
Expected: all tests pass without network access or credentials.

- [x] **Step 3: Run changed-module compilation and repository integrity**

Run: `.venv/bin/python -m compileall -q email_automation/claim_pipeline`
Expected: exit 0.

Run: `git diff --check`
Expected: exit 0.

- [x] **Step 4: Run the full backend suite**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
Expected: existing suite plus new foundation tests pass.

- [x] **Step 5: Review blast radius**

Confirm the diff contains no imports or calls to Graph, Firebase, Sheets, OpenAI, outbox, processing, follow-up, scheduler, or notification modules. Confirm no production mode reads the new package yet.

- [x] **Step 6: Commit verification adjustments**

```bash
git add email_automation/claim_pipeline tests docs/superpowers/plans/2026-07-22-claim-pipeline-foundation.md
git commit -m "Complete no-effect claim pipeline foundation"
```

## Completion Gate

This slice is complete only when the contracts and fixture catalog are deterministic, every cross-reference fails closed, the runtime mode defaults to `off`, approval-gated actions cannot be marked automatic, the package has no production-service imports, and the full backend suite remains green. It does not authorize extractor integration, Firestore persistence, shadow execution, Sheet mutation, or email sending.
