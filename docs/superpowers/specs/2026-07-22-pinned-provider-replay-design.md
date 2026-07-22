# Pinned Provider Replay Design

**Status:** Approved as the next gate in the 2026-07-22 Active Experiment

**Deliverable:** Both code and findings

## Goal

Measure a pinned OpenAI model against the sanitized 19-case interpretation and 28-case claim corpus without enabling any production behavior. The experiment must prove its own provider-call, usage, latency, cost, and variance accounting independently from the component that generates semantic proposals.

## Boundaries

- Keep recorded replay as the default and preserve its exact oracle.
- Provider mode requires an explicit command-line opt-in and `OPENAI_API_KEY` in the process environment.
- Pin provider `openai`, model `gpt-5.2-2025-12-11`, prompt revision, SDK retry count zero, timeout, structured JSON mode, and a maximum of 84 calls.
- Send only the sanitized fixture-derived extraction requests.
- Never import or call Graph, Firebase, Sheets, mailbox, queue, drafting, follow-up, campaign, or deployment code.
- Reports contain only safe identifiers, hashes, counts, token totals, latency, cost, error classes, and pass/fail outcomes.

## Architecture

### Semantic adapter

A dependency-free adapter in the isolated claim package converts a `ClaimExtractionRequest` into the pinned prompt and delegates one call to a transport protocol. It returns the model output but does not own authoritative accounting.

### Instrumented transport

An OpenAI-specific transport outside the isolated package owns the SDK client and an append-only in-memory attempt ledger. It records an attempt before the SDK call, disables SDK retries, parses successful response usage, calculates cost from a pinned pricing revision, and records incomplete accounting on every exception or missing usage field.

### Replay reconciliation

The replay runner snapshots transport telemetry before and after every proposal. A non-recorded case passes accounting only when exactly one attempt is observed, returned usage agrees with the independently observed delta, usage is complete, and the aggregate observed call count equals the planned call count. Provider exceptions remain safe error-class identifiers and still retain their observed attempt.

### Evaluation

The existing stored claim digests and exact review bindings remain authoritative. The report keeps proposal and outcome digests per repeat, exposes model misses by safe case ID, and fails on any semantic mismatch or variance. The model is not given expected fixture outputs, and expected digests are not modified in response to misses.

## Failure Handling

- No key or missing explicit opt-in: reject before client construction.
- Timeout, rate limit, provider error, or malformed response: one visible failed case, no retry, incomplete accounting where usage cannot be proven.
- Telemetry undercount, overcount, or adapter mismatch: fail the case and overall evaluation.
- Dirty source tree: evaluation may run, but the report cannot pass.
- Any report privacy violation or side-effect import: stop the experiment.

## Verification

Tests must prove zero-retry configuration, one-call telemetry, failure accounting, partial usage rejection, cost math, malformed response handling, adapter/telemetry mismatch, aggregate reconciliation, explicit CLI opt-in, bounded calls, safe output, and import isolation. Then run focused and full suites, a one-case transport smoke before any full provider run, bounded repeated provider replay, adversarial review, and a clean committed replay.
