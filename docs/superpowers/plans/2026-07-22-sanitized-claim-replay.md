# Sanitized Claim Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, no-effect replay runner that executes the currently versioned 14-case interpretation and 20-case claim boundary catalogs repeatedly and emits an exact, privacy-safe identity, correctness, variance, usage, latency, and cost report.

**Architecture:** Keep replay orchestration inside the isolated `claim_pipeline` package and keep environment discovery plus optional local JSON output in a script. Reuse the existing strict interpretation and claim catalogs; a recorded adapter materializes their model proposals at the same one-call-per-case boundary a later pinned OpenAI adapter will implement. Reports expose only counts, issue codes, hashes, and case IDs, never message bodies, addresses, email identities, signatures, or claim values. This checkpoint proves the runner and current boundary corpus, not the still-missing multi-turn Jill/Baylor incident lattice.

**Tech Stack:** Python 3.12-compatible standard library, immutable dataclasses, existing claim-pipeline fixtures and validators, `unittest`.

**Deliverable:** both code and findings.

---

### Task 1: Freeze Replay Contracts and Bounds

**Files:**
- Create: `email_automation/claim_pipeline/replay.py`
- Modify: `email_automation/claim_pipeline/__init__.py`
- Create: `tests/test_claim_pipeline_replay.py`

- [x] **Step 1: Write failing contract tests**

Test that `ReplayIdentity.create(...)` has a stable content-derived ID, validates 64-character SHA-256 inputs, records commit/replay-surface/runtime/dependency/fixture/schema/provider/model/prompt identities, self-verifies direct construction, and rejects repeats outside `1..10` or a call plan above `MAX_REPLAY_CALLS`.

```python
identity = ReplayIdentity.create(
    code_revision="a" * 40,
    source_tree_hash="f" * 64,
    source_tree_dirty=False,
    python_version="3.12.11",
    dependency_lock_hash="b" * 64,
    interpretation_fixture_hash="c" * 64,
    claim_fixture_hash="d" * 64,
    extraction_schema_version=1,
    provider_id="recorded",
    model_id="fixture-output-v1",
    prompt_id="recorded-claim-proposal-v1",
    prompt_hash="e" * 64,
    repeats=3,
    case_count=20,
    interpretation_case_count=14,
)
self.assertEqual(identity.identity_id, ReplayIdentity.create(**same).identity_id)
```

- [x] **Step 2: Run the contract tests and verify RED**

Run: `python3 -m unittest tests.test_claim_pipeline_replay.ReplayContractTests -v`

Expected: import failure because `claim_pipeline.replay` does not exist.

- [x] **Step 3: Implement immutable contracts**

Add `ReplayIdentity`, `ProposalUsage`, `ProposalResponse`, `InterpretationReplayResult`, `ReplayCaseResult`, and `ReplayReport` frozen dataclasses. Use canonical ASCII JSON plus SHA-256 for identities, integer token/latency/micro-USD fields for exact aggregation, and strict text/hash/count validation. Cap repeats at 10 and total calls at 2,560.

- [x] **Step 4: Export the replay API and run GREEN**

Run: `python3 -m unittest tests.test_claim_pipeline_replay.ReplayContractTests tests.test_claim_pipeline_isolation -v`

Expected: all tests pass and no service import is introduced.

### Task 2: Implement One-Call Recorded Replay

**Files:**
- Modify: `email_automation/claim_pipeline/replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [x] **Step 1: Write failing execution tests**

Create a counting fake adapter and prove the runner executes and compares all 14 interpretation cases on every repeat, invokes the adapter exactly `claim_case_count * repeats`, executes `normalize_message_evidence`, `resolve_entities`, `build_claim_extraction_request`, and `extract_claims`, compares complete accepted claim and issue bindings with each case's exact expected result, and reconciles declared provider calls, tokens, latency, and billed cost exactly.

```python
report = run_claim_replay(
    interpretation_catalog=interpretation_catalog,
    claim_catalog=claim_catalog,
    adapter=adapter,
    identity=identity,
)
self.assertEqual(len(claim_catalog.cases) * identity.repeats, adapter.calls)
self.assertTrue(report.passed)
self.assertEqual(sum(item.usage.input_tokens for item in report.results), report.input_tokens)
```

- [x] **Step 2: Run the execution tests and verify RED**

Run: `python3 -m unittest tests.test_claim_pipeline_replay.ReplayExecutionTests -v`

Expected: failure because the adapter and runner are absent.

- [x] **Step 3: Implement the adapter boundary and executor**

Define a `ProposalAdapter` protocol with immutable provider/model/prompt identity and `propose(case_id, request, evidence, entities)`. Implement `RecordedProposalAdapter` by translating each existing fixture's evidence indexes and subject descriptors to the runtime IDs. Implement `run_claim_replay` with strict catalog cross-reference validation, exactly one adapter invocation per case/repeat, safe exception classification by exception type only, exact expected-result comparison, proposal/outcome digests, variance detection, and usage totals.

- [x] **Step 4: Prove failure and variance remain visible**

Add tests where an adapter fails one invocation, returns a different proposal on a repeat, or returns semantically wrong claims. Assert the report fails, identifies the affected case by ID, does not leak exception text or evidence, and distinguishes proposal variance from accepted-outcome variance.

- [x] **Step 5: Run replay and isolation tests GREEN**

Run: `python3 -m unittest tests.test_claim_pipeline_replay tests.test_claim_pipeline_isolation -v`

Expected: all tests pass.

### Task 3: Add a Reproducible Local Runner

**Files:**
- Create: `scripts/run_claim_pipeline_replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [x] **Step 1: Write failing CLI tests**

Test a subprocess invocation with `--repeats 3`: it must report all 14 interpretation cases across 42 comparisons plus all 20 claim cases and 60 calls, stamp the current Git SHA, bounded replay-surface hash and dirty flag, Python version, lock hash, both fixture hashes, extraction schema, recorded adapter identity, zero provider-billed usage, and no raw fixture text or email addresses. It returns zero only for a clean reproducible pass and nonzero for a dirty development evaluation. Test `--output` writes the same canonical report intentionally and no file is written by default.

- [x] **Step 2: Run the CLI tests and verify RED**

Run: `python3 -m unittest tests.test_claim_pipeline_replay.ReplayCliTests -v`

Expected: failure because the script does not exist.

- [x] **Step 3: Implement the local CLI**

Resolve paths from the repository root, obtain `git rev-parse HEAD`, hash only the isolated replay source surface plus `requirements.lock`, load both versioned fixture catalogs, build the recorded adapter and replay identity, execute the runner, print canonical JSON, and return nonzero for a failed report. Accept only `--repeats 1..10` and an optional local `--output` path; do not enumerate unrelated untracked files, load environment secrets, or import service modules.

- [x] **Step 4: Run the complete three-repeat replay**

Run: `python3 scripts/run_claim_pipeline_replay.py --repeats 3 --output /tmp/sitesift-claim-replay.json`

Expected: 14 interpretation cases, 20 claim cases, 42 exact interpretation comparisons, 60 exact adapter calls, all expected outcomes correct, no interpretation/proposal/outcome variance, no errors, zero billed tokens/cost for the recorded adapter. Before commit the semantic evaluation passes but the reproducibility gate remains failed because the source is dirty; rerun after commit must pass cleanly.

### Task 4: Verify, Review, and Record the Gate

**Files:**
- Modify: `docs/superpowers/plans/2026-07-22-sanitized-claim-replay.md`
- Update through vault helper: `clients/mohr/projects/email-automation/email-automation.md`
- Modify: `/Users/baylorharrison/Documents/GitHub.nosync/brain/projects/email-automation/project.md`

- [x] **Step 1: Run focused and full verification**

Run the replay test module, all `test_claim_pipeline*` modules, package compilation, fixture JSON parse, import-isolation test, `git diff --check`, and the full backend suite. Record exact counts and failures rather than inferring success from exit text.

- [x] **Step 2: Perform adversarial review**

Review for hidden network/service imports, adapter double calls, catalog mismatch, nondeterministic serialization, PII/raw-evidence leakage, exception-message leakage, false-positive expected comparisons, overflow/unbounded call plans, and reports that claim model cost evidence when using recorded responses. Fix any finding test-first and rerun the affected gates.

- [ ] **Step 3: Commit the isolated backend checkpoint**

Stage only the replay module, package export, tests, CLI, and plan; inspect the staged diff; commit with a bounded message. Do not push, deploy, run production shadow, or touch Jill/Baylor campaign state.

- [ ] **Step 4: Record durable findings**

Update the canonical Active Experiment through `brain-write-page`, verify its Firestore backlog mirror, update the Brain repo project/backlog with the exact commit and test evidence, run the narrow brain audit, and commit only those intentional Brain repo files.

- [ ] **Step 5: Set the next proof gate**

The next experiment first expands the fixture set across consequential predicates and sanitized multi-turn Jill/Baylor incident shapes. Only then run a separately bounded pinned-model adapter/evaluation with a fixed model snapshot and prompt/schema hashes, predeclared token/cost/latency budget, repeated runs, no persistence/effects, and no policy/action integration until exact accuracy and variance findings are reviewed.
