# Production frontend read-only smoke evidence

**Recorded:** 2026-08-05 (America/Phoenix)
**Deliverable:** finding
**Verdict:** Public and authenticated production surfaces render, and the
latest retained self-canary proves a complete prior round trip. Campaign
creation and automation remain intentionally closed. Production remains
**NO-GO** for a new canary and user return.

## Scope and effect boundary

- Read-only browser inspection covered the public landing page, `/login`, the
  authenticated `/operations` page, the authenticated `/dashboard` page, one
  admin support diagnostic, one returning-user conversation history, and one
  retained self-canary result.
- No campaign was created or launched. No email, reply, invitation, form,
  upload, toggle, permission change, or other external communication occurred.
- No production record was written or deleted. UI expansion, selection, and
  navigation were the only interactions.
- A new canary was not authorized because the current turn did not name one
  exact self-owned recipient, and the production operations controls were
  closed.

## Observed production state

- `https://email-automation-cache.web.app/` rendered the SiteSift landing page.
- `https://email-automation-cache.web.app/login` rendered the Microsoft sign-in
  entry point.
- `https://sitesiftai.com/operations` rendered the authenticated admin
  operations surface.
- Campaign creation: **Closed**.
- Campaign automation: **Closed**.
- Aggregate state: 0 live, 0 stopping, 0 stop-failed, 0 queued, 0 blocked before
  send, 0 pending responses, 1 historical processing failure, and 0 dead-letter
  items.
- The single support diagnostic was a historical `stale_manual_review` item
  outside a live campaign. It is not evidence of an active send queue.
- The returning-user cohort inspected had no actionable item or warning. Its
  stopped-campaign conversation history loaded successfully, including a newer
  inbound reply received after the campaign stopped.
- The authenticated dashboard loaded 24 campaign rows and reported 12 completed
  campaigns, 47 completed properties, and 341 sheet updates.

## Retained self-canary proof

The retained 2026-08-01 self-canary row expanded in production and showed:

- one exact self-recipient thread;
- three messages (initial outreach, inbound response, terminal reply);
- extracted total square feet, rent, operating expenses, dock, drive-in,
  ceiling-height, and power values;
- `Conversation Ended — No further follow-up needed`; and
- `Property Complete — All required fields filled`.

This is useful retained production evidence, but it predates the current B2-C
work and is not a substitute for a fresh exact-SHA postdeploy canary.

## Production defects and provenance gaps

1. The unauthenticated `/login` page emitted
   `Failed to write logs: FirebaseError: Missing or insufficient permissions`.
   Sign-in UI still rendered; impact appears limited to client logging until
   reproduced under authenticated use.
2. The authenticated production bundle reported two historical Firestore
   WebChannel transport warnings. The inspected operations, dashboard, and
   conversation views nevertheless loaded; these warnings need correlation
   with server/request telemetry before being classified as current failures.
3. Firebase Hosting metadata reads returned HTTP 403 for the current CLI
   credentials. The live asset was `static/js/main.6321b88b.js`, but no trusted
   mapping from that asset to a Git commit was available. Production deploy
   provenance is therefore an open release gate.
4. The separate frontend checkout contains pre-existing uncommitted work on a
   rescued feature branch. It was preserved untouched. Frontend branch cleanup
   and an exact commit-to-deploy proof remain required before the next release.

## Decision

The production frontend is reachable and previously completed a real
self-canary, but a fresh campaign is blocked by the closed operations controls,
missing exact self-recipient authorization in the current turn, and incomplete
deploy provenance. These findings reinforce the current sequence: finish the
bounded contact-authority milestone, restore exact GitHub/deploy traceability,
then run a newly authorized frontend canary before any user return decision.
