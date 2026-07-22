# Pinned Provider Replay Implementation Plan

> **Execution:** Follow test-driven development and verification-before-completion. Do not weaken fixture expectations after observing provider output.

**Goal:** Add an explicitly enabled, independently instrumented, no-effect OpenAI replay mode for the sanitized claim corpus.

**Deliverable:** Both code and findings.

---

### Task 1: Define Independent Telemetry Contracts

**Files:**
- Modify: `email_automation/claim_pipeline/replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [ ] Write failing tests for one-attempt deltas, undercount, overcount, incomplete usage, provider exceptions, and adapter/telemetry mismatch.
- [ ] Add immutable transport telemetry snapshots/deltas.
- [ ] Make provider replay accounting authoritative from telemetry while preserving recorded replay behavior.

### Task 2: Add The Pinned Semantic Adapter And OpenAI Transport

**Files:**
- Create: `email_automation/claim_pipeline/provider_replay.py`
- Create: `scripts/claim_pipeline_openai_transport.py`
- Create: `tests/test_claim_pipeline_provider_replay.py`
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`

- [ ] Write failing tests for prompt identity, exact request serialization, response parsing, fixed pricing, complete usage, timeout/provider errors, and no retries.
- [ ] Implement the dependency-free semantic adapter and transport protocol.
- [ ] Implement the OpenAI transport with a zero-retry client, fixed timeout, safe error propagation, and append-only telemetry.

### Task 3: Add Explicit Provider CLI Mode

**Files:**
- Modify: `scripts/run_claim_pipeline_replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [ ] Write failing tests for default-off behavior, explicit opt-in, fixed model, call cap, missing key, source hashing, and privacy-safe output.
- [ ] Add `--provider openai --allow-provider-calls` while keeping recorded mode as the default.
- [ ] Reject provider mode before construction unless every safety gate passes.

### Task 4: Verify, Measure, Review, And Record

- [ ] Run focused tests, compilation, isolation, diff checks, and the full backend suite.
- [ ] Run one bounded provider smoke, then the approved repeated corpus only if telemetry and privacy gates hold.
- [ ] Record exact accuracy, variance, calls, tokens, latency, cost, and safe failure classes without changing the oracle.
- [ ] Run adversarial review, fix blockers, commit locally, and update durable project state.
