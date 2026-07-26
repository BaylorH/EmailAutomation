# Bounded Claim Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert strict model proposals into immutable, evidence-backed claims or explicit review issues without persistence, policy decisions, or production-service access.

**Architecture:** Add one pure extraction boundary that builds a minimal schema-versioned model request and interprets a strict JSON response. A separate predicate-validation module owns deterministic authority, freshness, subject-binding, value, unit, correction, contradiction, and hostile-input rules; accepted output reuses the existing immutable `Claim` contract and bundle validator.

**Tech Stack:** Python 3.12 standard library, frozen dataclasses, enums, JSON, `unittest`, existing claim-pipeline contracts and entity/evidence types.

---

## File Map

- Create `email_automation/claim_pipeline/claim_validation.py`: predicate families, authority/freshness rules, subject-evidence binding, value/unit checks, correction checks, contradiction detection.
- Create `email_automation/claim_pipeline/extraction.py`: strict request/response boundary, immutable issue/result contracts, deterministic claim construction and ordering.
- Create `tests/test_claim_pipeline_extraction.py`: broad behavior tests for extraction and validation classes.
- Create `tests/fixtures/claim_pipeline_claim_cases.json`: versioned semantic lattice covering target/alternate/suite/freshness/units/corrections/hostile/no-claim combinations.
- Create `email_automation/claim_pipeline/claim_fixtures.py`: exact parser for the claim-case catalog.
- Create `tests/test_claim_pipeline_claim_fixtures.py`: execute every fixture through the real extractor.
- Modify `email_automation/claim_pipeline/__init__.py`: expose only the public read-only claim API.
- Modify `tests/test_claim_pipeline_isolation.py`: require the public API while preserving the service-import allowlist.

### Task 1: Lock the extraction boundary with failing tests

- [ ] Add tests proving `build_claim_extraction_request()` emits only schema version, tenant/campaign identity, bounded evidence, known entities, prior claims, supported predicates, and output schema.
- [ ] Add tests proving `extract_claims()` supports an empty claim list and explicit model review, rejects malformed JSON/unknown keys/missing fields as visible issues, and never raises away a model-shape failure.
- [ ] Add tests proving stable inputs yield stable issue and claim identities and deterministic ordering.
- [ ] Run `.venv/bin/python -m unittest tests.test_claim_pipeline_extraction -v` and verify it fails because the extraction module does not exist.

### Task 2: Lock semantic safety with failing tests

- [ ] Add table-driven tests for exact evidence excerpts, tenant/campaign/entity existence, target versus alternate binding, implicit target references, explicit suite isolation, actor authority, fresh versus quoted/forwarded instructions, and prompt-injection-like evidence.
- [ ] Add table-driven predicate tests for availability, transaction type, area, rent, OpEx, clear height, counts, dates, term, power, identity/referral, opt-out, call/tour/information requests, and correction metadata.
- [ ] Add conflict tests proving incompatible explicit values for the same evidence/entity/predicate are all routed to review and exact duplicates collapse safely.
- [ ] Run the focused test module again and verify failures name the missing validation behavior.

### Task 3: Implement the minimum pure extractor and validators

- [ ] Add frozen `ClaimExtractionIssue` and `ClaimExtractionResult` contracts with stable self-verifying identities and JSON-safe serialization.
- [ ] Implement strict root/candidate/review key checks, enum/type conversion, bounded confidence checks, and `Claim.create()` construction.
- [ ] Implement the predicate registry and rules from Task 2. Invalid candidates become issues containing identifiers and reason codes, not raw email bodies.
- [ ] Reuse `validate_claim_bundle()` for final provenance integrity, converting any unexpected bundle violation into a visible fail-closed issue.
- [ ] Sort accepted claims and issues by stable identity and return tuples.
- [ ] Run `.venv/bin/python -m unittest tests.test_claim_pipeline_extraction -v` until green.

### Task 4: Add the broad executable case lattice

- [ ] Define a strict versioned fixture schema containing normalized evidence, resolved entity seeds, model proposals, prior claims when needed, and exact accepted predicates/entities plus issue codes.
- [ ] Include at least 18 cases across target availability, alternate facts, split suites, not-a-fit language with no availability claim, rent versus OpEx, monthly versus annual basis, quoted opt-out, fresh opt-out, correction, contradiction, hostile input, malformed candidate, explicit model review, and no-claim output.
- [ ] Execute each case through real evidence normalization, entity resolution, and claim extraction.
- [ ] Reject fixture unknown keys, duplicate IDs, and unstable manifests.
- [ ] Run `.venv/bin/python -m unittest tests.test_claim_pipeline_claim_fixtures -v` until green.

### Task 5: Preserve isolation and review adversarially

- [ ] Export the new read-only API from `claim_pipeline.__init__` and extend the boundary test.
- [ ] Run all claim-pipeline tests and verify no Graph, Firebase, Sheets, OpenAI, processing, messaging, or follow-up imports appear.
- [ ] Review for bypasses involving Unicode/case evidence spans, booleans treated as numbers, NaN/Infinity, cross-campaign entities, duplicate candidates, corrections to another entity/predicate, and historical instructions.
- [ ] Add a failing regression for every valid finding before patching it.

### Task 6: Verify and checkpoint

- [ ] Compile changed Python modules.
- [ ] Validate fixture JSON and inspect the exact git diff for unrelated changes.
- [ ] Run the full backend suite with the repository's established test command; expected result is all tests passing.
- [ ] Commit only this bounded slice with message `Add bounded claim extraction and validation`.
- [ ] Update the canonical Active Experiment and backlog standing through the vault helper, then update the Brain project map without touching unrelated dirty files.

## Execution Result

- Completed test-first implementation of the pure request/response boundary, predicate validators, and 20-case claim lattice.
- Closed adversarial findings for wrong-entity excerpts, cross-evidence contradictions, mislabeled numbers, mixed review/claim output, malformed object values, forwarded subjects, correction scope/authority/chronology, request intent, and complete request-size bounds.
- Verified 159 claim-pipeline tests and 1,974 full backend tests with exit code 0; schema, JSON fixtures, compilation, import isolation, and diff integrity are clean.
- Kept the package disconnected from production services and effects. The next gate is sanitized deterministic Jill/Baylor replay, not production integration.
