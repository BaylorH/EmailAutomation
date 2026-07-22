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
- [x] Run the 57-call variance gate only if the one-repeat quality gate passes unchanged.

### Measured Checkpoint

The first separate-oracle checkpoint at `dfdb830` passed the deterministic replay but only 6/19 provider-quality cases, which kept the 57-call gate locked. Field-level diagnostics, a required-claim rather than exhaustive oracle, deterministic identity/review handling, text-backed claim normalization, and narrower provider instructions raised the clean one-repeat gate to 19/19 at `42f65eb`.

The first 57-call run at `42f65eb` produced 57/57 semantically acceptable results, but the harness incorrectly treated harmless raw quote, confidence, and rejected-candidate variation as functional variance. Commit `59b0bac` added a quality-normalized outcome digest over accepted claim semantics and evidence-bound reviews while retaining raw proposal/outcome variance as diagnostics. Its clean recorded replay passed 57/57 interpretation and 87/87 candidate-validation outcomes, and its clean one-repeat provider gate passed 19/19. The next 57-call run correctly found one real failure: 56/57 passed because one broker-correction response marked the new rent claim `asserted` while still linking it to the exact prior claim and emitting the paired correction claim.

Five correction-only sanitized probes reproduced that provider variation once. Commit `bc435d4` now normalizes the paired domain claim to corrected modality only when the domain and correction claims share the same fresh evidence, subject, actor role, known prior claim, and prior predicate. Unknown or mismatched supersession remains fail-visible. The full backend suite passed 2,057 tests.

Final clean proof on `bc435d48a749a24ff31169f8279f5e11033f6a45`:

- Recorded three-repeat replay: 57/57 interpretation and 87/87 candidate-validation results passed with zero variance and zero provider calls.
- One-repeat provider gate: 19/19 results passed; 46,656 input tokens, 4,668 output tokens, 51,324 total tokens, 69,672 ms aggregate latency, 88,457 micro-USD, 19/19 billed calls, complete usage, zero errors, and zero semantic variance.
- Three-repeat provider gate: 57/57 results and 57/57 interpretations passed; 139,968 input tokens, 13,973 output tokens, 153,941 total tokens, 209,048 ms aggregate latency, 238,365 micro-USD, 57/57 billed calls, complete usage, zero errors, and zero quality-outcome variance.
- Raw diagnostic variance remained in broker correction, complete property facts, and workflow intents, but no accepted claim semantics or review bindings varied. Validator-rejected near misses were limited to three invalid unit-price rent candidates and three unsupported dock candidates.
- The final report contains no email-address marker, Baylor/Jill identifier, BP21 identifier, or raw fixture street address. The source tree was clean and no production mailbox, sheet, campaign, queue, deployment, or user data was touched.

This completes the provider-quality oracle gate. It proves the isolated interpretation and claim-proposal boundary, not the later policy, action, persistence, or browser campaign layers.
