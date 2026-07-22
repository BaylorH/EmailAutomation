# Provider Quality Oracle Implementation Plan

> **Execution:** Follow test-driven development and verification-before-completion. Do not alter accepted claim digests in response to provider output.

**Goal:** Replace the provisional validator-derived provider oracle with a separately hashed, request-complete provider-quality catalog and privacy-safe mismatch reporting.

**Deliverable:** Both code and findings.

### Task 1: Define And Validate The Provider Catalog

- [x] Write failing tests for exact schema, source-case partitioning, request equivalence, fixture-hash binding, claim-union equality, review categories, evidence indexes, and immutability.
- [x] Add the provider-quality fixture loader and the 19-case sanitized expectation catalog.
- [x] Prove every one of the 29 validator cases belongs to exactly one provider request and every interpretation case has claim-quality coverage.

### Task 2: Separate Candidate And Provider Evaluation

- [x] Write failing tests proving provider mode uses 19 complete request expectations while recorded mode still uses all 29 candidate cases.
- [x] Add the provider-quality fixture hash to replay identity.
- [x] Replace validator-issue translation with exact claim/review evaluation against the provider catalog.
- [x] Add privacy-safe mismatch codes and reject all provider candidates that fail deterministic validation.

### Task 3: Constrain Provider Review Output

- [x] Write failing prompt/adapter tests for exact supported review category tokens.
- [x] Update the pinned prompt revision and hash without exposing expected case outputs.
- [x] Verify malformed or free-form review reasons fail visibly and safely.

### Task 4: Verify And Measure

- [x] Run fixture, replay, provider, CLI, isolation, compilation, focused, full, and diff-integrity checks.
- [x] Commit a clean implementation checkpoint and rerun the clean recorded three-repeat replay.
- [x] Run one clean 19-call provider repeat and record exact quality, calls, tokens, latency, and cost.
- [ ] Run the 57-call variance gate only if the one-repeat quality gate passes unchanged.

### Measured Checkpoint

Backend `dfdb830` passed the clean recorded gate with 57/57 interpretation and 87/87 candidate-validation outcomes. Its clean provider-quality run completed exactly 19/19 billed calls with complete usage, zero transport errors, 36,833 input tokens, 4,388 output tokens, 61,023 ms aggregate latency, and 125,889 micro-USD. Six of 19 complete-request cases passed. The 57-call variance run was stopped. The failure report remained privacy-safe and contained no raw addresses, email identifiers, claim values, or review prose.

The next iteration adds predicate-count and exact-detail mismatch classes before another paid run. Accepted claim digests, complete request groups, and review expectations remain unchanged.
