# Browser-First Production Clearance Design

**Status:** Approved by Baylor on 2026-08-06

**Deliverable:** both (code and production-readiness findings)
**Decision:** SiteSift remains NO-GO for customer return until the gates below
produce exact production evidence.

## Goal

Restore confidence in SiteSift by fixing and proving the product in small,
deployable vertical slices. A feature is closed only after a user can exercise
it through the live frontend and the resulting mailbox, Firestore, Sheet,
notification, audit, queue, and recovery state all agree.

The machine-readable execution state is
[`docs/release-safety/production-clearance-state.json`](../../release-safety/production-clearance-state.json).
The single behavioral rubric remains
[`docs/release-safety/feature-gradebook.json`](../../release-safety/feature-gradebook.json),
amended through this design rather than replaced by another competing rubric.

## Product decisions

### Browser-first means browser-first

All production product actions must use the deployed SiteSift UI:

- sign in and verify the active sender;
- upload and review the workbook;
- configure Ask, Note, Skip, and required-for-close fields;
- review names, recipients, properties, messages, subjects, and follow-ups;
- prepare and start the campaign;
- inspect and resolve notifications/actions;
- pause, resume, stop, or approve user-visible work;
- review conversations and completion state.

The production-clearance run may read Firestore, Graph, Google Sheets,
deployment configuration, artifact metadata, GitHub, and CI directly. Those
surfaces are evidence/readback, not a substitute for the product path. No
production feature may be stamped from direct Firestore seeding, an API call
that bypasses the UI, a local Playwright simulation, or a synthetic queue
insertion.

Deployment operations remain CLI/CI operations. Repairs to production data are
separate, explicitly reviewed migrations; a browser test may never silently
repair its own evidence.

### The identity rule is role binding, not context hiding

The AI should receive and use:

- the campaign owner and authenticated sender persona;
- the represented-client background, requirements, and disclosure policy;
- the target property and exact stable row identity;
- the broker/addressee and current thread participants;
- the newest broker-authored message and relevant conversation history;
- alternate properties, contacts, and attachments with their own provenance;
- the campaign's Ask, Note, Skip, formula, and required-field configuration.

Every fact must remain bound to its role, source message, property, and
campaign. The unsafe behaviors are role substitution, stale/quoted text acting
as new evidence, facts crossing properties, a company being used as a person's
name, or a sender/signature from another account—not the mere presence of names
or background context.

### Core tour escalation is not advanced tour scheduling

A broker offering or requesting a tour is a core campaign event. It must create
a visible, source-bound operator action and pause or continue according to the
approved core policy. Route planning, itinerary generation, and tour-invite
sending are advanced features. They may remain `OFF_SAFE`, but they cannot hide
or consume the core tour action.

### One immutable event, several projections

Notifications are projections of an event, not the event itself. The canonical
event envelope is:

```text
eventId, schemaVersion, eventType, reasonCode, featureId
userId, clientId, threadId, propertyAnchor, sourceMessageIdentity
occurredAt, lifecycleState, severity, actionability
dedupeScope, dedupeKey, stateEffects[], sheetEffects[], auditRefs[]
copyKey, projectionStatus
```

`actionability` is one of:

- `approval_required`
- `reply_required`
- `manual_review_required`
- `awareness_only`
- `silent_audit`

Every source message can create several semantically different events, but the
same event may settle only once. Dedupe uses canonical source identity plus the
semantic occurrence. A thread-wide `handled=true` value is never sufficient.

## Production conversation corpus

### Historical semantic seeds

The corpus must reuse the meaning of real failures without copying customer
PII or replaying one canned body indefinitely. Required source families are:

- `tests/E2E_REPLY_CHEATSHEET.md`: flyer language falsely treated as a tour and
  rent/OpEx provenance errors;
- `tests/REAL_WORLD_TEST_DATA.md` and `tests/conversations/real_world_*`:
  forwarded facts, call offers, confidential questions, PDFs, unavailable
  properties, and alternatives;
- `tests/fixtures/jill_readonly_replay_scenarios.json`: sanitized incremental
  completion, non-fit plus replacement, forwarded contacts, PDFs, and tours;
- `tests/test_jill_live_campaign_regressions.py`: rent/OpEx confusion,
  remediation versus terminal non-fit, cross-property attachments, and
  misleading unavailable wording;
- `tests/test_jill_june_regressions.py`: representation changes, LOI/no-space,
  tour-only restrictions, ancillary properties, and quoted history;
- `docs/release-safety/surface-aprime-real-ai-findings.md`: quoted/forwarded
  misattribution, role confusion, numeric-unit errors, and multi-intent model
  failures;
- `docs/release-safety/surface-a-broker-language-bugs.md`: the 464-phrase
  deterministic robustness sweep;
- `tests/e2e_broker_replies.md` and `tests/conversations/edge_cases/*`:
  complete, partial, hostile, conflicting, wrong-contact, property-issue, and
  compound responses.

Historical material supplies semantic seeds, not production clearance. Every
seed is sanitized and labeled `production_report`, `production_history`,
`production_model_misread`, or `synthetic_near_miss`.

### Variation contract

Each scenario record contains:

```text
scenarioFamily
productionPattern
semanticFacts
expectedEvents
forbiddenEvents
expectedReplyPolicy
tone
register
wording
informationOrder
quoteStyle
attachmentBundle
turnTiming
variantId
bodySha256
lastProductionUse
```

The browser runner chooses an unused variant or creates a fresh sanitized
paraphrase preserving the semantic facts. Exact broker-response bodies may not
repeat across production runs. Repeating a semantic scenario is required for
regression confidence; repeating its wording is prohibited.

The minimum variation pool for each high-risk family is:

- helpful, terse, rushed, frustrated, formal, and rambling tone;
- direct, hedged, typo-heavy, regional, forwarded, quoted-history-heavy, and
  multi-intent phrasing;
- facts before the request, request before facts, correction in a later turn,
  and stale contradictory facts only in quoted history;
- body-only, one PDF, several attachments, link-only, protected/broken link,
  and an attachment for another property;
- immediate reply, delayed reply, second reply before processing, manual user
  continuation before retry, and follow-up due during a state transition.

Every feature stamp includes at least one historic semantic seed, one generated
fresh variant, one negative control, and one cross-feature collision. Touched
send-risk features additionally rotate at least three values across phrasing,
thread shape, timing, and data quality from the preceding release.

## Clearance row contract

Every scenario is graded through this ordered chain:

```text
campaign control
  -> source trigger
  -> role-bound context
  -> classification and response policy
  -> Firestore transition
  -> notification/operator action
  -> outbox and Graph effect or explicit no-send
  -> Sheet effect or explicit no-write
  -> audit/recovery evidence
  -> deployed feature stamp
```

Each row records:

- feature, release lane, trigger, and adversarial variants;
- exact sender/addressee/client/property/source bindings;
- expected classification, reason, and response/no-response policy;
- Firestore before/after state and ordered-transition requirements;
- notification label, severity, actionability, and allowed operator controls;
- locally verified email/recipient/Cc/subject/signature/attachment contract, with
  only role aliases, opaque identifiers, counts, and hashes committed;
- exact Sheet cells, formulas, row identity, and provenance contract;
- idempotency, ordering, retry, stop, and recovery behavior;
- frontend, Functions, and backend production revisions;
- scenario/variant IDs and exact outbound/inbound body hashes;
- automated, browser, Firestore, Graph, Sheet, audit, and rollback evidence;
- current status and blockers.

## Evidence and status

Evidence levels:

- `E0`: assertion or static inspection.
- `E1`: focused unit test.
- `E2`: integrated application replay with controlled provider boundaries.
- `E3`: deployed artifact, revision, configuration, traffic, and rollback
  readback.
- `E4`: production browser scenario plus mailbox, Firestore, Graph, Sheet,
  notification, audit, queue, scheduler, and recovery readback.

Feature state progresses only as follows:

```text
BLOCKED -> CODE_GREEN -> DEPLOYED_DARK -> BROWSER_PASS -> PROD_CLEARED
```

Any state may return to `BLOCKED` when production changes, evidence expires, or
a mismatch is found. The prior immutable passing checkpoint remains historical
evidence and is never rewritten. `OFF_SAFE` may also return to `BLOCKED` when a
route, entitlement, UI surface, or indirect trigger changes.

An advanced feature may move from `BLOCKED` to `OFF_SAFE` only when the UI,
route, worker, entitlement, and indirect trigger are all proven unreachable.
No percentage or average can compensate for a P0 failure.

## Durable checkpoint contract

Every milestone produces two immutable commits:

1. A code checkpoint after red/green verification and review.
2. An evidence checkpoint after deployment and production readback.

Both are pushed immediately. The evidence entry records:

```text
checkpointId, featureId, productionBaseSha, candidateSha, ciRun
artifactDigest, revision, configHash, trafficPercent, rollbackTarget
browserScenarioIds, messageVariantIds, exactBodyHashes
firestoreReadback, graphReceipt, sheetReadback, auditReadback
defects, rollbackResult, status, evidenceSha
```

Committed evidence contains no raw recipient, sender, customer, property, or
message-body PII. Exact values are verified transiently during the controlled
run; repository evidence retains role aliases, opaque IDs/hashes, counts, and
sanitized outcomes. A PII scan is mandatory before each evidence commit.

Failed checkpoints are retained with their reproduction and next action. They
are never edited into a passing record. Previously `PROD_CLEARED` feature
stamps stay closed unless a dependency or production change invalidates them;
invalidation creates a new ledger entry and a visible downgrade.

## Release architecture

The current backend candidate is 90 commits and 96 files beyond the deployed
backend. It remains dark and is preserved as reviewed architectural work. It
is not promoted as one release. Production fixes branch from the exact deployed
baseline and cherry-pick or reimplement only the smallest reviewed vertical
slice needed for the next feature stamp.

The frontend candidate and Functions candidate have an incompatible rollout
contract: either half breaks campaign creation when paired with current
production. The fix is a versioned compatibility path:

1. Deploy an authenticated `apiV2`/callable path without changing current UI.
2. Prove the dark endpoint and rollback configuration.
3. Deploy Hosting that explicitly selects `apiV2`.
4. Browser-prove campaign creation and Sheet compensation.
5. Retire the legacy path only after stale-client traffic is zero.

Production milestones are therefore narrow and frequent, but never half of an
incompatible frontend/backend pair.

## Milestone order

1. **R0 — Source authority:** map GitHub to exact deployed Hosting, Functions,
   Cloud Run, workflows, configuration, and rollback revisions.
2. **R1 — Release rails:** exact-SHA CI, versioned coordinated deploys, dark
   traffic, browser-session preflight, and automated postdeploy readback.
3. **R2 — Notification controls:** authenticate and normalize resolve,
   dismiss, clear, counts, pause/resume, outbox, and audit semantics. This may
   deploy dark before R3, but final browser clearance waits for an R3-R4
   browser-launched self-owned campaign; retained customer records are never
   used to manufacture action proof.
4. **R3 — Campaign start:** versioned launch command, sender/mailbox health,
   capability separation, transactional publication, quota reservation,
   worksheet identity, and Sheet compensation.
5. **R4 — Event/action lifecycle:** immutable source-bound receipts, visible
   call/tour/question actions, projection recovery, dedupe, and completion.
6. **R5 — Operational truth:** reconcile indexes, orphan children,
   stopped-campaign roots, missing-root notifications, outbox/audit/Graph
   order, health, and completion obligations.
7. **R6 — Conversation feature stamps:** run fresh historical-style variants
   for extraction, attachments, availability, alternatives, contact/compliance,
   questions, calls, tours, follow-ups, actions, and completion.
8. **R7 — Self-canary and staged return:** exact-SHA production campaign,
   rollback proof, independent evidence review, and explicit cohort decision.

The advanced authority work already built on the dark candidate is paused
unless a narrow production milestone needs it. This prevents more days of
provider-free architecture from displacing the product paths users actually
touch.

## Browser preflight result on approval day

The dedicated browser was controllable: it exposed the production login DOM,
resolved exactly one Pricing button, navigated to `/pricing`, returned to
`/login`, and was left ready for Microsoft sign-in. No form, campaign, email,
or production record was created.

The authenticated state was absent in that browser. Parallel Chrome control
timed out twice before a dashboard tab could be controlled. Therefore R1 must
prove a stable dedicated authenticated session while Baylor uses a separate
browser before any production campaign is attempted.

## User-return gate

Jill or any wider customer cohort remains closed until:

- all core rubric rows are `PROD_CLEARED` at the exact deployed revisions;
- advanced rows are `PROD_CLEARED` or `OFF_SAFE`;
- no P0 or P1 issue is unresolved;
- Firestore integrity and queue reconciliation are clean or explicitly
  quarantined outside active campaigns;
- browser, mailbox, Graph, Sheet, notification, audit, scheduler, health, and
  rollback evidence agree;
- an independent reviewer approves the immutable evidence checkpoint; and
- Baylor makes the explicit cohort-release decision.
