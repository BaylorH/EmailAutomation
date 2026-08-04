# SiteSift production-clearance delivery train

**Status:** Active control plan, 2026-08-04
**Deliverable:** both (code and production-readiness findings)
**Outcome:** safely run a frontend-launched, fully observed campaign whose every
source, row, effect, retry, and user-visible state is durably attributable and
reconcilable.

## Product truth now

- B1 exact-source authority is complete, independently approved, frozen at code
  head `a3fcdf51a9b721b4b61be857476942498a292495`, documented at
  `6d79271fc71f145b2c76082ae68ac0edd61cc9b3`, and pushed with exact remote
  readback.
- The new release train starts from that frozen checkpoint on
  `codex/sitesift-production-clearance-20260804`. The backend checkpoint is
  118 commits ahead of and 0 behind the freshly fetched `origin/main`. That is a
  69-file delta with 106,967 insertions and 2,094 deletions. B1 evidence covers
  only the delta from `2b5e785`; that baseline was already 84 commits and 50
  files ahead of main. Final certification must therefore re-prove the complete
  main-to-candidate product, not borrow B1's narrower green.
- Production is still **NO-GO**. `main` owns a scheduled workflow, so merging is
  a production-runtime action rather than administrative cleanup. Milestone
  branches may be pushed; `main`, deployment, runtime flags, and campaigns stay
  closed until their explicit gates below.
- The complete B1 suite is 606/606 and the retained M2 suite is 669/669, but the
  older campaign-clearance lane is currently 85/88. Its two follow-up failures
  are stale state expectations and its health failure is a stale Firestore fake
  query contract. The separate auth-isolation test file is also stale against
  the now-required Firebase token decorator and is not included in normal test
  discovery.
- GitHub has scheduled runtime workflows but no push/PR CI for the release
  branch. No GitHub Actions run exists for the B1 branch.
- The prior Jill clearance plan has 35 unchecked steps. Its seven planned
  committed artifacts are absent. Its private evidence ledger exists and must
  be preserved uncommitted and uninspected until the evidence lane is rebased;
  it is input, not current clearance.
- The older Jill suites contain 166 tests, but semantic mapping is stale. The
  release fixture map still reports 101 stress gaps and 10 live-proof gaps.
  July's conditional-GO packet cannot certify this candidate.
- The active `email-admin-ui` checkout is not a release candidate: it has 17
  local changes and is 270 commits behind and 1 commit ahead of its locally
  tracked `origin/main`. That frontend remote ref has not been freshly fetched,
  so actual GitHub-main drift is unknown. The checkout must be preserved
  untouched; B4 starts with a fresh fetch and a new isolated worktree.

## Reorganized milestone board

| Milestone | State | Product exit gate | GitHub / production action |
|---|---|---|---|
| F0 — B1 exact-source authority | COMPLETE | 606 focused + 669 retained, two independent approvals | Code and evidence pushed; production off |
| M0 — Trustworthy release gates | ACTIVE | Campaign and auth isolation gates green locally and in credential-free CI | Push train branch; no merge/deploy |
| M1 — B2 stable row authority | NEXT | One stable row ID and retained row owner across terminal, opt-out, split/late roots, moves, cleanup | Push reviewed milestone; no deploy |
| M2 — B3 effect authority | BLOCKED by M1 | Every Graph/Sheet/notification/cleanup mutation requires the live execution tuple; ambiguity never replays | Push reviewed milestone; no deploy |
| M3 — B4 real-path and frontend/rules cutover | BLOCKED by M1-M2 | Real backend and frontend entry paths consume B1-B3; rules emulator and all ordered races pass | Candidate push; controlled disabled deploy only after review |
| M4 — Complaint and campaign defect corpus | PARALLEL READ-ONLY / final gate blocked by M3 | Every recent confirmed defect and historical recurrence maps to a current executable regression | Evidence push; no effects |
| M5 — Production canary | BLOCKED by M3-M4 | One explicitly authorized self-owned campaign reconciles provider receipts, datastore, Sheet, UI, queues, health, and scheduler | User launches or names exact self-owned target; stop on first mismatch |
| M6 — Cohort release | BLOCKED by clean M5 | Canary clean, rollback proven, zero uncertain/actionable work, independent release review | Staged allowlist, then wider cohort only by explicit decision |

Milestones are gates, not equal effort estimates. B1 is one of four M3
architecture phases; no honest production-ready percentage should be inferred
from its completion.

## Operating rules

1. One backend release-train branch from this point forward. Each milestone gets
   a reviewed commit, local verification, push, remote SHA readback, and evidence
   update before the next milestone begins.
2. Red test first for every defect or behavior change. A failed fixture or stale
   expectation is repaired to the production contract; production code is not
   changed merely to make an old test green.
3. No provider effects in M0-M4. Credentials are removed, OpenAI is empty, the
   Firestore emulator address is unreachable, and CI provider egress is
   blackholed. Send-path tests may set outbound mode to `live` only against
   faked provider boundaries so the safety code is actually exercised.
4. No automatic Graph message deletion. B3 converts all draft delete paths to
   retained `cleanup_required` work unless a provider-enforced conditional
   delete contract is separately proven.
5. The dirty frontend checkout is read-only. B4 uses a clean worktree and one
   coherent frontend release branch; user changes are never absorbed by
   accident.
6. Production state is evidence, not assumption. Repository tests cannot prove
   what is deployed; M3-M5 require exact artifact, revision, configuration,
   runtime identity, provider receipt, and rollback readback.
7. Agent-run external contact is prohibited unless the current user turn names
   the exact self-owned recipient. Without that, M5 is a user-launched canary.

## M0 — Restore trustworthy release gates

**Deliverable:** code

- [x] Capture the existing campaign clearance RED: 88 tests, 3 failures.
- [x] Trace the two follow-up failures to a stale expectation: the current
  safety gate withholds after `hasInboundReply` without claiming or rewriting
  business state.
- [x] Trace the health failure to the test Firestore double lacking the current
  `where(filter=FieldFilter(...))` contract.
- [x] Capture the auth-isolation RED and trace it to its stale unauthenticated
  test client plus renamed retained-flow timestamp/TTL fields.
- [x] Repair only the campaign test expectations and fake query adapter.
- [x] Repair the auth-isolation harness to authenticate with a mocked verified
  Firebase token, assert token UID wins over body UID, and exercise current
  `ts`/`_FLOW_TTL_SECONDS` semantics.
- [x] Run the 88 campaign tests, 7 auth-isolation tests, 606-test B1 focused
  suite, 669-test retained M2 suite, compile, and diff checks under corrected
  offline containment. Provider egress was blackholed and all authoritative
  runs exited 0.
- [ ] Freeze the full freshly fetched main-to-candidate file/feature/mutation
  inventory so later gates certify all 118 commits, not only B1.
- [x] Add credential-free GitHub CI for this release train. It has
  read-only repository permissions, no secrets, empty OpenAI key, an
  unreachable emulator, Python 3.12 matching the production image, and a
  hash-locked `requirements.lock` install. The job defaults outbound mode to
  `paused`, so the release and B1 gates remain paused. Only the retained M2
  suite step uses `live`; its provider boundaries are faked and provider egress
  remains blackholed.
- [ ] Commit, push, verify remote SHA, observe CI, and stop on any non-pass.

## M1 — B2 stable row authority

**Deliverable:** code

### Confirmed gap

Stable row authority is absent. `rowBindings[]` exists only in the approved
design. Terminal ownership is inferred from mutable `clientId + rowNumber`,
selects a thread-root claimant, and retains settlement on a thread. Opt-out is a
separate best-effort path that can swallow persistence/Sheet failures and stop
only the current root. `sync_thread_row_numbers_after_insert()` has zero
callers, so insertion can silently invalidate stored coordinates.

### Ordered build

- [ ] Write and independently review a bounded B2 design and TDD plan before
  production edits.
- [ ] Add stable user-scoped row identity plus canonical bounded, sorted,
  deduplicated immutable `rowBindings[]`. Identity must survive insert, move,
  sort, duplicate address/contact values, deletion tombstone, and late repair.
- [ ] Add retained row-transition owner generations linked to exact B1 source,
  snapshot, selection, and ledger hashes. Priority is verified hard opt-out,
  then terminal, then human review.
- [ ] Bind every initial, combined, split, recreated, and late thread root to
  retained row authority. Roots become projections and cannot resurrect a
  settled row.
- [ ] Move terminal and opt-out through the same retained owner. Opt-out becomes
  mandatory, transactional, exact-readback settlement across every bound root;
  the suppression record is only a compatibility projection.
- [ ] Preserve canonical row identity/owner/settlement through cleanup, expose
  orphan/owner-drift/late-active-root/legacy ambiguity health, and complete the
  row/terminal/opt-out race matrix.
- [ ] Independently review, run full affected and retained suites, commit, push,
  and verify the milestone SHA. Keep B2 provider-free and default-disabled.

## M2 — B3 execution and effect authority

**Deliverable:** code

### Confirmed gap

Graph send permits and terminal Sheet sagas are strong prototypes, but no
single execution authority covers all effects. Generic replies/outreach,
portions of follow-up draft lifecycle, broad Sheet writers, notifications, and
cleanup remain outside one exact retained tuple. Existing per-item send health
repairs work but are transient until their final health write.

### Ordered build

- [ ] Freeze AST manifests for every Graph, Sheet, notification/counter, and
  cleanup mutation; classify each as fenced, unfenced, quarantined, or
  admin-only. CI rejects new direct mutations.
- [ ] Add the immutable live tuple:
  `canonicalSourceId + ledgerHash + workKey + payloadHash + executionEpoch + executionClaimId`.
  Bind it transactionally to B1 work and B2 row authority where applicable.
- [ ] Add retained effect attempts with deterministic effect/attempt IDs,
  target and payload hashes, request-start, provider deadline, and outcomes for
  applied, reconciled-applied, definitely-not-applied, needs-reconciliation,
  cleanup-required, and operator-review.
- [ ] Make the B3 capability mandatory for every Graph send and fence create,
  patch, attachment, and send phases. Remove legacy unfenced branches.
- [ ] Replace all 21 automatic draft-delete callers with retained
  `cleanup_required` work; quarantine the mailbox-deletion admin endpoint.
- [ ] After B2, generalize terminal Sheet attempts across every write. Bind
  stable row identity and expected before/after hashes; prohibit blind rollback
  and replay after ambiguous readback.
- [ ] Validate the exact tuple inside notification/counter transactions and
  serialized cleanup plans; retain deterministic outcomes.
- [ ] Run crash/takeover cutpoints for every provider family and derive health
  from durable effect records. Old owners must never mutate or settle.
- [ ] Make send-budget reservation atomic with execution authority and require
  explicit user/campaign admission before raw outbox work can reach a send
  permit. Current caps and outbox admission are insufficient under concurrency.
- [ ] Independently review, run complete mutation inventories and retained
  suites, commit, push, and verify. B3 does not deploy independently.

## M3 — B4 real paths, frontend, rules, and cutover

**Deliverable:** both

- [ ] Fresh-fetch frontend GitHub main, record the exact merge base/divergence,
  and create a clean isolated worktree from that fetched head. Preserve the 17
  dirty-checkout changes without modifying or salvaging them; locally untracked
  safety files already have upstream equivalents and are not a release base.
- [ ] Migrate real terminal, opt-out, human-review, scanner, retry, replay,
  pending, outbox, follow-up, and scheduler entry paths to B1-B3 authority.
- [ ] Replace the intentional B1 real-path stubs before enforcement: bind a
  reviewed classifier evidence adapter, deterministic hard-opt-out verifier,
  owner-dispatch consumer, and accurate consumer-availability gate.
- [ ] Centralize one server-authority collection manifest covering B1-B3 and
  generate both rules exclusions and static mutation inventories from it.
- [ ] Make frontend mutations server-only, authenticated, tenant-scoped, and
  version/hash checked. Firestore rules explicitly exclude every B1-B3
  authority/projection collection from any overlapping generic owner rule.
- [ ] Remove broad authenticated direct-write authority for client subtrees,
  action audit, outbox, threads, and notifications. Route commands through
  tenant-scoped server endpoints or exact field-allowlisted input records.
- [ ] Replace static-string-only rules assertions with emulator tests that prove
  the frontend cannot directly create, alter, settle, or delete authority and
  projection records.
- [ ] Verify every required Firestore index, schema/version compatibility edge,
  and stale-frontend failure state before cutover; failures must be visible and
  non-mutating.
- [ ] Run rules-emulator tests and every ordered cross-family race pair with one
  durable winner, one visible loser, zero duplicate effects, and exact UI state.
- [ ] The race matrix explicitly includes opt-out↔terminal, opt-out↔human,
  terminal↔human, stop/cancel↔send, manual↔automatic continuation,
  row-move↔late-root creation, and archive/delete interleavings.
- [ ] Reconcile auth-isolation, scheduler lifecycle, health, rollback, and
  backend/frontend artifact versions in one release packet.
- [ ] Re-run the complete feature registry, release rubric, static inventories,
  and product suites across the full freshly fetched main-to-candidate delta.
  B1 baseline-scoped evidence cannot certify the other 84 pre-B1 commits.
- [ ] Add an explicit per-user/per-campaign coordinator enforcement scope. The
  current global mode cannot safely express a one-campaign canary.
- [ ] Do not enable the current `shadow` mode for an active user: it halts inbox,
  retry, and pending work instead of observing the legacy path. Either build a
  true scoped legacy-effects-plus-compare shadow mode or classify shadow as a
  maintenance-only blocked mode and use a reviewed alternative cutover gate.
- [ ] Run legacy alias, retained Terminal A, marker-only, and replay-claim
  migration preflight before enforcement. Ambiguity blocks scope admission.
- [ ] Deploy the reviewed candidate with new authority disabled; verify exact
  artifact and rollback. Progress to any observational or narrowly scoped
  enforced mode only when its semantics are proven and the previous gate has
  zero unexplained divergence.
- [ ] Prove rollback interlock: enforced mode may return to the legacy disabled
  path only after B1-B3 in-flight work is zero/drained, or the disabled path
  itself refuses to bypass retained authority.

## M4-M6 — Defect corpus, canary, and cohort release

**Deliverable:** finding, then an explicit release decision

- [ ] Rebase the untouched Jill evidence plan onto the exact M3 candidate and
  current external-contact rules. Old production evidence informs scenarios but
  never proves current behavior.
- [ ] Preserve the existing private ledger in place, do not commit it, and do
  not treat its contents as sanitized until the rebased evidence workflow
  validates it.
- [ ] Build sanitized recent-report and historical-recurrence records without
  committing PII. Map every confirmed defect to a current executable regression
  and every non-applicable report to a dated rationale.
- [ ] Re-map all 166 existing Jill tests semantically and rebaseline the 101
  stress gaps plus 10 live-proof gaps against the exact candidate; filenames do
  not count as coverage.
- [ ] Run deterministic L1, integration/rules L2, and local provider-contract
  simulations. Agent-run OpenAI/provider calls would disclose externally and
  are not authorized; real model/provider proof occurs only through the
  user-launched product canary. Any mismatch becomes a separate minimal TDD fix
  before continuing.
- [ ] Preflight the exact deployed artifact, runtime flags, kill switch,
  allowlist, caps, queues, health, scheduler owner, rollback, and every canary
  recipient.
- [ ] M5 effect boundary: the user launches the canary, or explicitly names the
  exact self-owned recipient in the current turn. The agent observes and
  reconciles read-only; it does not infer recipients or contact anyone else.
- [ ] Certify only if provider receipts equal outbox/audit/thread/campaign/Sheet
  state, all queues and uncertainty are zero, UI state is correct, scheduler
  ownership is released, and rollback remains exact.
- [ ] Keep wider users closed until an independent review approves the canary
  evidence and a staged allowlisted cohort completes cleanly.

## Immediate execution order

1. Complete and push M0.
2. Write the exact B2 design and TDD plan from the confirmed audit.
3. Build B2 in reviewed slices, pushing every milestone.
4. Continue B3, then B4; do not interleave speculative feature work.
5. Keep the complaint-corpus lane read-only and sanitized in parallel, then bind
   it to the exact B4 candidate before any campaign decision.
