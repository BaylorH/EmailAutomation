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
- [ ] Commit a clean implementation checkpoint and rerun the clean recorded three-repeat replay.
- [ ] Run one clean 19-call provider repeat and record exact quality, calls, tokens, latency, and cost.
- [ ] Run the 57-call variance gate only if the one-repeat quality gate passes unchanged.
