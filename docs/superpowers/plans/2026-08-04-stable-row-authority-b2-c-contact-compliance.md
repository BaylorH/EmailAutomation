# Stable Row Authority B2-C Contact Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans task by
> task. Use superpowers:test-driven-development before every behavior change,
> superpowers:systematic-debugging for unexpected failures,
> superpowers:requesting-code-review at every review gate, and
> superpowers:verification-before-completion before every publication claim.

**Goal:** Implement provider-free immediate contact suppression, retry-safe
verified opt-out and authenticated release authority, bounded leased row
fan-out, exact restoration, and late-association convergence without changing
production behavior.

**Architecture:** Extend `row_authority.py` with strict immutable contact
transition receipts, aliases, contact settlements/heads, fan-out
heads/obligations/results, and transaction-composable planners. A deterministic
request receipt linearizes contact retries. A bounded latest-settlement loader
separates monotonic row generation allocation from the effective owner restored
by release. Fan-out workers page stable contact-row edges, atomically apply one
row result at a time, and use state revision, leases, and fencing to make stale
work harmless. Existing B2-B association, claim, settlement, and B1-link
planners remain the only row-authority primitives.

**Tech stack:** Python 3.12, standard-library-only production module, injected
Firestore-shaped transaction fake, `unittest`, AST/static containment tests,
and GitHub Actions.

**Plan deliverable:** both (provider-free code and immutable
production-clearance findings)

**Normative amendment:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md`

**Base B2 design:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`

**B1 identity bridge:**
`docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md`

**Program roadmap:**
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`

**Baseline:** `3904c79f65e54d4e146189e50845c6fb078f7c3d`

**Publication checkpoints:** `B2-C plan`, `B2-C contracts/history`,
`B2-C transitions`, `B2-C bounded workers`, `B2-C row application`,
`B2-C final`, and `B2-C evidence`.

**Safety boundary:** No provider/API import or call, send, campaign,
notification, reply, Sheet write, production Firestore read/write, migration,
runtime adoption, environment enablement, deploy, `main` merge, frontend/rules
change, or external communication. Provider networking stays blackholed.
Remote writes are limited to reviewed milestone commits on Baylor's owned
`codex/sitesift-production-clearance-20260804` branch. Production and Jill's
return remain NO-GO.

## Frozen implementation decisions

1. Only a stored, fully validated v2 B1 contact link may create contact
   opt-out authority. Public callers cannot supply the link or any identity,
   evidence, priority, settlement, fan-out, or result hash.
2. `contactOptOutTransitionRequests/{transitionId}` is the immutable
   idempotency/readback authority. The deterministic ID binds v2 B1 authority
   for opt-out or actor/request/expected-active-settlement authority for
   release. The contact settlement includes the transition ID; the receipt
   freezes resulting contact/fan-out head hashes. Every settlement consumer
   validates its exact creating receipt.
3. A different verified B1 opt-out while the canonical contact is already
   active creates only an allowed exact alias plus an `already_active` receipt
   pointing to the existing active settlement/fan-out. It creates no new
   contact or row generation. Exact retry reads its original receipt.
4. Authenticated release accepts an exact expected active contact settlement
   and deterministic client request. A stale or different release cannot
   release later authority and never creates/repairs aliases.
5. Suppression is a zero-write read. A valid active head suppresses; valid
   released or complete absence allows; every RPC/integrity ambiguity
   suppresses or raises an error that B4 must map to suppress.
6. A fan-out starts unleased at fence 1. Acquisition, renewal, takeover, and
   superseding increment its fence; superseding also clears the old lease.
   Worker mutations require exact revision/hash/owner/deadline/fence. Terminal
   fan-outs have null lease/cursor.
7. Discovery and supersession query 129, process at most 128, and persist a
   row-field cursor plus `cursorProcessedCount`. Every page advances the count
   by the exact number of validated obligations. Terminal transition requires
   the prior count plus the terminal page size to equal the frozen obligation
   cardinality, then clears both cursor and count. Binding snapshot drift
   resets both before a rescan. Apply/release resolves one obligation per
   transaction; equal counts start a separate bounded 32-result certification
   pass before completion.
8. `observedRowHeadHash` is always the row-head before-image. The result matrix
   in the amendment is exhaustive.
9. Row allocation uses a descending `generation` query of the latest two row
   settlements and allocates generation and fence above the maximum of
   historical and effective state. Divergence requires either an exact release
   bridge or exact active-supersession bridge. Complete lineage and generation
   `<=3` assumptions are removed without changing published v1 row schemas.
10. Release validates the effective row contact claim and restores any
    still-effective opt-out whose owner key equals the canonical mailbox being
    released, including an older epoch left by a superseded partial release.
    It restores that settlement's exact lower-priority predecessor, keeps the
    released settlement as latest history, and advances the release-result
    pointer. Another canonical contact is never restored.
11. A genuine late association updates the current fan-out atomically.
    Active nonterminal work gains an obligation and cursor reset; released
    nonterminal work gains an immediate noop result; active-complete
    synchronously resolves applied/dominated/deleted; released-complete
    CAS-recertifies only its binding snapshot with unchanged counts and an
    explicit health rule.
12. Delayed B1 source-settlement links validate historical released
    generations directly and change only the latest-link pointer. An
    `already_active` receipt does not mint a row link for a B1 source that never
    owned a row generation.
13. All B2-C queries are user-subcollection queries with equality filters,
    explicit order, bounded limits, and exact document reads. Query changes
    participate in fake transaction conflict detection.
14. Fan-out results include exact request/generation addresses beside hashes,
    allow `apply/noop/row_deleted`, and do not allow an unreachable
    `already_restored` first write.
15. A fan-out stores the current binding association count independently from
    binding revision/hash, and freezes its first completion binding
    revision/hash/association count, work counts, and time. Late
    recertification updates only the current snapshot, so historical release
    fan-outs remain auditable after later contact epochs without changing
    B2-B's independent revision/count contract.
16. Every mutation planner calculates its exact writes after bounded reads and
    before the first transaction write; no path exceeds 129 writes or the
    existing 400-write guard.
17. Existing public direct B1 contact claim and public standalone contact
    settlement remain blocked. Contact authority is available only through the
    new composed B2-C methods.
18. B2-C remains runtime-unwired. Finishing it advances only to B2-D; B3/B4
    still own provider effects, real adapters, frontend/rules, deployment, and
    authorized production proof.
19. The immutable transition receipt is the storage trust root for its frozen
    fan-out hash after mutable binding-snapshot progress. B2-C must prove that
    every application path uses create-only receipt writes. B4 must prove that
    deployed API, frontend, rules, and privileged runtime paths cannot update
    or delete receipts before production or Jill clearance.
20. A historical release-noop result may be certified against only its exact
    current authority or exact immediate successor. Contact-fanout successors
    must postdate and match the newer contact settlement/receipt, including its
    authority link, payload, fan-out, canonical owner, and causal timestamps;
    independent B1/operator successors retain their own direct lineage and
    result-time bounds.

## Provider-free API delta

Add strict pure builders/validators for:

```python
build_contact_alias_document(...)
validate_contact_alias_document(...)
build_contact_transition_request_document(...)
validate_contact_transition_request_document(...)
build_contact_settlement_document(...)
validate_contact_settlement_document(...)
build_contact_head_document(...)
validate_contact_head_document(...)
build_contact_fanout_head_document(...)
validate_contact_fanout_head_document(...)
build_contact_fanout_obligation_document(...)
validate_contact_fanout_obligation_document(...)
build_contact_fanout_result_document(...)
validate_contact_fanout_result_document(...)
```

Add or extend `RowAuthorityStore` methods:

```python
read_contact_optout_suppression(...)
record_verified_contact_optout(...)
record_authenticated_contact_release(...)
acquire_contact_fanout_lease(...)
discover_contact_fanout_page(...)
process_contact_fanout_obligation(...)
certify_contact_fanout_page(...)
supersede_contact_fanout_page(...)
record_contact_row_association(...)  # composed with current contact fan-out
```

Public methods return canonical artifact dictionaries and semantic outcomes,
never provider objects. Transition methods return the exact request receipt,
contact settlement/head, fan-out head, and allowed aliases. Worker methods
return the exact updated fan-out and artifacts committed or read back.

## File map

- Modify `email_automation/row_authority.py`: all B2-C constants, strict
  schemas/hashes, bounded loaders/planners, public store methods, and
  release-aware B2-B replay/refactors.
- Modify `tests/source_coordinator_fakes.py`: backward-compatible order
  direction and true ordered-field cursor semantics.
- Modify `tests/row_authority_fakes.py`: B2 query/write helpers only if the
  retained bounded wrapper needs additional observation hooks.
- Create `tests/test_row_authority_contact_compliance.py`: B2-C pure,
  transaction, lease, release, and late-association tests.
- Create `tests/test_row_authority_contact_fanout_discovery.py`: bounded
  discovery, cursor reset, immutable obligation replay, and race tests.
- Create `tests/test_row_authority_contact_fanout_completion.py`: bounded
  certification, exact evidence, cardinality, and retry tests.
- Create `tests/test_row_authority_contact_fanout_supersession.py`: bounded
  supersession, terminal linkage, historical evidence, and causal-order tests.
- Modify `tests/test_row_authority_contracts.py`: domain/schema/public-surface,
  containment, fake capability, and CI discovery inventories.
- Modify `tests/test_row_authority_ownership.py`: release-aware generation,
  replay, delayed source-link, and frozen B2-B compatibility tests.
- Modify the B1 contact-identity consumption rule, normative/base B2 design,
  and roadmap only at the approved plan publication boundary.
- Create
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-c.md` only
  after final code verification and independent reviews.

No runtime, provider, frontend, rule, workflow, dependency, migration, or
deployment file is expected to change.

## Task and publication order

Task 0 reviews and publishes this exact child plan. Tasks 1-2 publish the
contracts/history milestone. Task 3 publishes contact transitions and
suppression. Task 4 publishes bounded workers. Task 5 publishes one-row apply
and active late-association convergence. Task 6 publishes the complete
fan-out/release implementation. Task 7 performs full blackholed verification,
two fresh reviews, immutable evidence, and exact-SHA GitHub proof. Do not start
a later task while its prior milestone is unreviewed, locally red, unpushed,
remote-SHA mismatched, or CI failed.

Use this interpreter for every Python command:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python
```

## Task 0: Approve and publish the B2-C child plan

**Files:**

- Create:
  `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md`
- Create:
  `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md`
- Modify: `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`
- Modify: `docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md`
- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`

- [x] **Step 1: Run structural plan checks**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python - <<'PY'
from pathlib import Path

spec = Path(
    "docs/superpowers/specs/"
    "2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md"
).read_text(encoding="utf-8")
plan = Path(
    "docs/superpowers/plans/"
    "2026-08-04-stable-row-authority-b2-c-contact-compliance.md"
).read_text(encoding="utf-8")
b1 = Path(
    "docs/superpowers/specs/"
    "2026-08-04-b1-contact-identity-binding-amendment.md"
).read_text(encoding="utf-8")
assert "**Deliverable:** both" in spec
assert "**Plan deliverable:** both" in plan
assert "sitesift.contact.optout_transition_id.v1" in spec
assert "already_active" in spec
assert "order by generation DESCENDING" in spec
assert "released + complete release fan-out" in spec
assert "Production and Jill's return remain NO-GO" in plan
assert "### B2-C consumption rule" in b1
assert "already_active" in b1
assert plan.count("- [ ] **Step") >= 35
print("ok")
PY
git diff --check
! rg -n 'TO[D]O|T[B]D|FIX[M]E|PLACEH[O]LDER|pending decisio[n]' \
  docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md \
  docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md
```

- [x] **Step 2: Obtain two independent no-finding approvals**

Reviewer A checks transition idempotency, active-to-active behavior, v2 B1
provenance, alias/suppression fail-closed behavior, historical row generation,
source-link replay, and write/query bounds. Reviewer B checks every schema and
nullable matrix, leases/fences/cursors, discovery completion, supersession,
restore lineage, late associations, race outcomes, and activation containment.
Any Critical or Important finding is corrected and all three exact revised
normative files are re-reviewed.

- [x] **Step 3: Mark the approved design and roadmap checkpoint**

After two approvals, change the amendment status to
`Approved by two independent reviewers`. Add a short normative-amendment link
to the base B2 contact section. Add this roadmap status immediately before the
existing B2-C code checkbox:

```markdown
- [x] B2-C child plan and contract amendment are independently approved and
  published.
```

Do not mark B2-C code green.

- [x] **Step 4: Commit only the reviewed documentation milestone**

```bash
git add \
  docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md \
  docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md \
  docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "docs: freeze B2-C contact compliance"
```

- [x] **Step 5: Push and prove exact-SHA plan CI**

Use the exact publication protocol in the program roadmap. Record the exact
local/remote SHA and successful `production-clearance-ci.yml` run/job URL in
this plan's implementation log. Do not open a PR, merge, deploy, or access
production.

## Task 1: Extend ordered query fakes and freeze contact contracts

**Files:**

- Modify: `tests/source_coordinator_fakes.py`
- Modify: `tests/row_authority_fakes.py` only if required
- Modify: `tests/test_row_authority_contracts.py`
- Create: `tests/test_row_authority_contact_compliance.py`
- Modify: `email_automation/row_authority.py`

- [x] **Step 1: Write the failing fake-order and cursor tests**

Add selected tests named:

```text
ContactQueryFakeTests.test_order_by_direction_defaults_ascending_and_supports_descending
ContactQueryFakeTests.test_start_after_uses_ordered_field_tuple_not_document_path
ContactQueryFakeTests.test_order_ties_use_document_path_and_cursor_is_exclusive
ContactQueryFakeTests.test_query_phantom_retries_transaction
ContactQueryFakeTests.test_invalid_direction_or_cursor_shape_fails_before_writes
```

Use document IDs whose lexical path order differs from their `rowId` and
`generation` values. Run only these tests and preserve the selected RED output
in the implementation log.

- [x] **Step 2: Implement the minimal backward-compatible fake behavior**

Add `direction="ASCENDING"` to `order_by`; canonicalize
`ASCENDING|DESCENDING`; order by declared field tuple plus document path; make
mapping/list/tuple/snapshot cursors compare those values; retain equality
filters, limits, event recording, and transaction phantom detection. Existing
single-argument callers must remain byte-behavior compatible.

- [x] **Step 3: Verify the fake tests GREEN and retained B1 fake users**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contact_compliance.ContactQueryFakeTests -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator \
  tests.test_source_coordinator_integration -q
```

- [x] **Step 4: Write failing independent contact hash/schema tests**

Cover both new hash domains and all seven contact record builders/validators.
Use independent canonical JSON and fixed expected digests, not production hash
helpers. Prove exact keys, explicit nulls, paths, user scope, timestamp bounds,
booleans-as-integers rejection, contact settlement transition ID inclusion,
transition receipt discrimination, fan-out state/lease/cursor matrix, result
address/hash and disposition/reason matrix including deleted-row noop, rejection
of `already_restored`, all current-binding/completion-certificate
revision/hash/count correlations, and cross-domain/cross-user drift.

Selected RED names:

```text
ContactContractTests.test_transition_id_and_receipt_match_independent_vectors
ContactContractTests.test_alias_settlement_head_and_fanout_hashes_match_independent_vectors
ContactContractTests.test_transition_receipt_exact_kind_outcome_head_hash_and_nullability_matrix
ContactContractTests.test_contact_settlement_requires_transition_id_and_exact_origin
ContactContractTests.test_fanout_head_state_lease_cursor_and_superseding_matrix
ContactContractTests.test_complete_fanout_rejects_crossed_binding_revision_and_count_deltas
ContactContractTests.test_fanout_result_matrix_is_exhaustive
ContactContractTests.test_every_contact_record_rejects_path_hash_schema_and_user_drift
ContactContractTests.test_all_contact_domains_are_registered_and_runtime_contained
```

- [x] **Step 5: Implement only strict constants/builders/validators**

Do not add a store method. Keep all production values derived from validated
inputs, include every null in hashes, and reject unknown keys. Preserve every
B2-A/B digest.

- [x] **Step 6: Verify contract GREEN and frozen legacy vectors**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contact_compliance.ContactContractTests \
  tests.test_row_authority_contracts -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowOwnershipContractTests -q
```

## Task 2: Make row allocation and historical replay release-aware

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`
- Modify: `tests/test_row_authority_contact_compliance.py`

- [x] **Step 1: Write failing descending-history allocation tests**

Prove empty history, ordinary settled history, restored-clear history, restored
terminal/human history, current pending generation above latest settlement,
two complete opt-out/release cycles, duplicate top generation, malformed path,
head/latest hash drift, missing release bridge, and candidate path collision.

Selected RED names:

```text
ReleaseAwareRowHistoryTests.test_next_generation_uses_max_effective_and_latest_historical_generation
ReleaseAwareRowHistoryTests.test_repeated_release_cycles_never_reuse_generation_or_fence
ReleaseAwareRowHistoryTests.test_latest_settlement_query_is_descending_bounded_to_two
ReleaseAwareRowHistoryTests.test_duplicate_or_malformed_latest_generation_is_ambiguous
ReleaseAwareRowHistoryTests.test_latest_pair_rejects_generation_gap_or_fence_regression
ReleaseAwareRowHistoryTests.test_current_unsettled_generation_rejects_gap_or_stale_fence
ReleaseAwareRowHistoryTests.test_released_head_requires_exact_result_bridge
ReleaseAwareRowHistoryTests.test_normal_pending_supersession_uses_dominated_bridge_not_release_bridge
ReleaseAwareRowHistoryTests.test_current_pending_generation_can_exceed_latest_settlement
ReleaseAwareRowHistoryTests.test_candidate_generation_or_settlement_collision_writes_nothing
```

- [x] **Step 2: Implement a shared bounded row-history loader**

Read current effective artifacts, latest two settlements, the exact release or
active-dominated bridge when latest/effective differ, and candidate paths.
Allocate both generation and first fence above historical/effective maxima.
Validate only direct correlations; never read `1..N`. Remove
generation/priority-depth assumptions without weakening priority comparison or
B2-B exact retry semantics, and do not add fields or versions to published v1
row documents.

- [x] **Step 3: Route every allocator and replay through the loader**

Cover direct B1 claims, no-pending operator decline, contact-fanout private
claims, lease/settlement retry as applicable, and source-settlement link
forward/retry validation. Preserve v1 and B2-B non-release output bytes.

- [x] **Step 4: Write failing delayed historical link/replay tests**

Selected RED names:

```text
ReleaseAwareRowHistoryTests.test_settlement_retry_after_release_reads_historical_generation
ReleaseAwareRowHistoryTests.test_source_link_after_release_updates_only_latest_link_pointer
ReleaseAwareRowHistoryTests.test_source_link_after_newer_generation_validates_exact_old_artifacts
ReleaseAwareRowHistoryTests.test_historical_link_missing_duplicate_or_future_proof_is_ambiguous
ReleaseAwareRowHistoryTests.test_nonreleased_b2b_claim_and_link_vectors_are_byte_identical
```

- [x] **Step 5: Make the historical replay tests GREEN**

Never reactivate an old owner during settlement/link readback. Keep the newest
historical settlement pointer and the effective owner independent.

- [x] **Step 6: Run the complete B2-B ownership suite**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership \
  tests.test_row_authority_contact_compliance.ReleaseAwareRowHistoryTests -q
```

- [x] **Step 7: Review, commit, push, and prove `B2-C contracts/history`**

Obtain an independent review of the Task 1-2 diff. Correct every Critical or
Important finding. Run `git diff --check`, compile the production module, and
the B2 automatic discovery command. Commit only the reviewed files with:

```bash
git commit -m "feat: add B2-C contracts and release-aware history"
```

Push the owned branch and prove exact local SHA == remote SHA == successful
workflow `headSha`. Record the commit/run/job in the implementation log.

## Task 3: Implement retry-safe contact transitions and suppression

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contact_compliance.py`
- Modify: `tests/test_row_authority_contracts.py`

- [x] **Step 1: Write failing alias and suppression-read tests**

Cover the complete alias table, unseen plus variants, active/released/absent
heads, exact latest settlement and creating-receipt validation, alias/head RPC
failures, malformed records, partial authority, and raw identity
non-persistence.

Selected RED names:

```text
ContactSuppressionTests.test_alias_creation_validation_and_conflict_table
ContactSuppressionTests.test_active_contact_suppresses_exact_and_unseen_plus_variant
ContactSuppressionTests.test_valid_released_or_absent_contact_allows
ContactSuppressionTests.test_every_alias_head_and_settlement_failure_is_fail_closed
ContactSuppressionTests.test_missing_or_mismatched_creating_receipt_cannot_allow_release
ContactSuppressionTests.test_suppression_is_zero_write_and_persists_no_raw_identity
```

- [x] **Step 2: Implement pure suppression classification and zero-write API**

Compute hashes internally from verified user/raw mailbox, validate exact and
self aliases, head, and latest settlement, and return the amendment's four
semantic outcomes. Any fake read exception must never produce `allow`.

- [x] **Step 3: Write failing first-opt-out transaction tests**

Build real stored B1 v2 bundles. Prove the caller cannot provide contact hashes
or a link. Cover aliases equal/distinct, first generation, released-to-active,
receipt/settlement/head/fan-out atomicity, planned writes, two-worker CAS,
pre-apply failure, exact retry, apply-then-raise, partial readback, and v1/mixed
identity rejection.

Selected RED names:

```text
ContactTransitionTests.test_verified_optout_derives_v2_identity_and_creates_exact_authority_atomically
ContactTransitionTests.test_verified_optout_after_release_allocates_next_contact_generation
ContactTransitionTests.test_transition_request_linearizes_retry_and_two_worker_race
ContactTransitionTests.test_two_distinct_initial_optouts_commit_one_epoch_and_one_already_active_receipt
ContactTransitionTests.test_three_active_optouts_create_three_receipts_but_one_epoch
ContactTransitionTests.test_optout_preapply_apply_then_raise_and_partial_readback
ContactTransitionTests.test_v1_or_caller_supplied_contact_authority_cannot_write
ContactTransitionTests.test_transition_write_count_matches_exact_after_image
```

- [x] **Step 4: Implement the verified-opt-out planner/store method**

Compose B1 bundle validation, alias planner, deterministic request, contact
settlement/head, and initial unleased apply fan-out at fence 1 in one
transaction. Build all
hashes internally. The receipt freezes the exact resulting contact/fan-out head
hashes. Receipt-first retry validates immutable artifacts exactly and mutable
heads only as allowed forward successors; it never allocates again.

- [x] **Step 5: Write failing active-to-active receipt tests**

Cover exact same request retry; distinct v2 proof for same exact identity;
distinct plus alias for same canonical identity; allowed current fan-out states;
concurrent release; malformed/superseding fan-out; and proof that contact and
row generations/counts do not advance.

Selected RED names:

```text
ContactTransitionTests.test_active_to_active_creates_only_durable_receipt
ContactTransitionTests.test_active_to_active_may_add_only_missing_exact_alias
ContactTransitionTests.test_active_to_active_never_advances_contact_or_row_epoch
ContactTransitionTests.test_active_to_active_racing_release_retries_or_fails_stale
ContactTransitionTests.test_active_to_active_requires_exact_current_fanout
ContactTransitionTests.test_active_to_active_validates_active_settlements_creating_receipt
```

- [x] **Step 6: Implement already-active receipt behavior**

Read and CAS-protect the active head/current fan-out even when only immutable
documents are created. Allow `discovering|applying|complete`; reject every
other state. Validate the active settlement's own `created` receipt. Return the
exact later receipt as B2 completion evidence.

- [x] **Step 7: Write failing authenticated-release transaction tests**

Cover actor/client request/expected settlement binding, exact aliases,
generation append, released head/new fan-out, prior fan-out superseding,
terminal prior fan-out validation, stale request, repeated different request,
two-worker race, and apply-then-raise readback.

Selected RED names:

```text
ContactReleaseTransitionTests.test_release_binds_actor_request_and_exact_active_settlement
ContactReleaseTransitionTests.test_release_never_creates_or_repairs_alias
ContactReleaseTransitionTests.test_release_supersedes_current_nonterminal_apply_fanout_atomically
ContactReleaseTransitionTests.test_stale_or_repeated_distinct_release_is_zero_write
ContactReleaseTransitionTests.test_release_retry_and_apply_then_raise_read_exact_receipt
ContactReleaseTransitionTests.test_old_release_receipt_retry_after_new_optout_never_mutates_new_epoch
ContactReleaseTransitionTests.test_historical_release_lookup_is_unique_bounded_and_path_exact
ContactReleaseTransitionTests.test_historical_release_lookup_missing_duplicate_or_drift_is_zero_write
ContactReleaseTransitionTests.test_transition_retry_accepts_valid_fanout_progress_and_later_contact_epochs
ContactReleaseTransitionTests.test_first_winner_time_is_retained_on_same_id_retry
```

- [x] **Step 8: Implement authenticated release and transition superseding CAS**

Derive identity from the active settlement, not caller mailbox material. Create
the release receipt/settlement/head/fan-out and prior-fan-out transition in one
bounded transaction. New fan-outs are unleased; superseding clears the prior
lease and increments its fence. Read an existing receipt before current-head
allocation so historical exact retry is zero-write. Resolve the expected
settlement first with the amendment's canonical+settlement-hash `limit(2)`
query, so its exact identity and receipt path remain derivable after later
epochs. Do not restore rows here.

- [x] **Step 9: Verify, review, commit, and publish `B2-C transitions`**

Run all Task 3 classes, complete B2 tests, B1 tests, provider blackhole, compile,
and diff checks. Obtain an independent review, correct findings, commit:

```bash
git commit -m "feat: add retry-safe contact suppression transitions"
```

Push and prove the exact SHA/run/job under the roadmap protocol.

## Task 4: Implement leased discovery, completion, and supersession

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contact_compliance.py`
- Modify: `tests/test_row_authority_contact_release_integrity.py`
- Modify: `tests/test_row_authority_contact_releases.py`
- Modify: `tests/test_row_authority_contact_transition_integrity.py`
- Modify: `tests/test_row_authority_contact_transitions.py`
- Create: `tests/test_row_authority_contact_fanout_discovery.py`
- Create: `tests/test_row_authority_contact_fanout_completion.py`
- Create: `tests/test_row_authority_contact_fanout_supersession.py`

- [x] **Step 1: Write failing lease/takeover state-machine tests**

Selected RED names:

```text
ContactFanoutLeaseTests.test_nonterminal_mutation_requires_exact_unexpired_lease_and_fence
ContactFanoutLeaseTests.test_new_fanout_is_unleased_at_fence_one
ContactFanoutLeaseTests.test_acquisition_and_renewal_increment_revision_and_fence
ContactFanoutLeaseTests.test_expired_takeover_increments_revision_and_fence
ContactFanoutLeaseTests.test_stale_worker_cannot_write_after_takeover_or_superseding
ContactFanoutLeaseTests.test_terminal_fanout_rejects_takeover_and_has_null_lease_cursor
```

- [x] **Step 2: Implement fan-out lease acquisition, renewal, and takeover**

Reuse timestamp/fence validation conventions from row leases. A worker argument
never selects state or outcome; state transitions are derived from stored
documents. Superseding clears any old lease and increments the fence before a
new worker may acquire it.

- [x] **Step 3: Write failing discovery pagination and cursor-reset tests**

Cover 0, 1, 128, 129, and multiple pages; exact obligation replay; malformed
edge/obligation; binding change before/behind/after cursor; cursor reset;
phantom retry; and discovery-to-applying transition.

Selected RED names:

```text
ContactFanoutDiscoveryTests.test_discovery_pages_128_with_129th_as_sentinel
ContactFanoutDiscoveryTests.test_discovery_uses_row_field_cursor_and_exact_obligation_replay
ContactFanoutDiscoveryTests.test_binding_revision_drift_resets_cursor_before_rescan
ContactFanoutDiscoveryTests.test_earlier_sorted_late_row_is_never_skipped
ContactFanoutDiscoveryTests.test_stable_exhaustion_moves_to_applying_with_null_cursor
ContactFanoutDiscoveryTests.test_fanout_work_requires_contact_settlements_exact_creating_receipt
ContactFanoutDiscoveryTests.test_discovery_failure_or_drift_writes_nothing
```

- [x] **Step 4: Implement bounded discovery planner/store method**

Write at most 128 obligations plus one head. Validate all query documents and
the current contact head/settlement/creating receipt. Never start a write before
every page read is complete.

- [x] **Step 5: Write failing completion and supersession tests**

Task 4 certification starts only after obligation and result counts are equal.
Unequal counts return `needs_processing` without an obligation/result scan or
write. The bounded unresolved-result locator, its equal-count cursor reset, and
late recertification are exercised with `process_contact_fanout_obligation` and
association composition in Tasks 5-6, where those behaviors are implemented.
Cover certification pages 0/1/32/33/128/129, stable binding, missing/swapped/
extra immutable evidence, supersession of only discovered unfinished
obligations, 128/129 pages, new-contact correlation, and terminal matrices.

Selected RED names:

```text
ContactFanoutCompletionTests.test_completion_requires_no_next_obligation_equal_counts_and_stable_binding
ContactFanoutCompletionTests.test_certification_pages_32_with_33rd_as_sentinel
ContactFanoutCompletionTests.test_certification_rejects_missing_swapped_or_extra_evidence
ContactFanoutCompletionTests.test_completion_never_reads_unbounded_history
ContactFanoutSupersessionTests.test_superseding_pages_only_discovered_unfinished_obligations
ContactFanoutSupersessionTests.test_superseding_never_discovers_edges_or_mutates_rows
ContactFanoutSupersessionTests.test_exact_exhaustion_creates_linked_terminal_superseded_head
```

Deferred selected tests, retained verbatim for Tasks 5-6:

```text
ContactFanoutCompletionTests.test_resolution_pages_32_with_33rd_as_sentinel_before_missing_result
ContactFanoutCompletionTests.test_resolution_exhaustion_with_unequal_counts_is_ambiguous
ContactFanoutCompletionTests.test_equal_counts_reset_cursor_and_start_bounded_certification
ContactFanoutCompletionTests.test_first_completion_certificate_is_frozen_across_late_recertification
```

- [x] **Step 6: Implement certification and supersession planners/store methods**

Every superseded result uses the exact obligation, before-image row head, and
newer contact settlement. Clear lease/cursor only at exact terminal
transition. `certify_contact_fanout_page` validates at most 32 exact pairs plus
one sentinel and is the only path from equal-count `applying` to `complete`.
It runs separate bounded obligation and result queries and compares their
ordered row-ID pages locally; it does not assume a Firestore cross-collection
union.

Both methods take the verified user, fan-out ID, exact expected fan-out head,
lease owner hash, and caller-frozen action timestamp. Certification returns
`disposition`, `fanoutHead`, `obligations`, and `results`, with
`needs_processing|page_certified|certification_complete`. Supersession returns
`disposition`, `fanoutHead`, and `results`, with
`page_superseded|supersession_complete`. Exact retry returns the deterministic
original disposition and artifacts with zero writes.

- [x] **Step 7: Run all Task 4 tests and retained transaction-race controls**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contact_compliance.ContactFanoutLeaseTests \
  tests.test_row_authority_contact_fanout_discovery.ContactFanoutDiscoveryTests \
  tests.test_row_authority_contact_fanout_completion.ContactFanoutCompletionTests \
  tests.test_row_authority_contact_fanout_supersession.ContactFanoutSupersessionTests -v
```

## Task 5: Apply contact opt-out obligations and converge late active rows

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contact_compliance.py`
- Modify: `tests/test_row_authority_ownership.py`

- [x] **Step 1: Write failing one-row apply tests**

Use the real contact transition, obligation, private contact claim, and
settlement chain. Cover clear/terminal/human rows, different equal-priority
contact dominance, a deleted historical row noop, historical release state,
current head/contact race, result-before-image, exact retry, pre-apply,
apply-then-raise, and write count.

Selected RED names:

```text
ContactFanoutApplyTests.test_apply_atomically_creates_claim_generation_settlement_head_and_result
ContactFanoutApplyTests.test_apply_dominance_creates_claim_set_and_result_without_generation
ContactFanoutApplyTests.test_apply_deleted_row_records_noop_without_claim_or_generation
ContactFanoutApplyTests.test_apply_uses_v2_contact_settlement_link_not_thread_authority
ContactFanoutApplyTests.test_multiple_thread_evidence_roots_produce_one_canonical_row_claim
ContactFanoutApplyTests.test_apply_result_hashes_exact_row_head_before_image
ContactFanoutApplyTests.test_apply_loses_safely_to_contact_head_or_fence_advance
ContactFanoutApplyTests.test_apply_retry_preapply_and_apply_then_raise_are_exact
```

- [x] **Step 2: Refactor generic claim input to canonical one-row bindings**

Permit the private contact planner to derive a deterministic single-row
binding without a persisted thread binding. Existing direct B1 callers must
still validate and derive from their exact stored thread binding. Keep public
direct contact claims blocked. Permuting or adding supporting thread evidence
must not change the contact claim request ID.

- [x] **Step 3: Implement atomic apply obligation processing**

Compose claim and settlement planners inside the fan-out transaction. The
accepted path has no externally visible intermediate claimed state. Advance
result count/cursor exactly once.

- [x] **Step 4: Write failing nonterminal and active-complete association tests**

Selected RED names:

```text
ContactLateAssociationTests.test_nonterminal_association_adds_obligation_and_resets_cursor_atomically
ContactLateAssociationTests.test_existing_association_new_evidence_does_not_change_fanout
ContactLateAssociationTests.test_active_complete_late_row_is_applied_and_recertified_atomically
ContactLateAssociationTests.test_active_complete_late_row_may_record_dominated_result
ContactLateAssociationTests.test_active_complete_deleted_row_is_noop_and_recertified_atomically
ContactLateAssociationTests.test_late_association_ambiguity_creates_no_edge_or_evidence
```

- [x] **Step 5: Extend the B2-B association executor by composition**

Remove only the blanket contact-head sentinel. Read and validate the current
contact/fan-out state before any write; invoke the existing association planner
and the appropriate deterministic obligation/result planner in one
transaction. Preserve the original 3/1/0-write behavior when no contact head
exists.

- [x] **Step 6: Run Task 5 plus complete B2-B compatibility tests**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contact_compliance.ContactFanoutApplyTests \
  tests.test_row_authority_contact_compliance.ContactLateAssociationTests \
  tests.test_row_authority_ownership -q
```

Local verification used the split apply/late-association modules that hold the
selected tests. The Task 5 plus B2-B gate passed 356 tests, and the complete
`test_row_authority*.py` regression discovery passed 560 tests before publication.

## Task 6: Restore same-canonical rows and converge late released associations

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify:
  `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md`
- Create: `tests/test_row_authority_contact_fanout_release.py`
- Create: `tests/test_row_authority_contact_fanout_release_origin.py`
- Create: `tests/test_row_authority_contact_late_release.py`
- Retained gates: `tests/test_row_authority_contact_compliance.py`,
  `tests/test_row_authority_ownership.py`

- [x] **Step 1: Write failing release result-matrix tests**

Selected RED names:

```text
ContactFanoutReleaseTests.test_release_restores_exact_terminal_or_human_predecessor
ContactFanoutReleaseTests.test_release_restores_clear_without_reusing_generation
ContactFanoutReleaseTests.test_release_noops_when_row_optout_was_dominated_or_not_applied
ContactFanoutReleaseTests.test_release_rejects_impossible_equal_priority_different_owner_lineage
ContactFanoutReleaseTests.test_partial_release_a_then_optout_and_release_b_restores_still_a_controlled_row
ContactFanoutReleaseTests.test_release_never_restores_another_canonical_contacts_optout
ContactFanoutReleaseTests.test_release_loses_safely_to_newer_contact_transition
ContactFanoutReleaseTests.test_release_result_uses_before_image_and_exact_nullable_matrix
```

- [x] **Step 2: Write failing restore-lineage corruption tests**

Cover missing/duplicate predecessor hash lookup, wrong row, non-lower
generation, wrong generation hash/claim, dominated/contact-optout/nonsettled or
non-lower-priority predecessor, missing/malformed/crossed originating apply
obligation or result for both current and older same-canonical epochs,
same-canonical older epoch versus different canonical owner, stale row head,
and partial readback. Prove `already_restored` cannot be a first-write result.
Every failure must preserve the complete before-image.

The frozen priority lattice has no valid first-write
`noop/different_effective_owner` path: an applied contact opt-out is already at
the maximum priority, and a release bridge has its own deterministic row
result. Keep the tuple reserved in the result schema, prove the valid
different-canonical-first path is `row_optout_not_applied`, and reject a
synthetic direct equal-priority successor without writes.

- [x] **Step 3: Implement exact release planner and obligation processing**

Resolve the current effective contact claim, the selected settlement's
mandatory originating apply obligation and result evidence from its own epoch,
and lower-priority lineage with bounded exact queries. Restore any effective
same-canonical contact opt-out regardless of its older epoch; never restore a
different canonical owner. Restore only effective fields, preserve latest
historical/source fields, create result plus row/fan-out heads atomically, and
allocate no row generation.

- [x] **Step 4: Write failing released-complete late-association tests**

Selected RED names:

```text
ContactLateReleaseAssociationTests.test_released_nonterminal_late_row_adds_immediate_noop_and_resets_cursor
ContactLateReleaseAssociationTests.test_released_complete_late_row_recertifies_without_obligation_or_result
ContactLateReleaseAssociationTests.test_released_complete_recertification_preserves_terminal_certificate
ContactLateReleaseAssociationTests.test_released_complete_count_exception_survives_later_contact_epoch
ContactLateReleaseAssociationTests.test_released_complete_next_active_epoch_discovers_late_row
ContactLateReleaseAssociationTests.test_released_malformed_or_terminal_fanout_writes_nothing
ContactLateReleaseAssociationTests.test_released_contact_fanout_race_creates_no_late_artifacts
```

- [x] **Step 5: Implement exact released-state late-association behavior**

During `discovering|applying`, atomically create association/evidence/binding
head, release obligation, and immediate `noop/row_optout_not_applied` result;
advance both counts and reset the phase cursor. After `complete`, commit only
association/evidence/binding head plus a CAS-recertified fan-out snapshot.
Increment its revision/update/hash while retaining complete state, fence, null
lease/cursor/superseding link, and unchanged counts. Freeze the B2-D count
exception against the immutable first-completion certificate, prove it remains
healthy after a later contact epoch, and prove the next active epoch discovers
the new association.

- [x] **Step 6: Prove delayed B1 settlement-link compatibility end to end**

Test both orders: B1 source settlement before row fan-out settlement/link, and
after row fan-out settlement including after contact release. Prove exact v2
link preservation, no B1 writes from B2, no link for an already-active receipt
that never owned the row, and no effective-owner reactivation.

- [x] **Step 7: Run the complete B2-C and B2 suites**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contact_compliance \
  tests.test_row_authority_contact_fanout_release \
  tests.test_row_authority_contact_fanout_release_origin \
  tests.test_row_authority_contact_late_release \
  tests.test_row_authority_contracts \
  tests.test_row_authority_identity_location \
  tests.test_row_authority_ownership -q

../codex-release-a-medium-recovery-20260714/.venv/bin/python - <<'PY'
import unittest

modules = (
    "tests.test_row_authority_contact_compliance",
    "tests.test_row_authority_contact_fanout_release",
    "tests.test_row_authority_contact_fanout_release_origin",
    "tests.test_row_authority_contact_late_release",
    "tests.test_row_authority_contracts",
    "tests.test_row_authority_identity_location",
    "tests.test_row_authority_ownership",
)

def flatten(suite):
    tests = []
    for item in suite:
        tests.extend(flatten(item) if isinstance(item, unittest.TestSuite) else [item])
    return tests

loaded = flatten(unittest.TestLoader().loadTestsFromNames(modules))
test_ids = [test.id() for test in loaded]
new_ids = [
    test_id
    for test_id in test_ids
    if any(
        module in test_id
        for module in (
            "test_row_authority_contact_fanout_release.",
            "test_row_authority_contact_fanout_release_origin.",
            "test_row_authority_contact_late_release.",
        )
    )
]
assert len(test_ids) == len(set(test_ids)) == 480, (len(test_ids), len(set(test_ids)))
assert len(new_ids) == len(set(new_ids)) == 79, (len(new_ids), len(set(new_ids)))
PY

../codex-release-a-medium-recovery-20260714/.venv/bin/python -m pytest -q \
  tests/test_row_authority\*.py
```

Fresh corrected-candidate evidence: the authoritative seven-module gate passed
480/480 tests, loader inspection found 480/480 unique total IDs with 79/79
unique Task 6 IDs, and the complete row-authority discovery passed 642 tests
plus 1,351 subtests. The worker gates include immutable latest-contact proofs,
mutable-head rollback rejection, exact replay through later fan-out/contact/
location successors, frozen obligation chronology, and zero-write corruption
failures.

- [ ] **Step 8: Review, commit, push, and prove `B2-C final` code**

Obtain two independent full-diff reviews from the published B2-C plan commit
through the code candidate. Correct every Critical or Important finding and
rerun affected plus complete gates. Commit only reviewed code/tests:

```bash
git commit -m "feat: converge contact opt-out fanout and release"
```

Push and prove exact local/remote/workflow SHA and successful run/job. Do not
merge, deploy, or access production.

## Task 7: Freeze complete B2-C evidence and advance only to B2-D

**Files:**

- Create:
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-c.md`
- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
- Modify:
  `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md`

- [ ] **Step 1: Run all mandatory local gates from a clean candidate**

Run automatic discovery of every `test_row_authority*.py`, complete B1,
retained M2, release/auth, provider blackhole, `compileall`, `pip check`, YAML
parse of all workflows, static runtime containment, privacy scan, and
`git diff --check`. Record exact counts/durations and command outputs. No gate
may be inferred from an older commit.

- [ ] **Step 2: Obtain two fresh final reviews**

Reviewer A traces every amendment acceptance item to executable evidence and
audits production-code correctness. Reviewer B independently audits privacy,
runtime/provider containment, query/write bounds, race/refutation coverage,
GitHub provenance, and production posture. Correct findings and repeat both
reviews on the exact revised diff.

- [ ] **Step 3: Create immutable evidence**

Record baseline, plan SHA/run, each code milestone SHA/run/job, exact reviewed
diff digest, local gate counts/durations, test/refutation map, write bounds,
provider-free/static containment proof, reviewer outcomes, clean-tree status,
and explicit statement:

```text
B2-C is provider-free and runtime-unwired. No deploy, campaign, production
data, provider effect, external contact, or Jill clearance occurred.
Production remains NO-GO. The next gate is the independently reviewed B2-D
child plan.
```

- [ ] **Step 4: Update only truthful roadmap and implementation-log status**

Mark B2-C green only after exact code CI and final evidence are complete. Keep
B2-D, B3-B4, production clearance, and Jill checkboxes open. Add every exact
SHA/run/job to the implementation log below.

- [ ] **Step 5: Commit and publish final evidence**

```bash
git add \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-c.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md
git diff --cached --check
git diff --cached
git commit -m "docs: record B2-C contact compliance evidence"
```

Push and prove the exact evidence SHA/run/job. Recheck local HEAD == remote
branch == workflow `headSha`, conclusion `success`, and clean worktree. Stop at
the B2-D planning boundary.

## Exact publication protocol

For every checkpoint, use a task-specific variable name and the program
roadmap's exact-SHA selection. The minimum proof is:

```bash
git push origin codex/sitesift-production-clearance-20260804
B2C_CHECKPOINT_SHA="$(git rev-parse HEAD)"
B2C_REMOTE_SHA="$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)"
test "$B2C_CHECKPOINT_SHA" = "$B2C_REMOTE_SHA"
gh run list \
  --branch codex/sitesift-production-clearance-20260804 \
  --workflow production-clearance-ci.yml \
  --commit "$B2C_CHECKPOINT_SHA" \
  --limit 3 \
  --json databaseId,headSha,status,conclusion,url
```

Select only a run whose `headSha` equals `B2C_CHECKPOINT_SHA`, wait with
`gh run watch RUN_ID --exit-status`, and then prove `headSha` equality and
`conclusion == success`. Never reuse an older run. Never open a PR, direct-push
or merge `main`, deploy, launch a campaign, or contact a user under this plan.

## Implementation log

- Baseline: `3904c79f65e54d4e146189e50845c6fb078f7c3d`, clean and equal to the owned
  remote branch when planning began.
- Plan review: executor reviewer and contract-audit reviewer independently
  returned APPROVE with no Critical or Important findings after all corrections.
- Plan publication: local HEAD, owned remote branch, and workflow head SHA were
  `ac2563709e607d6d682457c1a51db3d5e91730a2`; Production Clearance CI
  [run 31010099996](https://github.com/BaylorH/EmailAutomation/actions/runs/31010099996),
  [job 92319757381](https://github.com/BaylorH/EmailAutomation/actions/runs/31010099996/job/92319757381)
  completed successfully.
- Publication-record checkpoint: local HEAD, owned remote branch, and workflow
  head SHA were `732fab4003a83486c9f4bd8570c804b78751e738`;
  [run 31010330183](https://github.com/BaylorH/EmailAutomation/actions/runs/31010330183),
  [job 92320550877](https://github.com/BaylorH/EmailAutomation/actions/runs/31010330183/job/92320550877)
  completed successfully.
- Task 1 query-fake RED: all five selected tests executed; seven assertions
  failed only because `direction` was unsupported and field-value cursors were
  rejected. GREEN: all five selected tests pass, and 211 retained source
  coordinator/fake integration tests pass.
- Query-fake review then exposed partial-prefix, bare `__name__`, and reference
  cursor gaps. Each was reproduced RED, corrected, and independently approved
  with the same five selected and 211 retained tests green.
- Task 1 contact-contract RED: all nine selected tests executed and failed only
  on the absent B2-C domains and seven builder/validator pairs; there were zero
  setup errors or unexpected exceptions.
- Contract-oracle review rejected forged transition/fan-out fixtures and added
  released-head, re-opt-out predecessor, self-alias, obligation-correlation,
  completion, and fully paired result-matrix refutations. The corrected nine
  tests remain clean RED on only the absent builder/validator pairs.
- Task 1 contract GREEN: nine contact-contract tests, 25 retained primitive
  contract tests, and 27 full row-ownership contract tests all pass (61 total),
  with compile and diff checks clean. The stale nonexistent
  `B1AuthorityLinkTests` plan selector was corrected to the containing
  `RowOwnershipContractTests` class. No store or runtime wiring was added.
- Task 1 semantic review then reproduced accepted-invalid epoch, head revision,
  fan-out fence/lease/count/supersession/completion-snapshot, and restoration
  generation shapes as strict RED cases. Builders now reject every shape while
  preserving all published hashes and schemas. The nine contact tests and all
  61 focused contract/vector tests returned GREEN after correction.
- Two independent Task 1 final reviews approved the corrected diff with no
  remaining Critical or Important findings. Together they observed the five
  ordered-query fake tests, 211 retained source-coordinator tests, all 61
  focused contract/vector tests, compilation, and diff checks GREEN.
- Fresh automatic B2 discovery after Task 1 ran 313 tests successfully.
- Task 2 allocation/history RED covered the ten planned cases plus restored
  clear/owner, combined release and active-dominated bridges, exact query/path
  shape, restored-artifact drift, commit uncertainty, multi-cycle accepted
  replay, predecessor-link drift, historical release proof, and forged
  `blocked_by_claim_set` history. Each failed on the intended absent bounded
  behavior before its production correction.
- Task 2 GREEN routes direct claims, lease takeover, settlement retry, source
  linking, and operator decline through descending generation history limited
  to two, with exact authority/release reads and query readback. Adversarial
  review then reproduced and corrected equal-time replay ordering, direct and
  release-restored owner-at-time brackets, deep N+1/N+2 restoration exits,
  swallowed retryable reads, Firestore ordered-field existence semantics,
  exact-hash query invisibility, and predecessor proof path drift. Each
  correction gained a public-store or query-fake zero-write regression.
- The final Task 1-2 candidate passes 67 release-aware tests, 197 retained
  ownership tests, the exact combined 264-test command, all 381 automatically
  discovered `test_row_authority*.py` tests, and all 211 retained source
  coordinator/fake integration tests. Production-module compilation and diff
  checks are clean. Two independent final reviews approved the exact candidate
  with no remaining Critical or Important findings. The staged diff SHA-256
  was `220cad935d7ab42ce29f02a1e9e53093b250cfa4ff2f1dd301b6a61bc7ba2978`.
- Task 2 code publication: local HEAD, owned remote branch, and workflow head
  SHA were `a0dbc970aa5df063988faa0aa50b89f168abf6d4`;
  [run 31031535151](https://github.com/BaylorH/EmailAutomation/actions/runs/31031535151),
  [job 92393190332](https://github.com/BaylorH/EmailAutomation/actions/runs/31031535151/job/92393190332)
  completed successfully. This milestone remains provider-free and
  runtime-unwired; it did not deploy or contact a user.
- Task 3 RED/GREEN covered suppression, verified opt-out, already-active
  receipts, authenticated release, historical retries, alias chronology,
  receipt/head reconstruction, binding monotonicity, and fan-out successor
  reachability. The final focused transition gate passed 66 tests and automatic
  B2 discovery passed 461 tests. Adversarial review first found retry-integrity
  gaps; every finding received a focused RED regression and production
  correction. The exact revised diff then received a clean independent review.
- The final Task 3 candidate passed 95 release/identity tests, 617 complete B1
  tests, 461 complete B2 tests, and 669 retained M2 tests under provider and
  Firestore blackholes (1,842 total). Compilation, dependency, and diff checks
  were clean. The staged code/test diff SHA-256 was
  `95c5dfc198bad0c502527705e9714d0803bec5a3063439f467fd476eb82a5722`.
- Task 3 code publication: local HEAD, owned remote branch, and workflow head
  SHA were `9531206e660779ead93ad47867b1519f447e7f58`;
  [run 31042024656](https://github.com/BaylorH/EmailAutomation/actions/runs/31042024656),
  [job 92428426959](https://github.com/BaylorH/EmailAutomation/actions/runs/31042024656/job/92428426959)
  completed successfully. This milestone remains provider-free and
  runtime-unwired; it did not deploy or contact a user.
- Task 4 RED/GREEN implemented lease acquisition/takeover, bounded discovery,
  32-pair certification, and 128-obligation supersession. A generic
  `cursorProcessedCount` now proves exact page cardinality across discovery,
  certification, and supersession before any terminal transition.
- Task 4 adversarial review reproduced cardinality drift, false historical
  release-noop evidence, future contact authority, an over-broad causal bound
  on independent B1 successors, and crossed contact authority links. Every
  finding received a fixture-clean RED regression and production correction.
  The final independent review returned CLEAN with no remaining Critical or
  Important findings.
- The reviewed Task 4 candidate passes the 26-test focused worker gate, all
  501 complete B2 tests, 95 release/identity/Jill tests, 617 complete B1 tests,
  and 669 retained M2 tests under provider and Firestore blackholes (1,882
  total). Compilation, dependency, workflow-YAML, provider-containment, staged
  diff, and artifact checks are clean. The staged code/test diff SHA-256 is
  `b96d9ad8e6799a4f4f3a12a7a72b650ea14549fddedb4390b42370d760732538`.
- Task 4 code publication: local HEAD, owned remote branch, and workflow head
  SHA were `daff8c82c5c011fb41ce6ebba6def4c583da0828`;
  [run 31054694964](https://github.com/BaylorH/EmailAutomation/actions/runs/31054694964),
  [job 92469544623](https://github.com/BaylorH/EmailAutomation/actions/runs/31054694964/job/92469544623)
  completed successfully. This milestone remains provider-free and
  runtime-unwired; it did not deploy or contact a user.
- Task 5 RED/GREEN implemented ordered one-row apply with an atomic final
  settlement and active late-association convergence for nonterminal and
  completed apply fan-outs. Permanent regressions cover exact 6/7/10/11-write
  paths, bounded-history query after-images, unknown commit recovery, expired
  lease reset, deleted/dominated rows, evidence-only stability, and current
  contact/fan-out races.
- The reviewed Task 5 candidate passes the 356-test apply/late/B2-B gate and
  all 560 automatically discovered `test_row_authority*.py` tests under
  provider and Firestore blackholes. Compilation and diff checks are clean.
  Independent review returned CLEAN with no Critical or Important findings;
  the exact code/test diff SHA-256 is
  `93278685c13f785b01ea079cd999eca99209dbb4ada20f995c29443b4fe6e771`.
- Task 5 code publication: local HEAD, owned remote branch, and workflow head
  SHA were `800d065db5af2304c924e730fd7d87c84910282f`;
  [run 31059647024](https://github.com/BaylorH/EmailAutomation/actions/runs/31059647024),
  [job 92484567669](https://github.com/BaylorH/EmailAutomation/actions/runs/31059647024/job/92484567669)
  completed successfully. This milestone remains provider-free and
  runtime-unwired; it did not deploy, launch a campaign, or contact a user.
- Plan reviews, publication SHA/run, selected RED outputs, code milestone
  SHAs/runs, final diff digest, final review outcomes, and evidence SHA/run are
  appended here only after they exist and are independently verified.
