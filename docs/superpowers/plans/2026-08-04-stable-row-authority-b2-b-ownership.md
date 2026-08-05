# Stable Row Authority B2-B Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans to
> implement this plan task by task. Use superpowers:test-driven-development for
> every behavior change, superpowers:systematic-debugging for every unexpected
> failure, superpowers:requesting-code-review at the frozen review gates, and
> superpowers:verification-before-completion before every publication claim.

**Goal:** Create the provider-free, runtime-unwired B2 ownership authority that
binds threads and contacts to stable rows, arbitrates immutable all-or-none row
claims, fences leases, settles logical row outcomes, records authenticated
declines, and links independently settled B1 evidence without authorizing any
provider effect.

**Architecture:** Extend the existing standard-library-only
`row_authority.py`. Pure builders and validators freeze every B2-B schema and
domain hash before store mutations are added. `RowAuthorityStore` continues to
accept injected Firestore-shaped and transaction-executor dependencies. Every
mutation validates complete input before opening a transaction, performs all
fresh reads before any write, applies a bounded full-document plan, and
classifies executor failure through exact before/after readback. Immutable
bindings are the only row-resolution authority; mutable row heads are full
document CAS records. B1 collections are read through narrowly frozen local
validators and are never imported or written.

**Tech stack:** Python 3.12 standard library, injected Firestore-shaped
interfaces, existing B2 bounded fake, `unittest`, AST/static containment, and
GitHub Actions.

**Plan deliverable:** both (provider-free code and B2-B clearance evidence)

**Approved design:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`

**Program roadmap:**
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`

**Completed predecessor:**
`docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a1.md`

**Baseline:** `5676b26ca61ba447e759a36be43d658d1bb8a7a9`

**Publication checkpoint:** `B2-B`

**Safety boundary:** No provider/API client/import/call, send, campaign,
notification, reply, Sheet write, production Firestore read/write, runtime
adoption, deploy, `main` merge, frontend/rules change, migration execution, B3
effect permit, B2-C contact transition/fan-out/release, or external
communication. Remote writes are limited to reviewed milestone commits on
Baylor's owned `codex/sitesift-production-clearance-20260804` branch. A branch
push is not a production release. Production remains NO-GO.

## Frozen implementation decisions

### Canonical and path rules

1. Every new B2-B hash uses the already approved byte contract:
   `domain.encode("utf-8") + b"\0" + canonical_json_bytes(material)`.
   `schemaVersion: 1` and `userScopeHash` are top-level members of every B2-B
   material. Hash output fields are never members of their own inputs.
2. Persisted documents have exactly the registry fields in the approved
   design. Validators reject missing, unknown, mistyped, over-bound,
   noncanonical, hash-drifted, timestamp-drifted, and invalid correlated-null
   fields. Booleans never satisfy integer positions. Builders and returned
   documents are defensive copies.
3. Raw verified user IDs and raw mailboxes are never stored. The verified user
   ID is validated as one safe Firestore document segment before it is hashed
   or used in a reference. Complete exact/canonical mailbox identity hashes are
   structural B2-B inputs; B2-C alone establishes alias equivalence.
4. `threadRowBindings/{threadId}` uses the exact thread ID as its document ID.
   Therefore a thread ID must satisfy both approved `opaque` validation and the
   existing safe Firestore single-segment rule: no slash, dot-only/reserved
   segment, controls, invalid Unicode, or invalid UTF-8. It is never encoded,
   trimmed, or case-normalized. Stored validators enforce the same rule.
5. Caller-frozen B2 timestamps are exact UTC RFC3339 strings with six
   fractional digits and `Z`. Every head-changing event time is greater than or
   equal to the freshly read head's `updatedAt`; equality is valid and backward
   time is a zero-write conflict. A claim cannot predate its thread binding or
   any bound identity/head; takeover cannot predate the current head; settlement
   cannot predate its claim/generation/current head; operator action cannot
   predate its binding/current heads; and a source link cannot predate either
   the B1 or B2 settlement or the current head. Contact evidence cannot predate
   its supporting thread binding or stable association. B1's retained aware
   datetimes are normalized to UTC only for comparison and are never rewritten.
   A B1-derived claim also cannot predate the source identity `createdAt`,
   classification `snapshotPersistedAt`, transition-owner `createdAt`, or
   work-ledger `createdAt`; mutable B1 `updatedAt` is not a readiness boundary
   and cannot invalidate an exact retry. A new lease deadline is strictly later
   than the claim/takeover timestamp.
6. `MAX_ROW_BINDINGS == 128` applies after unique canonical normalization. Raw
   duplicates that reduce to at most 128 are valid; 129 unique row IDs fail
   before a transaction or reference is created. The named pure helper
   `_require_row_authority_planned_writes(value)` accepts only a non-boolean
   JSON uint from 0 through `MAX_ROW_AUTHORITY_PLANNED_WRITES == 400` and rejects
   401. Fixed-size or caller-bound operations validate their exact count before
   transaction entry. State-derived claim and decline operations validate their
   mathematical worst case before opening a transaction (385 and 386
   respectively), then derive and validate the exact count on every fresh
   callback after all reads and before the first staged write. The exact value,
   not the worst case, is stored as `plannedWrites`.

### Immutable thread and contact bindings

7. `normalize_row_bindings(row_ids, primary_row_id)` returns unique
   lexicographically sorted `RowBinding` dictionaries. Exactly one entry is
   `primary`; every other entry is `related`. Persisted duplicates, unsorted
   bindings, role drift, primary drift, count drift, or hash drift are invalid.
8. The frozen binding hash domains and complete logical fields are:
   - `sitesift.row.bindings.v1`: `rowBindings`, `primaryRowId`,
     `bindingCount`;
   - `sitesift.thread.row_binding.v1`: `threadId`, `clientId`,
     `rowBindingsHash`, `primaryRowId`, `bindingCount`, `createdAt`;
   - `sitesift.row.thread_edge_id.v1`: `rowId`, `threadId`;
   - `sitesift.row.thread_edge.v1`: `edgeId`, `rowId`, `threadId`, `role`,
     `threadBindingHash`, `createdAt`.
9. `RowAuthorityStore.bind_thread_rows(...)` reads, in deterministic reference
   order, the candidate thread binding, every row identity and row authority
   head in sorted row order, then every reverse edge in sorted row order. It
   performs all reads before writes. Every identity must match the user scope,
   row ID, and one exact `clientId`; its head must match the identity and current
   location authority. A valid `deleted` row remains bindable for historical
   late-root discovery. Binding never changes identity, location, head,
   generation, settlement, or provider state.
10. A new thread binding creates exactly `1 + bindingCount` immutable
    documents. Exact binding plus all exact reverse edges returns
    `already_applied` with zero writes. Partial presence is ambiguous; immutable
    drift is conflict; neither is repaired. Identical workers yield one
    `created` and one `already_applied`; divergent proposals preserve the first
    committed binding.
11. Contact binding domains and fields are:
    - `sitesift.contact.row_edge_id.v1`: canonical mailbox hash, row ID;
    - `sitesift.contact.row_edge.v1`: edge ID, canonical mailbox hash, row ID,
      `createdAt`;
    - `sitesift.contact.row_evidence_id.v1`: edge ID, thread binding hash,
      exact identity hash;
    - `sitesift.contact.row_evidence.v1`: evidence ID, edge ID, thread ID,
      thread binding hash, exact identity hash, `createdAt`;
    - `sitesift.contact.row_binding_head.v1`: canonical mailbox hash,
      `stateRevision`, `associationCount`, nullable `lastAssociationHash`, and
      timestamps.
12. `RowAuthorityStore.record_contact_row_association(...)` processes exactly
    one row. It reads the contact opt-out head existence sentinel, stored thread
    binding, supporting reverse edge, row identity/head, candidate contact
    edge, candidate evidence, and contact binding head before writes. The
    stored binding and reverse edge—not caller hashes—must prove the row/thread
    association. Deleted rows are permitted as historical evidence.
13. Public B2-B contact association fails closed with zero writes if any
    `contactOptOutHeads/{canonicalHash}` document exists, without validating or
    mutating that B2-C-owned record. The implementation separates a private
    transaction-composable association planner from the public executor: the
    planner consumes already-read exact prerequisites, returns planned
    association mutations, and never opens or commits a nested transaction.
    B2-C will later invoke that planner inside its combined
    association-plus-current-fan-out-obligation transaction. B2-B's public
    wrapper never reads or writes contact settlements, fan-out heads,
    obligations, results, aliases, suppression decisions, release state, or
    cursors.
14. The first contact association/evidence atomically creates the edge,
    evidence, and a count-one binding head: three writes. A valid preinitialized
    empty head advances from count zero/null last hash by one revision. An exact
    existing edge plus new evidence creates only the evidence and leaves the
    head byte-for-byte unchanged. Exact immutable edge/evidence plus either its
    exact original head or a strictly validated monotonically advanced contact
    binding head is zero-write `already_applied`; retrying an older association
    never restores `associationCount` or `lastAssociationHash`. Missing head
    with an edge, evidence without its edge, a regressed/noncorrelated head,
    partial presence, or malformed head is ambiguous; immutable drift is a
    conflict. Immediate executor-failure readback remains stricter and accepts
    only the complete exact before-image or disposition-specific after-image.

### B1 links, origins, and deterministic operator actions

15. `B1Link` is exactly `canonicalSourceId`, `snapshotImmutableHash`,
    `selectionHash`, `ownerDecisionHash`, `ledgerHash`, `ownerKind`, `ownerKey`,
    `workKey`, `payloadHash`, nullable `hardOptOutEvidenceHash`, and
    `authorityLinkHash`. The hash domain is
    `sitesift.row.b1_authority_link.v1`. Hard opt-out evidence is required only
    when `ownerKind == contact_optout` and must be null otherwise. A pure link
    builder accepts the exact stored B1 source identity, ready classification,
    transition owner, work ledger, and one exact work-key selector; it never
    accepts raw link fields. It independently validates the B1 schemas and
    frozen `hashKind` materials, requires one exact selected `delegate_owner`
    work entry, and derives every link field. For verified contact opt-out,
    `hardOptOutEvidenceHash` is exactly the validated classification's non-local
    deterministic evidence hash and the selected contact-opt-out candidate must
    bind that same hash. Model prose can never mint priority three.
16. Claim origins are discriminated and have no permissive fallback:
    - `b1_source`: complete B1 link/hash; operator and fan-out hashes null;
    - `authenticated_operator`: exact stored operator action; B1 link/hash and
      fan-out ID null; owner is derived as `human_decision`, owner key is
      `actorScopeHash`, work key is `actionId`, and payload hash is
      `operatorActionHash`;
    - `contact_fanout`: complete verified-contact-opt-out B1 link/hash plus
      `fanoutId`; operator hash null.
17. The only public generic B2-B claim mutation is provenance-validated
    `b1_source` for `terminal|human_decision`. It accepts a safe
    `canonicalSourceId` and exact full-hash B1 work-key selector, reads the B1
    `sourceIdentities`, `sourceClassifications`, `sourceTransitionOwners`, and
    `sourceWorkLedgers` bundle in the deciding transaction, derives the B1 link,
    obtains the source identity's exact safe `threadId`, and resolves rows from
    that stored B2 thread binding. Caller-supplied thread IDs or links are not
    accepted. A valid direct B1 `contact_optout` is still rejected because it
    would bypass B2-C's contact settlement/head/fan-out protocol.
18. `authenticated_operator` and `contact_fanout` remain exact discriminated
    claim schemas plus a private transaction-composable claim planner. They are
    not standalone store methods and the planner never opens a nested
    transaction. `record_operator_decline` invokes the operator planner and
    atomically settles `human_declined`. B2-C will invoke the one-row contact
    planner only inside the same transaction that has validated the exact
    contact settlement, contact head, fan-out head, obligation, and stable
    contact-row association. Public B2-B calls reject both origins.
19. Operator action identity is derived from actor scope, exact row binding
    hash, domain-hashed opaque client request ID, fixed action `decline`, fixed
    reason `decline_property`, and issued time. The public API accepts neither
    an action ID nor priority. An existing action with any drift is conflict.
    Dismiss, stop, and resume have no B2-B action or settlement API.

### Claims, generations, and all-or-none arbitration

20. Priority is derived only: `contact_optout = 3`, `terminal = 2`, and
    `human_decision = 1`. No public or internal transaction API accepts a
    caller-supplied priority or `plannedWrites`.
21. `RowAuthorityStore.claim_row_set(...)` validates the safe canonical source
    ID and work-key selector before references. Each transaction retry reads the
    exact B1 identity, classification, transition owner, and work ledger; reads
    the B2 thread binding selected by the source identity; derives and validates
    the user-scoped B1 link, canonical bindings, and request ID; reads that
    candidate claim set; then reads every row identity, head, current effective
    generation, and candidate generation/settlement reference in deterministic
    sorted order. Only after all reads does it derive/validate exact planned
    writes and stage mutations. Missing, malformed, hash-drifted, wrong-thread,
    wrong-work, non-delegated, direct-opt-out, or caller-forged authority writes
    nothing.
22. New claims may target only valid `active|nonviable` identities. `deleted`
    and `ambiguous` rows remain discoverable but cannot receive a claim. If the
    head has no effective owner and no latest/effective settlement, the first
    generation is one. If it has an effective owner, the next generation is
    `effectiveOwnerGeneration + 1`. A future B2-C post-release shape with no
    effective owner but historical settlement state fails closed until B2-C
    freezes its allocation rule.
23. Higher priority allocates a new immutable generation for every row and
    advances every head. Lower priority, or equal priority from a different
    source, creates only one durable dominated claim set. First transactionally
    committed equal-priority authority wins; lexical source/thread order is
    never an election.
24. Multi-row claims are all-or-none. If any row is dominated, at least that
    row receives `decision: dominated`; every otherwise claimable peer receives
    `blocked_by_claim_set`; no row generation/head changes. An accepted claim
    contains only accepted decisions and planned generation numbers. Accepted
    decisions never contain generation hashes, avoiding a claim/generation
    hash cycle.
25. Accepted generic-claim writes are exactly `1 + (2 * N) + D`: one claim
    set, `N` generations, `N` heads, and `D` dominated predecessor settlements
    for superseded lower generations that were `claimed|review_pending`.
    Maximum is 385 for 128 rows. Dominated generic claims write only the one
    claim set. The 385 worst case is validated before transaction creation; the
    exact callback-derived count is validated before writes and stored in
    `plannedWrites`.
26. A new generation has `leaseEpoch == 1`. Its first fence is prior head fence
    plus one, or one when absent. `predecessorHeadHash` is the complete prior
    head hash; `predecessorSettlementHash` is the prior effective settlement
    hash. A lower generation that is still claimed/review pending receives one
    immutable `dominated` settlement linked to the new generation. That
    predecessor settlement freezes the freshly read lower head's current
    `fencingToken`, never the generation's `firstFencingToken`; takeover may
    have advanced it. A settled lower generation is never rewritten.
27. A new human generation enters `review_pending`; terminal and contact
    generations enter `claimed`. Claimed/pending heads contain the derived
    effective owner, exact lease owner/deadline, and fence. When superseding an
    unsettled lower generation, `latestSettlementHash` becomes the dominated
    predecessor settlement while `effectiveSettlementHash` preserves the prior
    effective settlement. Otherwise both settlement fields are preserved until
    the new generation settles.
28. Normal deterministic request replay is distinct from executor-failure
    readback. An exact existing claim set plus every exact immutable generation
    and required predecessor settlement is `already_applied` when each current
    head is either the original result or a strictly validated forward state
    (location-only advance, fence advance, settlement, or higher generation).
    Replay never rewinds a later head. A missing immutable, regressed or
    noncorrelated current head, or request content/hash drift is conflict or
    ambiguous with zero writes. Immediate apply-then-raise succeeds only after
    the complete exact after-image; an exact before-image is retryable; partial,
    malformed, missing, unreadable, or mismatched immediate readback is
    ambiguous.

### Leases, settlements, and decline

29. `RowAuthorityStore.take_over_expired_lease(...)` requires an exact full
    previously read head in `claimed|review_pending`, its current immutable
    generation, no settlement for that generation, `expected.leaseUntil <
    takenAt`, and `newLeaseUntil > takenAt`. It performs one full head write,
    preserving the generation and immutable `leaseEpoch`, replacing the lease
    owner/deadline, and incrementing state revision and fencing token once.
    Replaying the exact old head after that exact takeover is zero-write
    `already_applied` when the current head is the exact takeover result or only
    its location fields have advanced while lease/fence/owner fields still
    match. A different later takeover cannot be proven from immutable evidence
    and conflicts; other stale heads conflict.
30. `RowAuthorityStore.settle_owner_generation(...)` publicly settles one
    `claimed` generation at the exact current fence. It reads identity, head,
    generation, claim set, candidate settlement, and any prior effective
    settlement before writing. The public B2-B method accepts only
    `terminal/terminal_source`. Contact-opt-out settlement remains an exact
    private transaction-composable planner invoked later by B2-C only after its
    contact settlement/head/fan-out obligation proof; human decline uses the
    dedicated action path. Direct public contact settlement is zero-write
    conflict.
31. Contact-opt-out settlement alone records the prior effective settlement in
    `supersededEffectiveSettlementHash` when one exists. Terminal settlement
    never carries it. Unrelated dominant/operator fields are null. The outcome
    evidence and logical outcome hashes are derived, never accepted from the
    caller. A successful settlement creates one immutable settlement and
    advances the head once to `settled`, clears lease fields, preserves the
    exact fence, and sets both latest/effective settlement hashes to the new
    settlement. On normal re-entry, an exact immutable settlement plus a
    strictly validated head at that settlement or a later location/generation
    state is zero-write `already_applied`; it never restores the older settled
    head. Executor-failure readback still requires the exact immediate
    before/after images.
32. `RowAuthorityStore.record_operator_decline(...)` resolves the exact stored
    thread binding and creates/validates one immutable operator action. If all
    bound rows are exact current `review_pending` generations, it settles those
    same generations at their current fences and creates no second claim. This
    path writes `1 + 2N` documents (action, settlements, heads), maximum 257.
33. If no bound row is review pending, operator decline atomically performs a
    priority-one operator claim. Accepted decline writes action, claim set, and
    generation/settlement/head per row: `2 + 3N`, maximum 386. The final head
    may move directly to `settled`; the immutable generation still stores its
    first fence. A dominated decline writes only action plus dominated claim
    set. Mixed pending/nonpending rows are fail-closed conflict with zero
    writes. The public decline wrapper validates 386 before transaction entry;
    every retry derives and validates the exact `2 + 3N` or dominated count
    after fresh reads and before writes. An exact action plus every immutable
    claim/generation/settlement is
    zero-write `already_applied` after a later valid head transition; later
    heads are never rewritten. Immediate executor failure uses exact full
    before/after readback.
34. Human-decline settlements require the exact operator action hash and
    `operator_decline`; dominated predecessor settlements require the dominant
    generation hash and `superseded_by_higher_priority`. All other conditional
    fields are null. No settlement schema admits provider-effect evidence.

### Read-only B1 post-settlement links

35. `RowAuthorityStore.link_b1_source_settlement(...)` accepts only row ID,
    generation, and caller-frozen `linkedAt`; it derives every hash from stored
    B2/B1 authority. It reads the row identity/head, owner generation, claim
    set, B2 settlement, candidate source link, and exact B1
    `sourceIdentities/{canonicalSourceId}`,
    `sourceClassifications/{canonicalSourceId}`,
    `sourceTransitionOwners/{canonicalSourceId}`,
    `sourceWorkLedgers/{canonicalSourceId}`, and
    `sourceSettlements/{canonicalSourceId}` documents before writes.
36. The B2 claim origin must be `b1_source|contact_fanout`, its embedded B1 link
    must validate exactly, and the generation/claim/B2 settlement must correlate
    to row/generation/hash. The same five-document B1 validator used during B1
    claiming revalidates identity/thread, ready classification and deterministic
    evidence lane, transition owner, one exact delegate-owner work entry, ledger
    hash, final-ledger evidence, and source settlement. It reproduces all
    consumed B1 `hashKind` materials and copies the claim's canonical source,
    snapshot, selection, owner-decision, ledger, owner kind/key, payload, and
    hard-opt-out correlations. B2 does not import `source_coordinator.py`.
37. B1 settlement validation reproduces `settlementHash` over the exact B1
    `source-settlement-v1` material and copies only its `identityHash`,
    `finalLedgerEvidenceHash`, `settlementRevision`, and `settlementHash` into
    the B2 link. No caller can supply those values. Any B1 drift writes nothing.
38. The head stores a source-link hash but not its generation/path. When
    `latestSourceSettlementLinkHash` is non-null and differs from the candidate,
    the transaction performs an exact user-scoped query of
    `rowSourceSettlementLinks` by `sourceSettlementLinkHash`, requires exactly
    one result, validates its document ID as `{sameRowId}--{generation}`, exact
    schema/hash and same row, and completes this query read before any write.
    If the candidate link already exists, the queried current link must have
    `linkedAt >= candidate.linkedAt` and the call is zero-write historical
    replay. If the candidate is absent/new, the queried current link must have
    `linkedAt <= candidate.linkedAt`; the transaction may then create the newer
    candidate and advance the head. Zero, duplicate, cross-row, malformed, or
    wrong-direction timestamp matches are ambiguous. No collection scan or
    unscoped query is permitted.
39. A new source link creates exactly one immutable
    `rowSourceSettlementLinks/{rowId}--{generation}` document and CAS-advances
    `head.latestSourceSettlementLinkHash` in one transaction; B1 receives zero
    writes. An exact existing link is zero-write `already_applied` when the head
    points to it or the exact query proves another same-row non-earlier link.
    A null/missing/drifted current pointer is ambiguous. Link drift is conflict;
    partial immediate apply/readback is ambiguous.

### Failure protocol and containment

40. Every transaction callback resets all captured preparation/readback state
    on each SDK retry. Callback-level validation/config/authority errors
    propagate with zero writes. Failure before callback or an exact complete
    before-image is retryable. An exact complete after-image is success. All
    other executor-failure readback is ambiguous and fail-closed.
41. All mutations use Firestore `create` for immutable records and full
    `set(..., merge=False)` for mutable heads. All reads occur before the first
    write. The B2 fake event log and write-ceiling wrapper prove both properties
    at 1, 128, and overflow boundaries.
42. `row_authority.py` remains standard-library-only and runtime-unwired.
    B2-B does not modify `source_coordinator.py`, provider modules, routes,
    frontend, rules, deployments, or workflows. `row_metadata.py` remains the
    only allowed importer of `row_authority.py`; no runtime module may import
    either B2 module.

## Frozen provider-free API surface

Pure functions added to `email_automation/row_authority.py`:

```python
normalize_row_bindings(row_ids, primary_row_id)
build_thread_row_binding_document(...)
validate_thread_row_binding_document(*, document)
build_row_thread_binding_documents(*, thread_binding_document)
validate_row_thread_binding_document(*, document)

build_contact_row_binding_document(...)
validate_contact_row_binding_document(*, document)
build_contact_row_binding_evidence_document(...)
validate_contact_row_binding_evidence_document(*, document)
build_contact_row_binding_head_document(...)
validate_contact_row_binding_head_document(*, document)

build_b1_authority_link(
    *, user_scope_hash, source_identity_document, source_classification_document,
    source_owner_document, source_ledger_document, work_key
)
validate_b1_authority_link(*, authority_link, user_scope_hash)
derive_owner_priority(owner_kind)
build_operator_action_document(...)
validate_operator_action_document(*, document)
build_claim_set_document(...)
validate_claim_set_document(*, document)
build_owner_generation_document(...)
validate_owner_generation_document(*, document)
build_owner_settlement_document(...)
validate_owner_settlement_document(*, document)
build_source_settlement_link_document(...)
validate_source_settlement_link_document(*, document)
```

Store methods added to `RowAuthorityStore`:

```python
bind_thread_rows(
    verified_user_id, thread_id, client_id, row_ids, primary_row_id, created_at
) -> {disposition, threadBinding, reverseBindings}

record_contact_row_association(
    verified_user_id, canonical_mailbox_identity_hash, exact_identity_hash,
    row_id, thread_id, created_at
) -> {disposition, association, evidence, bindingHead}

claim_row_set(
    verified_user_id, canonical_source_id, work_key, created_at,
    lease_owner_hash, lease_until
) -> {disposition, claimSet, generations, heads, predecessorSettlements}

take_over_expired_lease(
    verified_user_id, row_id, expected_head, new_lease_owner_hash,
    new_lease_until, taken_at
) -> {disposition, generation, head}

settle_owner_generation(
    verified_user_id, row_id, expected_head, settled_at
) -> {disposition, generation, settlement, head}

record_operator_decline(
    verified_user_id, thread_id, actor_scope_hash, client_request_id,
    issued_at
) -> {disposition, action, claimSet, generations, settlements, heads}

link_b1_source_settlement(
    verified_user_id, row_id, generation, linked_at
) -> {disposition, sourceSettlementLink, head}
```

The implementation must introduce transaction-composable private claim,
settlement, and contact-association planners that never create a transaction or
commit on their own. They accept only already validated discriminated
authority/prerequisites
and return deterministic mutation plans for the public B1/decline wrappers and
future B2-C wrapper. It may not expose public authenticated-operator,
contact-fan-out, or direct B1-contact-opt-out mutation paths, or widen any method
with caller-supplied authority link, thread binding, priority, write count,
generation hash, settlement hash, outcome evidence, B1 settlement fields, or
raw row lists for claims.

## File map

- Modify `email_automation/row_authority.py`: B2-B pure schemas/hashes,
  canonical bindings, origin validation, ownership plans, and store methods.
- Create `tests/test_row_authority_ownership.py`: complete B2-B pure,
  transaction, race, readback, bounds, and B1 containment tests.
- Modify `tests/test_row_authority_contracts.py`: expand the exact B2 hash-domain
  registry and static collection/import containment only.
- Modify `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`: child
  plan publication and B2-B code status only.
- Create
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-b.md`: local,
  review, exact-SHA GitHub, and production-posture evidence.

No workflow or B1 fake edit is expected. The existing
`test_row_authority*.py` discovery must collect the new test module, and the
existing bounded fake is sufficient.

## Task order

Task 0 freezes pure binding/contact schemas. Task 1 adds thread transactions.
Task 2 adds contact association transactions. Task 3 freezes B1/origin/operator
and ownership schemas. Task 4 implements all-or-none claims. Task 5 implements
lease takeover. Task 6 implements logical settlement and operator decline.
Task 7 adds read-only B1 source-settlement links. Task 8 performs complete
clearance and publishes B2-B.

Use this interpreter for every Python command:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python
```

## Mandatory plan-publication gate — complete before Task 0

The executor may not modify B2-B application or test code until this child plan
has one independent B2 design-compliance approval and a different independent
fresh-executor/TDD approval. Critical or Important findings reset the
corresponding approval.

- [x] **Step 1: Verify and independently review the plan**

Run:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python - <<'PY'
from pathlib import Path

path = Path(
    "docs/superpowers/plans/"
    "2026-08-04-stable-row-authority-b2-b-ownership.md"
)
text = path.read_text(encoding="utf-8")
assert "**Plan deliverable:** both" in text
assert "**Baseline:** `5676b26ca61ba447e759a36be43d658d1bb8a7a9`" in text
assert "Production remains NO-GO" in text
assert "1 + (2 * N) + D" in text
assert "2 + 3N" in text
assert "sourceSettlements/{canonicalSourceId}" in text
assert text.count("- [ ] **Step") >= 45
print("ok")
PY
git diff --check
! rg -n 'TO[D]O|T[B]D|FIX[M]E|PLACEH[O]LDER|pending decisio[n]' \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-b-ownership.md
```

Expected: parser prints `ok`, diff check has no output, placeholder scan has no
matches, and both fresh reviewers return `APPROVED` with no Critical or
Important finding.

- [x] **Step 2: Freeze and publish only the plan milestone**

After approvals, add this roadmap item immediately before the B2-B code item:

```markdown
- [x] B2-B child plan is independently approved and published.
```

Mark the three plan-gate steps complete, stage exactly the roadmap and this
plan, inspect the staged diff, and commit:

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-b-ownership.md
git diff --cached --stat
git diff --cached --check
git commit -m "docs: plan B2-B row ownership"
```

- [x] **Step 3: Prove the exact remote plan SHA is green**

```bash
git push origin codex/sitesift-production-clearance-20260804
B2_B_PLAN_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_B_PLAN_SHA"

B2_B_PLAN_RUN_ID=""
for attempt in {1..30}; do
  B2_B_PLAN_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_B_PLAN_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_B_PLAN_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_B_PLAN_RUN_ID" && break
  sleep 2
done
test -n "$B2_B_PLAN_RUN_ID"
gh run watch "$B2_B_PLAN_RUN_ID" --exit-status
test "$(gh run view "$B2_B_PLAN_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_B_PLAN_SHA"
test "$(gh run view "$B2_B_PLAN_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
test -z "$(git status --porcelain)"
```

Record the plan SHA and run URL in the implementation log. Do not open a PR,
merge, deploy, or touch production. Task 0 starts only after this succeeds.

### Task 0: Freeze canonical binding and contact schemas

**Files:**

- Modify: `email_automation/row_authority.py`
- Create: `tests/test_row_authority_ownership.py`
- Modify: `tests/test_row_authority_contracts.py`

- [ ] **Step 1: Write failing hash-registry and canonical-binding tests**

Create `RowBindingContractTests` with discriminating tests:

```text
test_binding_domains_are_registered_and_match_independent_vectors
test_binding_hashes_change_for_every_field_scope_null_order_and_domain
test_row_binding_normalization_deduplicates_sorts_and_preserves_one_primary
test_persisted_binding_rejects_empty_missing_primary_duplicate_unsorted_and_drift
test_128_unique_bindings_succeed_and_129_fail_before_reference_or_transaction
test_unsafe_thread_document_ids_fail_before_hash_or_reference_creation
test_binding_builders_and_validators_are_defensive
```

Independently compute fixed expected digests without calling `domain_hash`.
Update the static domain registry before adding production constants so the
first focused run is RED.

- [ ] **Step 2: Run the focused RED tests**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowBindingContractTests \
  tests.test_row_authority_contracts -v
```

Expected: FAIL because binding domains, builders, and validators are absent.

- [ ] **Step 3: Implement the minimum thread/reverse binding contracts**

Add exact domains, canonical normalization, strict builders/validators, safe
thread-document IDs, and reverse-edge construction. Do not add a store mutation
yet.

- [ ] **Step 4: Write failing contact schema/hash tests**

Add `ContactRowBindingContractTests`:

```text
test_contact_binding_domains_match_independent_vectors
test_contact_edge_identity_is_stable_across_thread_evidence
test_contact_evidence_identity_changes_with_thread_binding_or_exact_identity
test_contact_binding_head_accepts_absent_initial_and_exact_empty_shapes
test_contact_schemas_reject_missing_unknown_mistyped_null_count_hash_and_time
test_contact_builders_and_validators_are_defensive
```

- [ ] **Step 5: Run the contact contract tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowBindingContractTests \
  tests.test_row_authority_ownership.ContactRowBindingContractTests \
  tests.test_row_authority_contracts -v
```

Expected: FAIL specifically for missing contact contracts.

- [ ] **Step 6: Implement the minimum pure contact contracts**

Add only the exact contact edge/evidence/head domains, builders, validators,
and independent hashes. Do not add store mutations.

- [ ] **Step 7: Run the focused binding/contact tests GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowBindingContractTests \
  tests.test_row_authority_ownership.ContactRowBindingContractTests \
  tests.test_row_authority_contracts -v
```

Expected: PASS.

- [ ] **Step 8: Run the A0/A1 regressions and commit Task 0**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover -s tests -p 'test_row_authority*.py' -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m compileall -q email_automation tests
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py \
  tests/test_row_authority_contracts.py
git diff --cached --check
git commit -m "test: freeze B2-B binding contracts"
```

### Task 1: Create atomic thread and reverse bindings

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing creation, validation, and late-root tests**

Add `ThreadRowBindingStoreTests`:

```text
test_thread_binding_reads_all_binding_identity_head_and_edges_before_writes
test_thread_binding_creates_one_plus_n_documents_atomically
test_binding_rejects_missing_malformed_scope_client_identity_or_head_with_zero_writes
test_deleted_identity_remains_bindable_for_late_root_without_head_mutation
test_thread_binding_time_equal_latest_prerequisite_is_valid_and_earlier_is_rejected
test_binding_ignores_legacy_row_number_rows_and_thread_order
test_exact_binding_and_edges_retry_is_zero_write_already_applied
test_partial_presence_is_ambiguous_and_immutable_drift_is_conflict
```

- [ ] **Step 2: Run the focused RED tests**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ThreadRowBindingStoreTests -v
```

Expected: FAIL because `bind_thread_rows` is absent.

- [ ] **Step 3: Implement the minimum deterministic binding transaction**

Use `create` only, capture exact before/after tuples, reset callback state on
every retry, and return defensive binding/edge documents. No claim behavior.

- [ ] **Step 4: Add failing race and executor-failure tests**

```text
test_identical_binding_workers_yield_created_and_already_applied
test_divergent_binding_workers_preserve_first_commit
test_binding_preapply_failure_is_retryable_with_zero_writes
test_binding_apply_then_raise_requires_exact_binding_and_all_edges
test_binding_partial_or_malformed_readback_is_ambiguous
test_binding_129_overflow_never_opens_transaction
```

- [ ] **Step 5: Run the new race/readback tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ThreadRowBindingStoreTests -v
```

Expected: FAIL specifically for concurrency, executor failure/readback, or
overflow behavior that has not been implemented.

- [ ] **Step 6: Implement the minimum race/readback and bound behavior**

Add fresh-retry state reset, exact full before/after classification, immutable
first-winner handling, and pretransaction 129-binding rejection. Do not weaken
partial-state ambiguity.

- [ ] **Step 7: Run focused GREEN plus exact write-bound checks**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ThreadRowBindingStoreTests -v
```

Expected: PASS, including exact 2-write and 129-write success cases and
pretransaction 129-binding rejection.

- [ ] **Step 8: Run B2 regressions and commit Task 1**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover -s tests -p 'test_row_authority*.py' -v
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py
git diff --cached --check
git commit -m "feat: add immutable row bindings"
```

### Task 2: Record stable contact-row association evidence

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing association transaction tests**

Add `ContactRowAssociationStoreTests`:

```text
test_first_contact_association_reads_prerequisites_then_writes_exact_three
test_empty_contact_binding_head_advances_to_one
test_existing_edge_new_evidence_writes_only_evidence_and_preserves_head
test_exact_edge_evidence_and_head_retry_is_zero_write
test_supporting_thread_binding_and_reverse_edge_are_required_and_exact
test_deleted_row_accepts_historical_contact_evidence_but_grants_no_claim
test_existing_contact_optout_head_blocks_association_with_zero_writes
test_association_never_accesses_alias_settlement_fanout_or_release_collections
```

- [ ] **Step 2: Run focused RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ContactRowAssociationStoreTests -v
```

Expected: FAIL because the store method is absent.

- [ ] **Step 3: Implement the minimum single-row CAS association**

Validate hashes before references, require stored thread/reverse proof, read the
B2-C head-existence sentinel before writes, and support only 3/1/0 write
dispositions. Do not infer mailbox aliases or create fan-out work.

- [ ] **Step 4: Add failing concurrency and readback tests**

```text
test_identical_first_association_workers_create_one_edge_and_evidence
test_different_evidence_workers_create_one_edge_two_evidence_and_count_one
test_different_row_workers_cas_retry_to_count_two
test_old_association_retry_after_another_row_preserves_advanced_head
test_same_evidence_id_timestamp_drift_is_conflict
test_contact_evidence_time_cannot_precede_thread_binding_or_association
test_missing_head_edge_without_evidence_or_evidence_without_edge_is_ambiguous
test_association_preapply_failure_is_retryable
test_association_apply_then_raise_accepts_only_exact_disposition_after_image
test_association_partial_readback_is_ambiguous
test_private_association_planner_never_opens_or_commits_a_transaction
```

- [ ] **Step 5: Run the new concurrency/readback tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ContactRowAssociationStoreTests -v
```

Expected: FAIL specifically for concurrency, historical replay, timestamp,
private-planner, or executor readback behavior not yet implemented.

- [ ] **Step 6: Implement the minimum concurrency, replay, and readback behavior**

Factor the non-executing mutation planner, retain the public absent-opt-out-head
guard, distinguish immutable historical replay from strict executor readback,
and preserve a later valid binding head byte-for-byte.

- [ ] **Step 7: Run focused GREEN and regressions**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.ContactRowAssociationStoreTests -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py
git diff --cached --check
git commit -m "feat: add stable contact row evidence"
```

### Task 3: Freeze B1, origin, operator, claim, generation, and settlement schemas

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`
- Modify: `tests/test_row_authority_contracts.py`

- [ ] **Step 1: Write failing ownership-domain vector tests**

Add `RowOwnershipContractTests` covering every B2-B domain:

```text
test_all_ownership_domains_are_registered_and_match_independent_vectors
test_every_ownership_hash_changes_for_field_scope_null_time_and_domain_drift
test_b1_link_is_derived_from_exact_identity_classification_owner_ledger_bundle
test_b1_link_exact_schema_owner_correlations_and_hard_optout_requirement
test_b1_link_hash_changes_with_user_scope_and_cross_scope_validation_fails
test_hard_optout_link_uses_validated_nonlocal_deterministic_evidence_hash
test_forged_or_model_only_optout_bundle_cannot_build_priority_three_link
test_planned_write_validator_accepts_nonboolean_uint_0_through_400_and_rejects_401
test_priority_is_derived_and_cannot_be_supplied
test_operator_action_id_request_hash_and_action_hash_are_deterministic
test_claim_origin_union_rejects_every_invalid_cross_field_combination
test_accepted_and_dominated_claim_decisions_enforce_exact_nullability
test_generation_settlement_and_source_link_schemas_enforce_correlated_nulls
test_all_ownership_builders_and_validators_are_defensive
```

- [ ] **Step 2: Run focused RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowOwnershipContractTests \
  tests.test_row_authority_contracts -v
```

Expected: FAIL because ownership domains/builders are absent.

- [ ] **Step 3: Implement strict pure ownership contracts only**

Add fixed domains, independent exact-schema/hash validators for the B1 source
identity/classification/owner/ledger projections, bundle-derived B1 links,
the named pure planned-write validator, exact origin unions, derived priority,
operator action, claim set, generation, outcome evidence, logical outcome,
settlement, source link, and full head-transition helpers. The B1-link builder
requires the store-derived user scope. Preserve A1 head validation and location
fields exactly. A pure caller-field B1-link builder is forbidden.

- [ ] **Step 4: Run focused GREEN, full B2, compile, and commit Task 3**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowOwnershipContractTests \
  tests.test_row_authority_contracts -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover -s tests -p 'test_row_authority*.py' -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m compileall -q email_automation tests
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py \
  tests/test_row_authority_contracts.py
git diff --cached --check
git commit -m "test: freeze B2-B ownership contracts"
```

### Task 4: Implement all-or-none priority claims

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing accepted-claim tests**

Add `RowClaimStoreTests`:

```text
test_b1_claim_derives_link_from_exact_stored_source_bundle_and_thread_binding
test_b1_claim_rejects_missing_malformed_hash_drifted_wrong_thread_or_wrong_work_bundle
test_unsafe_canonical_source_or_b1_thread_id_fails_before_reference_or_transaction
test_well_formed_forged_terminal_or_model_optout_link_cannot_enter_public_claim
test_public_b1_contact_optout_claim_is_blocked_until_b2c
test_public_authenticated_operator_and_contact_fanout_claims_do_not_exist
test_private_operator_and_contact_planners_never_open_nested_transactions
test_first_claim_creates_claim_set_generation_and_head_per_row
test_human_claim_enters_review_pending_without_settlement
test_terminal_claim_enters_claimed
test_active_and_nonviable_are_claimable_deleted_and_ambiguous_are_not
test_claim_reads_b1_bundle_then_binding_then_derived_claim_set_then_rows_before_writes
test_claim_rejects_event_time_before_binding_identity_or_current_head
test_claim_time_must_follow_immutable_b1_identity_snapshot_owner_and_ledger_readiness
test_claim_validates_385_worst_case_before_executor_and_exact_count_before_write
```

- [ ] **Step 2: Run focused RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowClaimStoreTests -v
```

Expected: FAIL because `claim_row_set` is absent.

- [ ] **Step 3: Implement minimum accepted-claim planning and transaction**

Validate the mathematical 385-write worst case before opening the transaction.
Inside every fresh callback, read the four stored B1 documents, derive and
validate the user-scoped B1 link, reject direct opt-out, resolve/read the thread
binding, derive the request ID, read the candidate claim set and row state, then
derive/validate the exact write count before the first write. Build claim-set
hash before generation hashes. Use full head replacement and immutable creates
only. Keep non-B1 origins in a private non-executing planner for their atomic
wrappers.

- [ ] **Step 4: Write failing priority, supersession, and all-or-none tests**

```text
test_private_validated_optout_plan_dominates_terminal_and_human
test_terminal_dominates_human
test_unverified_model_optout_cannot_exceed_human_priority
test_equal_priority_first_commit_wins_without_lexical_election
test_lower_claim_writes_only_dominated_claim_set
test_multirow_dominated_marks_peers_blocked_and_advances_no_generation
test_higher_claim_dominates_unsettled_predecessor_and_preserves_effective_settlement
test_higher_claim_after_takeover_freezes_current_not_first_fence
test_higher_claim_never_rewrites_settled_lower_generation
test_ownerless_historical_postrelease_shape_fails_closed
test_first_and_next_generation_allocation_are_exact
```

- [ ] **Step 5: Run priority/supersession tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowClaimStoreTests -v
```

Expected: FAIL specifically for priority, all-or-none, current-fence, and
supersession behaviors just added.

- [ ] **Step 6: Implement dominated/supersession decisions and exact formulas**

Add `1 + 2N + D` accepted planning, one-write dominated planning, immutable
dominated predecessor settlements, and exact head settlement-field semantics.

- [ ] **Step 7: Write failing idempotency, race, overflow, and readback tests**

```text
test_exact_claim_retry_is_zero_write_already_applied
test_exact_claim_retry_after_location_settlement_or_higher_generation_keeps_later_head
test_existing_request_hash_or_timestamp_drift_is_conflict
test_identical_workers_create_one_claim_and_both_succeed
test_different_equal_priority_workers_preserve_first_commit
test_claim_128_rows_stays_at_or_below_385_writes
test_claim_rejects_malformed_stored_129_row_binding_with_zero_writes
test_claim_preapply_failure_is_retryable_with_zero_writes
test_claim_apply_then_raise_requires_claim_generations_heads_and_dominated_settlements
test_claim_partial_malformed_or_unreadable_readback_is_ambiguous
test_transaction_retry_rebuilds_every_decision_from_fresh_reads
```

- [ ] **Step 8: Run the new tests and observe the expected RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowClaimStoreTests -v
```

Expected: FAIL specifically for later-head resume, race/readback, and malformed
stored-binding behavior.

- [ ] **Step 9: Implement the minimum resume, race, and malformed-state behavior**

Separate immutable normal replay from strict immediate executor readback,
validate forward heads without restoring them, rebuild every decision from
fresh retry reads, and keep malformed/partial state zero-write and fail-closed.

- [ ] **Step 10: Run focused GREEN and full B2 regressions**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowClaimStoreTests -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
```

Expected: PASS.

- [ ] **Step 11: Commit Task 4**

```bash
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py
git diff --cached --check
git commit -m "feat: add all or none row claims"
```

### Task 5: Take over expired leases and enforce fences

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing lease tests**

Add `RowLeaseTakeoverTests`:

```text
test_expired_claimed_lease_takeover_advances_one_head_and_same_generation
test_expired_review_pending_lease_takeover_preserves_pending_state
test_takeover_increments_fence_and_state_revision_but_not_lease_epoch
test_unexpired_wrong_state_settled_or_malformed_takeover_writes_nothing
test_takeover_requires_exact_generation_and_absent_settlement
test_exact_old_head_takeover_replay_is_zero_write_already_applied
test_takeover_replay_after_location_only_advance_preserves_new_location
test_takeover_replay_after_different_takeover_conflicts_without_rewind
test_takeover_time_equal_to_head_update_is_valid_and_earlier_is_rejected
test_other_stale_head_takeover_is_conflict
test_takeover_preapply_and_apply_then_raise_classification_is_exact
```

- [ ] **Step 2: Run the lease tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowLeaseTakeoverTests -v
```

Expected: FAIL specifically because takeover behavior is absent.

- [ ] **Step 3: Implement the minimum one-head takeover CAS**

Validate exact current generation/settlement absence and timestamp lineage,
write only the full head, preserve immutable lease epoch, classify exact
location-only descendant replay, and conflict on a different later takeover.

- [ ] **Step 4: Run focused GREEN and stale-fence regressions**

Prove the old fence no longer matches the current head and that no generation or
settlement changed during takeover.

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowLeaseTakeoverTests -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
```

- [ ] **Step 5: Commit Task 5**

```bash
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py
git diff --cached --check
git commit -m "feat: fence row authority leases"
```

### Task 6: Settle generations and authenticate row-wide decline

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing logical settlement tests**

Add `RowSettlementStoreTests`:

```text
test_terminal_settlement_creates_exact_record_and_settled_head
test_private_contact_optout_settlement_freezes_prior_effective_settlement
test_public_contact_optout_settlement_is_blocked_until_b2c
test_terminal_cannot_carry_superseded_effective_settlement
test_settlement_derives_outcome_reason_evidence_and_logical_hash
test_settlement_requires_current_generation_current_fence_and_claimed_state
test_stale_fence_after_takeover_cannot_settle_or_change_head
test_exact_settlement_retry_is_zero_write_already_applied
test_settlement_retry_after_location_or_higher_generation_preserves_later_head
test_settlement_time_must_follow_claim_generation_and_current_head
test_settlement_preapply_apply_then_raise_and_partial_readback_are_classified
test_settlement_schema_contains_no_provider_effect_fields
test_private_settlement_planner_never_opens_or_commits_a_transaction
```

- [ ] **Step 2: Run the settlement tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowSettlementStoreTests -v
```

Expected: FAIL specifically because the settlement mutation is absent.

- [ ] **Step 3: Implement the minimum one-row settlement path**

Derive the outcome from the current generation, require its current fence,
enforce timestamp lineage, create one immutable settlement, replace one full
head, and separate later-head immutable replay from strict executor readback.
Both the public terminal wrapper and future B2-C contact wrapper use the same
non-executing private settlement mutation-plan contract; it never creates or
commits a transaction.

- [ ] **Step 4: Run the settlement tests GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowSettlementStoreTests -v
```

Expected: PASS.

- [ ] **Step 5: Write failing operator-decline tests**

Add `RowOperatorDeclineStoreTests`:

```text
test_pending_decline_creates_action_and_settles_same_generation_without_claim
test_no_pending_decline_creates_action_claim_generation_settlement_and_head_atomically
test_higher_owner_dominates_operator_decline_with_only_action_and_claim_set
test_mixed_pending_and_nonpending_binding_fails_closed
test_actor_target_client_request_action_or_timestamp_drift_writes_nothing
test_operator_action_time_equal_current_head_is_valid_and_earlier_is_rejected
test_pending_decline_128_rows_plans_257_writes
test_no_pending_decline_128_rows_plans_386_writes
test_decline_validates_386_worst_case_before_executor_and_exact_count_before_write
test_decline_does_not_expose_dismiss_stop_or_resume_mutations
test_operator_decline_exact_retry_race_and_readback_are_idempotent
test_operator_decline_retry_after_higher_transition_preserves_later_head
```

- [ ] **Step 6: Run the operator-decline tests and observe RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowOperatorDeclineStoreTests -v
```

Expected: FAIL specifically because the dedicated atomic decline paths are
absent.

- [ ] **Step 7: Implement the minimum dedicated atomic decline paths**

Implement all-pending same-generation settlement and none-pending
action-plus-private-claim-planner-plus-immediate-settlement. Keep mixed state a
conflict, enforce exact formulas/timestamps, and make immutable replay preserve
later valid heads.

- [ ] **Step 8: Run focused GREEN, full B2, and B1 regressions**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowSettlementStoreTests \
  tests.test_row_authority_ownership.RowOperatorDeclineStoreTests -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator -v
```

Expected: PASS.

- [ ] **Step 9: Commit Task 6**

```bash
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py
git diff --cached --check
git commit -m "feat: settle fenced row ownership"
```

### Task 7: Link independently settled B1 evidence without B1 writes

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_ownership.py`
- Modify: `tests/test_row_authority_contracts.py`

- [ ] **Step 1: Write failing B1 projection/link tests**

Add `RowSourceSettlementLinkTests`:

```text
test_source_link_reads_exact_b1_identity_classification_owner_ledger_and_settlement
test_source_link_copies_identity_final_ledger_revision_and_hash_from_b1_settlement
test_source_link_reuses_full_b1_bundle_validation_and_reproduces_settlement_hash
test_source_link_requires_b1_or_contact_origin_and_matching_work_entry
test_b1_canonical_source_snapshot_selection_owner_ledger_or_hard_evidence_drift_writes_nothing
test_source_link_creates_immutable_link_and_cas_advances_head
test_source_link_performs_zero_writes_to_every_b1_collection
test_exact_existing_source_link_is_zero_write_even_after_later_head_link
test_new_source_link_after_older_link_validates_old_pointer_then_advances
test_existing_link_with_missing_or_invalid_head_pointer_is_ambiguous
test_later_link_hash_query_requires_one_same_row_non_earlier_exact_result
test_later_link_query_zero_duplicate_cross_row_or_malformed_result_is_ambiguous
test_later_link_query_completes_before_any_source_link_or_head_write
test_source_link_time_must_follow_b1_b2_settlements_and_current_head
test_source_link_drift_is_conflict_and_partial_readback_is_ambiguous
```

- [ ] **Step 2: Run focused RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowSourceSettlementLinkTests \
  tests.test_row_authority_contracts -v
```

Expected: FAIL because B1 read-only projection validators and the link store
method are absent.

- [ ] **Step 3: Implement narrow independent B1 validation and link CAS**

Reuse the exact five-document B1 projection validator and reproduce B1
canonical JSON hashes locally without importing B1. Read every B1 reference
through the same transaction, never stage a B1 reference, validate final-ledger
evidence and settlement-time lineage, and derive all link fields. Update only
the B2 source-link record and B2 row head. When an exact target link exists but
the head points elsewhere, run the exact user-scoped hash query, require one
same-row non-earlier exact record, and complete that query read before returning
`already_applied`. When the target is new, require one same-row non-later exact
record before creating the candidate and advancing the head.

- [ ] **Step 4: Run focused GREEN, B1 regressions, compile, and containment**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_ownership.RowSourceSettlementLinkTests \
  tests.test_row_authority_contracts -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest tests.test_source_coordinator -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover -s tests -p 'test_row_authority*.py' -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m compileall -q email_automation tests
git diff --check
```

Expected: PASS with no runtime/provider import and no B1 source change.

- [ ] **Step 5: Commit Task 7**

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py \
  tests/test_row_authority_contracts.py
git diff --cached --check
git commit -m "feat: link row outcomes to B1 evidence"
```

### Task 8: Verify, review, evidence, and publish B2-B

**Files:**

- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
- Create: `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-b.md`

- [ ] **Step 1: Run the complete local clearance gates from a clean index**

```bash
export FIRESTORE_EMULATOR_HOST=127.0.0.1:9
export GOOGLE_CLOUD_PROJECT=sitesift-offline-ci
export OPENAI_API_KEY=
export SITESIFT_OUTBOUND_MODE=paused
export SITESIFT_SOURCE_COORDINATOR_MODE=disabled
export HTTP_PROXY=http://127.0.0.1:9
export HTTPS_PROXY=http://127.0.0.1:9
export ALL_PROXY=http://127.0.0.1:9
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export all_proxy=http://127.0.0.1:9
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  auth_service.test_auth_service_isolation \
  tests.test_jill_live_campaign_regressions \
  tests.test_full_campaign_e2e -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator_inventory \
  tests.test_source_coordinator \
  tests.test_source_coordinator_integration \
  tests.test_processing_retryability \
  tests.test_event_processing_order \
  tests.test_compound_nonviable_processing \
  tests.test_operator_message_replay \
  tests.test_pending_responses \
  tests.test_cleanup_retention \
  tests.test_system_health -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
SITESIFT_OUTBOUND_MODE=live \
  ../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_action_audit_backend \
  tests.test_broker_language_broker_attachment_or_link_only \
  tests.test_combo_karsen_launch_placeholder_and_tour_leak \
  tests.test_compound_nonviable_processing \
  tests.test_go_condition_send_failure_observability \
  tests.test_graph_immutable_sent_identity \
  tests.test_graph_message_id_path_encoding \
  tests.test_graph_subject_binding \
  tests.test_operator_message_replay \
  tests.test_outbound_kill_switch \
  tests.test_pending_completion_health \
  tests.test_pending_draft_review_resolution_api \
  tests.test_pending_responses \
  tests.test_pending_send_reconciliation_api \
  tests.test_post_settlement_completion_obligations \
  tests.test_processing_completion_guards \
  tests.test_processing_reply_indexing \
  tests.test_processing_reply_safety \
  tests.test_processing_retryability \
  tests.test_send_permits \
  tests.test_surface_d_6_ \
  tests.test_system_health \
  tests.test_terminal_completion_replay -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m compileall -q email_automation scripts tests
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m pip check
../codex-release-a-medium-recovery-20260714/.venv/bin/python - <<'PY'
from pathlib import Path
import yaml

for path in Path(".github/workflows").glob("*.yml"):
    yaml.safe_load(path.read_text(encoding="utf-8"))
print("ok")
PY
git diff --check
git status --short
```

Expected: every exact CI-mirrored suite PASS, compile/pip/YAML PASS, diff check
empty, index empty, and worktree clean. Evidence and roadmap edits begin only
after this clean-code checkpoint and the two fresh approvals.

- [ ] **Step 2: Obtain two fresh full-diff approvals**

Reviewer A checks the complete B2 design/roadmap compliance, transaction
invariants, cross-user/row safety, contact/B1 boundaries, and exact schemas.
Reviewer B independently checks executor-failure classification, race
linearizability, 400-write arithmetic, TDD discrimination, provider-free/runtime
containment, and regression evidence. Critical or Important findings require a
new RED reproducer, minimum fix, full gates, and fresh approval.

- [ ] **Step 3: Commit the reviewed B2-B code candidate**

```bash
git status --short
git diff --check
git add email_automation/row_authority.py \
  tests/test_row_authority_ownership.py \
  tests/test_row_authority_contracts.py
git diff --cached --stat
git diff --cached --check
git commit -m "feat: complete B2-B row ownership authority"
B2_B_CODE_SHA="$(git rev-parse HEAD)"
```

Skip this commit if Task 7 already leaves no code changes; use that exact HEAD
as `B2_B_CODE_SHA`. Do not amend a reviewed SHA after approval.

- [ ] **Step 4: Push and prove the exact B2-B code SHA in GitHub Actions**

```bash
B2_B_CODE_SHA="$(git rev-parse HEAD)"
git push origin codex/sitesift-production-clearance-20260804
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_B_CODE_SHA"

B2_B_RUN_ID=""
for attempt in {1..30}; do
  B2_B_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_B_CODE_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_B_CODE_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_B_RUN_ID" && break
  sleep 2
done
test -n "$B2_B_RUN_ID"
gh run watch "$B2_B_RUN_ID" --exit-status
test "$(gh run view "$B2_B_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_B_CODE_SHA"
test "$(gh run view "$B2_B_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
gh run view "$B2_B_RUN_ID" --json url,jobs \
  --jq '{runUrl: .url, jobs: [.jobs[] | {name, url, conclusion}]}'
gh run view "$B2_B_RUN_ID" --log | \
  rg 'Ran [0-9]+ tests in|OK$|No broken requirements|(^| )ok$'
```

- [ ] **Step 5: Write immutable evidence and update the roadmap**

Create the evidence file with:

- exact baseline, plan, and B2-B code SHAs;
- changed file/collection/API inventory;
- RED/GREEN discriminators and exact local suite counts/durations;
- independent reviewer names/verdicts and resolved findings;
- exact GitHub run/job URLs and code SHA;
- no-provider/no-runtime/no-B1-write containment proof;
- explicit production posture: runtime-unwired, no deploy/campaign, NO-GO;
- next gate: B2-C child plan publication.

Change only the roadmap B2-B status from unchecked to checked.

- [ ] **Step 6: Commit, push, and prove the final evidence SHA**

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-b.md
git diff --cached --check
git commit -m "docs: record B2-B ownership evidence"
git push origin codex/sitesift-production-clearance-20260804
B2_B_FINAL_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_B_FINAL_SHA"

B2_B_FINAL_RUN_ID=""
for attempt in {1..30}; do
  B2_B_FINAL_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_B_FINAL_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_B_FINAL_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_B_FINAL_RUN_ID" && break
  sleep 2
done
test -n "$B2_B_FINAL_RUN_ID"
gh run watch "$B2_B_FINAL_RUN_ID" --exit-status
test "$(gh run view "$B2_B_FINAL_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_B_FINAL_SHA"
test "$(gh run view "$B2_B_FINAL_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
test "$(git rev-parse HEAD)" = "$B2_B_FINAL_SHA"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_B_FINAL_SHA"
test -z "$(git status --porcelain)"
```

- [ ] **Step 7: Stop at the B2-C planning boundary**

Do not implement B2-C, B3, B4, runtime adoption, frontend changes, rules,
migration execution, deploy, campaign, or a Jill return decision under this
plan. Publish the exact B2-B SHA/run and retain production NO-GO. The next
milestone begins by independently approving and publishing
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md`.
