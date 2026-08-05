# B2 stable row authority design

**Status:** Approved design under the delegated production-clearance mandate, 2026-08-04

**Deliverable:** finding

**Production posture:** NO-GO; this phase is provider-free and runtime-unwired

## Outcome

B1 decides what one exact inbound source means. B2 makes one stable,
user-scoped logical row the sole owner of the resulting transition. A Sheet
coordinate and a thread root become compatibility projections rather than
authority inputs. B3 later performs and fences effects. B4 later wires the real
backend, frontend, rules, migration execution, and live proof.

## Current defect

- `email_automation/processing.py` derives terminal membership from mutable
  `clientId + rowNumber` in `_terminal_plan_members()` and chooses a
  lexicographically sorted thread root in `_build_terminal_finalization_plan()`.
- `_build_terminal_saga()` freezes coordinates and roots, not a stable row ID
  or exact B1 authority link. `_persist_terminal_settlement_projection()`
  retains settlement on a source thread, so a late root cannot discover it.
- Combined outreach stores `rows[]`, but terminal membership reads the singular
  `rowNumber`.
- `email_automation/sheet_operations.py` has no caller for
  `sync_thread_row_numbers_after_insert()`. Insert, move, sort, and
  `/api/decline-property` deletion can leave Firestore projections stale.
- `_store_contact_optout()` truncates SHA-256 to 16 hexadecimal characters.
  The opt-out handler independently mutates persistence, Sheet, roots,
  notification, and marker state while swallowing partial failures.
- Send-time opt-out reads are already fail-closed and compare both the exact
  lowercase address and its plus-alias-stripped mailbox identity; that safety
  behavior remains.
- B1 already derives verified priority as `contact_optout > terminal >
  human_decision`. Its real downstream adapter is intentionally unavailable.
  B2 consumes a validated B1 decision but does not classify or enable B1.

## Goals

1. Give each provider row a random, non-PII, never-reused logical identity.
2. Bind every singular, combined, split, recreated, and late thread root to a
   canonical bounded row set.
3. Linearize opt-out, terminal, and human decisions at a row head with durable
   generations, priority, leases, and fencing.
4. Retain logical settlements across movement, deletion, cleanup, restart, and
   late-root creation without claiming any provider effect occurred.
5. Produce a deterministic, offline-only migration plan that quarantines
   ambiguity instead of guessing.

## Non-goals

- No Graph, Sheets, notification, reply, or send-permit effect.
- No runtime adoption in `source_coordinator.py`, `processing.py`, `email.py`,
  `followup.py`, `messaging.py`, `sheet_operations.py`, or `app.py`.
- No provider reads, production migration, deploy, environment enablement,
  frontend change, Firestore rule change, or campaign.
- No reclassification of B1 evidence and no write to a B1 collection.
- No assertion that a logical settlement proves an email, Sheet write, or
  notification was applied. B3 owns effect evidence.

## Marker approaches

### Selected: row developer metadata

Use row-scoped Google Sheets DeveloperMetadata with `DOCUMENT` visibility,
metadata key `sitesift_row_id_v1`, and value `sr1_` plus 32 lowercase UUIDv4
hexadecimal characters. The provider-assigned numeric `metadataId` is a
location observation, never the logical identity.

Google documents that developer metadata stays associated with a row as its
location moves or the spreadsheet is edited, including insertion above the
row, and is deleted with the associated object. It also supports selecting
values through a developer-metadata data filter:

- [Sheets metadata guide](https://developers.google.com/workspace/sheets/api/guides/metadata)
- [DeveloperMetadata reference](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata)

Firestore remains durable after provider deletion. Deleting a marked row
advances the Firestore location lifecycle to `deleted`; reusing the coordinate
creates a new row ID.

### Rejected: hidden protected marker column

A user or integration can sort or copy only part of a range, omit the hidden
column, alter protection, or paste over it. The cell can therefore separate
from the business row. It is not an authority carrier.

### Rejected: dual metadata and hidden-column authority

Two markers create a reconciliation protocol before B2 has one source of
truth. A visible marker may be added later as a diagnostic projection, but it
cannot authorize or repair identity.

## Canonical encoding and validation

- Encode hash inputs as UTF-8 canonical JSON with sorted keys and compact
  separators. Prefix every hash input with a versioned domain string.
- Use complete lowercase SHA-256 hexadecimal values. Never truncate.
- Every conditional hash field is present and encoded as JSON `null` when it
  does not apply. Hashed timestamps are caller-frozen UTC RFC3339 strings with
  six fractional digits and `Z`. B2 exact schemas do not permit additional
  server-timestamp audit keys. Arrays use the canonical order specified by
  their schema.
- Every document has an exact-key schema. Unknown, missing, mistyped, or
  over-bound fields fail before writes.
- A boolean never satisfies an integer field.
- Immutable records are create-only. An exact retry with the same identity and
  hash is a no-op; any drift is a conflict with zero writes.
- `MAX_ROW_BINDINGS = 128`. Overflow quarantines before writes. It never
  truncates, silently splits, or changes how many messages may later send.
- `MAX_ROW_AUTHORITY_PLANNED_WRITES = 400` is an internal safety ceiling, not a
  claim about a provider hard write count. Each transaction validates its
  calculated writes before starting.

The following domains and logical payloads are fixed. Hash output fields are
never members of their own inputs. `schemaVersion` and `userScopeHash` are
included in every payload even where omitted below for readability, except
that `userScopeHash` itself contains neither field:

| Value | Domain | Complete logical payload beyond schema/user scope |
|---|---|---|
| `userScopeHash` | `sitesift.user.scope.v1` | exact verified token user ID, with no case or whitespace transformation |
| `contactIdentityHash` | `sitesift.contact.identity.v1` | normalization version, normalized mailbox identity |
| `markerHash` | `sitesift.row.marker.v1` | row ID, marker key/value, visibility, spreadsheet ID, sheet ID |
| `identityHash` | `sitesift.row.identity.v1` | row ID, client ID, spreadsheet ID, sheet ID, marker hash, creation kind/source hash |
| `headerHash` | `sitesift.row.header.v1` | ordered normalized header strings |
| `rowSnapshotHash` | `sitesift.row.snapshot.v1` | spreadsheet ID, sheet ID, header hash, ordered normalized cell strings |
| `observationEvidenceHash` | `sitesift.row.observation_evidence.v1` | observation kind, ordered exact marker/location observation objects |
| `revisionHash` | `sitesift.row.location.v1` | row ID, revision, nullable provider/display indexes, metadata ID, and row snapshot hash, marker hash, lifecycle, observation evidence hash, previous revision hash, observed time |
| `rowBindingsHash` | `sitesift.row.bindings.v1` | canonical row bindings, primary row ID, binding count |
| `bindingHash` | `sitesift.thread.row_binding.v1` | thread ID, client ID, row bindings hash, primary row ID, binding count, created time |
| `edgeId` | `sitesift.row.thread_edge_id.v1` | row ID, thread ID |
| `edgeHash` | `sitesift.row.thread_edge.v1` | edge ID, row ID, thread ID, role, thread binding hash, created time |
| `authorityLinkHashV1` | `sitesift.row.b1_authority_link.v1` | every legacy/terminal/human B1 v1 link field listed below except `authorityLinkHash`, including nullable hard-opt-out evidence hash |
| `authorityLinkHashV2` | `sitesift.row.b1_authority_link.v2` | every verified-contact B1 v2 link field listed below except `authorityLinkHash`, including hard-opt-out evidence plus exact/canonical identity hashes |
| `operatorActionId` | `sitesift.row.operator_action_id.v1` | actor scope hash, row bindings hash, client-request hash, action kind, reason code, issued time |
| `clientRequestHash` | `sitesift.row.operator_client_request.v1` | exact opaque client request ID |
| `operatorActionHash` | `sitesift.row.operator_action.v1` | action ID/kind, actor scope hash, row bindings hash, client request hash, reason code, issued time |
| `requestId` | `sitesift.row.claim_request_id.v1` | authority origin, nullable authority-link/operator-action/fan-out hashes, row bindings hash, owner kind/key, work key, payload hash |
| `claimSetHash` | `sitesift.row.claim_set.v1` | request ID, authority origin, nullable authority-link/operator-action/fan-out hashes, row bindings hash, owner kind/key, derived priority, planned writes, outcome, ordered row decisions, created time |
| `generationHash` | `sitesift.row.owner_generation.v1` | row ID, generation, request ID, claim-set hash, predecessor head/settlement hashes, owner kind/key, priority, lease epoch, first fence, created time |
| `logicalOutcomeHash` | `sitesift.row.logical_outcome.v1` | row ID, generation, owner kind/key, outcome, bounded reason/evidence hash |
| `outcomeEvidenceHash` | `sitesift.row.outcome_evidence.v1` | authority origin hashes, payload hash, outcome reason code |
| `settlementHash` | `sitesift.row.owner_settlement.v1` | row ID, generation/hash, exact fence, outcome, dominance/supersession links, nullable operator-action hash, outcome reason/evidence, logical outcome hash, settled time |
| `headHash` | `sitesift.row.authority_head.v1` | row ID, state revision, current location revision/hash/lifecycle, effective owner generation/hash/kind/priority, state, lease owner hash/deadline, fence, latest/effective settlement hashes, latest source-link hash, latest opt-out release-result hash, projection backlog count, created/updated times |
| `sourceSettlementLinkHash` | `sitesift.row.source_settlement_link.v1` | row ID, generation/hash, pre-authority link hash, B1 identity/final-ledger/settlement revision/hash, B2 settlement hash, linked time |
| `contactAliasHash` | `sitesift.contact.optout_alias.v1` | exact identity hash, canonical mailbox identity hash, created time |
| `contactSettlementHash` | `sitesift.contact.optout_settlement.v1` | canonical mailbox hash, generation, predecessor settlement hash, transition kind, exact identity hash, nullable B1 authority-link and hard-opt-out evidence hashes, nullable actor-scope hash and reason code, settled time |
| `contactHeadHash` | `sitesift.contact.optout_head.v1` | canonical mailbox hash, state revision, latest generation/settlement hash, nullable active opt-out settlement hash, state, active fan-out ID, created/updated times |
| `contactRowEdgeId` | `sitesift.contact.row_edge_id.v1` | canonical mailbox hash, row ID |
| `contactRowEdgeHash` | `sitesift.contact.row_edge.v1` | edge ID, canonical mailbox hash, row ID, created time |
| `contactRowEvidenceId` | `sitesift.contact.row_evidence_id.v1` | contact-row edge ID, thread binding hash, exact contact identity hash |
| `contactRowEvidenceHash` | `sitesift.contact.row_evidence.v1` | evidence ID, contact-row edge ID, thread ID, thread binding hash, exact contact identity hash, created time |
| `contactRowBindingHeadHash` | `sitesift.contact.row_binding_head.v1` | canonical mailbox hash, state revision, association count, last association hash, created/updated times |
| `contactFanoutId` | `sitesift.contact.optout_fanout_id.v1` | contact settlement hash, outcome `apply|release` |
| `contactFanoutObligationHash` | `sitesift.contact.optout_fanout_obligation.v1` | fan-out ID, row ID, contact row edge hash, expected contact settlement hash, outcome |
| `contactFanoutResultHash` | `sitesift.contact.optout_fanout_result.v1` | fan-out ID, row ID, obligation hash, outcome, disposition, reason code, observed row-head hash, nullable claim-set/row-settlement/released-row-settlement/restored-effective-settlement hashes, created time |
| `contactFanoutHeadHash` | `sitesift.contact.optout_fanout_head.v1` | fan-out ID, outcome, expected contact-settlement hash, state revision, state, binding revision/hash, discovery cursor, obligation/result counts, lease owner/deadline, fence, nullable superseding contact-settlement hash, created/updated times |
| `migrationReviewId` | `sitesift.row.migration_review_id.v1` | source snapshot hash, review kind, ordered affected identity hashes |
| `migrationReviewHash` | `sitesift.row.migration_review.v1` | review ID, source snapshot hash, review kind, ordered evidence hashes, disposition, created time |
| `migrationLinkId` | `sitesift.row.migration_link_id.v1` | prior row ID, replacement row ID, source snapshot hash, reason code |
| `migrationLinkHash` | `sitesift.row.migration_link.v1` | link ID, prior/replacement row IDs, source snapshot/evidence hashes, reason code, created time |

Document IDs derived from hashes use the complete hexadecimal digest. A hash
field may reference an externally frozen B1/B3 value, but B2 validates its
format and includes it verbatim in the listed parent payload.

Firestore requires transaction reads before writes, retries transactions after
conflicting edits, and applies successful writes atomically. The design also
stays below documented request and timeout bounds:

- [Firestore transactions](https://firebase.google.com/docs/firestore/manage-data/transactions)
- [Firestore quotas and limits](https://cloud.google.com/firestore/quotas)

## Normative schema registry

These definitions are authoritative for B2. `h64` is exactly 64 lowercase
hexadecimal characters. `rowId` matches
`^sr1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$`. `ts` is the
caller-frozen UTC timestamp format above. `uint` is a JSON integer from 0 through
`9007199254740991`; `pos` is `uint >= 1`; booleans never satisfy either.
`opaque` is an exact, already-NFC string of 1–512 UTF-8 bytes with no control
characters; it is rejected rather than trimmed or case-normalized. `code` is an
ASCII enum value of at most 64 bytes. `T?` means the key is mandatory and its
value is either `T` or JSON null. Every stored document below contains exactly
the named keys, including `schemaVersion: 1` and `userScopeHash: h64`; its
document ID exactly matches the path formula in the Record column. No raw
mailbox or verified user ID is stored.

The normative B1 contact-identity amendment is
`docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md`.
`B1LinkV1` is the existing exact shape
`{canonicalSourceId: opaque, snapshotImmutableHash: h64, selectionHash: h64,
ownerDecisionHash: h64, ledgerHash: h64, ownerKind:
contact_optout|terminal|human_decision, ownerKey: opaque, workKey: opaque,
payloadHash: h64, hardOptOutEvidenceHash: h64?, authorityLinkHash: h64}` and
uses the v1 domain. `B1LinkV2` is contact-only, uses the v2 domain, and adds
non-null `exactIdentityHash: h64` and
`canonicalMailboxIdentityHash: h64`. New terminal/human links and legacy
unbound contact links remain v1; B2-C accepts only a fully validated v2 link.
`B1Link` means the exact discriminated union `B1LinkV1|B1LinkV2`.

`RowBinding` is exactly `{rowId: rowId, role: primary|related}`. A
`RowDecision` is exactly `{rowId: rowId, decision:
accepted|dominated|blocked_by_claim_set, plannedGeneration: pos?,
winnerGenerationHash: h64?, winnerSettlementHash: h64?}` with the nullability
rules in the claim-set section.

Opaque row-snapshot cell and header values are provider-rendered strings,
Unicode-NFC normalized, with CRLF and CR normalized to LF while preserving all
other whitespace and case. Non-string inputs are invalid. The ordered header
list and ordered cell list each have at most 256 entries and each entry at most
8,192 UTF-8 bytes.

An observation-evidence object is exactly `{providerRowIndex: uint?,
displayRowNumber: pos?, metadataId: uint?, markerHash: h64,
rowSnapshotHash: h64}`. Evidence hashes `observationKind:
active|nonviable|deleted|ambiguous` and 0–128 such objects sorted by
`[providerRowIndex, metadataId, rowSnapshotHash]`, with JSON null ordered before
integers. Active/nonviable has exactly one fully non-null object, deleted has
zero, and ambiguous has at least two fully non-null objects.

### Row identity and ownership records

| Record | Exact fields after `schemaVersion`, `userScopeHash` |
|---|---|
| `rowIdentities/{rowId}` | `rowId: rowId`; `clientId: opaque`; `spreadsheetId: opaque`; `sheetId: uint`; `markerKey: sitesift_row_id_v1`; `markerValue: rowId`; `creationKind: fresh|migration`; `creationSourceHash: h64`; `markerHash: h64`; `identityHash: h64`; `createdAt: ts` |
| `rowLocationRevisions/{rowId}--{revision}` | `rowId: rowId`; `revision: pos`; `spreadsheetId: opaque`; `sheetId: uint`; `providerRowIndex: uint?`; `displayRowNumber: pos?`; `metadataId: uint?`; `markerHash: h64`; `rowSnapshotHash: h64?`; `lifecycle: active|nonviable|deleted|ambiguous`; `observationEvidenceHash: h64`; `previousRevisionHash: h64?`; `revisionHash: h64`; `observedAt: ts` |
| `rowAuthorityHeads/{rowId}` | `rowId: rowId`; `stateRevision: pos`; `currentLocationRevision: pos`; `currentLocationHash: h64`; `currentLocationLifecycle: active|nonviable|deleted|ambiguous`; `effectiveOwnerGeneration: pos?`; `effectiveOwnerGenerationHash: h64?`; `effectiveOwnerKind: contact_optout|terminal|human_decision?`; `effectivePriority: 1|2|3?`; `state: clear|claimed|review_pending|settled`; `leaseOwnerHash: h64?`; `leaseUntil: ts?`; `fencingToken: pos?`; `latestSettlementHash: h64?`; `effectiveSettlementHash: h64?`; `latestSourceSettlementLinkHash: h64?`; `latestOptOutReleaseResultHash: h64?`; `projectionBacklogCount: uint`; `headHash: h64`; `createdAt: ts`; `updatedAt: ts` |
| `threadRowBindings/{threadId}` | `threadId: opaque`; `clientId: opaque`; `rowBindings: RowBinding[1..128]`; `primaryRowId: rowId`; `bindingCount: pos`; `rowBindingsHash: h64`; `bindingHash: h64`; `createdAt: ts` |
| `rowThreadBindings/{edgeId}` | `edgeId: h64`; `rowId: rowId`; `threadId: opaque`; `role: primary|related`; `threadBindingHash: h64`; `edgeHash: h64`; `createdAt: ts` |
| `rowOperatorActions/{actionId}` | `actionId: h64`; `actionKind: decline`; `actorScopeHash: h64`; `rowBindingsHash: h64`; `clientRequestHash: h64`; `reasonCode: decline_property`; `issuedAt: ts`; `operatorActionHash: h64` |
| `rowClaimSets/{requestId}` | `requestId: h64`; `authorityOrigin: b1_source|authenticated_operator|contact_fanout`; `authorityLink: B1Link?`; `authorityLinkHash: h64?`; `operatorActionHash: h64?`; `fanoutId: h64?`; `rowBindings: RowBinding[1..128]`; `primaryRowId: rowId`; `bindingCount: pos`; `rowBindingsHash: h64`; `ownerKind: contact_optout|terminal|human_decision`; `ownerKey: opaque`; `workKey: opaque`; `payloadHash: h64`; `derivedPriority: 1|2|3`; `plannedWrites: uint`; `outcome: accepted|dominated`; `rowDecisions: RowDecision[1..128]`; `claimSetHash: h64`; `createdAt: ts` |
| `rowOwnerGenerations/{rowId}--{generation}` | `rowId: rowId`; `generation: pos`; `requestId: h64`; `claimSetHash: h64`; `predecessorHeadHash: h64`; `predecessorSettlementHash: h64?`; `ownerKind: contact_optout|terminal|human_decision`; `ownerKey: opaque`; `priority: 1|2|3`; `leaseEpoch: pos`; `firstFencingToken: pos`; `generationHash: h64`; `createdAt: ts` |
| `rowOwnerSettlements/{rowId}--{generation}` | `rowId: rowId`; `generation: pos`; `generationHash: h64`; `fencingToken: pos`; `outcome: contact_optout|terminal|human_declined|dominated`; `dominantGenerationHash: h64?`; `supersededEffectiveSettlementHash: h64?`; `operatorActionHash: h64?`; `outcomeReasonCode: verified_optout|terminal_source|operator_decline|superseded_by_higher_priority`; `outcomeEvidenceHash: h64`; `logicalOutcomeHash: h64`; `settlementHash: h64`; `settledAt: ts` |
| `rowSourceSettlementLinks/{rowId}--{generation}` | `rowId: rowId`; `generation: pos`; `generationHash: h64`; `authorityLinkHash: h64`; `b1IdentityHash: h64`; `b1FinalLedgerEvidenceHash: h64`; `b1SettlementRevision: pos`; `b1SettlementHash: h64`; `b2SettlementHash: h64`; `sourceSettlementLinkHash: h64`; `linkedAt: ts` |

The registry validates correlated nulls. A clear row head has all effective
owner, lease, fence, and effective-settlement fields null. A claimed or pending
head has an owner, lease, and fence. A settled head has effective owner and
settlement fields but null lease. An opt-out settlement alone may carry a
non-null `supersededEffectiveSettlementHash`; `operatorActionHash` is non-null
only for `human_declined`; `dominantGenerationHash` is non-null only for
`dominated`. Generation 1 has a non-null predecessor head because identity
initialization creates the clear head before claiming.

### Contact authority and fan-out records

| Record | Exact fields after `schemaVersion`, `userScopeHash` |
|---|---|
| `contactOptOutAliases/{exactIdentityHash}` | `exactIdentityHash: h64`; `canonicalMailboxIdentityHash: h64`; `contactAliasHash: h64`; `createdAt: ts` |
| `contactOptOutSettlements/{canonicalHash}--{generation}` | `canonicalMailboxIdentityHash: h64`; `generation: pos`; `predecessorSettlementHash: h64?`; `transitionKind: verified_optout|authenticated_release`; `exactIdentityHash: h64`; `authorityLink: B1Link?`; `authorityLinkHash: h64?`; `hardOptOutEvidenceHash: h64?`; `actorScopeHash: h64?`; `reasonCode: authenticated_release?`; `contactSettlementHash: h64`; `settledAt: ts` |
| `contactOptOutHeads/{canonicalHash}` | `canonicalMailboxIdentityHash: h64`; `stateRevision: pos`; `latestGeneration: pos`; `latestSettlementHash: h64`; `activeOptOutSettlementHash: h64?`; `state: active|released`; `activeFanoutId: h64`; `contactHeadHash: h64`; `createdAt: ts`; `updatedAt: ts` |
| `contactRowBindings/{edgeId}` | `edgeId: h64`; `canonicalMailboxIdentityHash: h64`; `rowId: rowId`; `contactRowEdgeHash: h64`; `createdAt: ts` |
| `contactRowBindingEvidence/{evidenceId}` | `evidenceId: h64`; `edgeId: h64`; `threadId: opaque`; `threadBindingHash: h64`; `exactIdentityHash: h64`; `contactRowEvidenceHash: h64`; `createdAt: ts` |
| `contactRowBindingHeads/{canonicalHash}` | `canonicalMailboxIdentityHash: h64`; `stateRevision: pos`; `associationCount: uint`; `lastAssociationHash: h64?`; `contactRowBindingHeadHash: h64`; `createdAt: ts`; `updatedAt: ts` |
| `contactOptOutFanoutHeads/{fanoutId}` | `fanoutId: h64`; `outcome: apply|release`; `expectedContactSettlementHash: h64`; `stateRevision: pos`; `state: discovering|applying|superseding|complete|superseded|ambiguous`; `bindingRevision: uint`; `bindingHeadHash: h64?`; `discoveryCursorRowId: rowId?`; `obligationCount: uint`; `resultCount: uint`; `leaseOwnerHash: h64?`; `leaseUntil: ts?`; `fencingToken: pos`; `supersedingContactSettlementHash: h64?`; `contactFanoutHeadHash: h64`; `createdAt: ts`; `updatedAt: ts` |
| `contactOptOutFanoutObligations/{fanoutId}--{rowId}` | `fanoutId: h64`; `rowId: rowId`; `contactRowEdgeHash: h64`; `expectedContactSettlementHash: h64`; `outcome: apply|release`; `contactFanoutObligationHash: h64`; `createdAt: ts` |
| `contactOptOutFanoutResults/{fanoutId}--{rowId}` | `fanoutId: h64`; `rowId: rowId`; `obligationHash: h64`; `outcome: apply|release`; `disposition: applied|dominated|restore|noop|superseded`; `reasonCode: claim_accepted|claim_dominated|exact_predecessor|row_optout_not_applied|already_restored|different_effective_owner|contact_head_advanced`; `observedRowHeadHash: h64`; `claimSetHash: h64?`; `rowSettlementHash: h64?`; `releasedRowSettlementHash: h64?`; `restoredEffectiveSettlementHash: h64?`; `contactFanoutResultHash: h64`; `createdAt: ts` |

For contact settlements, `verified_optout` requires the B1 and hard-opt-out
fields and null actor/reason fields; `authenticated_release` requires exact
predecessor, actor, and reason fields and null B1/evidence fields. An active
head requires `activeOptOutSettlementHash == latestSettlementHash`; a released
head requires it null. A new contact-binding head may have count 0 and null
last hash. Fan-out result disposition/outcome, nullable evidence, and reason are
the exact combinations defined in the fan-out protocol; all other combinations
are invalid. Binding revision 0 requires null binding-head hash; a positive
revision requires a hash. `supersedingContactSettlementHash` is required only
in `superseding|superseded`. Terminal fan-outs have null lease fields but retain
their last fencing token; an active lease requires both owner and deadline.

### Migration review records

`rowAuthorityMigrationReviews/{reviewId}` contains exactly `schemaVersion`,
`userScopeHash`, `reviewId: h64`, `sourceSnapshotHash: h64`, `reviewKind:
missing_row|missing_allocation|duplicate_anchor|conflicting_roots|malformed_combined_rows|
ambiguous_settlement|duplicate_marker|legacy_optout_collision`,
`affectedIdentityHashes: h64[1..128]` sorted lexicographically,
`evidenceHashes: h64[1..128]` sorted lexicographically, `disposition:
quarantined`, `migrationReviewHash: h64`, and `createdAt: ts`.

`rowAuthorityMigrationLinks/{linkId}` contains exactly `schemaVersion`,
`userScopeHash`, `linkId: h64`, `priorRowId: rowId`, `replacementRowId: rowId`,
`sourceSnapshotHash: h64`, `evidenceHash: h64`, `reasonCode: tab_replacement`,
`migrationLinkHash: h64`, and `createdAt: ts`. In B2 both record types are
canonical offline plan objects; B4 alone may persist them.

## Identity, location, and bindings

### `users/{uid}/rowIdentities/{rowId}` — immutable

Fields: `schemaVersion`, `rowId`, `userScopeHash`, `clientId`, `spreadsheetId`,
numeric `sheetId`, `markerKey`, `markerValue`, `creationKind`,
`creationSourceHash`, `markerHash`, `identityHash`, and `createdAt`.

`rowId` and `markerValue` are the same `sr1_...` value. `identityHash` includes
the user scope, immutable provider scope, marker contract, and creation source,
but not mutable coordinates.

### `users/{uid}/rowLocationRevisions/{rowId}--{revision}` — immutable

Fields: `schemaVersion`, `rowId`, positive `revision`, `spreadsheetId`,
`sheetId`, nullable zero-based provider row index, nullable one-based display
row number, nullable provider `metadataId`, `markerHash`, nullable
`rowSnapshotHash`,
lifecycle `active|nonviable|deleted|ambiguous`, `observationEvidenceHash`,
`previousRevisionHash`, `revisionHash`, and `observedAt`. Deleted or ambiguous
observations use JSON-null coordinates and row snapshot; an ambiguous
observation's evidence hash commits to the canonical conflicting-location set.

Move and sort append a revision for the same row ID. Insert creates a new row
identity. Delete appends `deleted`. A different sheet/tab creates a new row ID
and an explicit migration review/link; identity never silently crosses grids.

Initial creation is one Firestore transaction after exact validation of one
provider marker observation. It reads absence of the row identity, location
revision 1, and authority head, then creates all three. The initial head has
location revision/hash 1, lifecycle `active|nonviable`, state revision 1,
state `clear`, and every owner/lease/settlement/link field JSON null. An exact
retry is a no-op only after all three records read back exactly; a partial or
different record is ambiguous and fail-closed.

A move, sort, nonviable transition, delete, or ambiguity transaction reads the
identity, current head, and current immutable location revision before writes.
It allocates exactly `currentRevision + 1`, creates that revision, and CAS
advances the head's location fields and state revision. Firestore retries a
concurrent edit from fresh reads. If the candidate revision already exists,
exact revision-and-head readback is an idempotent success; any differing
revision or head is a conflict. Apply-then-raise uses the same exact readback.
`deleted` is irreversible for that row ID. A later marker at the same coordinate
must initialize a different random row ID; a duplicate marker advances the old
identity to `ambiguous` and never authorizes coordinate reuse.

### `users/{uid}/threadRowBindings/{threadId}` — immutable

Fields: `schemaVersion`, `threadId`, `clientId`, canonical `rowBindings`,
`primaryRowId`, `bindingCount`, `rowBindingsHash`, `bindingHash`, and
`createdAt`.

Each entry is exactly `{rowId, role}` where role is `primary|related`.
Persisted entries are unique by row ID and sorted lexicographically. Exactly
one entry is primary and equals `primaryRowId`. Equivalent raw input may be
normalized before persistence; persisted noncanonical input is invalid.

### `users/{uid}/rowThreadBindings/{edgeId}` — immutable

`edgeId` is the full domain-separated hash of user scope, row ID, and thread
ID. Fields link `rowId`, `threadId`, role, `threadBindingHash`, `edgeHash`, and
creation time. Reverse edges avoid mutable-coordinate scans.

Thread `rowNumber`, `rows[]`, `rowBindings`, `rowBindingsHash`, and
`primaryRowId` fields are compatibility projections only. They never decide a
claim, settlement, or late-root block.

## Claim, generation, and settlement records

### `users/{uid}/rowClaimSets/{requestId}` — immutable

`requestId` is deterministically derived from a discriminated authority origin,
canonical row bindings, owner, work key, and payload hash. `authorityOrigin` is
exactly `b1_source|authenticated_operator|contact_fanout`. A B1 source requires
an `authorityLinkHash` and JSON-null operator/fan-out hashes. An authenticated
operator requires an `operatorActionHash` and JSON-null B1/fan-out hashes. A
contact fan-out requires its `fanoutId` plus the originating contact opt-out's
frozen B1 `authorityLinkHash`, and a JSON-null operator hash. The claim set is
created in the deciding transaction and includes those values, derived
priority, `rowBindingsHash`,
`plannedWrites`, outcome `accepted|dominated`, exact row decisions,
`claimSetHash`, and creation time. Each row decision has the exact shape
`{rowId, decision, plannedGeneration, winnerGenerationHash,
winnerSettlementHash}` and the array is sorted by `rowId`. `decision` is
`accepted|dominated|blocked_by_claim_set`. An accepted claim set contains only
accepted decisions, each with `plannedGeneration` and JSON-null winner hashes.
A dominated claim set contains at least one dominated decision with JSON-null
`plannedGeneration` and exact winner hashes; a winner settlement is nullable
only while that winner is still claimed. Any otherwise claimable row is
`blocked_by_claim_set` with all three generation/winner fields JSON null. No
generation or head advances for any row in a dominated multi-row claim.
Generation hashes are never embedded in accepted decisions, so `claimSetHash`
is computable before each generation hashes the claim set. A dominated request
therefore has durable all-or-none evidence without allocating a generation.

The exact B1 link, required by `b1_source` and `contact_fanout`, contains
`canonicalSourceId`, `snapshotImmutableHash`,
`selectionHash`, `ownerDecisionHash`, `ledgerHash`, `ownerKind`, `ownerKey`,
`workKey`, `payloadHash`, conditional `hardOptOutEvidenceHash`, and
`authorityLinkHash`. A v2 verified-contact link additionally contains the exact
and canonical mailbox identity hashes copied from bound version-2 B1 evidence.
Hard opt-out evidence is required only for verified `contact_optout`; model
text cannot mint opt-out priority. Contact fan-out and contact settlement paths
reject legacy v1 contact links before writes.

### `users/{uid}/rowOperatorActions/{actionId}` — immutable

This is the sole B2 origin for a row-wide authenticated human action. It records
action `decline`, verified actor scope hash, canonical row bindings hash,
client-request hash, bounded reason code, issued time, and `operatorActionHash`.
Notification dismissal is UI-only and stop/resume remains thread-local; neither
can create this record. A decline may settle the exact current
`review_pending` generation with its current fence, or, when none exists,
create an `authenticated_operator` priority-1 claim and settle it as
`human_declined` in the same transaction. Actor, target, or request drift is a
zero-write conflict. B2 only defines and tests this pure contract; B4 owns the
authenticated route adapter. The no-pending claim uses owner kind
`human_decision`, actor-scope hash as owner key, action ID as work key, and
operator-action hash as payload hash. Settling an existing pending generation
does not create a second claim; its settlement freezes the operator-action
hash.

### `users/{uid}/rowAuthorityHeads/{rowId}` — mutable CAS head

Fields: positive monotonic `stateRevision`; current location
revision/hash/lifecycle; effective owner generation/hash/kind, derived
priority, state `clear|claimed|review_pending|settled`; lease owner and
deadline; positive fencing token; latest settlement hash; effective settlement
hash; latest B1 settlement link hash; latest opt-out release-result hash;
projection backlog count; `headHash`; and timestamps. All effective-owner,
lease, settlement, and link fields are present as JSON null when inapplicable.
Every successful CAS supplies the expected state revision and head hash and
increments `stateRevision` exactly once.

### `users/{uid}/rowOwnerGenerations/{rowId}--{generation}` — immutable

Fields link the claim set, predecessor head/settlement hashes, owner kind/key,
priority, lease epoch, first fencing token, and `generationHash`.

### `users/{uid}/rowOwnerSettlements/{rowId}--{generation}` — immutable

Fields link the generation and exact fence, and record
`contact_optout|terminal|human_declined|dominated`, dominance/supersession
links, nullable operator-action hash, logical outcome hash, and time.
`human_review_pending` is a head state, not a settlement: the same generation
and current fence must later settle as `human_declined` or remain visibly
pending. `human_declined` advances the head to `settled` and is a late-root
tombstone. `dominated` settles a generation that previously owned the head and
was later superseded; a request that never owned a generation records
`dominated` only in its immutable claim set. The head's effective settlement
plus its immutable settlement is the late-root tombstone. Contact release is
not a row-owner settlement or generation; its separately authenticated restore
protocol is defined below. No provider-effect fields are permitted.

### `users/{uid}/rowSourceSettlementLinks/{rowId}--{generation}` — immutable

The pre-settlement B1 authority link is frozen only in a `b1_source` or
`contact_fanout` claim set and owner generation. After B1 independently creates
its immutable source settlement,
this separate document is created once with that pre-link hash, B1
`identityHash`, `finalLedgerEvidenceHash`, settlement revision/hash, and the B2
logical settlement hash. It is never updated. The one-time post-settlement
record avoids circular settlement hashes.

## Contact-wide hard opt-out

The normative clarification for this section is
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-c-contact-compliance-amendment.md`.
It governs transition receipts, retry semantics, historical row allocation,
fan-out completion, late associations, and authenticated release wherever it
is more specific than this base design.

Normalize by Unicode-NFC normalizing the input, trimming surrounding Unicode
whitespace, applying Python `str.lower()`, and requiring exactly one nonempty
local part, one `@`, and one nonempty domain. Reject controls and normalized
values longer than 320 UTF-8 bytes. The canonical mailbox removes the first
`+` and everything after it from the local part. Do not remove dots or add
domain-specific equivalence. Store only complete identity hashes in authority
records, not raw addresses.

- `contactOptOutAliases/{exactIdentityHash}` is an immutable index to the
  canonical plus-stripped mailbox hash. The bare canonical identity has a
  self-index. Exact retry is a no-op; a conflicting mapping fails closed.
- `contactOptOutHeads/{canonicalMailboxIdentityHash}` is the immediate
  future-send suppression authority. It contains positive monotonic
  `stateRevision`, latest generation/settlement hash, nullable active opt-out
  settlement hash, `active|released` state, active fan-out ID, and head hash.
  Evidence is read from the immutable latest settlement and is not duplicated
  on the head. Every CAS checks the prior revision/hash and increments the
  revision exactly once.
- `contactOptOutSettlements/{canonicalHash}--{generation}` is immutable and
  discriminates `verified_optout|authenticated_release`. A verified opt-out
  freezes the complete originating B1 authority link and hard-opt-out evidence
  hash and uses JSON-null actor/reason hashes. A release links its exact active
  opt-out predecessor and stores authenticated actor/reason hashes with
  JSON-null B1 link/evidence fields.

Opt-out creation atomically creates or validates both exact and canonical alias
indexes, appends the settlement, and advances the canonical head. Release
atomically validates those indexes, appends a bounded-reason authenticated
release, and advances the same head; indexes and history remain. Every send
check computes both hashes, reads the exact alias index and canonical head, and
suppresses when the canonical head is active. A successful absent-alias read is
permitted for a previously unseen plus variant because the canonical hash is
computed directly: an active canonical head still suppresses, while a valid
released or successfully absent canonical head does not. An alias/head RPC
error, an existing conflicting alias, malformed data, or ambiguous state is
fail-closed. Absence and read failure are distinct results in the contract.

Row fan-out may converge in bounded transactions after contact suppression is
durable. Release never deletes history and does not undo an independent
terminal row settlement.

For a verified B1 opt-out, the atomic contact settlement, active contact head,
and apply fan-out head are B2's immutable completion evidence back to B1; B1
does not wait on every row result. This lets B1 create its source settlement
without a cycle. The fan-out remains mandatory fail-closed health work, and its
row source-settlement links can be appended after that B1 settlement exists.

### Bounded contact-to-row fan-out

Contact-wide authority has a provider-free, persisted convergence protocol:

- `contactRowBindings/{edgeId}` is an immutable contact-to-row association whose
  ID and hash depend only on canonical mailbox hash and row ID, not one thread.
  `contactRowBindingEvidence/{evidenceId}` separately freezes each supporting
  thread ID/binding hash and exact contact identity hash. Split, recreated, and
  late roots therefore add evidence without conflicting with the association.
  `contactRowBindingHeads/{canonicalHash}` is a mutable CAS head with
  `stateRevision`, association count, last association hash, head hash, and
  timestamps. Creating the first association and its first evidence atomically
  advances this head; later evidence is create-only and does not change the
  association count.
- `contactOptOutFanoutHeads/{fanoutId}` is created in the same transaction that
  advances the contact head. It contains
  `discovering|applying|superseding|complete|superseded|ambiguous`, the expected
  contact settlement, contact-binding revision/hash, discovery cursor,
  obligation and result counts, lease/fence, state revision, nullable
  superseding contact-settlement hash, and head hash.
- `contactOptOutFanoutObligations/{fanoutId}--{rowId}` is immutable and links
  one discovered contact-row edge to the exact contact settlement and required
  row outcome `apply|release`. Obligations are discovered in canonical row-ID
  pages and applied in transactions whose calculated writes remain at or below
  the internal 400-write ceiling. No page exceeds 128 row bindings.
- `contactOptOutFanoutResults/{fanoutId}--{rowId}` is immutable. It links the
  exact obligation and records disposition
  `applied|dominated|restore|noop|superseded`, a bounded enum reason, observed
  row-head hash, and nullable claim-set, row-settlement, released-row-settlement,
  and restored-effective-settlement hashes. Apply obligations allow
  `applied|dominated|superseded`; release obligations allow
  `restore|noop|superseded`.

Discovery reads the contact-row binding head, scans edges in row-ID order,
creates deterministic obligations, and advances the cursor with CAS. Before
changing to `applying` or `complete`, it re-reads the binding head. A changed
revision/hash continues discovery; it never certifies an older snapshot.
Completion requires exact readback of one matching immutable result per
obligation, any settlement/claim evidence named by each result, equal
obligation/result counts, an unchanged final binding head, and no malformed or
ambiguous association.

Creating a contact-row association while the contact head's current fan-out is
`discovering|applying` atomically creates the deterministic obligation for that
fan-out before the row can become effect-eligible; a released contact's release
obligation normally resolves `noop`. If the contact remains active after its
apply fan-out is `complete`, the association transaction synchronously creates
and resolves the one new obligation, advances the binding and fan-out snapshots
and counts, and leaves the fan-out `complete`; failure or ambiguity creates no
association. Thus a late row cannot appear behind a completed opt-out. A new
association after a released fan-out is complete needs no release obligation.

An apply obligation constructs a deterministic one-row `contact_fanout` claim:
it copies the complete B1 link
from the contact opt-out settlement, uses canonical mailbox hash as owner key,
`fanoutId--rowId` as work key, contact settlement hash as payload hash, and the
single-row binding hash. It then follows normal priority-3 generation and
settlement rules. This supplies the required claim set and originating B1 link
without inventing a second classifier.

A release obligation never allocates a claim set, owner generation, or source
priority. If the row head's effective settlement is the exact row opt-out
settlement produced from the released contact settlement, one CAS creates a
`restore` result and restores the settlement that the opt-out settlement had
superseded, or clear state when that hash was null. Effective owner
generation/kind/priority and state are copied from that restored settlement, or
set to JSON null/`clear`; `latestOptOutReleaseResultHash` advances. All later
claim comparisons use these effective-owner fields. If that exact row opt-out
was never applied, was already restored, or no longer controls the row, the
result is `noop` with an exact enum reason and the head is unchanged. This
cannot erase an independent terminal or human-decline settlement.

Release and opt-out races are fenced by contact-head state revision and active
settlement hash. Advancing the contact head also CAS-transitions its prior
`discovering|applying` fan-out to `superseding`; a missing, ambiguous, or
inconsistently terminal current fan-out blocks the contact transition with zero
writes. Bounded workers create `superseded`
results for every already-discovered unfinished obligation and then move the
fan-out to terminal `superseded`, linked to the newer contact settlement; no
new obligation may be discovered once superseding starts. A properly linked
`superseded` head is terminal and healthy, while `superseding`, an unlinked
superseded head, or any result mismatch is nonhealthy. A stale release cannot
clear a later opt-out, and a later opt-out's new apply fan-out dominates the
unfinished release. A concurrently created binding must add an obligation for
the contact head's current nonterminal fan-out. Any stale worker first observes
the changed contact head and can write only a `superseded` result, never a row
transition.

## Priority and transaction protocol

Priority is derived, never accepted from a caller:

```text
contact_optout = 3
terminal       = 2
human_decision = 1
```

An authenticated contact release is not a fourth source priority and cannot be
supplied by B1. It uses the release-result restore protocol above, valid only
against the exact active contact-opt-out settlement and row predecessor. An
authenticated operator decline is priority-1 `human_decision`; its immutable
operator action replaces the B1 link as the claim origin when no pending B1
human generation exists.

1. Resolve rows only from immutable bindings and validate the discriminated
   authority origin: complete B1 link, operator action, or contact fan-out link.
2. Read every identity, binding, head, predecessor generation, and settlement
   before any transaction write.
3. An exact request and hash is an idempotent resume. Drift writes nothing.
4. Higher priority creates the next generation and supersedes lower priority,
   including opt-out superseding a settled terminal generation.
5. Lower priority receives an immutable `dominated` claim-set outcome and
   cannot allocate a generation or advance the head.
6. Equal priority from a different source keeps the first transactionally
   committed winner. No thread-ID or source-ID lexical election is allowed.
7. Multi-row claims are all-or-none. If any valid target is dominated, the
   claim set is `dominated`; claimable peers are `blocked_by_claim_set` and no
   generation advances. Malformed input, hash drift, unreadable state, or a CAS
   conflict produces zero writes and is retried only from fresh reads.
8. Lease takeover increments the fencing token within the same generation.
   A stale fence cannot settle, alter a head, or project thread state.
9. Failure before commit has zero writes. Apply-then-raise is accepted only
   after exact readback of the claim set, every generation, and every head.
   Partial or mismatched readback is ambiguous and fail-closed.

Human-action mapping is explicit: notification dismissal is UI-only;
stop/resume is thread-local send state; explicit decline/delete is row-wide
`human_decision`. A pending row-wide decision holds the claimed generation in
`review_pending`; an explicit decline settles that generation as
`human_declined` and creates the late-root tombstone. None can bypass an
opt-out or terminal row settlement.

## Offline migration design

`scripts/row_authority_migration_report.py` accepts an explicit sanitized
offline snapshot and never constructs provider clients. It emits a canonical
plan and review records; it performs zero authority or provider writes.

The snapshot includes an explicit allocation map from each unmarked row's
evidence hash to a preallocated random UUIDv4 row ID. The report validates
format, uniqueness, user scope, and one-to-one use but never generates an ID.
A missing/reused allocation is quarantined as `missing_allocation`. Therefore
the same complete snapshot produces byte-identical row IDs and report output,
while allocation remains random rather than derived from business data.

- Singular roots bind their resolved row; combined roots bind all `rows[]` and
  retain the singular legacy row as primary; split roots converge on one row
  ID.
- Existing developer metadata is authoritative only after exact marker/schema
  validation and unique location readback.
- A tab replacement receives a preallocated new row ID and an explicit
  `rowAuthorityMigrationLinks` plan object; it never carries the prior identity
  silently.
- Missing rows, duplicate anchors without a marker, conflicting roots,
  malformed combined lists, ambiguous settlement geometry, metadata
  duplication, or a possible 16-hex legacy opt-out collision produces an
  immutable `rowAuthorityMigrationReviews/{reviewId}` plan entry and no
  authority write.
- A validated legacy terminal settlement is linked, never rewritten or
  deleted. Deleted rows retain Firestore identity and settlement history.
- Migration execution and real provider verification belong to B4.

## Health, retention, and rollout

Health is fail-closed for unreadable/overflow state, unbound roots, duplicate or
ambiguous markers, location revision/head mismatch, expired claims, stale
fences, missing or mismatched discriminated authority origins, projection
backlog, hash drift, binding overflow, opt-out collision, contact-row
association/evidence/index drift, nonterminal or ambiguous fan-out,
obligation/result/evidence mismatch, stale release work, and unresolved
migration reviews. `complete` and exactly linked `superseded` fan-outs are the
only healthy terminal fan-out states.

Automatic cleanup never deletes B2 authority or migration review records.
Explicit user-data reset must enumerate every B2 collection and use bounded
deletion. B2 has no runtime mode and is not imported by legacy effect paths;
therefore committing B2 cannot change production behavior. B3 must require a
stable binding and current tombstone check before an irreversible send. B4
must prove the metadata contract, migration, rules, UI, and scoped cutover on
the exact candidate before enablement.

## File boundary

Expected B2 files are:

- create `email_automation/row_authority.py` for schemas, hashes, canonical
  bindings, claims, generations, settlement, and readback;
- create `email_automation/row_metadata.py` for pure request/response dictionaries
  and marker validation only—no API client;
- create `scripts/row_authority_migration_report.py` for offline planning;
- add focused row-authority tests and a marker-aware fake;
- modify only health, explicit reset, cleanup retention, and static inventory
  tests/registries needed to recognize B2 authority.

The phase does not modify runtime adoption files named in Non-goals.

## Acceptance matrix

The implementation is complete only when executable tests prove:

- random user-scoped create-only identity; atomic identity/revision/head
  initialization; exact retry; drift rejection; concurrent revision CAS and
  apply-then-raise readback;
- developer metadata survives simulated insert, move, sort, and restart;
- deletion tombstones history and coordinate reuse gets a new ID;
- canonical dedupe/sort, one primary, combined and split binding, reverse-edge
  readback, 128 success, and 129 pre-write quarantine;
- verified opt-out dominates terminal and human, terminal dominates human,
  unverified model opt-out stays human review, and equal-priority first commit
  wins;
- exact and plus-alias variants resolve one canonical opt-out head, alias/head
  creation and release are atomic, successful missing alias is distinguished
  from read failure, and every alias/head RPC or validation failure blocks;
- stable contact-row associations support multiple split/recreated thread
  evidence records, advance a CAS index only on the first association,
  opt-out/release create durable fan-out heads, discovery is cursor-idempotent,
  current bindings add obligations, and completion requires exact immutable
  result/evidence readback against a stable index; a late binding behind an
  active completed fan-out is applied and re-certified atomically;
- apply fan-out builds the deterministic one-row claim with the originating B1
  link; release creates no claim/generation and records exact
  `restore|noop|superseded` dispositions; superseding is bounded and only an
  exactly linked terminal `superseded` fan-out is healthy;
- release restores only the prior non-opt-out effective row state, a stale
  release cannot clear a later opt-out, and a later opt-out fences unfinished
  release work;
- pending human review has no settlement, operator-action identity/target drift
  writes nothing, explicit decline settles as `human_declined`, and
  dismiss/stop/resume never create row settlements;
- multi-row all-or-none including mixed `dominated` plus
  `blocked_by_claim_set`, two-worker conflict, expired takeover, stale-fence
  refusal, pre-apply failure, exact apply-then-raise readback, and partial
  readback ambiguity;
- every registry record rejects missing, unknown, mistyped, over-bound, or
  invalid correlated-null fields and booleans in integer positions;
- every fixed hash domain reproduces from its complete payload, changes on any
  field drift, preserves explicit nulls/timestamp encoding, and cannot collide
  with another domain using the same values;
- exact B1 link copying, B1 drift refusal, opt-out evidence requirement, no B1
  writes, and create-only post-settlement link;
- offline singular/combined/split migration, duplicate/missing/deleted review,
  missing/reused allocation quarantine, tab-replacement link, legacy settlement
  link, truncated-hash collision quarantine, zero provider calls, and
  byte-identical report rerun;
- health failure for every listed inconsistency, cleanup retention, explicit
  reset inventory, no provider imports, and no B2 import or literal leakage into
  B1 or legacy runtime paths;
- complete B1, retained M2, release/auth, compile, and diff gates remain green.

## Resolved decisions

The marker carrier, 128 bound, overflow quarantine, contact normalization,
immutable opt-out release, human-action scope, tab-replacement behavior, B1/B2
settlement separation, provider-free boundary, and B3/B4 handoff are fixed by
this design. There are no deferred design choices inside B2.
