# Sanitized Incident Lattice Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the isolated claim replay fail closed unless every supported predicate and every required campaign-incident input has a semantically proven sanitized fixture.

**Architecture:** Upgrade the claim fixture schema with a coverage contract, per-case outcome proofs, and prior-claim context. Keep coverage validation inside the isolated claim package, and validate message-dependent incident labels at the replay boundary. Expand a few dense sanitized messages rather than duplicating one case per field; preserve exact expected claims and issue bindings.

**Tech Stack:** Python 3.12, immutable dataclasses, JSON fixtures, `unittest`, existing evidence/entity/claim/replay modules.

**Deliverable:** both code and findings.

**Proof scope:** Every supported predicate has accepted and rejected coverage. Availability and identity additionally prove ambiguity and wrong-entity handling, while rent proves correction and exact supersession. Incident labels prove sanitized input recognition and evidence binding only; provider quality, policy decisions, downstream effects, and actual follow-up suppression remain later gates.

---

### Task 1: Enforce Predicate And Incident Coverage

**Files:**
- Modify: `email_automation/claim_pipeline/claim_fixtures.py`
- Modify: `tests/test_claim_pipeline_claim_fixtures.py`
- Modify: `tests/fixtures/claim_pipeline_claim_cases.json`

- [x] Write failing tests for missing, dishonest, and incomplete coverage declarations.
- [x] Upgrade the fixture schema and require every `ClaimPredicate` to have accepted and rejected behavior, with targeted ambiguity, wrong-entity, and correction outcomes.
- [x] Require the campaign incident inputs named by the Active Experiment.
- [x] Prove declarations from exact accepted indexes and candidate-bound issue outcomes.

### Task 2: Add Multi-Turn Replay Context

**Files:**
- Modify: `email_automation/claim_pipeline/replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`
- Modify: `tests/test_claim_pipeline_claim_fixtures.py`

- [x] Add prior claims with explicit authority and chronology to fixture cases.
- [x] Resolve symbolic fixture supersession references into deterministic runtime claim IDs.
- [x] Include prior claims in extraction request identity and claim validation.
- [x] Require repeated-question and correction labels to have real prior/current claim shapes.

### Task 3: Expand Sanitized Incident Inputs

**Files:**
- Modify: `tests/fixtures/claim_pipeline_interpretation_cases.json`
- Modify: `tests/fixtures/claim_pipeline_claim_cases.json`
- Modify: `email_automation/claim_pipeline/entities.py`
- Modify: `tests/test_claim_pipeline_entities.py`

- [x] Add complete property-fact, workflow/referral, correction, repeated-request, and terminal-stop messages.
- [x] Add accepted and rejected cases for all 22 supported predicates.
- [x] Add attachment, link, alternate-property, split-suite, redirect, opt-out, call, tour, requirements-mismatch, correction, terminal, and continued-follow-up hazard inputs.
- [x] Fix the discovered `2 drive-ins` false alternate-property resolution with a failing regression first.

### Task 4: Verify And Record

**Files:**
- Modify: `docs/superpowers/plans/2026-07-22-incident-lattice-expansion.md`
- Update through helper: `clients/mohr/projects/email-automation/email-automation.md`
- Modify: `/Users/baylorharrison/Documents/GitHub.nosync/brain/projects/email-automation/project.md`
- Modify: `/Users/baylorharrison/Documents/GitHub.nosync/brain/projects/email-automation/backlog/backlog.md`

- [x] Run fixture, replay, isolation, compilation, JSON, diff, and full backend verification.
- [x] Run adversarial review for decorative coverage, common-mode oracles, prior-claim ordering, privacy leakage, and scope overstatement.
- [ ] Commit only the isolated backend checkpoint; do not push, deploy, or touch production.
- [ ] Record exact evidence and set the pinned-provider adapter as the next gate.
