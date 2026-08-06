# Browser-First Production Clearance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear SiteSift's production campaign workflow feature by feature using the live frontend, varied historical-style broker conversations, exact production readbacks, and durable GitHub/deployment checkpoints.

**Architecture:** Keep the oversized backend candidate dark and preserve its reviewed work. Ship narrow vertical production slices from the exact deployed backend/frontend baselines, using versioned compatibility paths whenever Hosting, Functions, and worker changes must coordinate. The existing feature gradebook is the single behavioral rubric; an immutable state/checkpoint ledger records progress without overwriting prior evidence.

**Tech Stack:** React, Firebase Hosting and Functions, Firestore, Python/Flask, Cloud Run, Microsoft Graph/Outlook, Google Sheets/Drive, GitHub Actions, Node test runner, Jest, Python unittest/pytest, Codex Browser control.

---

## Authoritative inputs and worktrees

Backend planning/candidate worktree:

```text
/Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/sitesift-m3-b1-source-authority-20260803
```

Frontend candidate worktree:

```text
/Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/sitesift-canonical-formula-20260802
```

Exact deployed baselines:

```text
backend:  92de8ab5bf841f5faa453de34cc3846bd65611af
frontend: 52aa66299751893bbf9ec596d7fa84c5a767933d
```

Active design and state:

- `docs/superpowers/specs/2026-08-06-browser-first-production-clearance-design.md`
- `docs/release-safety/production-clearance-state.json`
- `docs/release-safety/feature-gradebook.json`

Do not resume the superseded milestone order in
`docs/superpowers/plans/2026-08-04-sitesift-production-clearance-train.md`.
Its architecture and exact-SHA evidence remain valid inputs.

## Non-negotiable execution rules

1. Use the browser for every production product action. Direct Firestore,
   Graph, Sheet, deployment, and GitHub access is readback only unless a
   separately reviewed migration explicitly authorizes a repair.
2. Never create production state by calling an application endpoint directly,
   inserting Firestore work, or reusing the local simulated-browser E2E as
   production proof.
3. Run a red test before changing production behavior. Capture the expected
   failure, implement the smallest vertical fix, then run focused and retained
   suites.
4. Push the code checkpoint before deployment. After deployment, verify exact
   revision/digest/config/traffic, run browser proof, append evidence, commit,
   and push the evidence checkpoint.
5. Never deploy one half of an incompatible Hosting/Functions contract.
6. Never reuse an exact broker-response body in a later production run. Record
   `variant_id` and SHA-256 body hash.
7. A failed checkpoint remains immutable and `BLOCKED`; create a new checkpoint
   for the fix. Never edit a failure into a pass.
8. Only an exact self-owned recipient explicitly authorized in the current
   execution turn may receive an agent-run test. No customer address is used.
9. Stop on the first cross-system mismatch. Preserve all evidence; do not
   delete or repair the test record inside the same proof run.

## Task 1: Make the clearance contract executable

**Files:**

- Create: `tests/test_production_clearance_state.py`
- Modify: `docs/release-safety/production-clearance-checkpoints.jsonl`
- Create: `tests/fixtures/production_browser_conversation_variants.json`
- Create: `tests/test_production_browser_conversation_variants.py`
- Create: `scripts/select_production_browser_variant.py`
- Create: `scripts/scan_clearance_evidence_pii.py`
- Create: `tests/test_scan_clearance_evidence_pii.py`
- Modify: `docs/release-safety/feature-gradebook.json`
- Modify: `docs/release-safety/production-clearance-state.json`

- [ ] **Step 1: Write the state-contract RED**

Create assertions that the state file points to exactly one authoritative plan
and rubric, contains R0-R7 once, permits only the declared status transitions,
contains every required core/advanced feature stamp exactly once, has no
duplicate feature stamps, and records a browser preflight. Every status may be
invalidated to `BLOCKED`; only the declared forward transitions are allowed.

```python
def test_clearance_state_has_one_authoritative_control_path():
    state = load_json(STATE_PATH)
    assert state["authoritativePlan"].endswith(
        "2026-08-06-browser-first-production-clearance.md"
    )
    assert state["canonicalRubric"].endswith("feature-gradebook.json")
    assert [row["id"] for row in state["milestones"]] == [
        "R0", "R1", "R2", "R3", "R4", "R5", "R6", "R7"
    ]
    assert state["executionPolicy"]["productActions"] == "browser_only"
    assert len({row["featureId"] for row in state["featureStamps"]}) == len(
        state["featureStamps"]
    )
    assert "BLOCKED" in state["allowedTransitions"]["PROD_CLEARED"]
```

- [ ] **Step 2: Run the RED**

Run:

```bash
python3 -m unittest tests.test_production_clearance_state -v
```

Expected: failure because the validator/test module does not yet exist.

- [ ] **Step 3: Define the browser-variant schema and initial sanitized families**

The JSON root must be:

```json
{
  "schemaVersion": 1,
  "historicalSources": [],
  "scenarioFamilies": [],
  "productionUse": []
}
```

Each variant must include:

```json
{
  "variantId": "availability.quoted_correction.001",
  "scenarioFamily": "property_availability",
  "sourceClass": "production_model_misread",
  "semanticFacts": {},
  "expectedEvents": [],
  "forbiddenEvents": [],
  "expectedReplyPolicy": "continue",
  "axes": {
    "tone": "terse",
    "register": "informal",
    "informationOrder": "correction_then_facts",
    "quoteStyle": "outlook_top_post",
    "attachmentBundle": "none",
    "turnTiming": "immediate"
  },
  "body": "sanitized broker text",
  "bodySha256": "64 lowercase hexadecimal characters",
  "lastProductionUse": null
}
```

Seed at least these semantic families from the design's repository sources:

- flyer wording that must not become a tour;
- complete and partial facts with rent/OpEx/TI variation;
- true unavailable versus non-fit versus tour-unavailable;
- quoted stale terminal text followed by a fresh positive correction;
- confidential question plus useful facts;
- call request with and without a number;
- core tour offer, alternate time, and temporary tour restriction;
- another-property referral before/after original rejection;
- OOO, wrong contact, forwarded contact, and opt-out;
- attachment-only, protected link, and wrong-property attachment;
- manual mailbox continuation before retry;
- property issue severity and projection failure.

- [ ] **Step 4: Implement deterministic fresh-variant selection**

`scripts/select_production_browser_variant.py` must accept a family and a path
to the checkpoint history, reject any body hash already used in production,
and print exactly one unused record. If no unused record exists, exit nonzero
and require a new sanitized paraphrase; never fall back to a used body.

```python
def select_unused_variant(family, variants, used_hashes):
    eligible = [
        item for item in variants
        if item["scenarioFamily"] == family
        and item["bodySha256"] not in used_hashes
    ]
    if not eligible:
        raise RuntimeError(f"no unused production variant for {family}")
    return sorted(eligible, key=lambda item: item["variantId"])[0]
```

- [ ] **Step 5: Amend the gradebook with enforceable policies**

Add `browserExecutionPolicy`, `historicalSeedPolicy`, `clearanceStatusModel`,
and `checkpointPolicy` without deleting existing event, variation, state,
interaction, or evidence coverage. Add distinct core feature entries for call
and broker-tour actions to the Production V1 suite while leaving route planning,
itineraries, and tour invites in the advanced lane. Preserve schema
compatibility unless the test and all consumers are migrated in the same
commit.

- [ ] **Step 6: Add a fail-closed evidence PII scanner**

The scanner rejects email addresses, raw message bodies, personal names from a
deny-list supplied at runtime, and unapproved property addresses in committed
checkpoint/evidence files. It allows SHA-256 values, opaque IDs, role aliases,
counts, and already-sanitized scenario IDs. Tests must include true positives
and negative controls proving hashes are not mistaken for PII.

- [ ] **Step 7: Run contract verification**

```bash
python3 -m unittest \
  tests.test_production_clearance_state \
  tests.test_production_browser_conversation_variants \
  tests.test_scan_clearance_evidence_pii \
  tests.test_release_feature_registry \
  tests.test_production_v1_fixture_map -v
python3 -m json.tool docs/release-safety/production-clearance-state.json >/dev/null
python3 -m json.tool tests/fixtures/production_browser_conversation_variants.json >/dev/null
python3 scripts/scan_clearance_evidence_pii.py \
  docs/release-safety/production-clearance-state.json \
  docs/release-safety/production-clearance-checkpoints.jsonl
git diff --check
```

Expected: all tests pass; both JSON parses exit 0; `git diff --check` emits no
output.

- [ ] **Step 8: Commit and push the rubric checkpoint**

```bash
git add \
  docs/release-safety/feature-gradebook.json \
  docs/release-safety/production-clearance-checkpoints.jsonl \
  docs/release-safety/production-clearance-state.json \
  scripts/scan_clearance_evidence_pii.py \
  scripts/select_production_browser_variant.py \
  tests/fixtures/production_browser_conversation_variants.json \
  tests/test_production_browser_conversation_variants.py \
  tests/test_production_clearance_state.py \
  tests/test_scan_clearance_evidence_pii.py
git commit -m "test: enforce browser-first clearance contract"
git push origin codex/sitesift-production-clearance-20260804
```

Record the exact remote SHA in the state file in a separate evidence commit.

## Task 2: R0-R1 source authority and release rails

**Files:**

- Create: `docs/release-safety/production-release-manifest.json`
- Create: `scripts/verify_release_manifest.py`
- Create: `tests/test_release_manifest.py`
- Modify: `.github/workflows/production-clearance-ci.yml`
- Modify: `scripts/deploy_process_user.sh`
- Modify frontend: `.github/workflows/firebase-hosting.yml`
- Create frontend: `.github/workflows/firebase-functions-manual.yml`

- [ ] **Step 1: Write a manifest-validation RED**

The manifest requires exact backend/frontend commit, artifact digest, deployed
revision, configuration hash, traffic percentage, rollback target, workflow
state, and observed timestamp. Reject branch names where immutable SHAs are
required and reject a candidate without an exact-SHA successful CI run.

```python
def test_release_manifest_requires_immutable_provenance():
    manifest = load_manifest()
    assert len(manifest["backend"]["productionSha"]) == 40
    assert manifest["backend"]["artifactDigest"].startswith("sha256:")
    assert manifest["backend"]["trafficPercent"] == 100
    assert manifest["backend"]["rollbackRevision"]
    assert len(manifest["frontend"]["productionSha"]) == 40
```

- [ ] **Step 2: Capture current production truth without mutation**

Use `gcloud run services describe`, `gcloud run revisions describe`, Firebase
Hosting release metadata, Functions revision metadata, `gh run list`, and git
remote readback. Populate the manifest only from observed values. If Firebase
cannot map a Function revision to a commit, record `BLOCKED_PROVENANCE` rather
than inventing a mapping.

- [ ] **Step 3: Add exact-SHA CI and manual scoped deployment rails**

Backend CI must test the exact commit to be deployed. Functions deployment must
be manual, explicit about project/function/version, and separate from Hosting.
Hosting must never silently deploy a Functions-incompatible client.

- [ ] **Step 4: Reconcile GitHub source authority to deployed production**

Create a neutral immutable production tag at
`92de8ab5bf841f5faa453de34cc3846bd65611af`, push it, and prepare a reviewed PR
branch that brings the backend default branch to the exact deployed lineage
without a force push. Baylor manually opens the PR. Keep the scheduled
production workflow disabled throughout this source-only reconciliation. The
PR must show that the deployed commit is a
descendant of current `main`; otherwise stop and write a divergence plan rather
than manufacturing a merge.

```bash
git merge-base --is-ancestor \
  9e63704d3584966944814a93594d2be1e4b2fcb0 \
  92de8ab5bf841f5faa453de34cc3846bd65611af
git tag production/backend-20260802-92de8ab \
  92de8ab5bf841f5faa453de34cc3846bd65611af
git push origin production/backend-20260802-92de8ab
git push origin \
  92de8ab5bf841f5faa453de34cc3846bd65611af:refs/heads/release/production-source-baseline-20260806
```

Expected: the ancestry command exits 0, the tag and branch read back at the
production SHA. Prepare the PR title/body locally; Baylor manually opens the PR
because the agent may not submit external review/comment communication. The PR
remains draft until exact-SHA checks and review pass.

- [ ] **Step 5: Add postdeploy readback**

`scripts/deploy_process_user.sh` must print and verify the resulting revision,
image digest, commit tag, outbound mode, coordinator mode, traffic, and rollback
revision before declaring deployment success. A mismatch exits nonzero.

- [ ] **Step 6: Verify R0-R1 locally and remotely**

```bash
python3 -m unittest tests.test_release_manifest tests.test_process_user_production_deploy_contract -v
python3 scripts/verify_release_manifest.py docs/release-safety/production-release-manifest.json
git diff --check 92de8ab5bf841f5faa453de34cc3846bd65611af..HEAD
gh run list --branch codex/sitesift-production-clearance-20260804 --limit 10
```

Frontend:

```bash
npm ci
npm ci --prefix functions
npm run test:functions
npm test -- --watchAll=false --runInBand
npm run build:base-v1
```

Expected: local gates pass, exact candidate CI is successful, and no deploy is
performed by verification commands.

- [ ] **Step 7: Prove browser isolation under parallel use**

Authenticate the dedicated in-app SiteSift browser, read the dashboard, open
and close one non-mutating campaign detail, and verify the active sender. While
that session remains controlled, Baylor uses a different browser for unrelated
navigation. Repeat a SiteSift dashboard read and navigation. Any lost control,
profile crossover, sign-out, or sender change leaves R1 `BLOCKED`.

- [ ] **Step 8: Push the R1 code checkpoint**

Use a reviewed release-rail branch, push it, read back the remote SHA, and do
not merge directly to unprotected `main`.

## Task 3: R2 authenticated notification and action controls

**Files:**

- Modify backend: `app.py`
- Modify backend: `email_automation/notifications.py`
- Modify backend: `email_automation/messaging.py`
- Create backend: `tests/test_fe_contract_fuzz_dismiss_notification.py`
- Modify backend: `tests/test_dashboard_escalation_actions.py`
- Create frontend Functions: `functions/lib/notificationResolution.js`
- Create frontend Functions: `functions/lib/notificationResolution.test.js`
- Modify frontend: `src/contexts/NotificationsContext.js`
- Modify frontend: `src/components/NotificationsSidebar.jsx`
- Modify frontend: `src/components/InlineNewPropertyCard.jsx`
- Modify frontend: `src/components/ConversationsPanel.jsx`
- Modify frontend: `src/utils/notificationCounters.js`

- [ ] **Step 1: Create production-baseline worktrees**

Use the worktree skill and create backend/frontend release branches from the
exact deployed SHAs, not the oversized candidates:

```bash
git worktree add \
  /Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/sitesift-r2-notification-controls-20260806 \
  -b release/r2-notification-controls \
  92de8ab5bf841f5faa453de34cc3846bd65611af
git worktree add \
  /Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/sitesift-r2-notification-controls-20260806 \
  -b release/r2-notification-controls \
  52aa66299751893bbf9ec596d7fa84c5a767933d
```

- [ ] **Step 2: Write auth and semantic-action REDs**

Prove missing/invalid token performs zero reads/writes, token UID overrides body
UID, cross-tenant IDs fail closed, duplicate resolution is idempotent, and
informational clear/new-property dismiss/escalation resolve have different
explicit contracts.

```python
def test_dismiss_notification_uses_verified_uid(client, firestore):
    response = client.post(
        "/api/dismiss-notification",
        json={"uid": "attacker", "notificationId": "n1"},
    )
    assert response.status_code == 401
    assert firestore.mutations == []
```

- [ ] **Step 3: Implement one authenticated server command**

Derive tenant identity exclusively from the verified token. Load the
notification before selecting semantics. Return a structured terminal result
containing notification disposition, thread disposition, outbox disposition,
counter delta, highlight state, and audit ID. Bulk operations iterate the same
command semantics; they never batch-delete mixed records directly.

- [ ] **Step 4: Switch UI consumers to the authenticated command**

Every single/group/global operation sends an ID token and displays partial
failure. Rename controls when semantics differ: `Mark handled`, `Dismiss and
stop`, `Clear information`, and `Cancel pending outreach` must not share an
ambiguous `Clear` label.

- [ ] **Step 5: Run focused and retained tests**

```bash
pytest -q \
  tests/test_dashboard_escalation_actions.py \
  tests/test_fe_contract_fuzz_dismiss_notification.py
```

Frontend:

```bash
node --test functions/lib/notificationResolution.test.js
CI=true npm test -- --runInBand \
  src/contexts/NotificationsContext.test.jsx \
  src/components/NotificationsSidebar.test.jsx \
  src/components/InlineNewPropertyCard.test.jsx \
  src/components/ConversationsPanel.test.jsx
```

Expected: zero unauthenticated mutations, identical server semantics for
single/bulk operations, accurate counters, and explicit thread/outbox outcomes.

- [ ] **Step 6: Commit, review, push, and deploy dark**

Commit backend and frontend independently, push, obtain exact-SHA CI success,
and record review before deployment. Then deploy the authenticated server
command under a versioned route while current UI remains unchanged. Read back
revision/digest/config and keep traffic/function selection reversible.

- [ ] **Step 7: Hold R2 at DEPLOYED_DARK until browser-owned action data exists**

Do not exercise controls on retained customer records and do not seed actions
directly. R2 remains `DEPLOYED_DARK` until R3-R4 create a browser-launched,
self-owned campaign containing an informational record, call/question action,
and new-property action. Then exercise each visible control through the UI and
read Firestore/outbox/audit/counters. Append the R2 checkpoint, run the PII
scan, commit evidence, push, and set R2 to `PROD_CLEARED` only if every
readback matches.

## Task 4: R3 versioned campaign start and mailbox readiness

**Files:**

- Create frontend Functions: `functions/lib/campaignLaunchV2.js`
- Create frontend Functions: `functions/lib/campaignLaunchV2.test.js`
- Modify frontend Functions: `functions/index.js`
- Modify frontend Functions: `functions/lib/campaignCapabilities.js`
- Modify frontend Functions: `functions/lib/campaignLaunchSafety.js`
- Modify frontend Functions: `functions/lib/outboxTrigger.js`
- Modify frontend: `src/components/AddClientModal.jsx`
- Modify frontend: `src/components/ClientsTable.jsx`
- Modify frontend: `src/components/StartProjectModal.jsx`
- Modify frontend: `src/hooks/useMailboxHealth.js`
- Modify frontend: `src/utils/campaignLaunchGuard.js`
- Modify backend: `email_automation/email.py`
- Add backend: `tests/test_campaign_launch_v2_contract.py`
- Modify backend: `tests/test_outbox_safety.py`

- [ ] **Step 1: Write launch-transaction REDs**

Cover all launch aliases, missing recipient/body/Ask contract, ambiguous
worksheet, 249/250-item boundary, Sheet-created/Firestore-failed compensation,
stale mailbox, all eight start/initialDispatch/inboundAutomation combinations,
concurrent quota reservation, and launch status queued/sending/sent/failed.

- [ ] **Step 2: Implement authenticated `campaignLaunchV2`**

The server command validates and freezes one launch payload, verifies sender
and mailbox health, checks capabilities, creates or compensates Drive/Sheet
resources, and atomically publishes activation/audit/outbox within an explicit
bound. Larger campaigns become one immutable launch command processed in
bounded chunks, never a partially published browser batch.

```javascript
export async function campaignLaunchV2({actor, payload, stores}) {
  assertVerifiedActor(actor);
  const frozen = validateAndFreezeLaunch(payload);
  await assertMailboxReady(actor.uid, stores);
  await assertCapabilities(actor.uid, frozen.clientId, stores);
  return publishLaunchWithCompensation(frozen, stores);
}
```

- [ ] **Step 3: Reserve quota transactionally before Graph send**

Replace read-send-increment with reserve-send-settle. Concurrent reservations
must never exceed the configured cap. Definitely-not-sent releases capacity;
uncertain send retains reservation until reconciliation.

- [ ] **Step 4: Run automated gates**

```bash
node --test \
  functions/lib/campaignCapabilities.test.js \
  functions/lib/campaignLaunchSafety.test.js \
  functions/lib/campaignLaunchV2.test.js \
  functions/lib/outboxTrigger.test.js
CI=true npm test -- --runInBand \
  src/hooks/useMailboxHealth.test.js \
  src/utils/campaignLaunchGuard.test.js \
  src/components/AddClientModal.campaignAccess.test.js
pytest -q tests/test_campaign_launch_v2_contract.py tests/test_outbox_safety.py
```

- [ ] **Step 5: Commit, review, push, and require exact-SHA CI**

Commit backend and frontend independently, push both branches, read back remote
SHAs, obtain exact-SHA successful checks, and record review before any deploy.

- [ ] **Step 6: Deploy compatibility path in safe order**

Deploy `campaignLaunchV2` first without switching production Hosting. Prove it
dark and record revision/digest/config/rollback. Deploy Hosting that explicitly
selects v2, then read back its exact asset/commit mapping. Keep legacy UI/API
available for rollback until stale-client traffic is zero.

- [ ] **Step 7: Browser-prove campaign start**

Through production UI only: sign in, verify sender, upload a varied workbook,
choose the authoritative sheet, review role bindings, set Ask/Note/Skip/Required,
edit subject/messages/follow-ups, prepare, and start. Verify that `Started`
never appears before the corresponding lifecycle state. Firestore, Graph,
Sheet, Drive, and audit readback must reconcile. Prove compensation failures at
E2. Do not induce a live production failure unless a separately reviewed,
tenant-scoped, authenticated, audited, automatically expiring fault mechanism
has first been implemented and cleared; otherwise production proof checks that
the normal path creates no orphan Sheet.

- [ ] **Step 8: Commit and push R3 evidence**

Stamp `campaign_start`, `mailbox_preflight`, `capability_separation`,
`quota_reservation`, and `sheet_compensation` separately. One failure keeps R3
`BLOCKED` while preserving any independently closed stamps. Run the evidence
PII scanner before committing.

## Task 5: R4 source-bound events and core escalations

**Files:**

- Create backend: `email_automation/event_receipts.py`
- Create backend: `tests/test_event_receipts.py`
- Modify backend: `email_automation/messaging.py`
- Modify backend: `email_automation/processing.py`
- Modify backend: `email_automation/notifications.py`
- Modify backend: `email_automation/source_coordinator.py`
- Modify backend: `firestore.rules`
- Modify frontend: `src/utils/baseV1Isolation.js`
- Modify frontend: `src/utils/actionNotifications.js`
- Modify frontend: `src/contexts/NotificationsContext.js`

- [ ] **Step 1: Write the dotted-field and projection-loss REDs**

Use a Firestore fake that preserves literal dotted keys exactly as production
does. Prove two distinct source messages with the same reason produce two
receipts, one retry of the same source produces none, and notification failure
creates a retained processing failure before source settlement.

- [ ] **Step 2: Implement immutable source-bound receipts**

Receipt ID is a hash of canonical source identity, event type, reason, property
anchor, and schema version. Thread summaries are rebuildable projections.
Notification/receipt commit is transactional where possible; otherwise a
projection obligation blocks processed/complete state until settled.

- [ ] **Step 3: Separate core tour actions from planner artifacts**

Core broker tour offer/request/reschedule/unavailable actions remain visible.
Only route, itinerary, invite, and Results artifacts are filtered when the
advanced lane is off. Call and question actions follow their own reply/pause
contracts.

- [ ] **Step 4: Run event and UI gates**

```bash
pytest -q \
  tests/test_event_receipts.py \
  tests/test_rubric_core_event_classifier_duplicate_retry.py \
  tests/test_source_coordinator_integration.py \
  tests/test_aprime_tour_scheduling.py \
  tests/test_broker_language_broker_tour_available.py \
  tests/test_broker_language_broker_alternate_tour_time.py \
  tests/test_broker_language_broker_tour_unavailable.py
CI=true npm test -- --runInBand \
  src/utils/baseV1Isolation.test.js \
  src/utils/actionNotifications.test.js \
  src/contexts/NotificationsContext.test.jsx
```

- [ ] **Step 5: Commit, review, push, and require exact-SHA CI**

Commit backend, rules, and frontend changes in independently reviewable slices.
Push, read back each remote SHA, obtain exact-SHA successful checks, and record
review before deployment.

- [ ] **Step 6: Deploy disabled, read back, then enable only the self-campaign scope**

Deploy rules/schema compatibility first, then worker writers with event receipts
disabled, then Hosting. Verify exact revisions/digests/config/rollback. Enable
only the browser-launched self-owned campaign scope; global enablement is
prohibited.

- [ ] **Step 7: Browser-prove varied escalation sequences and close deferred R2**

Run unused variants for call with/without number, ordinary question,
confidential question plus facts, tour offer, alternate time, temporary tour
restriction, repeated distinct same-category question, and planner-off access.
Grade UI action, pause/reply policy, Firestore receipt, Sheet extraction,
outbound/no-send, and audit. Use the resulting self-owned informational,
call/question, and new-property actions to complete the deferred R2
resolve/dismiss/clear proof. Stamp each feature independently, append immutable
checkpoints, run the evidence PII scan, commit, and push.

## Task 6: R5 reconciliation, health, and completion truth

**Files:**

- Modify backend: `email_automation/email.py`
- Modify backend: `email_automation/system_health.py`
- Modify backend: `email_automation/pending_responses.py`
- Modify backend: `email_automation/send_permits.py`
- Modify backend: `email_automation/processing.py`
- Create backend: `scripts/audit_firestore_integrity.py`
- Create backend: `tests/test_audit_firestore_integrity.py`
- Modify frontend Functions: `functions/lib/stopCampaign.js`
- Modify frontend: `src/utils/campaignCompletion.js`

- [ ] **Step 1: Write ordering and integrity REDs**

Cover Graph accepted/audit failed, outbox deletion before retained receipt,
cancelled/canceled terminal aliases, pending projection/completion obligations,
stale/missing message indexes, child records beneath missing parents,
active/paused roots beneath stopped campaigns, and notifications beneath
missing clients.

- [ ] **Step 2: Retain provider acceptance before queue deletion**

Outbox finalization must write durable Graph acceptance/reconciliation and
terminal audit references before deleting or tombstoning queue work. Unknown
prior send stays visible and cannot retry autonomously.

- [ ] **Step 3: Implement a read-only integrity auditor**

Default execution performs zero writes and emits deterministic JSON grouped by
tenant/campaign without PII. It validates parent existence, index bijection,
thread/campaign terminal compatibility, notification ownership, queue/audit
links, and completion obligations. Repair mode is absent from this script.

- [ ] **Step 4: Unify health/completion terminal vocabulary**

`cancelled` and `canceled` are terminal everywhere. A campaign cannot complete
while a processing failure, terminal review, draft review, dead letter,
reconciliation, event projection, or completion obligation is unresolved.

- [ ] **Step 5: Verify**

```bash
pytest -q \
  tests/test_audit_firestore_integrity.py \
  tests/test_combo_health_visibility_after_hidden_failure.py \
  tests/test_outbox_safety.py \
  tests/test_system_health.py \
  tests/test_pending_completion_health.py \
  tests/test_post_settlement_completion_obligations.py
node --test functions/lib/stopCampaign.test.js
CI=true npm test -- --runInBand src/utils/campaignCompletion.test.js
```

- [ ] **Step 6: Commit, review, push, and require exact-SHA CI**

Commit worker, Functions, frontend, auditor, and tests in reviewed slices. Push,
read back remote SHAs, and obtain exact-SHA successful checks before deployment.

- [ ] **Step 7: Deploy disabled, read back, and enable only the self-campaign scope**

Deploy compatibility/rules first, then runtime writers disabled, then the UI.
Record exact revision/digest/config/rollback and enable only the controlled
self-campaign scope after dark readback is clean.

- [ ] **Step 8: Audit production read-only and classify each finding**

Run the auditor against production with write credentials disabled. Store only
counts, opaque IDs/hashes, and dispositions in evidence. Create separate,
reviewed repair plans for active-campaign blockers; do not repair during audit.
Use the browser to verify corresponding health, completion, and campaign state.
Append the checkpoint, run the evidence PII scan, commit, and push.

## Task 7: R6 varied browser conversation waves

**Files:**

- Modify: `tests/fixtures/production_browser_conversation_variants.json`
- Modify: `docs/release-safety/production-clearance-state.json`
- Append: `docs/release-safety/production-clearance-checkpoints.jsonl`

- [ ] **Step 1: Preflight exact production state**

Require exact deployed SHAs/revisions, successful CI, authenticated dedicated
browser, confirmed sender, explicit self-recipient authorization, outbound cap,
kill switch, scheduler ownership, zero unexplained queues, healthy rollback,
and no simultaneous browser-control conflict.

- [ ] **Step 2: Run Wave A — launch and extraction**

Use fresh variants for clean full facts, partial facts, corrections, pricing
units, formulas, PDFs, broken/protected links, and wrong-property attachments.
Drive upload/start through SiteSift and broker replies through browser webmail.

- [ ] **Step 3: Run Wave B — availability and alternatives**

Use fresh variants for true unavailable, requirements non-fit, tour-only
restriction, stale quoted rejection plus fresh correction, alternate suite,
alternate property, and false-alternative near misses.

- [ ] **Step 4: Run Wave C — people and escalation**

Use fresh variants for ordinary/confidential questions, call, tour, OOO, shared
inbox signer, wrong contact, forwarded contact, opt-out, hostile response, and
property issues.

- [ ] **Step 5: Run Wave D — timing and recovery**

Use fresh variants for repeated same-category messages, second reply before
processing, manual continuation before retry, follow-up due during pause/stop,
Graph accepted before audit failure, stop during claim, and completion with a
pending obligation.

- [ ] **Step 6: Grade after every message, not only campaign end**

For every turn read back source identity, index, claim/receipt, classification,
thread/client status, notification/action, outbox/Graph, Sheet/change log,
audit/recovery, and follow-up state. Stop the wave on first mismatch.

- [ ] **Step 7: Commit feature stamps immediately**

Append an immutable checkpoint, update only the affected feature stamps, commit,
run the evidence PII scanner, push, and verify remote SHA after each wave. Do
not wait for all waves before making progress durable.

## Task 8: R7 production self-canary, rollback, and cohort decision

**Files:**

- Modify: `docs/release-safety/production-clearance-state.json`
- Append: `docs/release-safety/production-clearance-checkpoints.jsonl`
- Create: `docs/superpowers/evidence/2026-08-06-browser-first-production-clearance.md`

- [ ] **Step 1: Verify all core stamps and advanced isolation**

Every core feature must be `PROD_CLEARED`. Every advanced feature must be
`PROD_CLEARED` or `OFF_SAFE`. Any P0/P1 or stale evidence blocks the canary.

- [ ] **Step 2: Run one complete production self-canary through the browser**

The canary must cover launch, complete and partial extraction, one attachment,
one human escalation, one terminal state, follow-up suppression/scheduling,
operator resolution, completion, and stop/rollback readiness. Use newly
authorized self-owned identities and fresh body hashes.

- [ ] **Step 3: Reconcile every system of record**

Graph provider receipts must equal outbox/audit/thread state. Sheet values and
change log must agree. Notification/actions and counts must agree. Scheduler,
health, failure, review, dead-letter, and completion-obligation queues must be
zero or intentionally retained with a blocking disposition.

- [ ] **Step 4: Rehearse rollback**

Prove the exact prior revision/config can regain traffic without bypassing
in-flight authority or reactivating stopped work. Record the actual result, not
only the rollback command.

- [ ] **Step 5: Obtain independent evidence review**

Use the requesting-code-review skill. The reviewer checks exact SHAs, browser
scenario/variant hashes, negative controls, readbacks, unresolved queues, and
rollback evidence. A finding creates a new blocked checkpoint.

- [ ] **Step 6: Commit and push the final evidence decision**

Only after verification:

```bash
python3 scripts/scan_clearance_evidence_pii.py \
  docs/release-safety/production-clearance-state.json \
  docs/release-safety/production-clearance-checkpoints.jsonl \
  docs/superpowers/evidence/2026-08-06-browser-first-production-clearance.md
git add \
  docs/release-safety/production-clearance-state.json \
  docs/release-safety/production-clearance-checkpoints.jsonl \
  docs/superpowers/evidence
git commit -m "docs: record browser-first production clearance"
git push
```

The final commit states either `PROD_CLEARED_FOR_STAGED_COHORT` or `BLOCKED`
with exact reasons. It does not silently enable customers.

- [ ] **Step 7: Baylor makes the cohort-release decision**

The agent may recommend a staged allowlist only after the evidence gate. Baylor
explicitly decides whether Jill or another cohort returns. The agent does not
contact customers.

## Resume protocol

At the start of any resumed task:

1. Read `docs/release-safety/production-clearance-state.json`.
2. Read the `authoritativePlan` named there.
3. Read only the latest checkpoint entry for the first non-closed milestone.
4. Verify local branch, remote SHA, CI, deployment revision, and production
   traffic before taking action.
5. Resume the first unchecked step in that milestone.
6. Never rerun a closed production variant hash.

This protocol makes an interrupted agent lose time, not progress.
