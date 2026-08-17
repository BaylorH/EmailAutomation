# SiteSift Production Automation Certification — Design

**Status:** approved in conversation on 2026-08-17
**Deliverable:** both — production code plus revision-bound capability findings
**Starting production anchor:** `1a20ba44a46e0aeed7620a6408856c0aacf6c7d9`
**Mission:** prove and improve SiteSift as a property-outreach automation product, one deployed business capability at a time, without depending on browser clicks or real mailbox delivery.

## Product definition of done

SiteSift succeeds when it accepts a configured property spreadsheet and, for every property, reaches one correct outcome:

- complete / viable;
- non-viable with a grounded reason; or
- explicit human review when the evidence is ambiguous or the action requires judgment.

It should reach that outcome using the fewest necessary professional messages, preserve every human-entered or formula-owned value, and leave accurate evidence in the correct Sheet row.

Zero-tolerance failures are:

- wrong recipient, row, property, suite, attachment, or sender identity;
- invented or unsupported facts;
- formula, identity, sibling-row, or human-value damage;
- asking again for facts already supplied, declined, or known;
- sending after reply, completion, stop, opt-out, or escalation;
- duplicate sends, writes, files, notifications, or terminal actions;
- hidden failure or silent limbo;
- unnatural or reputation-damaging language; and
- certification effects outside the isolated fixture or residue after cleanup.

## Decision

Build a private production-resident certification adapter in a dedicated Cloud Run certification twin. The twin runs the exact immutable container digest staged for `process-user`, but has a certification-only service account and fixture-only resource authority. It replaces browser/mailbox acquisition with an approved parameterized scenario, final external email delivery with a deterministic capture transport, and public Drive-permission publication with a deterministic would-publish capture. All matching, extraction, safety, lifecycle, Firestore, Sheets, private fixture-Drive work, retry, and audit behavior between those boundaries remains shared with ordinary production.

The twin exposes closed prepare, run, status, bounded human-review, abort, recover, and cleanup operations. Exact caller `sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com` has only private invoke plus exact fixture-config-secret read authority; it cannot write Firestore or call the frontend adapter directly. `POST /certification/prepare` accepts only approved `scenarioId`, unique `runId`, and `expectedRevision`. The runtime obtains the canonical payload from the in-image closed registry or, for spreadsheet admission, from private `certifyCampaignInput` invoked only by the certification runtime. It creates a permanent `PREPARING` record, then atomically creates `PREPARED` plus one-use authorization/input. `run` transactionally consumes both while moving to `CLAIMED`. Status/abort/recovery resolve interruption without repeat execution; cleanup-only repair can remove terminal residue without changing verdict. A bounded review phase shows Baylor an ordered transient list of every captured synthetic subject/body and requires one verdict per body digest; durable evidence retains only the ordered digest/rubric/verdict/safe-reason set.

The request never accepts arbitrary users, clients, recipients, message bodies, spreadsheet IDs, or resource locations. The canonical runtime registry lives at `email_automation/certification/scenario_registry.json`, is included in the image, and is the single source loaded by the route, runner, tests, and ranker. `scenarioRegistryDigest` is SHA-256 over its SiteSift-canonical-JSON-v1 bytes. Every one of the 91 capability scenarios has one complete manifest definition: logical fixture alias, oracle-projection alias, exact required/forbidden cardinalities, expected verdict, capability-stamp flag, producer kind, launch class, repeat count, and review flag/rubric. The runtime registry must match those objects one-for-one; it may not inherit a missing value. Aliases are safe repository names only—concrete client, recipient, Sheet, Drive, thread, event, and state identifiers exist solely in the bound numeric fixture-config secret. The only producer kinds are `backend_registry_v1` and `frontend_functions_adapter_v1`. The backend producer-artifact digest is SHA-256 over canonical `{kind, backendSourceRevision, scenarioRegistryDigest, scenarioId}`. The frontend producer-artifact digest is SHA-256 over independently read canonical `{kind, functionName, frontendSourceSha, firebasePackageHash, revision, imageDigest}`; adapter self-report is comparison-only. In both cases the authoritative `canonicalInputDigest` is computed by the Python runtime over SiteSift-canonical-JSON-v1 payload bytes and the sealed record stores those immutable bytes, never a caller-owned nested object. Execution binds scenario/run, source and image, both service revisions, production candidate, verified OIDC caller, immutable fixture-secret version and safe digest, producer kind, input digest, producer-artifact digest, and expiry into one immutable authorized-run identity and evidence preimage. With no prepared record, wrong verified caller, or wrong service, the route is inert.

Every authorization has an exact immutable schema and SHA-256 over all canonical fields except the digest field itself. Expiry is whole-second UTC RFC3339. CLAIMED/RUNNING carries a fencing generation checked before every provider/store effect. Every certification provider wrapper also uses a bounded deadline no longer than 60 seconds and registers an in-flight operation before contact, clearing it only after a conclusive provider response. A client timeout, connection loss, or cancellation is ambiguous: the registration remains, the run and its per-run resource partition move to `QUARANTINED`, and cleanup, reuse, terminal `PASS`, and capability stamping are forbidden. Recovery requires provider-server age beyond the 540-second service timeout plus a 180-second margin, expired lease, and an in-transaction second read; it CAS-revokes/increments the generation and enters `QUIESCING`. A conclusive old-generation operation waits at least the 60-second maximum in-flight duration plus a 15-second margin and reaches zero registrations before cleanup. An ambiguous operation is cleared only by provider-specific authoritative terminal evidence tied to its operation/idempotency marker; an absence-only snapshot is never sufficient. If the provider exposes no such proof, the fixture stays quarantined and the result remains `INSTRUMENT_BLOCKED:ambiguous_provider_effect` until a separately authorized disposal/reconciliation path resolves it—never a stamp. A call that passes its generation check and later times out while the provider commits after timeout is a required race test. Cleanup is allocated before fixture construction and runs before every terminal result or review expiry except this explicit quarantined-ambiguity state; `AWAITING_REVIEW` is the sole time-bounded 24-hour deferral and retains only its cleanup-owned transient review artifact. Raw exceptions are discarded; evidence carries only an allowlisted failure phase/code.

Agent-safe execution never sends fixture content to OpenAI and never creates a public Drive permission. Deterministic/no-model packs may run autonomously on the twin. A model-dependent scenario is marked `INSTRUMENT_BLOCKED:user_runtime_launch_required` until Baylor manually launches the exact printed product-runtime command; the agent may then inspect the sanitized evidence read-only. Public-link publication remains a separately named `NOT_TESTED` provider shell: certification proves the selected file, exact would-publish request, and zero actual public permissions, but does not claim that the external share was created.

## Why this approach won

### Rejected: seed Firestore and call `/process-user`

`refresh_and_process_user()` mixes outbox sending, mailbox scanning, Sent Items reconciliation, stored-failure replay, pending responses, follow-ups, cleanup, and health. It requires mailbox credentials and makes one failed business capability hard to isolate.

### Rejected: real browser and mailbox canaries as the primary proof

They prove transport and presentation but are slow, noisy, timing-sensitive, and unnecessarily expose external-effect risk. They obscure whether a failure belongs to SiteSift's automation or a provider shell.

### Rejected: separate local or shadow implementation

A second implementation can pass while production fails. Localhost, mocks, health checks, source review, and an undeployed shadow service are development evidence, not a production capability stamp.

### Chosen: request-scoped input and effect adapters in the serving code

The deployed revision processes approved raw-shaped fixtures through the same canonical code. Request-scoped dependencies avoid a global test-mode flag and concurrent-request leakage. Real provider/UI checks remain optional, separately labeled transport evidence.

## Production boundary

```text
browser form / Graph mailbox
             |
             v
     production input adapter
             |
             v
   canonical campaign/message/action
             |
             v
 SAME DEPLOYED SITESIFT BUSINESS LOGIC
             |
             v
 Firestore / Sheets / Drive / AI / audit
             |
             v
 production Graph delivery OR certification capture
```

One-for-one means identical behavior from the canonical input boundary onward. A capture-run stamp does not claim browser rendering, Microsoft delivery, mailbox subscription delivery, or provider availability. Those are optional thin-shell stamps.

## Required architecture

### Request-scoped runtime

Create a small runtime dependency object carried explicitly through the active call path. Ordinary production receives the normal Graph source/delivery and production clocks/counters. Certification receives approved fixture input, a fixed clock where needed, an isolated counter namespace, and capture delivery.

Never use a mutable module-level `test_mode`, monkey-patch production globals in the deployed service, or select behavior from caller-supplied arbitrary values.

### Canonical inbound message

Extract Graph hydration from `process_inbox_message()` and `_save_message_to_thread()` into one reusable source adapter. Both Graph acquisition and an approved fixture must produce the same immutable canonical envelope. The parser, thread matching, source authority, attachment handling, and all downstream logic remain shared.

### Final outbound capture

Introduce a narrow delivery interface at the last common boundary after SiteSift has selected the recipient, subject, body, signature, CC/BCC, attachments, safety result, and idempotency intent. Certification records the would-send payload and returns a synthetic provider receipt. It must preserve the same durable-intent / send / reconciliation state machine.

Every sending lane must use the boundary: initial outreach, automatic replies, pending replies, dashboard/manual replies, follow-ups, and any recovery lane permitted by the scenario. A stamp is invalid if an alternate Graph send can bypass capture.

Introduce a second narrow `DrivePublicationTransport` immediately around `permissions.create(type="anyone")`. Ordinary production uses the provider implementation. Certification validates the exact fixture file ID and permission body, records a would-publish receipt, and performs no provider permission mutation. Direct before/after reads must prove zero public permissions on every fixture file. PDF/link extraction and private fixture-Drive behavior can earn a core stamp; the external public-link shell stays `NOT_TESTED` until separately authorized and observed.

Model inference is also an explicit effect boundary. The twin contains the same model configuration as the candidate, but an agent-safe run uses a deny/capture transport and cannot earn a model-dependent business stamp. Those scenarios require a user-launched product-runtime invocation; their evidence must identify that launch class without storing fixture prompts, model input, or raw output. Local deterministic inference remains development evidence only.

### Exact-image certification twin and fixture scope

The registry owns only logical aliases for one dedicated certification identity and isolated client, Sheet, Drive folder, thread IDs, event IDs, and state prefix. The bound numeric fixture-config secret is the sole owner of the concrete identifiers; callers cannot choose them and source/image/manifest scans reject any leaked concrete value. `process-user-certification` runs the exact container digest of the untagged 0% candidate under `sitesift-certification-runtime@email-automation-cache.iam.gserviceaccount.com`. The exact operator is `sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com`. The release principal is resolved only from the existing local release-controller identity source, and the two existing controller constants must agree; these artifacts and durable evidence retain only its canonical member digest, never the raw principal. Provider IAM is compared to that resolved member, which receives resource-level Service Account Token Creator only on the operator account. The CLI impersonates the operator with `--include-email`; the route validates Google signature, issuer, exact audience, `email`, `email_verified=true`, and the independently pinned numeric operator service-account `uniqueId` as `sub`. It hashes the canonical verified email+uniqueId into evidence. Every other principal or claim mismatch is rejected.

The operator has private Cloud Run invoke on the twin and Secret Accessor on only `sitesift-certification-fixture-config`; application logic reads only the configured immutable numeric version and rejects aliases/drift. It has no Firestore, Sheet, Drive, queue, mailbox, send, AI-secret, or other-secret role. The twin runtime owns preparation and atomic database transitions. It has no production/default Firestore, queue, mailbox/Graph credential, or send authority. It can access only database `sitesift-certification`, exact fixture Sheet/private folder, the existing AI-secret reference required for candidate parity, and the one fixture-config secret. The separate frontend adapter runs as `sitesift-certification-input@email-automation-cache.iam.gserviceaccount.com`, is invoked only by the twin runtime, and may read only that fixture-config secret and exact fixture Sheet; it has no database, production-resource, write, send, queue, Drive, or AI authority. Broad roles and access to a second secret are forbidden.

Fixture configuration uses an enabled immutable numeric Secret Manager version matching `^[1-9][0-9]*$`, never `latest`. Its payload is parsed only by bounded SiteSift canonical JSON v1; the version and safe digest bind authorization, evidence, and invalidation. The ordinary candidate and twin both carry exact must-equal `SITESIFT_SOURCE_REVISION` and `SITESIFT_IMAGE_DIGEST` values; the external verifier independently compares them to Git and Cloud Run Admin API image/status readbacks. Unknown config differences block the stamp. The normalized comparator first validates all required candidate-only omissions, twin-only additions, and exact identity differences; it then replaces each validated asymmetry with paired sentinels before deep comparison and never drops an unknown key.

All ordinary mutating routes (`/process-user`, `/process-outbox`, and successors) are inert when `K_SERVICE=process-user-certification`; every `/certification/*` route is inert on ordinary `process-user`; health is the only shared route. Request-scoped clients fence Firestore paths (including snapshot references, transactions, and batches), Sheet A1 and grid/batch requests, Drive parents, and Drive permission bodies before provider calls. The runner proves one-use preparation/claim, unconditional handoff cleanup, replay, and zero residue. Process isolation, least-privilege IAM, route fences, and wrappers form independent containment layers.

### Evidence and verdicts

Every scenario declares required and forbidden effects before execution. The runner records a sanitized evidence artifact containing:

- full Git commit, production and twin revisions, shared immutable image digest, strict configuration comparison, immutable fixture-secret version plus safe canonical-content digest, and relevant prompt/model identity;
- scenario ID, run ID, input hash, and expected-effect hash;
- before/after projections from every relevant store;
- captured outbound cardinality and safe envelope summary;
- replay delta;
- cleanup and zero-residue result; and
- `PASS`, `FAIL`, `INSTRUMENT_BLOCKED`, or `NOT_TESTED`.

A successful HTTP response is never sufficient. Provider/local state and expected/observed effects must reconcile exactly.

## Universal stamp contract

Every capability must:

1. Bind to the exact production candidate/promoted revision, certification-twin revision, shared immutable image digest, strict canonicalized configuration comparison, immutable fixture-secret version, safe fixture-config digest, input-producer kind, canonical-input digest, and input-producer artifact digest.
2. Replace only the approved transport boundary.
3. Run ordinary deployed business logic below that boundary.
4. Declare required and forbidden effects first.
5. Read every relevant state store after execution.
6. Replay the same event and require zero additional effect.
7. Repeat every model-dependent scenario exactly three times with fresh run IDs after an explicit user-launched product-runtime invocation; all three must pass with bound prompt/requested/resolved-model identity, and agent-safe automation must stop before any external AI call.
8. Clean the exact fixture and prove zero residue.
9. Retain a sanitized revision-bound result.
10. Fail an intentionally wrong oracle or hostile-scope scenario, proving the harness can detect failure.

A twin stamp proves the exact deployed image's business behavior under the approved fixture boundary. It does not by itself prove the ordinary production identity's access to customer resources; the existing release controller separately proves production configuration, IAM, health, and traffic before promotion and again afterward.

## Ranked frontier, not a deleted backlog

The backlog remains the complete, effectively infinite memory of defects, improvements, ideas, and deferred work. It is not the execution order.

Maintain a small ranked frontier:

- exactly one active business capability stamp;
- at most one independently required instrumentation blocker;
- everything else stays ranked but inactive;
- new findings are captured immediately, scored, and parked unless they outrank the active work on direct user value or safety;
- old backlog work re-enters only when a failed stamp proves it blocks the spreadsheet-to-decision mission; and
- after every stamp or failure, recompute priority from production evidence.

Ranking order:

1. wrong external effect or customer/data safety;
2. inability to complete the core spreadsheet-to-decision job;
3. incorrect extraction, row binding, state, or message behavior;
4. hidden failure, duplicate, or recovery weakness;
5. user-facing communication quality and efficiency;
6. performance and cost;
7. adjacent capabilities, infrastructure polish, and convenience.

Results, Maps, Tour Scheduling, broad UI redesign, general repository cleanup, worker migration, and dormant native-image enablement do not outrank an unstamped core capability.

The frontier is machine-readable. Each capability records its dependencies, current stamp, blocking defect, business weight, safety weight, changed-code triggers, and evidence revision. Each newly deployed revision invalidates only stamps whose declared code/effect dependencies changed, then the ranker selects the highest-priority unmet or invalidated capability. A newly discovered issue is appended to the full backlog and linked to the capability it blocks; it does not become active merely because it is new.

## Autonomous mission loop

The executor's terminal condition is not “one task completed.” It continues without asking for routine approval through local work, IAM-private fixture/twin deployment, deterministic scenarios, readbacks, fixes, and reranking. It never pushes to a public Git remote, deploys a public/customer surface, changes production traffic, calls real AI, or creates a public Drive permission. At those boundaries it prints one exact Baylor command, records the blocker, and resumes read-only afterward. Public Drive sharing remains a separately labeled `NOT_TESTED` shell.

```text
read production/twin revisions, shared image, and frontier
        |
select highest-ranked unmet capability
        |
RED -> smallest shared-code fix -> regressions -> review -> commit
        |
manual push parity -> Baylor stages ordinary 0% candidate -> private twin -> safe run or user handoff -> replay -> cleanup
        |
record stamp/failure -> invalidate affected stamps -> re-rank
        |
continue until whole-product terminal condition
```

Ordinary test failures, review findings, deployment corrections, and certification failures are work, not reasons to return control. If context, session, or usage capacity ends, the executor commits safe progress and writes a durable resume handoff containing the exact active capability, evidence, blocker, and next command. A fresh session resumes the same frontier rather than rediscovering or choosing another backlog item.

## Capability sequence

1. Certification integrity and containment.
2. Spreadsheet admission and column mapping.
3. Authoritative known/missing/optional field contract.
4. Initial outreach content, recipient safety, identity, and dedupe.
5. Thread and property binding.
6. Plain-text extraction, corrections, and Sheet integrity.
7. Complete / non-viable / review decisions.
8. Natural immediate replies and closure.
9. Native-text PDF, scanned PDF, links, and separately enabled asset types.
10. Escalation and parameter-driven operator actions.
11. Follow-up timing, stop, cancel, opt-out, and redirect.
12. Retry, reordering, concurrency, fault recovery, and visibility.
13. Integrated multi-property scrub.
14. Optional Graph-delivery and browser-rendering checks after core certification.

## Capability acceptance matrix

### 0. Certification integrity

Required: exact revision, fixture-only writes, complete evidence, capture-only outbound behavior, replay, cleanup.
Forbidden: arbitrary scope, customer data, real delivery, unknown recipient, untagged writes, residue.
Stamp: hostile scope, dirty fixture, wrong revision, wrong oracle, replay omission, and cleanup failure all block `PASS`.

### 1. Spreadsheet admission

Input: workbook-shaped data containing aliases, custom columns, formulas, blank/invalid rows, duplicates, and multiple tabs.
Required: each valid property exactly once, deterministic mapping, visible ambiguous/invalid rows, formulas marked non-writable.
Forbidden: silent row loss, guessed mapping, duplicate client/row/work, formula ownership loss.

### 2. Field contract

Input: rows with known, missing, optional, accept-only, note, skipped, identity, and formula fields.
Required: exact authoritative missing-field set and order.
Forbidden: asking for known, skipped, identity, formula, unsupported, or declined fields.

### 3. Initial outreach

Input: campaign, row, contact, user identity, signature, and approved script context.
Required: one captured professional message to the exact contact, correct property/name/city, exact missing topics, one signature.
Forbidden: placeholder, wrong To/CC, any BCC, invented fact, confidential disclosure, Tour/LOI commitment, duplicate.

### 4. Thread and property binding

Input: exact and ambiguous message envelopes, same-broker multi-property variants, changed subjects, and negative matches.
Required: exact target or one review action.
Forbidden: wrong-row mutation, loose match, reply or update on ambiguity.

### 5. Text extraction and corrections

Input: complete, partial, vague, conflicting, corrected, ranged, declined, and long-history replies.
Required: every grounded supported fact; latest explicit correction wins; declines and known values persist.
Forbidden: hallucination, stale-value victory, field confusion, vague-to-fact conversion, read-only overwrite.

### 6. Attachments and links

Input: native-text/scanned PDFs, links, mixed-property, wrong-address, multi-suite, unsupported/private URLs; native images only when separately enabled.
Required: target-bound values/assets in correct columns or one quarantine; exact selected private fixture file and exact captured would-publish permission request.
Forbidden: cross-property/suite extraction, private-network fetch, partial batch effects, duplicate files/links, or any actual public permission during agent-run certification.
Each asset type receives a separate stamp. Public-link publication is a separate provider shell and remains `NOT_TESTED`; disabled native images remain `NOT_TESTED`.

### 7. Sheet integrity

Input: row reordered after thread creation, formulas, human values, notes, assets, and sibling rows.
Required: only predeclared cells change; formulas recalculate; notes/provenance append once.
Forbidden: sibling/wrong-row mutation, formula overwrite, identity or human-value loss.

### 8. Property decision

Input: complete, unavailable, poor-fit, alternative-property, still-missing, and ambiguous replies.
Required: exactly one coherent complete/viable, non-viable, or review state across Sheet, thread, audit, clocks, and highlights.
Forbidden: unsupported terminal decision, contradictory state, lost same-message fact, continued follow-up.

### 9. Reply language

Input: partial, complete, unavailable, correction, call request, confidential question, wrong contact, opt-out, alternative property.
Required: concise professional reply that thanks appropriately and asks only what remains.
Forbidden: robotic/internal wording, repeat question, false promise, confidential answer, automatic reply where judgment is required.
All deterministic invariants must pass and every finite scenario must receive a human naturalness verdict; averages cannot hide one severe message.

“Human naturalness verdict” means Baylor reviews each bounded captured synthetic message against the fixed rubric: ordinary broker-facing English, concise, correct tone, no internal/system wording, repeated question, awkward fragment, or false promise. `/review-input` is Baylor-manual only; agent mode refuses it and never captures its stdout or raw text. The manual command submits only body hash, rubric version, pass/fail, and safe reason. It cannot waive deterministic safety, and one severe message fails the pack. Until submitted, the scenario is `INSTRUMENT_BLOCKED:user_review_required`.

### 10. Follow-up timing

Input: fixed clocks before/at/after due time, weekend boundaries, inbound/manual reply before due, and max attempts.
Required: one follow-up only when due, with exact remaining questions.
Forbidden: early/post-reply/post-terminal/post-opt-out/stale/excess follow-up.

### 11. Escalation and operator actions

Input: calls, negotiation/legal questions, low confidence, mixed property, provider failure, then approve/edit/stop/resume parameters.
Required: one actionable review item; accepted action applies once to the right thread.
Forbidden: dropped question, automatic commitment, duplicate action, wrong-thread resume.

### 12. Stop, cancel, opt-out, redirect

Input: each control, including a race with worker claim.
Required: queues/clocks freeze and the authority applies across every outbound lane.
Forbidden: later send, resurrection, wrong-contact continuation, hidden pending work.

### 13. Retry and recovery

Input: failures around AI, Sheet, Drive, audit, capture, claim, and response plus duplicate/reordered delivery and lease collision.
Required: exactly one converged outcome or one visible actionable failure.
Forbidden: duplicate effect, swallowed failure, split-brain state, evidence deletion.

### 14. Whole scrub

Input: one campaign combining complete, partial, unavailable, correction, alternative, attachment, ambiguity, opt-out, silence, and failure cases.
Required: every property reaches the expected decision or review using minimal communication and coherent state.
Forbidden: cross-row effects, orphaned work, hidden unresolved properties, redundant messages.
Stamp only after every prerequisite capability is green on the same immutable image digest and its bound production/twin revisions, or a reviewed compatible successor whose declared dependencies did not change.

## Builder loop

For one active capability only:

1. Write the business-level RED.
2. Prove it fails for the intended missing behavior.
3. Make the smallest shared-production-logic change.
4. Run focused and neighboring regression tests.
5. Review and commit one understandable behavior.
6. Commit/review locally; Baylor performs the public Git push and any ordinary `process-user` 0%-staging or production-traffic command. The agent then verifies parity/readback and may deploy only the exact already-tested digest to the IAM-private fixture-only certification twin.
7. Invoke the exact production certification scenario.
8. Read required and forbidden effects.
9. Replay and clean up.
10. Record the stamp.
11. If it fails, fix only that defect and rerun the failed stamp plus affected prerequisites.
12. Re-rank the frontier before activating the next capability.

## Refutation condition

This design is refuted if certification can pass while using a second copy of business logic or a different image digest, can touch a non-fixture resource, can bypass an outbound lane, cannot distinguish required from forbidden effects, cannot bind evidence to both deployed revisions and their shared image, or cannot prove replay and cleanup. It is also refuted if the ranked frontier permits unrelated backlog work to interrupt an unstamped higher-value core capability without a recorded user-value or safety reason.
