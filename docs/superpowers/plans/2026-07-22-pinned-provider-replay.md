# Pinned Provider Replay Implementation Plan

> **Execution:** Follow test-driven development and verification-before-completion. Do not weaken fixture expectations after observing provider output.

**Goal:** Add an explicitly enabled, independently instrumented, no-effect OpenAI replay mode for the sanitized claim corpus.

**Deliverable:** Both code and findings.

---

### Task 1: Define Independent Telemetry Contracts

**Files:**
- Modify: `email_automation/claim_pipeline/replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [x] Write failing tests for one-attempt deltas, undercount, overcount, incomplete usage, provider exceptions, and adapter/telemetry mismatch.
- [x] Add immutable transport telemetry snapshots/deltas.
- [x] Make provider replay accounting authoritative from telemetry while preserving recorded replay behavior.

### Task 2: Add The Pinned Semantic Adapter And OpenAI Transport

**Files:**
- Create: `email_automation/claim_pipeline/provider_replay.py`
- Create: `scripts/claim_pipeline_openai_transport.py`
- Create: `tests/test_claim_pipeline_provider_replay.py`
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`

- [x] Write failing tests for prompt identity, exact request serialization, response parsing, fixed pricing, complete usage, timeout/provider errors, and no retries.
- [x] Implement the dependency-free semantic adapter and transport protocol.
- [x] Implement the OpenAI transport with a zero-retry client, fixed timeout, safe error propagation, and append-only telemetry.

### Task 3: Add Explicit Provider CLI Mode

**Files:**
- Modify: `scripts/run_claim_pipeline_replay.py`
- Modify: `tests/test_claim_pipeline_replay.py`

- [x] Write failing tests for default-off behavior, explicit opt-in, fixed model, call cap, missing key, source hashing, and privacy-safe output.
- [x] Add `--provider openai --allow-provider-calls` while keeping recorded mode as the default.
- [x] Reject provider mode before construction unless every safety gate passes.

### Task 4: Verify, Measure, Review, And Record

- [x] Run focused tests, compilation, isolation, diff checks, and the full backend suite.
- [x] Run one bounded provider smoke, then stop before the three-repeat corpus when the one-repeat quality gate fails.
- [x] Record exact one-repeat accuracy, calls, tokens, latency, cost, and safe failure classes without changing accepted claim digests.
- [ ] Run adversarial review, fix blockers, commit locally, and update durable project state.

### Measured checkpoint

The clean, independently observed one-repeat run at backend `e7e2279` used 28/28 billed calls, 51,873 input tokens, 8,570 output tokens, 121,613 ms aggregate latency, and 145,036 micro-USD. Usage was complete, there were zero transport errors, and the redacted report contained no raw addresses or email identifiers. Four of 28 cases passed the exact provider-quality outcome gate. The three-repeat variance run was not authorized by this result.

The run also disproved the assumption that the candidate-validation issue oracle can double as a provider-quality oracle: several fixtures intentionally contain malformed proposals so the validator can reject them, while a good provider should omit those proposals. Accepted claim digests remain authoritative. Before another paid run, provider review/omission outcomes must move into an explicit separately hashed fixture, and the report needs safe mismatch categories.

**Standing blocker:** The provisional provider-quality issue expectations are inferred from validator outcomes. They are useful for exposing the oracle-design problem, but they are not a valid final quality oracle. Task 4 remains open until the explicit provider fixture is independently reviewed and the one-repeat quality gate passes without weakening accepted claim digests.
