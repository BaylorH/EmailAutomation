# Provider Quality Oracle Design

**Status:** Approved by the standing SiteSift production-readiness experiment

**Deliverable:** Both code and findings

## Problem

The claim fixture catalog is a validator test suite, not a set of independent model requests. Several cases reuse one sanitized conversation while presenting different good or deliberately malformed candidates. Candidate-specific accepted indexes, rejection codes, and candidate indexes are therefore valid validator oracles but invalid provider-quality expectations. A capable provider should emit every supported valid claim in the request, omit malformed candidates it never saw, and request review only for genuine current ambiguity.

## Approaches Considered

1. Translate selected validator issue codes into provider issue codes. This is the current provisional approach and is rejected because candidate indexes and attack-specific errors do not exist in provider input.
2. Keep all 28 provider calls and hand-author a second expectation for each duplicate request. This can work, but wastes calls and permits duplicated requests to drift into contradictory expectations.
3. Group request-equivalent validator cases into one explicit provider-quality case. This is selected because it preserves all candidate tests, gives the provider one complete extraction target per distinct request, and reduces a three-repeat run from 87 to 57 calls.

## Catalog Contract

Add a versioned provider-quality fixture with one case for each unique combination of interpretation source and prior claims. Each case names:

- a report-safe provider case ID;
- its interpretation case ID;
- every source claim-case ID represented by the request;
- the complete sorted expected accepted-claim digest set;
- exact expected review items, each containing a controlled category and evidence index.

The catalog is separately hashed and its hash is included in replay identity. It also binds the exact claim fixture hash. Loading fails unless source claim cases form a complete, non-overlapping partition of the candidate catalog, grouped cases have identical interpretation/prior-claim inputs, and each expected claim set equals the union of the already authoritative accepted-claim digests in its source cases. The provider catalog cannot weaken or invent accepted claims.

Initial review categories are deliberately small:

- `entity_ambiguity` for current evidence that cannot be bound to one property;
- `insufficient_evidence` for a current statement that names a potentially useful fact but omits a required semantic basis.

The provider prompt requires the review `reason` field to contain exactly one supported category token. Free-form review prose is not graded or reported.

## Replay Behavior

Recorded candidate-validation replay covers all 29 cases, including the previously missing Outlook fresh-over-quoted claim case. Provider-quality replay uses the new 19-case catalog and the representative source case only to construct the sanitized request; expected outputs are never sent to the model.

Provider pass criteria are:

- exact semantic equality against the provider case's complete accepted-claim set after every proposed claim passes the unchanged deterministic validator;
- exact review category and evidence-index equality;
- no malformed or validator-rejected provider candidate;
- complete independent transport accounting;
- no proposal or outcome variance across repeats.

Semantic equality preserves evidence index, subject, predicate, value, actor role, polarity, modality, unit, effective date, and correction linkage. It intentionally ignores only the exact contiguous `evidenceText` span and the precise accepted confidence number: multiple validator-approved excerpts can support the same fact, and the prompt's `0.99` instruction conflicts with older accepted fixture confidence values between `0.95` and `0.98`. The fixture's full accepted digests remain unchanged and continue to govern deterministic candidate validation; this provider comparator prevents harmless quote or confidence variants from masquerading as model-quality failures.

The report adds only sorted safe mismatch codes and the provider-quality fixture hash. Allowed mismatch codes distinguish missing/unexpected claims, missing/unexpected reviews, review-binding errors, invalid review categories, and rejected provider candidates. Raw evidence, addresses, emails, model prose, claim values, and review prose remain absent.

## Failure And Cost Policy

The first paid gate is one clean 19-call repeat. Any semantic failure stops the three-repeat run. The 57-call variance gate runs only after one repeat passes without changing candidate claim digests. Provider/model/prompt/runtime/fixture identity, attempts, billed calls, token usage, latency, and micro-USD remain exact and independently reconciled.

## Verification

Tests must first fail for duplicate or missing source cases, request-equivalence violations, fixture-hash drift, weakened or invented claim digests, unsupported review categories, bad evidence indexes, candidate-issue leakage, unsafe mismatch text, wrong provider call counts, changed recorded behavior, rejected valid quote/confidence variants, and accepted behavioral-field drift. Then run fixture, replay, provider-adapter, CLI, isolation, compilation, focused, and full suites; commit cleanly; run one clean paid repeat; and stop again if the 19-case semantic gate does not pass.
