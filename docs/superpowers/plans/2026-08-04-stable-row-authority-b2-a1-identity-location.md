# Stable Row Authority B2-A1 Identity and Location Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans to
> implement this plan task by task. Use superpowers:test-driven-development for
> every behavior change, superpowers:systematic-debugging for any unexpected
> failure, and superpowers:verification-before-completion before each
> publication claim.

**Goal:** Create the provider-free, runtime-unwired row-marker, immutable row
identity, immutable location-revision, and mutable location-head authority that
turns a Google Sheets row marker into durable Firestore identity without
authorizing ownership or effects.

**Architecture:** Keep Google API calls outside B2. `row_metadata.py` creates
and parses exact DeveloperMetadata dictionaries only. `row_authority.py`
extends the A0 canonical primitives with exact schemas and a datastore-neutral
coordinator whose Firestore-shaped client and transaction executor are required
constructor dependencies. A B2-only marker-aware sheet fake and transaction
executor prove row movement, deletion, restart, CAS, concurrency, and
apply-then-raise behavior without provider access.

**Tech stack:** Python 3.12 standard library, injected Firestore-shaped
interfaces, existing B2 bounded fake, `unittest`, AST containment, GitHub
Actions.

**Plan deliverable:** both (provider-free code and B2-A1 clearance evidence)

**Approved design:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`

**Program roadmap:**
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`

**Completed predecessor:**
`docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a0.md`

**Baseline:** `5317c7714cd440f2f89f4055e366b831958b5020`

**Publication checkpoint:** `B2-A`

**Safety boundary:** No Sheets API client/import/call, provider read/write,
production Firestore read/write, runtime adoption, deploy, `main` merge,
campaign, frontend/rules change, B2 ownership/claim/settlement, migration
execution, or external communication. Remote writes are limited to reviewed
milestone commits on Baylor's owned
`codex/sitesift-production-clearance-20260804` branch. A branch push is not a
production release.

## Frozen implementation decisions

1. `email_automation/row_authority.py` remains standard-library-only. The
   coordinator requires both a Firestore-shaped object and a
   `transaction_executor(transaction, callback)` dependency. It never imports
   `google.cloud`, `googleapiclient`, or a runtime application module.
2. `email_automation/row_metadata.py` is also provider-client-free. Its only
   application import is the A0 row-ID validator from `row_authority.py`.
   Runtime import containment permits that one B2-to-B2 edge and no adopter.
3. The official Sheets shapes are frozen as of 2026-08-04:
   - create uses one `createDeveloperMetadata.developerMetadata` dictionary;
   - row location is a bounded `dimensionRange` with `dimension: ROWS` and
     `endIndex == startIndex + 1`;
   - responses add positive `metadataId` and read-only `locationType: ROW`;
   - visibility is exactly `DOCUMENT`;
   - metadata key is exactly `sitesift_row_id_v1` and value is the complete
     `sr1_` UUIDv4 row ID;
   - search uses one exact `developerMetadataLookup` over key, value,
     visibility, and row location type;
   - search parsing accepts only an official omitted/empty match list or an
     explicit `matchedDeveloperMetadata` list, validates every returned match
     and echoed filter, returns 0–128 matches in canonical order, and rejects
     129 before any authority decision. A lookup exception is never passed to
     the parser and can never become an empty/deleted observation.
4. The provider `metadataId` and zero-based coordinate are observations only.
   The complete row ID remains logical identity. No ID is derived from client,
   spreadsheet, sheet, coordinate, header, cell, email, or thread data.
5. Location changes use explicit full-document CAS: the caller supplies the
   exact previously read and validated head. The transaction either advances
   that head once, recognizes the exact already-applied result, or writes
   nothing. It never allocates a second revision for a retry and never applies
   a stale observation after a newer head.
6. Initial identity creation is exactly three writes after all reads:
   `rowIdentities/{rowId}`, `rowLocationRevisions/{rowId}--1`, and
   `rowAuthorityHeads/{rowId}`. All absent creates all three; all exact is a
   no-op; partial or drift is ambiguous and fail-closed.
7. A location advance is exactly two writes after all reads: create the next
   immutable revision and replace the full exact head. The head increments
   both its monotonic `stateRevision` and current location pointer while
   preserving all ownership fields byte-for-byte.
8. `deleted` is irreversible for a row ID. Coordinate reuse requires a newly
   generated marker/row ID. Duplicate valid observations for one row ID append
   an `ambiguous` revision with null location fields and never elect a winner.
9. Caller-frozen timestamps are exact UTC RFC3339 strings with six fractional
   digits and `Z`. Raw verified user IDs are used only for the existing user
   document path; documents store only `userScopeHash`. Raw mailbox data is
   never an A1 input.
10. A1 does not implement bindings, owners, generations, settlements, contact
    compliance, or runtime projection. It validates and preserves the complete
    head shape solely so later B2-B work cannot be corrupted by a location CAS.
11. Canonical payload member names omitted by the design prose are frozen here:
    header payload uses `orderedHeaders`; row-snapshot payload uses
    `spreadsheetId`, `sheetId`, `headerHash`, and `orderedCellValues`;
    observation-evidence payload uses `observationKind` and `observations`.
12. One initialization timestamp populates identity/revision `createdAt` or
    `observedAt` and head `createdAt`/`updatedAt`. Later location changes keep
    head `createdAt` and set `updatedAt == observedAt`.
13. An observation that is semantically identical to the current immutable
    location revision is a zero-write no-op; timestamp drift alone never
    creates revision churn. Two identical workers from one predecessor both
    succeed with one revision. Two different observations from one predecessor
    serialize to one success and one conflict; A1 does not invent a
    latest-provider-truth election.
14. An ambiguous revision stores the immutable identity's `markerHash` in the
    revision and each conflicting observation's own marker hash in evidence.
    Active/nonviable evidence must match the identity grid/marker. Deleted is
    terminal even if the old marker is later observed or duplicated.

Official shape references:

- <https://developers.google.com/workspace/sheets/api/guides/metadata>
- <https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata>
- <https://developers.google.com/workspace/sheets/api/reference/rest/v4/DataFilter>

## File map

- Create `email_automation/row_metadata.py`: exact request/search dictionaries,
  strict DeveloperMetadata parser, and no API client.
- Modify `email_automation/row_authority.py`: A1 validators, canonical hashes,
  exact identity/location/head schemas, mutation plans, and injected
  transaction coordinator.
- Modify `tests/row_authority_fakes.py`: marker-aware sheet model and a bounded
  Firestore transaction executor with retries.
- Create `tests/test_row_authority_identity_location.py`: all A1 marker,
  schema, initialization, revision, race, retry, and containment tests.
- Modify `tests/test_row_authority_contracts.py`: replace the empty importer
  allowlist with the one reviewed B2 edge and prove neither B2 module has a
  runtime adopter.
- Modify `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`: plan
  publication and code status only.
- Create
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a1.md`: local,
  review, exact-SHA GitHub, and production-posture evidence.

No workflow edit is expected: the existing `test_row_authority*.py` discovery
must collect the new file automatically.

## Task order

Task 0 strengthens containment and builds only the B2 test harness. Task 1
freezes pure marker dictionaries/parser. Task 2 adds exact canonical schemas
and hashes. Task 3 adds atomic initialization. Task 4 adds revision CAS and row
lifecycle behavior. Task 5 performs complete clearance and publishes B2-A.

Use this interpreter for every Python command:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python
```

## Mandatory plan-publication gate — complete before Task 0

The executor may not modify A1 application or test code until this child plan
has one independent B2 design-compliance approval and a different independent
fresh-executor/TDD approval. Critical or Important findings reset the
corresponding approval.

- [x] **Step 1: Verify and review the plan**

Run:

```bash
B2_A1_PY=../codex-release-a-medium-recovery-20260714/.venv/bin/python
"$B2_A1_PY" - <<'PY'
from pathlib import Path

path = Path(
    "docs/superpowers/plans/"
    "2026-08-04-stable-row-authority-b2-a1-identity-location.md"
)
text = path.read_text(encoding="utf-8")
assert "**Plan deliverable:** both" in text
assert "**Baseline:** `5317c7714cd440f2f89f4055e366b831958b5020`" in text
assert "transaction_executor(transaction, callback)" in text
assert "production remains NO-GO" in text
assert text.count("- [ ] **Step") >= 25
print("ok")
PY
git diff --check
rg -n 'TO[D]O|T[B]D|FIX[M]E|PLACEH[O]LDER|pending decisio[n]' \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md
```

Expected: parser prints `ok`, diff check has no output, placeholder scan has no
matches, and both reviewers return `APPROVED`.

- [x] **Step 2: Freeze and publish only the plan milestone**

After approvals, add a roadmap status item immediately before `B2-A1 is green
and published`:

```markdown
- [x] B2-A1 child plan is independently approved and published.
```

Stage exactly the roadmap and this plan, inspect the staged diff, and commit:

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md
git diff --cached --stat
git diff --cached --check
git commit -m "docs: plan B2-A1 identity and location"
```

- [x] **Step 3: Prove the exact remote plan SHA is green**

```bash
git push origin codex/sitesift-production-clearance-20260804
B2_A1_PLAN_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_A1_PLAN_SHA"

B2_A1_PLAN_RUN_ID=""
for attempt in {1..30}; do
  B2_A1_PLAN_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_A1_PLAN_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_A1_PLAN_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_A1_PLAN_RUN_ID" && break
  sleep 2
done
test -n "$B2_A1_PLAN_RUN_ID"
gh run watch "$B2_A1_PLAN_RUN_ID" --exit-status
test "$(gh run view "$B2_A1_PLAN_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_A1_PLAN_SHA"
test "$(gh run view "$B2_A1_PLAN_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
test -z "$(git status --porcelain)"
```

Record the plan SHA/run URL in the implementation log. Do not open a PR,
merge, deploy, or touch production. Task 0 starts only after this succeeds.

### Task 0: Strengthen B2 containment and add the A1-only harness

**Files:**

- Modify: `tests/test_row_authority_contracts.py`
- Create: `tests/test_row_authority_identity_location.py`
- Modify: `tests/row_authority_fakes.py`

- [x] **Step 1: Write failing containment and transaction-runner tests**

Create the A1 test file with `RowAuthorityA1ContainmentTests` and
`RowAuthorityA1HarnessTests`. Add discriminating tests named:

```text
test_only_row_metadata_may_import_row_authority
test_no_runtime_module_imports_row_metadata
test_transaction_executor_retries_one_stale_snapshot
test_transaction_executor_stops_after_max_attempts
test_transaction_executor_preserves_apply_then_raise
test_marker_fake_preserves_marker_through_insert_move_sort_and_restart
test_marker_fake_deletes_marker_with_row
test_marker_fake_can_expose_duplicate_locations_without_election
```

Update the A0 importer scan design in the failing test, not production code:

- `ROW_AUTHORITY_IMPORTER_ALLOWLIST` becomes exactly
  `{"email_automation/row_metadata.py"}`;
- scan both direct and conservatively detected dynamic imports;
- `row_metadata.py` itself has an empty runtime-importer allowlist;
- allowed direct roots for both B2 modules are only standard library plus the
  reviewed relative/import path from `row_metadata` to `row_authority`;
- synthetic literal and nonliteral dynamic-import probes remain required.

The test fake contract is:

- `run_bounded_transaction(transaction, callback)` calls the callback only
  inside a begun transaction, retries only `FakeTransactionAborted` from a
  fresh snapshot up to `transaction._max_attempts`, rolls back/cleans each
  failed attempt, and propagates any non-abort commit error unchanged;
- the marker sheet holds ordered row objects and provider metadata separately
  from coordinates, allocates positive metadata IDs, reindexes markers when
  rows insert/move/sort, survives `restart()`, deletes metadata with a deleted
  row, and returns all duplicate matches in deterministic coordinate/ID order;
- the fake never imports or calls a Sheets client.

- [x] **Step 2: Run the exact RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location \
  tests.test_row_authority_contracts -v
```

Expected: only the missing A1 harness behaviors fail. Containment and all 23
retained A0 tests pass; a marker-module, B1 fake, or unrelated failure is an
invalid RED.

- [x] **Step 3: Implement only the test harness**

Append the transaction executor and marker-aware sheet to
`tests/row_authority_fakes.py`. Keep `BoundedFakeFirestore` and retained B1
fakes byte-for-byte unchanged outside additive A1 helpers. Do not create
`row_metadata.py` yet.

The marker model must make it impossible to confuse identity with coordinate:
each row object owns an internal token; marker metadata attaches to that token;
all coordinate operations derive their response dictionaries from the token's
current index.

- [x] **Step 4: Run the complete Task 0 GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowAuthorityA1HarnessTests \
  tests.test_row_authority_identity_location.RowAuthorityA1ContainmentTests \
  tests.test_row_authority_contracts.BoundedRowAuthorityFakeTests -v
```

Expected: all selected harness, containment, and retained A0 fake tests pass.
No red test is committed at the Task 0 boundary.

- [x] **Step 5: Commit Task 0**

```bash
git add tests/row_authority_fakes.py \
  tests/test_row_authority_contracts.py \
  tests/test_row_authority_identity_location.py
git diff --cached --check
git commit -m "test: add B2-A1 marker transaction harness"
```

### Task 1: Add exact pure DeveloperMetadata dictionaries and parser

**Files:**

- Create: `email_automation/row_metadata.py`
- Modify: `tests/test_row_authority_identity_location.py`
- Modify: `tests/test_row_authority_contracts.py`

- [x] **Step 1: Write complete failing marker contract tests**

Add `RowMetadataContractTests` covering:

```text
test_row_metadata_module_exists
test_b2_modules_have_no_provider_or_runtime_imports
test_create_request_has_exact_official_row_shape
test_search_request_has_exact_key_value_visibility_and_row_lookup
test_direct_metadata_parser_returns_exact_observation
test_parser_accepts_exact_positive_ids_and_zero_index
test_parser_rejects_unknown_missing_mistyped_and_boolean_fields
test_parser_rejects_wrong_key_value_visibility_location_type_or_dimension
test_parser_rejects_unbounded_multirow_reversed_or_wrong_sheet_ranges
test_parser_rejects_non_uuid4_marker_values
test_search_parser_distinguishes_successful_empty_from_lookup_failure
test_search_parser_validates_every_wrapper_and_echoed_filter
test_search_parser_returns_two_and_128_matches_in_canonical_order
test_search_parser_rejects_129_matches_before_authority
test_fake_create_response_round_trips_through_parser
test_moved_sorted_and_restarted_marker_parses_at_new_coordinate
test_deleted_marker_search_returns_an_explicit_empty_result
test_duplicate_matches_remain_distinct_and_deterministically_ordered
```

The A1 test module must remain importable while `row_metadata.py` is absent: do
not import that module at file scope. Resolve it lazily inside marker-contract
helpers after the existence assertion so the complete Task 1 RED executes
instead of failing during test discovery.

Exact public API:

```python
MARKER_KEY = "sitesift_row_id_v1"
MARKER_VISIBILITY = "DOCUMENT"
ROW_LOCATION_TYPE = "ROW"
ROW_DIMENSION = "ROWS"

build_row_marker_create_request(*, row_id, sheet_id, provider_row_index)
build_row_marker_search_request(*, row_id)
parse_row_developer_metadata(metadata)
parse_row_marker_search_response(response, *, expected_row_id)
```

`build_row_marker_create_request` returns exactly the single request object
inside a Sheets `batchUpdate.requests[]` array. It omits `metadataId` so Sheets
assigns it and omits read-only `locationType`. `build_row_marker_search_request`
returns exactly the `developerMetadata.search` request body with one filter.

`parse_row_developer_metadata` accepts the direct five-key DeveloperMetadata
object returned by create/get/search unwrapping and returns exactly:

```python
{
    "rowId": "sr1_...",
    "sheetId": 0,
    "providerRowIndex": 0,
    "displayRowNumber": 1,
    "metadataId": 1,
}
```

It requires an exact positive `metadataId`, exact `locationType: ROW`, exact
single-row `dimensionRange`, exact key/value/visibility, and exact integer
types. Unknown fields fail closed. Booleans never satisfy integers.

`parse_row_marker_search_response` accepts `{}` or an exact explicit empty
match list as successful zero matches because protobuf JSON may omit an empty
repeated field. A nonempty response must contain only
`matchedDeveloperMetadata`; every wrapper has exactly `developerMetadata` and
`dataFilters`, and the echoed filters must equal the one frozen lookup. Any
malformed or irrelevant returned match fails the whole response rather than
being filtered into a false deletion. The result is a defensive tuple sorted
by `(providerRowIndex, metadataId, rowId)` and is bounded to 128.

- [x] **Step 2: Run marker RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowMetadataContractTests -v
```

Expected: failures identify the missing module/functions only.

- [x] **Step 3: Implement the minimum pure module**

Create `row_metadata.py` with no network-capable object, service builder,
credential handling, execution method, or logging. Reuse
`row_authority.validate_row_id`; do not duplicate the regex or UUID contract.
Return fresh dictionaries and never retain caller dictionaries.

- [x] **Step 4: Run marker and containment GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowMetadataContractTests \
  tests.test_row_authority_identity_location.RowAuthorityA1ContainmentTests \
  tests.test_row_authority_contracts -v
```

Expected: all selected tests pass and the only application import of
`row_authority` is `row_metadata`; no application module imports
`row_metadata`.

- [x] **Step 5: Commit Task 1**

```bash
git add email_automation/row_metadata.py \
  tests/test_row_authority_contracts.py \
  tests/test_row_authority_identity_location.py
git diff --cached --check
git commit -m "feat: add pure row metadata contracts"
```

### Task 2: Add canonical A1 hashes and exact document schemas

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_identity_location.py`

- [x] **Step 1: Write failing normalization/hash tests**

Add `RowIdentityHashContractTests` for:

- exact opaque input: already NFC, 1–512 UTF-8 bytes, no control characters,
  no trimming/case transformation;
- timestamp: exact `YYYY-MM-DDTHH:MM:SS.ffffffZ`, valid calendar/time, UTC only;
- provider-rendered header/cell normalization: NFC and CRLF/CR to LF while
  preserving every other character, whitespace, and case;
- maximum 256 headers and 256 cells, each at most 8,192 UTF-8 bytes;
- exact integer types and JSON-safe bounds; booleans and integer subclasses
  fail;
- complete frozen vectors for `markerHash`, `identityHash`, `headerHash`,
  `rowSnapshotHash`, `observationEvidenceHash`, `revisionHash`, and the initial
  `headHash`, reproduced by an independent reference hasher;
- every field drift, explicit-null drift, scope drift, order drift, and domain
  substitution changes the digest.

Freeze these exact public signatures and return contracts. Every `dict` return
is a fresh exact-key defensive dictionary; every validator returns a fresh
validated copy and never the caller's object.

```python
def normalize_provider_text(*, value, field_name) -> str: ...

def marker_hash(
    *, row_id, spreadsheet_id, sheet_id, user_scope_hash
) -> str: ...

def header_hash(*, ordered_headers, user_scope_hash) -> str: ...

def row_snapshot_hash(
    *, spreadsheet_id, sheet_id, ordered_headers,
    ordered_cell_values, user_scope_hash
) -> str: ...

def build_row_observation(
    *, spreadsheet_id, marker_observation, ordered_headers,
    ordered_cell_values, user_scope_hash
) -> dict: ...

def observation_evidence_hash(
    *, lifecycle, observations, user_scope_hash
) -> str: ...

def build_row_identity_document(
    *, user_scope_hash, row_id, client_id, spreadsheet_id, sheet_id,
    creation_kind, creation_source_hash, created_at
) -> dict: ...

def validate_row_identity_document(*, document) -> dict: ...

def build_row_location_revision_document(
    *, identity_document, revision, lifecycle, observations,
    previous_revision_hash, observed_at
) -> dict: ...

def validate_row_location_revision_document(
    *, document, identity_document
) -> dict: ...

def build_initial_row_authority_head(
    *, identity_document, location_revision_document, created_at
) -> dict: ...

def build_location_advanced_head(
    *, expected_head, location_revision_document
) -> dict: ...

def validate_row_authority_head(*, document) -> dict: ...
```

`build_row_observation` consumes exactly the parsed five-key marker dictionary
from Task 1 and returns exactly the five-key canonical evidence object from the
design. It derives `displayRowNumber`, marker hash, header hash, and snapshot
hash; callers cannot supply those derived values separately. The location
builder derives user scope, row ID, spreadsheet/sheet, and marker hash from the
validated identity and derives geometry from the observation cardinality. The
head builders derive row/scope/location values from validated documents;
callers cannot override them. Initial `created_at` must equal identity
`createdAt` and revision `observedAt`; advanced `updatedAt` is derived from the
new revision's `observedAt` while prior `createdAt` is preserved.

- [x] **Step 2: Write complete failing exact-schema tests**

Add `RowIdentityDocumentSchemaTests` that prove:

- identity, location revision, and head contain exactly the normative keys;
- row ID equals marker value and all marker/identity/revision/head hashes
  recompute;
- active/nonviable has non-null coordinates/snapshot; deleted/ambiguous has
  null coordinates/snapshot;
- revision 1 has null previous hash; later revisions require a complete hash;
- the initial head is revision/state 1, `clear`, backlog 0, and has every
  owner/lease/fence/settlement/link field explicitly null;
- claimed/review-pending/settled head shapes are validated only to the approved
  correlated-null registry and are preserved by location updates;
- missing, unknown, mistyped, over-bound, invalid enum, bad ID, bad hash,
  timestamp drift, and boolean-as-integer inputs all fail;
- each builder derives the fields identified above and rejects an inconsistent
  identity, revision, observation, or timestamp instead of accepting an
  override.

- [x] **Step 3: Run the combined Task 2 RED before implementation**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowIdentityHashContractTests \
  tests.test_row_authority_identity_location.RowIdentityDocumentSchemaTests \
  -v
```

Expected: only the missing A1 helpers fail; both test classes load and execute,
and all retained A0 canonical vectors remain green. Do not implement one class
before the other class's RED is observed.

- [x] **Step 4: Implement primitive validation, fixed domains, and schemas**

Use `domain_hash` and flat payloads only. Never put a `payload` object below
the canonical envelope. Hash inputs are exactly the approved design table:

- `sitesift.row.marker.v1`: row ID, marker key/value, visibility, spreadsheet
  ID, sheet ID;
- `sitesift.row.identity.v1`: row ID, client ID, spreadsheet ID, sheet ID,
  marker hash, creation kind, creation source hash;
- `sitesift.row.header.v1`: `orderedHeaders` containing the ordered normalized
  header strings;
- `sitesift.row.snapshot.v1`: spreadsheet ID, sheet ID, header hash, ordered
  normalized cell strings under `orderedCellValues`;
- `sitesift.row.observation_evidence.v1`: `observationKind` and `observations`
  containing the ordered exact observation objects;
- `sitesift.row.location.v1`: row ID, revision, nullable provider/display
  indexes, nullable metadata ID, nullable row-snapshot hash, marker hash,
  lifecycle, observation-evidence hash, nullable previous-revision hash, and
  observed time. The document's repeated spreadsheet/sheet fields are
  validated against identity but are not repeated in this hash payload;
- `sitesift.row.authority_head.v1`: every approved head field except
  `headHash`.

Evidence observations sort by
`[providerRowIndex, metadataId, rowSnapshotHash]`, with null before integers.
Active/nonviable requires exactly one fully non-null object; deleted requires
zero; ambiguous requires 2–128 fully non-null objects. Do not deduplicate a
duplicate observation silently.

Builders return new exact dictionaries. Validators return defensive exact
copies only after recomputing every hash and correlation. They never mutate or
retain caller data. Catch `RecursionError`, encoding errors, and calendar parse
errors and raise `RowAuthorityConfigError`.

- [x] **Step 5: Make all Task 2 tests GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowIdentityHashContractTests \
  tests.test_row_authority_identity_location.RowIdentityDocumentSchemaTests \
  tests.test_row_authority_contracts -v
```

Expected: all selected tests pass and every A0 frozen vector is unchanged.

- [x] **Step 6: Commit Task 2**

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_identity_location.py
git diff --cached --check
git commit -m "feat: add row identity location schemas"
```

### Task 3: Initialize identity, first revision, and clear head atomically

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_identity_location.py`

- [x] **Step 1: Write complete failing initialization tests**

Add `RowIdentityInitializationTests`:

```text
test_initialization_creates_exact_three_documents_atomically
test_initialization_supports_active_and_nonviable_only
test_exact_initialization_retry_is_zero_write_noop
test_partial_existing_state_is_ambiguous_with_zero_writes
test_any_existing_document_drift_is_ambiguous_with_zero_writes
test_invalid_marker_snapshot_timestamp_and_scope_fail_before_transaction
test_preapply_commit_failure_has_zero_writes_and_is_retryable
test_apply_then_raise_succeeds_only_after_exact_three_document_readback
test_apply_then_raise_partial_or_drifted_readback_is_ambiguous
test_initialization_never_stores_raw_verified_user_or_mailbox_material
test_initialization_calculates_three_writes_below_the_bound
```

The public coordinator API is:

```python
class RowAuthorityStore:
    def __init__(self, firestore, *, transaction_executor) -> None: ...

    def initialize_row_identity(
        self,
        *,
        verified_user_id,
        client_id,
        spreadsheet_id,
        marker_observation,
        headers,
        cells,
        lifecycle,
        creation_kind,
        creation_source_hash,
        created_at,
    ) -> dict: ...
```

`marker_observation` is exactly the defensive dictionary returned by
`row_metadata.parse_row_developer_metadata`. Its row ID is already random and
provider-observed. The method returns exact defensive copies of identity,
revision 1, and initial head documents in exactly:

```python
{
    "disposition": "created",  # or exactly "existing"
    "identity": {...},
    "locationRevision": {...},
    "authorityHead": {...},
}
```

`created` means this invocation's commit or exact apply-then-raise readback
created all three documents. `existing` means all three exact documents were
already present before the transaction and no write was buffered. No other
return key or disposition is permitted.

- [x] **Step 2: Run initialization RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowIdentityInitializationTests -v
```

- [x] **Step 3: Implement reference construction and prepare phase**

References are exactly:

```text
users/{verified_user_id}/rowIdentities/{rowId}
users/{verified_user_id}/rowLocationRevisions/{rowId}--1
users/{verified_user_id}/rowAuthorityHeads/{rowId}
```

Validate all inputs and the planned write count before asking the client for a
transaction. Inside the callback, read all three references before buffering
any write. All absent creates exact documents; all exact returns without
writes; every other combination raises `RowAuthorityAmbiguous` with zero
writes.

- [x] **Step 4: Implement commit-outcome readback**

If the required transaction dependency cannot start, raise
`RowAuthorityRetryable`. If a prepared commit raises:

1. read all three references outside the failed transaction;
2. exact expected readback is success;
3. exact before-state readback is `RowAuthorityRetryable`;
4. unreadable, partial, or drifted readback is `RowAuthorityAmbiguous`.

Never treat one or two matching records as success. Never retry a non-abort
inside the generic executor.

- [x] **Step 5: Run initialization and retained fake GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowIdentityInitializationTests \
  tests.test_row_authority_identity_location.RowAuthorityA1HarnessTests \
  tests.test_row_authority_contracts -v
```

- [x] **Step 6: Commit Task 3**

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_identity_location.py
git diff --cached --check
git commit -m "feat: initialize stable row identities"
```

### Task 4: Append location revisions with exact head CAS

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_identity_location.py`

- [x] **Step 1: Write failing move/sort/nonviable tests**

Add `RowLocationRevisionTests` proving insert/move/sort/restart preserves the
row ID while appending revisions, provider/display indexes advance together,
metadata ID remains an observation, nonviable retains one exact snapshot, and
each successful change increments location revision by one and state revision
by one without assuming those counters are equal. A restart or repeat that
supplies the same lifecycle, geometry, metadata ID, marker/snapshot hash, and
observation-evidence hash is a zero-write no-op even when `observed_at` is
newer.

Public API:

```python
class RowAuthorityStore:
    def advance_row_location(
        self,
        *,
        verified_user_id,
        row_id,
        expected_head,
        observations,
        lifecycle,
        observed_at,
    ) -> dict: ...
```

`observations` are exact canonical evidence objects. For active/nonviable it
contains one object. For deleted it is empty. For ambiguous it contains 2–128
objects. Snapshot hashing happens before this call through the Task 2 helpers;
the transaction never reads a provider.

The method returns fresh exact documents in exactly:

```python
{
    "disposition": "advanced",  # or "unchanged" or "already_applied"
    "identity": {...},
    "locationRevision": {...},
    "authorityHead": {...},
}
```

`advanced` means this invocation's commit or exact apply-then-raise readback
created the next revision and advanced the head. `unchanged` means the proposed
semantic observation already equals the current immutable revision and no
candidate was allocated. `already_applied` means the caller supplied the prior
head but the exact deterministic candidate revision/head were already durable.
No other return key or disposition is permitted.

- [x] **Step 2: Write failing deletion, ambiguity, and coordinate-reuse tests**

Cover:

```text
test_delete_appends_tombstone_with_null_location_and_snapshot
test_deleted_row_id_can_never_be_reactivated
test_coordinate_reuse_initializes_a_different_random_row_id
test_duplicate_markers_append_ambiguous_revision_without_election
test_ambiguous_revision_has_null_location_and_commits_all_evidence
test_sheet_or_spreadsheet_drift_cannot_move_an_identity_across_grids
```

An old deleted/ambiguous identity is never repurposed to authorize a new
coordinate. A different spreadsheet or numeric sheet ID requires another row
ID and later migration review/link work; A1 writes no migration object.

- [x] **Step 3: Write failing retry, race, and readback tests**

Cover:

```text
test_exact_location_retry_is_zero_write_noop
test_stale_expected_head_writes_nothing
test_existing_candidate_revision_drift_writes_nothing
test_two_identical_workers_create_one_revision_and_both_succeed
test_two_different_observations_from_one_head_yield_one_conflict
test_transaction_abort_retries_from_fresh_reads
test_preapply_failure_is_retryable_with_zero_writes
test_apply_then_raise_requires_exact_revision_and_head_readback
test_partial_apply_then_raise_readback_is_ambiguous
test_location_change_is_exactly_two_planned_writes
test_location_change_preserves_all_nonlocation_head_fields
```

The test with a claimed or settled synthetic head is schema-only: it proves
location CAS preserves valid ownership fields, but A1 does not create any
claim, generation, or settlement document.

- [x] **Step 4: Run the complete Task 4 RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_identity_location.RowLocationRevisionTests -v
```

- [x] **Step 5: Implement exact revision/head CAS**

Before writes, validate the full caller-supplied `expected_head`, then read:

1. immutable identity;
2. actual head;
3. immutable location revision named by `expected_head`;
4. candidate next-revision reference.

Apply this precedence exactly:

1. A deleted expected head conflicts, including when the old marker is later
   observed or duplicated.
2. If actual equals the deterministically built result head and candidate
   equals the exact candidate revision, return `already_applied` with no write.
3. Otherwise actual must equal expected. Any present candidate at this point is
   an orphan/drift conflict with zero writes.
4. With actual equal to expected and candidate absent, compare the proposed
   lifecycle, geometry, metadata ID, marker/snapshot hash, and
   observation-evidence hash to the validated current immutable revision. If
   equal, return `unchanged` without considering timestamp drift.
5. Only when those semantic fields differ may the transaction create the next
   revision, replace the full exact head, and return `advanced`.

Malformed reads are ambiguous; valid CAS mismatch or immutable drift is
conflict; both write zero.

After a prepared commit exception, read identity, previous revision, candidate
revision, and head. Accept only exact before or exact after sets using the same
retryable/ambiguous distinction as initialization. Do not infer success from a
matching head hash alone.

- [x] **Step 6: Run complete A1 and retained A0 GREEN**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover \
  -s tests -p 'test_row_authority*.py' -v
```

Expected: every A0 and A1 test passes. Record the actual test count and
duration; do not copy a planned count into evidence.

- [x] **Step 7: Commit Task 4**

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_identity_location.py
git diff --cached --check
git commit -m "feat: add row location revision CAS"
```

### Task 5: Review, verify, publish B2-A, and freeze evidence

**Files:**

- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
- Modify:
  `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md`
- Create:
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a1.md`

- [x] **Step 1: Run the complete offline local gate**

```bash
B2_A1_PY=../codex-release-a-medium-recovery-20260714/.venv/bin/python
B2_A1_OFFLINE_ENV=(
  OPENAI_API_KEY=
  FIRESTORE_EMULATOR_HOST=127.0.0.1:9
  HTTP_PROXY=http://127.0.0.1:9
  HTTPS_PROXY=http://127.0.0.1:9
  ALL_PROXY=http://127.0.0.1:9
  NO_PROXY=127.0.0.1,localhost
  http_proxy=http://127.0.0.1:9
  https_proxy=http://127.0.0.1:9
  all_proxy=http://127.0.0.1:9
  no_proxy=127.0.0.1,localhost
  SITESIFT_SOURCE_COORDINATOR_MODE=disabled
)

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A1_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A1_PY" -m unittest discover \
  -s tests -p 'test_row_authority*.py' -v

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A1_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A1_PY" -m unittest \
  auth_service.test_auth_service_isolation \
  tests.test_jill_live_campaign_regressions \
  tests.test_full_campaign_e2e -v

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A1_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A1_PY" -m unittest \
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

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A1_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=live "$B2_A1_PY" -m unittest \
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

"$B2_A1_PY" -m py_compile \
  email_automation/row_authority.py email_automation/row_metadata.py \
  tests/row_authority_fakes.py \
  tests/test_row_authority_contracts.py \
  tests/test_row_authority_identity_location.py
"$B2_A1_PY" -m pip check
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/production-clearance-ci.yml"); puts "ok"'
git diff --check 2b5e785
```

Expected: all commands exit 0. The release/auth baseline remains 95, complete
B1 remains 606, retained M2 remains 669, B2 is the actual newly measured count,
and static checks are clean.

- [x] **Step 2: Obtain two fresh independent full-diff approvals**

One reviewer checks exact B2 design/A1-plan compliance. A different reviewer
checks correctness, security/privacy, resource bounds, transaction/readback
semantics, race discrimination, fake fidelity, provider/runtime containment,
and maintainability. Critical/Important findings block publication and require
a failing test, focused/full rerun, new commit, and re-review.

- [x] **Step 3: Create exact evidence and pre-publication commit**

Create the evidence file with exactly:

```markdown
# Stable Row Authority B2-A1 Evidence

## Candidate and scope
## Commits
## DeveloperMetadata contract proof
## Identity and location hash proof
## Transaction and lifecycle proof
## Local verification
## Static containment
## Independent reviews
## GitHub exact-SHA run
## Production posture and next milestone
```

Record full SHAs, exact changed-file inventory, frozen vectors, counts,
durations, retry/race/readback results, and approvals. Before push, the GitHub
section says `pending exact-SHA publication`. Production posture must state:

`B2-A1 adds provider-free, runtime-unwired identity/location authority only;
production remains NO-GO.`

Mark only completed child-plan steps. Do not mark roadmap `B2-A1 is green and
published` until remote evidence is read back.

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a1.md
git diff --cached --check
git commit -m "docs: freeze B2-A1 local evidence"
```

- [x] **Step 4: Push and verify the exact B2-A candidate**

Use the roadmap's exact publication protocol. Select CI only with the exact
local head SHA, wait with `--exit-status`, prove remote/local/head SHA equality,
prove `conclusion: success`, and extract run/job URLs plus all four test
count/duration lines. Do not open a PR, merge, or deploy.

- [x] **Step 5: Freeze remote evidence and reverify the final evidence SHA**

Replace the pending phrase with the exact candidate SHA, run/job URLs, test
counts/durations, and compile/diff result. Mark roadmap `B2-A1 is green and
published` complete, then:

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a1.md
git diff --cached --check
git commit -m "docs: freeze B2-A1 remote evidence"
git push origin codex/sitesift-production-clearance-20260804
```

Run a second exact-SHA GitHub verification for this evidence commit. Finish
only when local HEAD, remote branch, workflow `headSha`, and successful
conclusion all match and the worktree is clean.

- [x] **Step 6: Stop at the B2-B boundary**

Update the working plan to make the B2-B ownership child plan the next active
milestone. Do not implement bindings, claims, generations, settlements,
contact compliance, runtime adoption, deployment, or production activity as
part of B2-A1.
