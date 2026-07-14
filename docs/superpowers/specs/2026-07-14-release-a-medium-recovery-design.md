# Release A Medium Recovery Design

## Goal

Remove the production defects exposed by the failed 10-row Baylor-only campaign without widening user scope, enabling follow-ups, or changing the customer-facing campaign workflow.

## Evidence

The medium run proved six vetoes: same-timestamp campaign outbox items sent in Firestore document order, a nine-minute request timeout, a swallowed Google Sheets 429, an incorrect multi-suite total, a 1 GiB worker OOM, and an interrupted wrong-contact review. The final veto is downstream of the OOM rather than a separate classifier defect.

## Design

1. Sort same-timestamp outbox documents by campaign row number, with document ID as the final deterministic tie-breaker. Non-campaign and older work keep their existing `createdAt` priority.
2. Let proposal-application exceptions escape `apply_proposal_to_sheet`. The existing inbox scanner will then create a retryable `processingFailures` record and leave the inbound message unprocessed.
3. Prefer an explicitly labeled aggregate such as `10,000 SF total` over individual suite figures. This deterministic extraction may replace a conflicting model proposal only when the latest broker-authored text carries the explicit total marker.
4. Recycle the single gunicorn worker after each request so retained PDF/OpenAI/Google client memory cannot accumulate across warm requests. Raise the service limit to 2 GiB for one-request headroom while keeping concurrency at one and the existing 540-second lease-safe timeout.
5. Treat a successful bounded/retried Cloud Tasks drain as normal. For manual release proof, invoke the Baylor-scoped endpoint until queues are quiescent; no global scheduler is used.

## Safety Boundaries

- Only `users/{uid}/outbox`, the matched campaign thread/message, its exact Sheet row, and existing failure/audit collections are affected.
- Recipient, placeholder, signature, campaign-stop, dedupe, reply-all, and follow-up gates are unchanged.
- Large proof remains blocked until a fresh 10-row campaign passes with opt-out sent last.

## Verification

Focused tests must first fail and then pass for all four changes. The full backend suite, deployment dry-run/readback contracts, no-send checks, and a fresh Baylor-only 10-row campaign must pass before the large proof is launched.
