# B2-C Contact Compliance Contract Amendment

**Status:** Approved by two independent reviewers

**Goal:** Make verified contact-wide opt-out, immediate fail-closed
suppression, bounded row convergence, and authenticated release safe across
retries, races, late row associations, and repeated opt-out/release cycles.

**Deliverable:** both (provider-free contact-authority code and
production-clearance findings)

**Depends on:**

- `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`
- `docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md`
- B1/B2 identity-bridge evidence commit
  `3904c79f65e54d4e146189e50845c6fb078f7c3d`

**Production posture:** NO-GO. This amendment is provider-free and
runtime-unwired. It authorizes no deployment, provider call, campaign,
production data access, user enablement, or Jill return decision.

## Why the approved B2 design needs this amendment

The base B2 design correctly fixes contact identity, authority priority,
contact fan-out, and release restoration. Implementation inventory and two
independent pre-code audits exposed five underspecified boundaries that must be
frozen before B2-C code exists:

1. A released row restores an older effective owner or clear state while its
   immutable opt-out settlement remains the latest historical settlement.
   Allocating from only the restored effective generation can therefore reuse
   an immutable generation ID and fencing token.
2. A second verified opt-out while the same canonical contact is already
   active cannot safely create another priority-3 row claim. Equal priority
   makes the newer fan-out dominated, and releasing only the newer contact
   epoch would leave the older row opt-out active.
3. A contact settlement addressed only by canonical identity and generation
   has no stable request key. An exact retry after an unknown commit outcome
   could append a second contact generation instead of reading back the first.
4. A persisted row-ID cursor can miss a concurrently inserted earlier-sorted
   association unless binding-snapshot drift resets the phase cursor. A
   completed release also needs an explicit health rule for later associations
   that correctly need no release work.
5. Release restore and delayed B1 source-settlement linking need bounded exact
   proof after the released row owner is no longer effective. Complete lineage
   reads and the existing priority-depth generation bound cannot support
   repeated cycles.

This document is normative for B2-C and overrides only the conflicting or
underspecified B2 passages named below. All existing B2-A/B contracts and
published byte vectors remain fixed unless an acceptance test in this
amendment explicitly requires a release-aware refactor.

## Additional hash domains and exact records

Hash inputs continue to use the B2 byte contract:

```text
domain.encode("utf-8") + b"\0" + canonical_json_bytes(material)
```

Every material includes `schemaVersion: 1` and `userScopeHash` at top level.
Every document has the B2 exact-key, exact-type, exact-path, explicit-null, and
hash-reproduction rules. The following domains are added:

| Value | Domain | Complete logical payload beyond schema/user scope |
|---|---|---|
| `contactTransitionId` | `sitesift.contact.optout_transition_id.v1` | transition kind, exact/canonical identity hashes, nullable B1 authority-link/hard-opt-out hashes, nullable actor-scope/client-request/expected-active-settlement hashes, nullable release reason |
| `contactTransitionRequestHash` | `sitesift.contact.optout_transition_request.v1` | transition ID and all identity fields above, outcome, resulting contact generation/settlement/head/fan-out hashes, requested time |

`contactOptOutTransitionRequests/{contactTransitionId}` contains exactly these
fields after `schemaVersion` and `userScopeHash`:

```text
contactTransitionId: h64
transitionKind: verified_optout|authenticated_release
exactIdentityHash: h64
canonicalMailboxIdentityHash: h64
authorityLinkHash: h64?
hardOptOutEvidenceHash: h64?
actorScopeHash: h64?
clientRequestHash: h64?
expectedActiveOptOutSettlementHash: h64?
reasonCode: authenticated_release?
outcome: created|already_active
resultingContactGeneration: pos
resultingContactSettlementHash: h64
resultingFanoutId: h64
resultingContactHeadHash: h64
resultingFanoutHeadHash: h64
requestedAt: ts
contactTransitionRequestHash: h64
```

A verified opt-out transition ID has non-null B1 authority-link and hard
evidence hashes, null actor/request/expected-active/reason fields, and derives
both identity hashes only from the validated v2 B1 link. An authenticated
release transition ID has null B1/evidence fields and non-null actor scope,
client-request hash, exact expected active settlement, and reason. Release
derives both identity hashes from that exact active settlement and its valid
aliases; it cannot introduce a mailbox binding.

`clientRequestHash` uses the already frozen B2 operator client-request domain
and bounded opaque request-ID contract. `outcome == already_active` is valid
only for `verified_optout`; it points to the exact current active settlement
and fan-out without appending a contact generation. Every release receipt and
the first opt-out receipt use `created`. The two resulting mutable-head hashes
freeze their exact transaction after-images in the immutable receipt; those
documents may later advance normally. `requestedAt` is the winning attempt's
immutable audit time and is intentionally excluded from the transition ID. An
existing receipt with the same semantic authority is returned even when a
later retry supplies a different observation time; actor, client-request,
expected settlement, reason, B1 authority, or identity drift is not a retry.

The contact settlement schema gains one mandatory field,
`contactTransitionId: h64`, and that field joins the
`sitesift.contact.optout_settlement.v1` hash payload. For a `created` receipt,
the referenced settlement's transition ID equals the receipt ID. For an
`already_active` receipt, the referenced settlement belongs to the earlier
creating receipt; the new receipt is the durable proof that this later B1
authority was accepted under already-active contact authority. This
one-directional correlation avoids a hash cycle.

Every consumer of a contact settlement uses one shared exact loader. It reads
the settlement's `contactTransitionId`, loads the immutable creating receipt,
and requires `outcome == created`, matching kind/identity/generation/settlement/
fan-out, and a reproduced receipt hash. Suppression, a later transition,
`already_active` receipt creation, discovery, apply, release, supersession, and
health may not trust a contact settlement without this receipt. In particular,
a release settlement with a missing or mismatched client-request-bearing
receipt is fail-closed and can never authorize `allow`.

The B2-C fan-out-result schema also gains four mandatory nullable address
fields: `claimRequestId: h64?`, `rowGeneration: pos?`,
`releasedRowGeneration: pos?`, and
`restoredEffectiveGeneration: pos?`. They join the existing result hash
payload and correlate with their matching hashes under the exhaustive result
matrix below. B2-C is the first implementation of this record, so this
clarification changes no published result byte vector.

The fan-out-head schema gains mandatory
`bindingAssociationCount: uint` beside the current binding revision/hash, plus
six mandatory completion-certificate fields:
`completionBindingRevision: uint?`, `completionBindingHeadHash: h64?`,
`completionBindingAssociationCount: uint?`,
`completionObligationCount: uint?`, `completionResultCount: uint?`, and
`completedAt: ts?`. They join the existing fan-out-head hash payload. Binding
revision `0` requires null binding hash and association count `0`; a positive
revision requires a non-null hash and copies the independently validated
binding head's exact association count. All six completion fields are null
outside `complete`. A complete head requires the revision, association count,
both work counts, and time; completion revision `0` requires a null completion
binding hash and a positive revision requires a non-null hash. These fields
freeze the first complete certificate and never change during a
late-association recertification. B2-C is also the first implementation of this
record, so no published fan-out-head byte changes.

For every complete fan-out:

```text
completionObligationCount
  == completionResultCount
  == completionBindingAssociationCount
completionBindingRevision <= bindingRevision
completionBindingAssociationCount <= bindingAssociationCount
bindingRevision - completionBindingRevision
  == bindingAssociationCount - completionBindingAssociationCount
completedAt <= updatedAt
```

Revision and association count are deliberately independent B2-B fields. For
`outcome == apply`, current `obligationCount == resultCount ==
bindingAssociationCount`. For
`outcome == release`, current counts stay equal to the frozen completion counts
while current binding revision and association count may advance together
through exact released-complete recertification. Every crossed relation is
invalid.

## Contact alias invariants

Verified opt-out validates or atomically creates the exact identity alias and
the canonical identity self-alias:

| Exact alias | Canonical self-alias | Result |
|---|---|---|
| absent | absent | create both, or one document when the hashes are equal |
| absent | valid | create only the exact alias |
| valid | valid | no alias write |
| valid | absent | ambiguous; zero writes |
| conflicting or malformed | any | ambiguous; zero writes |
| any | conflicting or malformed | ambiguous; zero writes |

An authenticated release requires both exact and canonical self-aliases to
exist and validate; it never repairs or creates either alias. Alias document
timestamps are immutable creation evidence and are not rewritten by retry.

## Stable contact-transition semantics

### First verified opt-out or verified opt-out after release

`record_verified_contact_optout` accepts a stored, fully validated B1 bundle
and derives the v2 B1 link internally. No caller can supply an authority link,
hard-opt-out hash, exact identity hash, canonical identity hash, generation,
priority, settlement hash, fan-out ID, or receipt outcome.

In one transaction it:

1. validates the strict v2 B1 link and alias state;
2. proves the deterministic transition request does not already exist;
3. validates an absent contact head or an exact released head and its latest
   settlement;
4. appends generation `1` or `latestGeneration + 1` with transition kind
   `verified_optout` and the transition ID;
5. advances the contact head to `active` and points it at the new apply fan-out;
6. creates the unleased `discovering` apply fan-out at fence `1` and the
   current exact
   contact-binding snapshot; and
7. creates the `created` request receipt.

If the prior contact head is released, its current release fan-out must be
valid `discovering|applying|complete`. A missing, ambiguous, malformed,
`superseding|superseded`, or incorrectly linked current fan-out blocks the new
transition. A nonterminal prior fan-out is CAS-advanced to `superseding`, its
lease is cleared, its fence increments, and the new contact transition and
fan-out are committed atomically.

### Verified opt-out while already active

The same deterministic transition ID is an exact retry and reads its stored
receipt before inspecting or allocating current authority. It validates the
immutable referenced settlement/creating receipt and treats the receipt's
frozen head hashes as the committed after-image. Current mutable contact and
fan-out heads may be any strictly validated reachable successor, including
progress, completion, supersession, release, or a later active epoch. The retry
returns without mutation even if current health is separately fail-closed. A
different valid v2 B1 authority for the same
canonical identity does **not** append a contact settlement, advance the
contact head, or create a fan-out. It atomically creates any permitted missing
exact alias plus an `already_active` request receipt pointing to the exact
current active settlement and current apply fan-out.

Before that receipt is created, the transaction validates the current contact
head, active settlement and its creating receipt, aliases, and fan-out
correlation and requires the current fan-out to be
`discovering|applying|complete`. A concurrent release or
head/fan-out change causes a transaction retry from fresh reads. A
`superseding|superseded|ambiguous`, missing, or malformed current fan-out is
zero-write ambiguity.

The receipt, active head, referenced contact settlement, and fan-out head are
durable B2 completion evidence for the later B1 source. B4 may let B1 settle
from this exact receipt without inventing a second contact epoch. No later B1
source acquires a row source-settlement link unless one of its own B1 links was
actually frozen into a row claim; the receipt is its contact-level evidence.

This rule preserves equal-priority first-winner row semantics and lets one
authenticated release clear the current canonical suppression state, including
older same-canonical row work left by a superseded release fan-out.

### Authenticated release

`record_authenticated_contact_release` requires the exact canonical contact
hash, expected active contact settlement hash, authenticated actor scope,
opaque client request ID, bounded reason `authenticated_release`, and
caller-frozen request time. It derives the exact identity from the
active settlement and validates both aliases.

Before computing the release transition ID, the method performs one bounded
user-scoped query of `contactOptOutSettlements` with equality on canonical
identity and `contactSettlementHash`, `limit(2)`. It requires exactly one valid
document with the exact `canonical--generation` path and exact creating
receipt, then derives the exact identity from that settlement. This lookup
works whether the expected settlement is current or historical; missing,
duplicate, malformed, or mismatched results are zero-write ambiguity.

The deterministic release transition ID binds the expected active settlement.
An exact receipt is read first and returns its historical result without
touching a later epoch. Its immutable after-image hashes remain authoritative
when the referenced mutable heads have validly progressed. A never-applied
request against a released head, a stale expected active settlement, or
identity/actor/request/reason drift fails with zero writes.

In one transaction the first valid release appends the next contact generation,
advances the head to `released`, creates its unleased discovery fan-out at
fence `1` and
`created` receipt, and CAS-transitions any prior nonterminal apply fan-out to
`superseding`. It never deletes an alias, contact settlement, fan-out, row
settlement, or source evidence.

### Transition retry and forward-readback contract

All transition methods query the deterministic receipt path before allocating
a generation. A valid existing receipt makes the semantic request permanently
idempotent:

- immutable receipt, settlement, and alias documents reproduce exactly;
- the receipt's resulting head hashes prove the original atomic after-image;
- a current contact head may equal that after-image or be a valid later
  generation with internally exact current settlement/creating receipt/fan-out;
- the referenced fan-out may equal its after-image or follow only the allowed
  state-revision/fence/count/cursor transitions for the same immutable fan-out
  identity and expected contact settlement; and
- missing/corrupt current state is reported fail-closed but never authorizes a
  second transition or changes the historical receipt.

The first committed `requestedAt` and alias creation times win. A later same-ID
observation time is ignored; every semantic authority field is rederived and
must match the receipt. Tests exercise retry
after discovery progress, completion, release, later re-opt-out, and an
immediate competing fan-out mutation after the transition commit.

The reachable new-request matrix is exact:

| Prior contact | New request | Result |
|---|---|---|
| absent | verified opt-out | create first active epoch and `created` receipt |
| absent | release | zero-write conflict |
| active | different verified opt-out | `already_active` receipt and allowed exact alias only |
| active | release for exact active settlement | create released epoch; CAS current apply fan-out if nonterminal |
| released | verified opt-out | create new active epoch; CAS current release fan-out if nonterminal |
| released | different release | zero-write conflict |
| any | exact existing transition ID | receipt-first historical retry; zero writes |

For a new contact-changing request, a current `discovering|applying` fan-out is
CAS-moved to `superseding`; `complete` is accepted unchanged; and
`superseding|superseded|ambiguous`, missing, malformed, or incorrectly linked
current fan-out blocks. A fan-out already `superseded` cannot be the current
fan-out of the unchanged contact head.

## Immediate fail-closed suppression read

`read_contact_optout_suppression` is provider-free and write-free. It accepts
the verified user ID and raw candidate mailbox only long enough to reproduce
the B2 exact/canonical hashes; raw identity is not returned or persisted. It
reads the exact alias, canonical self-alias, canonical head, and exact latest
contact settlement plus its creating transition receipt needed to validate the
state.

Its semantic result is one of:

| Decision | Reason | Required proof |
|---|---|---|
| suppress | active | valid aliases as applicable, exact active head, active settlement, and creating receipt |
| allow | released | valid canonical self-alias, exact released head, release settlement, and creating receipt |
| allow | absent | successful absence of aliases and head, or an unseen exact plus variant beside no canonical authority |
| suppress | ambiguous | any RPC failure, malformed/partial/conflicting record, hash/path drift, or impossible correlation |

An absent exact alias beside a valid canonical self-alias is permitted: the
computed canonical head still decides. An existing exact alias requires the
canonical self-alias. Any canonical head requires the canonical self-alias and
its exact latest settlement. Missing exact alias never bypasses an active
canonical head. B4 must map every raised read error and every `ambiguous`
result to suppression; there is no fail-open exception lane.

## Fan-out state, lease, and cursor contract

The existing fan-out schema is retained. Its correlated state is now exact:

| State | Lease owner/deadline | Superseding settlement | Cursor |
|---|---|---|---|
| discovering | both null or both non-null | null | nullable last discovered row ID |
| applying | both null or both non-null | null | nullable phase row ID |
| superseding | both null or both non-null | non-null | nullable last superseded row ID |
| complete | both null | null | null |
| superseded | both null | non-null | null |
| ambiguous | both null | null | null |

Every fan-out retains a positive fencing token. Initial fan-outs start at
fence `1` with no lease. Acquisition, renewal, or expired takeover increments
both fan-out state revision and fence and writes owner/deadline. Releasing a
lease clears both fields and retains the fence. Every worker mutation requires
the exact expected state revision, head hash, lease owner, unexpired deadline,
and fence. Stale workers cannot create obligations/results or mutate rows.

Moving a prior nonterminal fan-out to `superseding` increments its state
revision and fence, resets its phase cursor to null, writes the newer contact
settlement hash, and clears any prior lease. Only `discovering|applying` may
enter `superseding`. Terminal states are immutable except the exact
completed-fan-out late-association recertifications defined below.

### Discovery

Discovery queries `contactRowBindings` inside the user scope with equality on
the canonical identity, ascending `rowId`, a field-value cursor, and
`limit(129)`. It creates at most the first 128 deterministic obligations and
advances the cursor to their last row. The 129th result is only an exhaustion
sentinel and is not written in that page.

Every discovery transaction reads the current binding head. If its
revision/hash differs from the fan-out snapshot, the transaction updates the
fan-out snapshot and resets the discovery cursor to null before continuing.
Existing exact obligations are validated no-ops; drift is ambiguity. Resetting
is mandatory because a concurrently associated row may sort before the old
cursor.

When a page contains at most 128 remaining edges, the same transaction may
create those obligations and move the fan-out to `applying`, with the phase
cursor reset to null, only if the binding snapshot remains exact. Discovery
never reads or writes a provider.

### Apply, release, and completion certification

Resolution handles at most one missing result per transaction in ascending
row-ID order. While `resultCount < obligationCount`, the worker queries a
`limit(33)` ordered obligation page after the phase cursor and exact-reads each
deterministically addressed result. It validates at most the first 32 existing
pairs and atomically creates at most the first missing result with all row
mutations; the 33rd obligation is an exhaustion sentinel. If all first 32
already have exact results, the transaction advances the cursor past those 32
without changing `resultCount`. Exhaustion while counts still differ is
ambiguity. Existing exact results never increment the count. Drift is
ambiguity. A new result increments `resultCount` exactly once. When the counts
first become equal, the transaction resets the cursor to null; it does not
certify completion yet.

While the counts are equal, the same `applying` state uses its cursor for a
full bounded certification pass. Each query uses `limit(33)`, validates at most
32 ordered obligation/result pairs plus all named claim/generation/settlement
evidence, and advances past only those 32; the 33rd record is an exhaustion
sentinel. Firestore has no cross-collection union query: the transaction issues
separate equality-plus-`rowId` ordered queries to obligations and results,
merges their row IDs locally, and requires the two page sequences and
exhaustion sentinels to match exactly. Missing, extra, duplicate, crossed, or
unreadable evidence is ambiguity. Only cursor exhaustion, empty next-page
queries, exact count equality, and a final unchanged binding snapshot may
clear the lease/cursor and
enter `complete`. That transition freezes the six completion-certificate
fields from the exact binding snapshot/counts/time, including count equality
with the fan-out's copied `bindingAssociationCount`. A new
association resets
the cursor and, when it adds work, makes the counts unequal. B2-C therefore
proves every immutable result without an unbounded transaction; B2-D later
re-audits the same set in bounded pages.

`observedRowHeadHash` is always the exact row-head **before-image** read by the
result transaction. This avoids a release hash cycle because the restored
after-image points at `contactFanoutResultHash`.

### Superseding

Superseding scans only already-created obligations in ascending row-ID pages of
at most 128. It creates `superseded/contact_head_advanced` results for every
unfinished obligation without any row mutation. It never discovers a new
contact-row edge. Each page advances the count/cursor under the superseding
lease. When no obligation remains, exact count equality moves the head to
terminal `superseded`, clears lease/cursor, and retains the newer contact
settlement link.

## Exact fan-out result matrix

Every nullable address and hash key remains present. An address and its hash
are either both populated as shown or both null. These are the only valid
disposition/reason/evidence combinations:

| Outcome | Disposition / reason | claim request/hash | row generation/settlement | released generation/settlement | restored generation/settlement |
|---|---|---:|---:|---:|---:|
| apply | applied / claim_accepted | non-null | non-null | null | null |
| apply | dominated / claim_dominated | non-null | null | null | null |
| apply | noop / row_deleted | null | null | null | null |
| apply | superseded / contact_head_advanced | null | null | null | null |
| release | restore / exact_predecessor | null | null | non-null | both nullable together |
| release | noop / row_optout_not_applied | null | null | null | null |
| release | noop / different_effective_owner | null | null | non-null | null |
| release | superseded / contact_head_advanced | null | null | null | null |

An apply result and its one-row claim/settlement commit atomically. A dominated
claim creates the immutable dominated claim set but no generation or row
settlement. A deleted row records the explicit apply noop so B2-B historical
association remains valid without minting ownership. A superseded result
creates no claim. A release never creates a row claim, generation, or owner
settlement. `already_restored` is not a valid first-write result: the exact
one-result-per-fan-out-row retry returns its existing result instead.

The contact apply claim derives the canonical one-row binding
`[{rowId, role: primary}]` directly from the obligation's row ID and user
scope. It never selects one supporting thread or accepts a thread binding from
the caller. Multiple, recreated, or permuted contact-row evidence documents
therefore produce the same row-binding hash, request ID, and claim.

## Release-aware row history and monotonic allocation

Every generation-allocating transaction replaces complete-lineage and
priority-depth reads with a bounded current/history proof. Within the user
scope it queries:

```text
rowOwnerSettlements
  where rowId == targetRowId
  order by generation DESCENDING
  limit 2
```

Each result must have the exact `rowId--generation` path and valid schema. With
one result its generation must be `1`. With two results, the first generation
must equal the second plus one and its final fencing token must be strictly
greater than the second's. Any duplicate, gap, reversal, or fence regression
is ambiguous. By induction, every noncurrent allocated generation has one
immutable settlement, and the row head's `latestSettlementHash` names the
greatest settled generation; it may be null only when the query is
successfully empty. Allocation uses:

```text
nextGeneration =
  max(effectiveOwnerGeneration or 0,
      firstStoredSettlementGeneration or 0,
      secondStoredSettlementGeneration or 0) + 1

firstFencingToken =
  max(currentHeadFencingToken or 0,
      firstStoredSettlementFencingToken or 0,
      secondStoredSettlementFencingToken or 0) + 1
```

The transaction also reads the exact effective generation/claim/settlement
when present and the candidate generation/settlement paths before writes. It
does not read generations `1..N` and no longer rejects a generation merely
because it exceeds priority `3`. Restoring a lower fence or clearing the
effective fence never rewinds the allocation floor.

When the current head is `claimed|review_pending`, its unsettled generation
must be `1` with first fence `1` if the settlement query is empty, or exactly
one generation and one first-fence step above the latest stored settlement.
The head fence must be at least that first fence. A current generation gap,
regressed first fence, or head fence below the generation is ambiguous and can
neither settle nor authorize a later allocation.

When `latestSettlementHash != effectiveSettlementHash`, the loader accepts
only one of two bounded bridge shapes:

1. **Release-restored:** `latestOptOutReleaseResultHash` resolves one exact
   valid release result whose released/restored generation-and-hash pairs match
   the latest historical settlement and the effective settlement or clear
   state.
2. **Active supersession:** the head is `claimed|review_pending`; its exact
   current generation is newer than the latest settlement; the latest
   settlement is `dominated` and names that current generation as dominant;
   and the current generation's predecessor settlement equals the head's
   effective settlement.

A release-restored head may subsequently have a newer current
claimed/review-pending generation; the loader validates both its release bridge
and that direct current generation. Every other historical divergence remains
fail-closed.

This design intentionally does **not** add allocation fields to the published
v1 row head, generation, or settlement schemas. The bounded latest-settlement
query plus current-head proof supplies monotonic generation/fence allocation
while preserving every B2-A/B hash and stored document byte. B2-D must freeze
the required production composite indexes before runtime adoption.

This bounded loader is shared by direct B1 claims, authenticated operator
decline, contact fan-out apply, settlement replay, lease takeover, and late
active-complete association. It preserves all frozen B2-B vectors for rows
that have never been release-restored.

## Exact release restoration

A release obligation derives the canonical mailbox from the authenticated
current release settlement and revalidates the exact current released contact
head/fan-out before any row result. It inspects the row's current effective
generation, claim, and settlement plus the selected row settlement's own
originating apply evidence.

For `restore/exact_predecessor`, the row head's effective owner must be
`contact_optout` and that generation's exact claim owner key must equal the
canonical mailbox being released. The effective row opt-out may have been
created by the immediately preceding active epoch **or by an older unfinished
same-canonical epoch**. This is required for the race:

```text
epoch A applies some rows
release A restores only some rows
epoch B supersedes unfinished release A
epoch B is equal-priority dominated on rows still controlled by A
release B restores both B-controlled and still-A-controlled rows
```

The transaction validates the selected released settlement's generation,
claim set, v2 contact authority, canonical owner key, and
`supersededEffectiveSettlementHash`. From that claim's exact `fanoutId` and row
ID it derives the originating apply obligation/result paths and requires a
valid `applied/claim_accepted` result whose claim-request, row-generation, and
row-settlement addresses/hashes match the selected artifacts. It also validates
the originating fan-out's immutable identity fields and resolves the claim's
contact-settlement payload hash to one exact same-canonical contact settlement
and creating receipt. This evidence is mandatory even when the selected
settlement belongs to older epoch A; missing, malformed, or crossed originating
evidence is zero-write ambiguity. A
non-null predecessor settlement is
resolved by exact hash query with `limit(2)`, must be unique, belong to the
same row, have a lower generation, and validate with its exact generation and
claim. It must be a settled lower-priority `terminal|human_decision` owner with
outcome `terminal|human_declined`; a `dominated` or `contact_optout`
predecessor is never restorable. A null predecessor restores clear state. A
different canonical contact's opt-out is never restored.

The restored row-head after-image:

- retains `latestSettlementHash` as the released opt-out settlement;
- restores `effectiveSettlementHash`, effective generation/hash/kind/priority,
  state, and fence from the exact predecessor, or clears them;
- has null lease fields;
- retains `latestSourceSettlementLinkHash` and projection backlog state;
- advances `latestOptOutReleaseResultHash` to the new result hash; and
- increments row state revision exactly once.

If no same-canonical opt-out controls the row and the current release epoch has
no exact applied target, release records `noop/row_optout_not_applied`. If the
current epoch's exact applied target exists but another effective owner now
controls the row, it records `noop/different_effective_owner` with that target's
generation/hash. Exact retry returns the existing result rather than creating
an `already_restored` result. A contact-head advance observed before commit
creates only the superseded result; it cannot restore a row. Consequently a
stale release A cannot clear epoch B, while current release B can clear a
still-effective same-canonical settlement from A.

## Late contact-row association

The B2-B association planner remains the single edge/evidence/index authority.
`record_contact_row_association` composes it with the current contact head and
fan-out as follows:

| Current contact state | New association behavior |
|---|---|
| no contact head | existing edge/evidence/binding-head transaction |
| active + discovering/applying | add deterministic obligation, update binding snapshot/count, reset phase cursor to null |
| released + discovering/applying | add deterministic release obligation plus immediate `noop/row_optout_not_applied`, advance both counts, update binding snapshot, reset phase cursor to null |
| active + complete apply fan-out | synchronously add and resolve one obligation as applied, dominated, or deleted-row noop; recertify counts/snapshot and remain complete |
| released + complete release fan-out | add no obligation/result; CAS-recertify the fan-out to the new binding snapshot with counts unchanged and remain complete |
| current fan-out superseding/superseded/ambiguous/missing/malformed | zero-write ambiguity |

Creating only new evidence for an already-existing association does not change
the binding head or fan-out snapshot. Every genuine association transaction is
all-or-none. Deleted rows remain valid historical B2-B associations; an active
fan-out resolves them as `apply/noop/row_deleted` rather than rejecting the
association or creating row ownership.

A completed apply fan-out always has obligation/result counts equal to the
association count in its current binding snapshot, so every active late
association recertifies it synchronously. A completed release fan-out may have
fewer obligations/results than the current recertified association count,
because later released-state associations require no release work. Its exact
late-association after-image CAS-checks the prior fan-out revision/hash,
increments `stateRevision`, advances `bindingRevision`/`bindingHeadHash` and
`bindingAssociationCount`/`updatedAt`, recomputes the fan-out hash, and retains
`complete`, its fencing token, null lease/cursor/superseding link, unchanged
counts, and the byte-exact first completion certificate. B2-D health validates
that certificate for both current and historical fan-outs. For `apply`,
current obligation/result counts equal current `bindingAssociationCount`. For
`release`, counts equal the frozen completion association/work counts while
the current saved binding revision and association count may be higher only
through these released-complete recertifications. A later
contact epoch therefore does not invalidate the retained release fan-out. The
next active epoch discovers every then-current association.

## Delayed source-settlement links and replay

`link_b1_source_settlement` remains create-only and supports both direct B1 and
contact-fan-out origins. It must validate the exact historical generation,
claim, row settlement, B1 bundle, and source settlement even when release has
restored another effective row owner or clear state. It may advance only the
head's `latestSourceSettlementLinkHash`; it may not reactivate the released
owner or change effective fields.

Exact settlement and source-link retry use stored request/artifact identity,
not a complete lineage walk or a hard-coded generation ceiling. Missing,
duplicate, malformed, future-dated, or mismatched historical proof is
zero-write ambiguity. A later active contact receipt whose B1 link never
created a row claim does not mint a row source-settlement link.

## Query, fake, and write bounds

The B2 fake gains Firestore-compatible
`order_by(field, direction="ASCENDING"|"DESCENDING")` and field-value
`start_after` semantics over the ordered tuple plus document-path tie-breaker.
Query phantom changes remain transaction conflicts. Required query shapes use
only equality filters, explicit ordering, limits, and exact document reads.

Maximum planned writes are frozen below the internal 400-write ceiling:

| Transaction | Maximum writes |
|---|---:|
| first verified opt-out with two aliases/receipt | 6 |
| re-opt-out after release with current fan-out complete / nonterminal | 5 / 6 |
| release with current fan-out complete / nonterminal | 4 / 5 |
| already-active receipt with one new alias | 2 |
| discovery or supersession page | 129 |
| apply obligation | 7 |
| dominated apply | 3 |
| deleted-row apply noop | 2 |
| release restore / noop | 3 / 2 |
| active / released association during nonterminal fan-out | 5 / 6 |
| active-complete late association | 11 |
| released-complete late association | 4 |

Every planner computes its exact write count after bounded reads and before the
first transaction write.
Pre-apply failure has zero writes. Apply-then-raise succeeds only after exact
readback of the receipt and every artifact expected from that transaction;
partial or mismatched readback is ambiguous.

## Provider-free public surface

The B2-C store surface is:

```python
read_contact_optout_suppression(...)
record_verified_contact_optout(...)
record_authenticated_contact_release(...)
acquire_contact_fanout_lease(...)
discover_contact_fanout_page(...)
process_contact_fanout_obligation(...)
certify_contact_fanout_page(...)
supersede_contact_fanout_page(...)
record_contact_row_association(...)  # extended composition, same B2-B authority
```

Method arguments may contain verified local inputs, expected revisions/hashes,
lease values, and caller-frozen timestamps. They never accept a caller-derived
priority, contact generation, settlement/fan-out/result hash, result
disposition, raw B1 link, or row decision. All authority artifacts are read or
derived inside the transaction.

## Containment and activation boundary

- `row_authority.py` remains standard-library-only and writes no B1 record.
- No B1 or legacy runtime module imports or constructs B2-C.
- No provider client, network call, production Firestore, frontend, rule,
  migration, deployment, environment flag, or campaign is part of B2-C.
- B3 must consume stable row/contact authority before irreversible effects.
- B4 owns authenticated route adapters, production Firestore indexes/rules,
  real hard-opt-out verifier wiring, migration execution, frontend behavior,
  deployment, and an explicitly authorized self-recipient canary.
- Jill remains NO-GO until B2-D, B3, B4, deployment evidence, production
  frontend proof, and the separate go/no-go record all pass.

## Acceptance and refutation

B2-C is accepted only when hermetic tests and immutable evidence prove:

1. strict contact/receipt/fan-out schemas, independent digest vectors, exact
   path/hash/nullability matrices, and no cross-user replay;
2. deterministic transition idempotency across exact retry, two-worker race,
   pre-apply failure, apply-then-raise, and partial readback;
3. active-to-active verified opt-out creates only an exact receipt/allowed
   alias and never creates a second contact or row authority epoch;
4. suppression is active immediately after the contact transaction, permits a
   valid released/absent contact, and fails closed on every read/integrity
   failure and unseen plus variant;
5. descending settlement queries and field cursors match Firestore semantics,
   detect drift/phantoms, and never reuse generation or fence after repeated
   opt-out/release cycles;
6. leased discovery/apply/superseding state transitions, cursor resets, page
   bounds, acquisition/takeover, stale-fence refusal, and bounded complete
   evidence certification;
7. apply result, release result, and correlated-null matrices reject every
   unlisted combination;
8. release restores exactly the lower-priority predecessor or clear state for
   any still-effective same-canonical opt-out epoch, never erases an independent
   contact's settlement, and loses safely to a newer contact transition;
9. nonterminal, active-complete, released-complete, and deleted-row late
   associations follow their exact atomic behavior and health exception;
10. delayed B1 source-settlement linking and exact replay work before and after
    release without reactivating the row owner;
11. all B2-A/B, B1, retained M2, release/auth, compile, containment, and
    provider-network-blackhole gates remain green;
12. two independent reviewers approve the exact code diff with no Critical or
    Important finding, and the owned branch plus exact-SHA GitHub Actions run
    match.

Until all twelve conditions pass, B2-D, deployment, production campaign
execution, and Jill's return remain **NO-GO**.
