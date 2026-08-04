# Shared Exact-Source Coordinator Design

**Status:** Product direction approved on 2026-08-03. B1 is authorized for
local, offline implementation. B2-B4 remain separately gated build phases.
**Deliverable:** code
**Production decision:** NO-GO. Completing B1 does not authorize deployment,
mail, campaign activity, provider calls, production writes, or user clearance.
**Baseline:** local M2 checkpoint `2b5e785bbc46754de16ca439e463793653e45f84`,
whose pre-commit 35-file aggregate was
`dde9dd965215812b3039557c69e034e085c41aaa6fa558ab5e75939d598311b4`.

## Product outcome

SiteSift must never lose, double-process, misclassify, or let two transition
families act on the same inbound source. Before a Sheet write, notification,
thread transition, follow-up change, reply preparation, opt-out, terminal saga,
or human-decision action can occur, the system must have one durable answer to
four questions:

1. Which exact inbound source is this, across all verified Graph and RFC aliases?
2. Which complete proposal was classified for it, and was the model called once?
3. Which transition family, if any, owns it?
4. Which source obligations are complete, durably delegated, dominated, blocked,
   or still pending?

The coordinator is server authority. Thread roots, handled-event maps, processed
markers, pending-response documents, row numbers, and transition-specific sagas
are projections or consumers; none may independently answer those questions.

## Delivery board

| Phase | Product capability | Exit gate |
| --- | --- | --- |
| B1 | Exact-source identity, frozen classification, deterministic transition owner, thread head, immutable work ledger, blocked admission/wake, and strict marker settlement | Scanner, retry, replay, and pending-response admission use one coordinator in enforced offline tests; every source is admitted; zero provider mutations |
| B2 | Stable user-scoped row identity, immutable sorted `rowBindings[]`, and retained row-transition owner | Terminal and opt-out use one retained owner across split roots, cleanup, settlement, and late roots |
| B3 | Monotonic execution epoch, unique claim ID, effect intent/outcome, takeover reconciliation, and stale-owner fences | Every Graph/Sheet/notification/cleanup mutation requires the exact live execution tuple; ambiguity never authorizes replay |
| B4 | Real terminal/opt-out/human integration, complete mutation inventories, frontend server-only rules, and cross-family barriers | Real entry paths consume B1-B3; rules emulator and every ordered race pair prove one winner and one durable loser |

B1 is the only implementation scope of the first plan. The package remains
incomplete until B2-B4 pass their own plans and reviews.

## Current production defects this closes

- `has_processed` fails open on a Firestore read error, and `mark_processed`
  swallows write failures.
- Same-thread batching stores and marks every earlier source processed while
  classifying only the newest source.
- Operator replay creates and settles Graph/RFC processed claims directly.
- A proposal can begin Sheet and notification effects before the full proposal
  and winning transition are durably frozen.
- Terminal, opt-out, and human-decision branches can each infer their own
  ownership from mutable event order.
- Pending response identity is thread-keyed and can be overwritten by later
  source work.
- Existing source aliases, failure artifacts, handled maps, and terminal
  settlements are useful evidence but do not form one canonical source owner.

## Hard invariants

1. **No authority by absence.** Missing, unreadable, malformed, or conflicting
   authority state is a retryable or operator-visible block, never permission.
2. **One canonical source.** A verified alias maps to exactly one retained
   `canonicalSourceId`; late aliases attach without changing it.
3. **One fresh classification.** Only the current classification claim may call
   the classifier/model, and request start is durably fenced before the call.
4. **Freeze before effects.** The complete proposal, all transition candidates,
   ordinary obligations, selection, and hashes are immutable before downstream
   work.
5. **One transition owner.** Hard contact opt-out wins over terminal, which wins
   over aggregate human decision. A caller cannot elect itself.
6. **One active thread hold.** A distinct source cannot replace an unresolved
   owner for the same thread; it is durably admitted and blocked behind it.
7. **Every source is enumerable.** Mailbox unread state, scan window, and cursor
   position cannot strand admitted or blocked work.
8. **Settlement is evidence-based.** Processed aliases are projections written
   only after every ledger entry is completed, delegated, or validly dominated.
9. **Retries reuse authority.** Scanner retry, failure retry, operator replay,
   queue wake, and late alias repair reuse the same source, snapshot, owner, and
   ledger.
10. **B1 has zero provider mutations.** Its tests inject classifiers and use
    local fakes only. Graph/Sheets effects remain unreachable from coordinator
    state-machine tests.

## Runtime containment

The integration is guarded by one server configuration:

```text
SITESIFT_SOURCE_COORDINATOR_MODE = disabled | shadow | enforced
```

- `disabled` is the default and preserves the exact pre-B1 runtime path.
- `shadow` may compute pure identities and proposed records in memory but makes
  no coordinator, marker, cursor, domain, or provider write.
- `enforced` enables coordinator authority and is permitted only in hermetic
  tests during B1. Production enablement requires B4 review.

Unknown or malformed values resolve to `disabled` and emit a safe health/config
error without switching behavior. A static zero-effect test proves the disabled
path does not construct a coordinator or add Firestore/provider calls.

## B1 records

The coordinator owns these server-only records:

```text
users/{userId}/sourceIdentities/{canonicalSourceId}
users/{userId}/sourceAliases/{sourceAliasKey}
users/{userId}/sourceClassifications/{canonicalSourceId}
users/{userId}/sourceTransitionOwners/{canonicalSourceId}
users/{userId}/threadTransitionHeads/{threadId}
users/{userId}/sourceWorkLedgers/{canonicalSourceId}
users/{userId}/sourceDeferredWork/{workKey}
users/{userId}/inboundPendingAdmissions/{canonicalSourceId}
users/{userId}/blockedSources/{canonicalSourceId}
users/{userId}/sourceSettlements/{canonicalSourceId}
```

All deterministic keys use full SHA-256 with domain-separated input. Hashes use
one canonical JSON encoder: UTF-8, sorted keys, compact separators, finite JSON
values only, and no timestamps or mutable fields in immutable hashes.

### Source identity and aliases

`canonicalSourceId` is a random opaque UUID allocated once by the winning
identity transaction; it is not derived from a Graph ID, RFC message ID, thread,
row, address, or event. Supported aliases are normalized Graph message ID and
normalized `internetMessageId` values:

```text
sourceAliasKey = sha256(
  "source-alias-v2\0" + userId + "\0" + aliasType + "\0" + normalizedAlias
)
```

Graph IDs are opaque: normalization trims surrounding whitespace and otherwise
preserves bytes/case. RFC message IDs trim surrounding whitespace and one or
more enclosing angle brackets while preserving the remaining bytes/case. Empty,
non-string, control-character, or over-bound aliases are rejected. Changing
normalization is a schema migration, not an inline cleanup.

`MAX_SOURCE_ALIAS_BYTES = 1024` after normalization and
`MAX_SOURCE_ALIASES = 8` per canonical source. Exceeding either bound blocks
before identity/alias mutation.

Admission reads every supplied alias in one transaction:

- none mapped: create one source identity and bind every supplied alias;
- exactly one owner: verify it and attach every unbound verified alias;
- more than one owner: write no identity/alias mutation and return a structured
  `source_alias_conflict` block;
- an alias already bound to another owner: fail closed;
- no usable alias: create no authority and return `source_identity_missing`.

Admission accepts the canonical hydrated-message mapping, not caller-assembled
aliases or evidence hashes. `admit_or_repair_source_identity(...)` validates the
reviewed hydration/replay evidence kind, extracts the Graph/RFC fields, computes
the evidence hash, and constructs a module-private typed envelope internally.
A late repair must contain at least one already-bound alias alongside the new
alias in that same hydrated mapping. B1 never accepts a proof-only disjoint
merge: two disjoint aliases are quarantined as
`source_alias_bridge_required` for B4 migration. Apply-then-raise is accepted
only after strict readback of the identity and every alias document.

Graph conversation IDs and internal thread IDs are routing evidence only; they
are never source aliases and replay may not merge sources by conversation ID.
Once a non-empty internal `threadId` is bound to an identity, that binding is
immutable. Evidence that would bind the same source to a different internal
thread returns `source_thread_conflict` with zero identity, alias, or routing
writes. A late alias may be attached only when the coordinator-extracted
envelope also contains an already-owned typed Graph or RFC alias. Static
adoption tests limit calls to the admission API to the exact hydration/replay
adapters and hermetic tests, and reject production construction or import of the
module-private envelope type.

The retained identity stores its immutable ID, immutable creation hash,
monotonic verified-alias set, thread binding when known, and lifecycle state.
Alias projections store their type, normalized-value hash, and sole owner.

### Authoritative classification

`sourceClassifications/{canonicalSourceId}` has the state machine:

```text
unclaimed -> claimed -> request_started -> snapshot_ready
                 |              \-> classification_request_ambiguous
                 |-> retry_required
                 \-> snapshot_ready (verified deterministic model-not-applicable)
unclaimed -> legacy_terminal_quarantined
```

Fresh-classification states store a positive monotonic `classificationEpoch`,
globally unique `classificationClaimId`, server lease, immutable
`classificationInputHash`, stable `modelRequestKey` (null only for
`not_applicable`), and
`modelRequestState = not_started | started | not_applicable | captured | ambiguous`.
The legacy quarantine has epoch `0`, null claim/lease/request key, and
`modelRequestState = not_applicable`; it can never transition into a fresh claim.

The classifier claimant must commit `request_started` before invoking a fresh
classifier/model. After `started`, lease expiry never authorizes a second model
call. A crash before start permits a verified higher-epoch takeover. A crash
after start but before capture becomes `classification_request_ambiguous` and
requires operator-visible resolution; it does not elect an owner.

Read-only source hydration and classification-input acquisition may occur before
request start, but every byte supplied to the classifier is canonicalized and
bound by `classificationInputHash` before `request_started`. Input drift blocks;
it cannot silently create a new request. B1 tests inject these reads and perform
no network or provider access.

A verified deterministic hard opt-out bypasses the model and records
`modelRequestState = not_applicable`; tests assert zero model calls. A model-only
or otherwise unverified opt-out signal cannot elect hard opt-out. It normalizes
to the supported human candidate `needs_user_input` with reason
`unverified_optout_review` and remains subject to the same frozen snapshot and
decision rules.

The deterministic lane uses
`persist_deterministic_classification_snapshot(...)`, which consumes the exact
current claim plus canonical classification input in one transaction. It calls
the coordinator's injected pure hard-opt-out verifier and accepts no
caller-supplied evidence, candidate, or winner. B1 production wiring leaves the
verifier absent, hermetic tests use a strict fake, and B4 owns the reviewed real
adapter. A module-private verified result is required before the method computes
and stores `classificationInputHash`, sets
`modelRequestState = not_applicable`, and freezes `snapshot_ready` without a
request key or model call. Input or evidence drift is a conflict with zero
writes.

The snapshot commit contains:

```text
completeProposalSnapshot, completeProposalHash
transitionCandidates, ordinaryObligations
selectionSnapshot, selectionHash, snapshotImmutableHash
deterministicEvidence, snapshotPersistedAt
```

`MAX_CLASSIFICATION_SNAPSHOT_BYTES = 614400` canonical bytes. The input itself
is not retained in the authority document—only its hash and versioned schema—so
the snapshot bound is checked independently before mutation.

The commit performs no owner, saga, Sheet, notification, follow-up, reply,
marker, cursor, or provider write. Only strict readback of `snapshot_ready`
authorizes owner election or ordinary work materialization. A differing retry
returns `classification_snapshot_conflict` with zero writes.

### Deterministic transition decision

The coordinator derives one explicit decision solely from the frozen snapshot:

1. verified hard `contact_optout`;
2. terminal transition;
3. aggregate `human_decision`;
4. explicit `none` for an ordinary-only source.

The reviewed B1 human-decision candidate set is `call_requested`, actionable
tour review, supported `needs_user_input`, ordinary wrong-contact pause,
verified `forwarded_observed`, and persisted-policy
`disabled_policy_suppressed`. Confirmed/non-tour tour outcomes, `new_property`,
ordinary field updates, and unrelated informational work remain ordinary ledger
obligations. Unknown transition-shaped candidates block snapshot selection;
they do not silently become an automatic reply.

The snapshot stores
`candidateTaxonomyVersion = "source-candidate-taxonomy-v1"`. Its normalized
transition types are exact: verified hard `contact_optout`; terminal
`property_unavailable | close_conversation`; human `call_requested |
actionable_tour_review | needs_user_input | wrong_contact_pause |
forwarded_observed | disabled_policy_suppressed`; and explicit `none` after all
items normalize as ordinary. Confirmed/non-tour tour, `new_property`, field
updates, generic reply, and allowlisted informational payloads are ordinary
ledger work. Any other transition-shaped type fails closed.

`sourceTransitionOwners/{canonicalSourceId}` retains the source ID, snapshot and
selection hashes,
`ownerKind = none | contact_optout | terminal | human_decision`, nullable
deterministic `ownerKey`, and monotonic revision. `ownerKey` must be null only
for `none`; a missing owner record never means `none`. Creation consumes the
stored snapshot; callers may provide an expected kind only as an assertion. A
mismatch fails before any downstream effect.

### Thread transition head and blocked admission

`threadTransitionHeads/{threadId}` is the cross-source linearization record:

```text
threadHeadRevision
activeOwnerKey, activeOwnerKind, activeCanonicalSourceId
activeGeneration
activeState = active | releasing | clear
updatedAt
```

B1 does not add B3 execution epoch/claim fields. When a source with an elected
transition contends for an occupied head, one transaction creates or verifies:

- its authoritative immutable `inboundPendingAdmissions` record;
- immutable saved-history binding and exact index binding;
- its `blockedSources` same-transaction projection; and
- the unchanged winning head evidence.

The loser produces no transition, Sheet, notification, follow-up, reply,
processed, read, or cursor effect. Pending admission states are
`pending | blocked | processing | settled`. Blocked lifecycle is
`blocked -> eligible -> claimed -> settled | settled_as_new_blocker`.
The authoritative admission also carries nullable `wakeGeneration`, full-hash
`wakeToken`, `wakeState = none | eligible | claimed | consumed`, and nullable
`wakeClaimId`; no separate wake-token collection exists.

Wake order is received instant, sent instant, then canonical source ID. One
transaction releases the old head and writes one monotonic eligible wake token
onto the oldest admission. A second transaction,
`claim_wake_and_rebind_generation(...)`, compare-and-sets that exact token to one
unique claim, rebinds the complete remaining admission/projection set, and
creates or verifies the next head generation. Apply-then-raise in either phase
is accepted only by strict readback of that phase; neither transaction is
blindly replayed. Queue processing does not depend on mailbox unread state or
the scanner's time window. `MAX_BLOCKED_SOURCES_PER_THREAD = 100`; admission of
source 101 fails closed without a partial admission, history/index write,
projection, or head mutation. A claimed source that creates a new hold settles
as the new blocker and suppresses an additional wake.

### Immutable source-work ledger

`sourceWorkLedgers/{canonicalSourceId}` freezes an ordered entry list. Each entry
has a full-hash `workKey`, kind, immutable payload/hash, selected owner,
dominance outcome, and required completion evidence. The ledger hash covers the
source, proposal/selection hashes, and ordered entries.

Canonical entry order is computed before hashing. Semantically duplicate items
receive deterministic occurrence ordinals in that order before `workKey`
derivation, so no entry overwrites another. A ledger is rejected before any
write when it exceeds `MAX_SOURCE_WORK_ENTRIES = 128`, canonical serialized size
of 600 KiB, or 400 writes in the materialization transaction. Canonical bytes,
not Firestore map iteration or caller order, define every hash.

Mutable entry state is:

```text
pending | applying | completed | delegated | dominated
```

Legal forward transitions are `pending -> applying -> completed`,
`pending -> delegated`, and `pending -> dominated`; exact idempotent replay is
allowed and every other transition blocks. `applying` is local bookkeeping only
and grants no external-effect authority in B1. `completed` requires the exact
versioned completion-evidence schema for the work kind and its evidence hash.

The only mutation APIs are `record_source_work_applying(...)`,
`complete_source_work_entry(...)`, `delegate_source_work_entry(...)`, and
`dominate_source_work_entry_from_selection(...)`. Each requires exact canonical
source, ledger, and work hashes. Delegation atomically creates/verifies the
matching deferred record; dominance reads the stored selection and accepts no
caller-provided winner.

`delegated` requires a deterministic durable work document with matching owner,
payload hash, and completion contract. `dominated` is legal only when the frozen
selection names the exact dominating transition. Logs, swallowed booleans,
handled-event maps, or legacy marker documents are not completion evidence.

`sourceDeferredWork/{workKey}` is the durable target for delegation. It stores
the canonical source, ledger hash, entry payload hash, target owner, wake
condition, immutable binding hash, and mutable state
`deferred | eligible | claimed | completed | blocked`. Creation or completion
must be transactionally verified with the corresponding ledger entry; a queue
log or mutable thread flag is not delegation.

The dominance matrix is explicit:

| Work | Opt-out wins | Terminal wins | Human wins |
| --- | --- | --- | --- |
| opt-out transition | delegate opt-out | impossible | impossible |
| terminal transition/reply | dominated | delegate terminal | impossible |
| human action | dominated | dominated | delegate human |
| generic automatic reply | dominated | delegate terminal policy | dominated no-send |
| field update/new property/unrelated informational work | preserve | preserve | preserve |

### Strict processed and handled projections

`sourceSettlements/{canonicalSourceId}` is the one canonical processed
authority. It is create-only and stores the canonical source ID, identity hash,
snapshot hash, selection hash, explicit owner-decision hash, ledger hash, final
ledger-evidence hash, the exact active thread-head acquisition binding for an
owned transition (or null for an explicit none owner), complete typed alias set
and hash, settlement revision, and server settlement time. The thread-head
binding is frozen from the validated active head in the settlement transaction
and is covered by the settlement hash; it is never inferred from a later head
or mutable admission timestamp. The alias set is complete for aliases known at
the settlement transaction; later verified aliases create projections against
the retained identity and settlement without mutating the settlement. Exact
idempotent replays are accepted; different content is
`source_settlement_conflict`. No projection or legacy marker may be
used to synthesize this record.

Only `settle_source_markers_if_ready(...)` creates the canonical settlement and
writes processed aliases. In one transaction it requires the exact identity,
snapshot, explicit owner decision, settled ledger, pending-admission state, and
thread-head outcome, then creates or verifies the settlement and every known
alias projection. Strict readback must match the canonical settlement content
and revision before the scanner may advance a cursor/read projection.

Read failure, malformed state, write failure, partial apply, or readback failure
raises a typed retryable/ambiguous error. It never returns `False` or an empty
map. Late alias repair for a settled source creates the missing processed alias
projection from retained canonical settlement without reclassification.

Legacy `processedMessages` and `handledEvents` remain compatibility projections
only and must point to the canonical source, settlement revision, and settlement
hash. They never grant authority by document existence. During B1, cleanup may
not delete coordinator authority or its processed alias evidence. Existing
cleanup tests must prove retained B1 records survive.

## B1 integration boundary

### Pre-election write allowlist

Before strict readback of a complete frozen snapshot, explicit transition
decision, immutable ledger, and any required thread-head claim, an adopted lane
may write only:

- canonical source identity and typed alias bindings;
- immutable inbound history plus exact source/thread routing indexes;
- pending admission and its same-transaction blocked projection;
- classification claim, request-start, ambiguity, and frozen-snapshot records;
- retained Terminal A classification quarantine with its exact evidence hashes;
- canonical-source-bound processing-failure visibility.

Everything else is forbidden before that barrier, including thread status,
timestamps or reactivation; client recovery; follow-up state; message-order test
writes; Sheet logging or mutation; asset upload; notification or counter writes;
handled-event state; transition sagas; pending-response writes; reply or draft
preparation; processed/read/cursor projections; and every provider mutation.
Read-only hydration/input acquisition is not authority and is allowed only when
its exact classifier input is durably hash-bound as described above. The one
classifier/model request is fenced by `request_started`; during B1 it is an
injected local callback, and real model-provider adoption remains B4 work. A
test tripwire covers each forbidden category. The coordinator may authorize
later work, but B1 itself grants no provider-effect authority.

### Scanner

- Resolve or admit every exact source before batching.
- Remove the behavior that history-saves and marks earlier messages processed
  while classifying only the last message.
- Process each source oldest-first or atomically block it behind the active head.
- Do not advance processed/read/cursor state without strict source settlement.
- On exact-source failure, keep that source and every later same-thread source
  enumerable and unprocessed.

### Processing and classifier

- Admit identity after exact source hydration and before generic domain work.
- Recover existing snapshot/owner/ledger before any fresh classifier call.
- Split the current proposal seam so `propose_sheet_updates(...)` returns to the
  coordinator for a snapshot-only commit before line-of-business effects.
- Dispatch only the persisted winner. Terminal A remains a downstream consumer;
  B1 does not rewrite its Sheet attempt or execution fence.
- An owner whose B4 transition adapter is not yet active remains durably blocked
  in enforced tests; it is never marked processed or allowed to fall through.

### Retry and operator replay

- Processing-failure retry resolves the canonical source and reuses its state.
- Operator replay claims the canonical source, not separate Graph/RFC processed
  documents, and cannot directly complete markers.
- A replay with conflicting aliases or differing frozen proposal is refused
  before effects.
- Active or settled Terminal A authority is retained and quarantined through the
  bridge below; B1 never starts a fresh classification for that source or
  replaces retained terminal settlement evidence.
- Legacy processed or handled markers without canonical settlement evidence
  block for operator-visible migration; they do not fabricate B1 settlement.
- An in-progress legacy replay claim is quarantined until its outcome is
  reconciled. It cannot be treated as either unprocessed or canonically settled.

#### Retained Terminal A bridge

The coordinator receives a retained-authority loader dependency. Production
enforced wiring may bind only the reviewed adapter over the existing strict
`_terminal_retry_disposition(...)`; B1 tests inject a strict fake. The loader
validates the complete thread settlement history and exact Graph/RFC/thread
source binding before returning `kind = active | settled`, thread ID, typed
source aliases, source-message key, saga key when active, immutable saga or
settlement hash, complete validated-record hash, and canonical binding hash.
The coordinator wraps this result in a module-private evidence type; callers
cannot supply an evidence object or hash.

`quarantine_retained_terminal_authority(...)` accepts only exact source/thread
identifiers, invokes the loader, transactionally verifies its result against the
canonical source identity, and writes a create-only
`sourceClassifications` record in `legacy_terminal_quarantined` with
`modelRequestState = not_applicable` and the retained evidence hashes. It does
not fabricate a B1 proposal snapshot, decision, ledger, or settlement. Strict
readback returns `legacy_terminal_authority_retained`, which forbids a fresh
classifier, B1 election, marker/cursor advancement, and generic domain work.
Conflicting evidence is `legacy_terminal_authority_conflict` with zero writes.

The quarantine lifecycle is
`legacy_terminal_quarantined -> migrated_b1 | retained_terminal_complete`.
Resolution requires B4 to map a complete proposal/obligation set or explicitly
retain the exact settled Terminal A result; B1 provides health visibility but no
automatic resolver. Disabled mode continues the unchanged M2 path, while B1
enforced mode blocks these legacy sources safely.

### Pending responses

- Pending response records carry `canonicalSourceId`, proposal/selection hashes,
  and ledger `workKey`.
- A thread-keyed legacy document cannot be overwritten by a later source.
- Enqueue, require, claim, and clear operations verify both exact canonical
  source and `workKey`; clearing source A cannot delete or settle source B work.
- B1 validates ownership only; Graph send authority remains B3 work.

The enforced APIs are `queue_pending_response(...)` with required canonical
source/work/hash fields, `require_pending_response_exact(...)`,
`claim_pending_response_for_send_exact(...)`, and
`clear_pending_response_exact(...)`. Exact clear also requires the expected
pending revision and returns a typed `PendingResponseClearResult`; it never
returns a naked boolean. The legacy thread-only clear wrapper is disabled-mode
compatibility only and raises before writes in enforced mode.

### Health

Health reports bounded counts for ambiguous classifications, blocked sources,
pending admissions, unsettled ledgers, alias conflicts, and marker ambiguity.
Unreadable or over-bound collections fail health closed.

Scanner, processing-failure retry, operator replay, blocked wake, and pending
response consume the same structured coordinator disposition. No adopted lane
interprets `None`, document absence, or a naked boolean as settlement, cursor
advance, wake, or effect authority.

## Deferred Graph draft DELETE inventory

The release record's “other 16 deferred DELETE sites” is a narrow caller count:
three `_delete_graph_reply_draft(...)` calls in `email.py` and thirteen in
`followup.py`, excluding five M2-owned `processing.py` callers. The repository
contains other Firestore/admin deletion operations, so 16 is not a whole-repo
semantic delete count.

B1 records an exact static manifest and regression test but performs no Graph
DELETE. Microsoft Graph provides no reviewed unchanged-version condition for
message DELETE. Therefore automatic draft deletion is not a B3 entitlement:
the release-safe target is a durable `cleanup_required` obligation and audited
manual cleanup unless a separate provider contract proves a server-enforced
conditional delete. B3/B4 own integration of that policy.

## Files and responsibilities

- Create `email_automation/source_coordinator.py`: schemas, canonical hashing,
  typed outcomes/errors, transaction-aware B1 APIs, and no provider imports.
- Create `tests/source_coordinator_fakes.py`: transaction-capable local fake used
  only by coordinator tests.
- Create `tests/test_source_coordinator.py`: unit/state-machine and concurrency
  barriers.
- Modify `email_automation/processing.py`: mode gate, admission, proposal freeze,
  scanner/retry settlement integration.
- Modify `email_automation/messaging.py`: strict coordinator-backed aliases and
  marker compatibility boundary.
- Modify `email_automation/operator_replay.py`: canonical-source replay claim and
  settlement delegation.
- Modify `email_automation/pending_responses.py`: source-bound pending identity.
- Modify `email_automation/system_health.py`: bounded coordinator health.
- Modify or explicitly quarantine `scheduler_runner.py`: its duplicate
  `has_processed`/`mark_processed` implementation may not remain an untracked
  authority writer in enforced mode.
- Modify `main.py`: retention cleanup must preserve coordinator authority and
  B1 processed projections until a separately reviewed retention contract.
- Modify focused existing tests for scanner, retry, replay, pending response,
  cleanup retention, terminal compatibility, and health.
- Create `tests/fixtures/graph_draft_delete_callers.json` and a static inventory
  test with the exact 16 deferred callers plus five M2-owned exclusions.

## Acceptance and refutation

B1 is accepted only if hermetic evidence proves all of the following:

- Graph-first/RFC-enriched and RFC-first/Graph-enriched admission each retain one
  source, proposal, and owner; contradictory alias owners fail closed.
- A classification request can begin at most once across crash, lease expiry,
  takeover, replay, and scanner retry.
- Verified hard opt-out makes zero model calls; model-only opt-out becomes the
  supported human-review candidate and never wins hard opt-out.
- Every permutation of the same proposal selects the same winner and losing
  transition families create zero effects; ordinary-only work persists an
  explicit `none` decision.
- Snapshot, explicit decision, ledger, and required head claim strictly precede
  every forbidden domain or provider seam.
- Two same-thread sources are both admitted; the earlier source is never marked
  processed merely because the later one was classified.
- A distinct-source race leaves one thread owner and one fully enumerable loser.
- Oldest-first wake/rebind remains complete beyond mailbox filters, admits only
  one claimant, and source 101 at the declared queue bound fails closed.
- Marker outages or partial commits produce zero effects and zero cursor/read
  advance; only exact canonical settlement readback authorizes projections.
- Replay and pending-response paths cannot manufacture a second source owner or
  overwrite or clear another source's exact work.
- Active/settled Terminal A authority is adopted unchanged; legacy marker-only
  or in-progress replay ambiguity is quarantined rather than reclassified.
- Disabled mode is byte-for-byte behaviorally compatible at observable seams.
- Existing terminal M2 focused suites remain green.
- Coordinator tests record zero Graph, Sheets, Drive, OpenAI, credential, or
  network calls.

B1 is refuted by any lost source, second model request, mutable snapshot, second
transition owner, direct marker writer in an adopted lane, non-enumerable blocked
work, provider mutation, default-on behavior, or regression in retained M2
terminal guarantees.

## Non-goals for B1

- Stable row IDs, `rowBindings[]`, or user-level row owners (B2).
- General execution epochs/claim IDs, provider intents, or takeover effect
  reconciliation (B3).
- Full terminal/opt-out/human real-path cutover, frontend rules, or deployment
  enablement (B4).
- Automatic Graph draft deletion.
- Production migration or coalescing ambiguous legacy aliases.
- Campaign creation, mail, user enablement, provider calls, deployment, push, or
  production data access.

## Product go/no-go after B1

Even a fully verified B1 remains **NO-GO** for production. It removes the first
architectural blocker and creates the evidence base for B2-B4. Production
clearance still requires the complete M3 package, the M4 defect-family
regressions, one exact candidate freeze, and Baylor-owned end-to-end campaign
evidence before any Jill campaign decision changes.
