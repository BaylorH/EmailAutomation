# Evidence Normalization and Entity Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure, read-only interpretation layer that separates fresh, quoted, forwarded, signature, attachment, and link evidence and binds that evidence to target, alternate-property, suite, and contact entities without production side effects.

**Architecture:** Add two isolated modules under `email_automation.claim_pipeline`. `evidence.py` accepts immutable raw message/external-text inputs and emits immutable `EvidenceEnvelope` values plus explicit extraction failures. `entities.py` consumes only normalized evidence and immutable target seeds, then emits `EntityRef` values, match metadata, and explicit ambiguity issues. A strict versioned fixture catalog drives broad behavior without importing the production worker.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `enum`, `hashlib`, `html.parser`, `json`, `re`, `urllib.parse`), existing claim-pipeline contracts, `unittest`, JSON fixtures.

---

## File Structure

- Create `email_automation/claim_pipeline/evidence.py`: raw read-only inputs, explicit evidence failures, deterministic body segmentation, and normalization result.
- Create `email_automation/claim_pipeline/entities.py`: target seed contracts, address/suite/contact canonicalization, deterministic entity binding, and ambiguity issues.
- Create `email_automation/claim_pipeline/interpretation_fixtures.py`: strict loader for versioned normalization/entity cases.
- Modify `email_automation/claim_pipeline/contracts.py`: bind normalized evidence identity to campaign scope while preserving legacy unscoped contract callers.
- Modify `email_automation/claim_pipeline/__init__.py`: export the new public read-only API.
- Create `tests/fixtures/claim_pipeline_interpretation_cases.json`: broad interpretation cases.
- Create `tests/test_claim_pipeline_evidence.py`: normalization/provenance tests.
- Create `tests/test_claim_pipeline_entities.py`: subject-isolation tests.
- Create `tests/test_claim_pipeline_interpretation_fixtures.py`: fixture integrity and executable-oracle tests.
- Modify `tests/test_claim_pipeline_isolation.py`: preserve the no-production-import boundary.

## Task 1: Immutable Raw Inputs and Evidence Results

**Files:**
- Create: `email_automation/claim_pipeline/evidence.py`
- Test: `tests/test_claim_pipeline_evidence.py`

- [x] **Step 1: Write failing construction and immutability tests**

Test these exact contracts:

```python
raw = RawMessageEvidence(
    tenant_id="uid-1",
    campaign_id="campaign-1",
    message_id="message-1",
    direction=Direction.INBOUND,
    actor=Actor("Alex", "alex@example.com", ActorRole.BROKER),
    observed_at="2026-07-22T12:00:00Z",
    subject="RE: 123 Industrial Ave",
    body="Suite B is available.",
)
result = normalize_message_evidence(raw)
self.assertEqual((EvidenceSource.SUBJECT, EvidenceSource.FRESH_BODY), tuple(item.source_kind for item in result.evidence))
```

Also require `ExternalEvidenceInput` to accept only `attachment` or `link`, require exactly one of usable content or error, normalize list inputs to tuples, and reject non-string sequence members.

- [x] **Step 2: Run the evidence tests red**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_evidence`

Expected: import failure because `email_automation.claim_pipeline.evidence` does not exist.

- [x] **Step 3: Implement immutable input/result contracts**

Define frozen:

```python
ExternalEvidenceInput(source_kind, location, content="", error="")
RawMessageEvidence(tenant_id, campaign_id, message_id, direction, actor, observed_at, subject="", body="", signature="", external=())
EvidenceFailure(failure_id, tenant_id, campaign_id, message_id, source_kind, location, reason, parent_evidence_id=None)
EvidenceNormalizationResult(evidence=(), failures=())
```

All sequence fields normalize to tuples. All IDs are deterministic SHA-256 values. Failures are immutable and self-verifying. No placeholder evidence is created for an extraction failure.

- [x] **Step 4: Run the construction tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_evidence`

Expected: contract tests pass.

## Task 2: Deterministic Evidence Segmentation

**Files:**
- Modify: `email_automation/claim_pipeline/evidence.py`
- Modify: `tests/test_claim_pipeline_evidence.py`

- [x] **Step 1: Write failing segmentation tests**

Cover:

- subject and fresh body;
- Gmail `On ... wrote:` and `>` history as `quoted_body`;
- Outlook `-----Original Message-----` and `From:/Sent:` history as `quoted_body`;
- Gmail/Apple forwarded dividers as `forwarded_body`;
- explicit signature input as `signature`;
- attachment/link content with parent evidence;
- attachment/link failures as `EvidenceFailure` with no fabricated content envelope;
- empty body with a valid attachment;
- CRLF/LF identity stability;
- deterministic output order and IDs.

- [x] **Step 2: Run segmentation tests red**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_evidence`

Expected: failures for missing segmentation and external-evidence behavior.

- [x] **Step 3: Implement the minimal deterministic segmenter**

Rules:

1. Normalize line endings and trim boundary blank lines only.
2. Preserve non-quoted text as one or more `fresh_body` regions.
3. Treat standalone original-message dividers and Outlook header blocks as quoted history through the end.
4. Treat forwarded-message dividers as forwarded evidence through the end.
5. Treat `On ... wrote:` plus following quote-prefixed lines as quoted evidence; later non-prefixed bottom-posted text returns to fresh evidence.
6. Emit exact line-range locations such as `body:lines-1-3`.
7. Parent quote/forward/signature/external evidence to the first fresh body envelope, or subject when no fresh body exists.
8. Never infer facts, fetch links, parse PDFs, or import production helpers.

- [x] **Step 4: Run evidence tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_evidence`

Expected: all normalization tests pass.

## Task 3: Deterministic Entity Resolution

**Files:**
- Create: `email_automation/claim_pipeline/entities.py`
- Test: `tests/test_claim_pipeline_entities.py`

- [x] **Step 1: Write failing entity tests**

Require these behaviors:

```python
seed = EntitySeed(
    entity_type=EntityType.TARGET_PROPERTY,
    label="123 Industrial Ave",
    canonical_address="123 Industrial Avenue",
    relationship="target",
    aliases=("123 Industrial Ave",),
)
result = resolve_entities(
    tenant_id="uid-1",
    campaign_id="campaign-1",
    seeds=(seed,),
    evidence=evidence,
)
```

- target aliases resolve to the seeded target;
- a different explicit street address becomes an `alternate` property, never target evidence;
- `Suite A is leased. Suite B remains available.` creates two separate suite entities under the target address;
- repeated equivalent addresses deduplicate;
- actor email creates one broker-contact entity;
- `the other building` without an address creates `ambiguous_alternate`, not a fake property;
- multiple competing addresses create a review issue;
- quoted/forwarded evidence remains linked with lower confidence and cannot silently replace the target;
- list inputs cannot mutate frozen outputs.

- [x] **Step 2: Run entity tests red**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_entities`

Expected: import failure because the resolver does not exist.

- [x] **Step 3: Implement canonicalization and resolution**

Define frozen `EntitySeed`, `EntityMatch`, `ResolutionIssue`, and `EntityResolutionResult`. Implement:

- punctuation/case/whitespace and common street-suffix canonicalization;
- conservative US-style street-address extraction;
- `Suite`, `Ste`, `Unit`, and `Space` extraction;
- exact target/alias matching before alternate creation;
- contextual suite creation under the target only when no competing address is present;
- deterministic contact creation from evidence actors;
- confidence/match-kind metadata outside stable `EntityRef` identity;
- issue codes `ambiguous_alternate`, `multiple_property_candidates`, and `unbound_suite`.

Never use an LLM, campaign policy, row writes, or external lookups.

- [x] **Step 4: Run entity tests green**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_entities`

Expected: all entity tests pass.

## Task 4: Versioned Executable Interpretation Fixtures

**Files:**
- Create: `email_automation/claim_pipeline/interpretation_fixtures.py`
- Create: `tests/fixtures/claim_pipeline_interpretation_cases.json`
- Test: `tests/test_claim_pipeline_interpretation_fixtures.py`

- [x] **Step 1: Write failing strict-loader tests**

Require exact root/case/message/seed/external/expected keys, valid contract enums, unique case IDs, immutable loaded mappings, reproducible manifest hashes, and rejection of unknown keys or non-string text.

- [x] **Step 2: Run fixture-loader tests red**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_interpretation_fixtures`

Expected: import failure because the loader and catalog do not exist.

- [x] **Step 3: Implement the strict loader and at least 10 cases**

The fixture catalog must execute, not merely describe, these cases:

1. fresh reply over quoted stale history;
2. Outlook original-message history;
3. forwarded different-property text;
4. split Suite A/Suite B state;
5. wrong-property attachment;
6. extraction failure remains visible;
7. link-only alternate property;
8. ambiguous `other building` wording;
9. target-address alias plus signature contact;
10. attachment-only reply;
11. multiple property candidates.

Each expected object declares exact source-kind counts, failure codes, entity keys/types/relationships, and issue codes.

- [x] **Step 4: Execute every fixture through real normalization and resolution**

Run: `.venv/bin/python -m unittest tests.test_claim_pipeline_interpretation_fixtures`

Expected: every case matches its expected normalized evidence, failures, entities, and issues.

## Task 5: Isolation, Review, and Repository Verification

**Files:**
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`

- [x] **Step 1: Export the public interpretation API**

Export raw inputs, results, normalizer, entity seeds/results/resolver, and fixture loader from `email_automation.claim_pipeline`.

- [x] **Step 2: Extend isolation tests**

Reject imports of Graph/Firebase/Sheets/OpenAI/requests/BeautifulSoup and all production processing, messaging, follow-up, attachment, and property-image modules from the claim-pipeline package.

- [x] **Step 3: Run the complete focused suite**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_claim_pipeline_contracts \
  tests.test_claim_pipeline_validation \
  tests.test_claim_pipeline_mode \
  tests.test_claim_pipeline_fixtures \
  tests.test_claim_pipeline_evidence \
  tests.test_claim_pipeline_entities \
  tests.test_claim_pipeline_interpretation_fixtures \
  tests.test_claim_pipeline_isolation
```

Expected: all focused tests pass without network access or credentials.

- [x] **Step 4: Run compilation and diff integrity**

Run: `.venv/bin/python -m compileall -q email_automation/claim_pipeline`

Run: `git diff --check`

Expected: both exit 0.

- [x] **Step 5: Run independent adversarial review**

Probe quote/forward confusion, wrong-property binding, collapsed suites, forged IDs, ambiguous aliases, hidden extraction failures, nondeterministic ordering, and prohibited imports. Add a failing regression before fixing every valid finding.

- [x] **Step 6: Run the full backend suite**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

Expected: the existing suite plus the interpretation tests pass.

Adversarial review added regressions for campaign-bound evidence, cross-tenant and
cross-campaign rejection, inner Gmail/Outlook actors, child attachment parent
binding, false addresses/suites from ordinary prose, directional aliases,
role-neutral contact identity, exact fixture provenance/content/entity labels,
and absolute/relative production-import bypasses.

- [x] **Step 7: Commit the complete slice**

```bash
git add docs/superpowers/plans/2026-07-22-evidence-entity-interpretation.md \
  email_automation/claim_pipeline \
  tests/fixtures/claim_pipeline_interpretation_cases.json \
  tests/test_claim_pipeline_evidence.py \
  tests/test_claim_pipeline_entities.py \
  tests/test_claim_pipeline_interpretation_fixtures.py \
  tests/test_claim_pipeline_isolation.py
git commit -m "Add read-only evidence and entity interpretation"
```

## Completion Gate

This slice is complete only when common reply structures preserve fresh/quoted/forwarded provenance, external extraction failures remain explicit, alternate addresses cannot bind to the target, split suites remain separate, ambiguous entities produce review issues, every output is deterministic and immutable, independent review is clean, and the full backend suite passes. It does not authorize claim-model calls, persistence, shadow processing, Sheet mutation, email sending, deployment, or production enforcement.
