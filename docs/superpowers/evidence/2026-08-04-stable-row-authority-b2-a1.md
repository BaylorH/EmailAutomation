# Stable Row Authority B2-A1 Evidence

## Candidate and scope

- Approved-plan baseline:
  `cdd2bdc087e46dd15233697f7a6a71bb9ced58dd`.
- Independently reviewed code candidate:
  `4c35f941b762975c589ead4a117e98ae79470b5b`.
- Deliverable: both provider-free B2-A1 code and clearance evidence.
- The implementation inventory from the approved-plan baseline is exactly:
  - `email_automation/row_authority.py`
  - `email_automation/row_metadata.py`
  - `tests/row_authority_fakes.py`
  - `tests/test_row_authority_contracts.py`
  - `tests/test_row_authority_identity_location.py`
- The pre-publication evidence commit additionally changes only this evidence
  file, the B2 roadmap, and the B2-A1 child plan.
- Scope contains pure Google Sheets DeveloperMetadata request/response
  contracts, immutable row identity/location schemas and hashes, bounded
  Firestore transaction fakes, atomic initialization, and exact location/head
  CAS. It contains no provider client, production data read/write, runtime
  adopter, deployment, campaign, frontend, rules change, binding, owner,
  generation, settlement, contact-compliance decision, or outbound action.

## Commits

- `162175a1eda18913087a50f72f9dc342780b8541` — `test: add B2-A1 marker transaction harness`
- `eecdbf109012ade4ad9307862e531e7b0dc3085d` — `feat: add pure row metadata contracts`
- `6ee2c2749c19e70191b0a868d2ad92ef64e7cc1f` — `feat: add row identity location schemas`
- `42c4cd9b33ed6d1cb864b07d3d4a0a2ba32a065e` — `feat: initialize stable row identities`
- `9417d560f70a77d25d2bbd2619f9c4fb9d96c69a` — `fix: classify row initialization retries by phase`
- `a9e474f17343beebf1a42164adeea767248f7e66` — `feat: add row location revision CAS`
- `4c35f941b762975c589ead4a117e98ae79470b5b` — `fix: validate row authority document paths`

## DeveloperMetadata contract proof

- The create contract emits one exact `createDeveloperMetadata` request with
  key `sitesift_row_id_v1`, the canonical row ID as value, `DOCUMENT`
  visibility, and a `ROWS` dimension range covering exactly
  `[providerRowIndex, providerRowIndex + 1)` on one numeric sheet ID.
- The search contract emits one exact `dataFilters` entry whose
  `developerMetadataLookup` fixes the key, value, `DOCUMENT` visibility, and
  `ROW` location type. These shapes match the official Google Sheets
  [developer metadata guide](https://developers.google.com/workspace/sheets/api/guides/metadata),
  [DeveloperMetadata schema](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata),
  and [DataFilter schema](https://developers.google.com/workspace/sheets/api/reference/rest/v4/DataFilter).
- Direct and search parsers require exact wrappers, fields, types, echoed
  filters, one-row ranges, UUIDv4 row IDs, and positive metadata IDs. Successful
  empty search is distinct from malformed/failed lookup; matches are bounded at
  128 and returned in deterministic order without electing among duplicates.
- The provider-shaped fake proves the marker follows its physical row through
  unrelated insert, move, sort, and restart; deletion removes it, and duplicate
  locations remain explicit evidence.

## Identity and location hash proof

- All A1 hashes are independently recomputed from canonical JSON and frozen to
  these exact vectors:
  - marker: `5dae5d60c0db2f02e951c7f38e6b71f37171ad3fbe8ad9110976a42359d6447d`
  - identity: `5b110eaa888cd16e17aabb34192b8c6f731cac7303f36545ba4879ee41b0349b`
  - header: `633fa62b41647ee95e2338fde9b5b1152ac5a1af5dfec4aa315d98faeec73f75`
  - row snapshot: `de7bd24bec9be791fc9961c10ce246fb06e8585d3ae8555c194c174dbc763882`
  - observation evidence: `2c0683283b3360378a1ba9e6db70f8a3b8631e88b090d88b97bb845b98bf27e4`
  - revision: `c6f3b86e8a86ab03845aa985b334f1b22030c6d87f40abdae943a432595c04a9`
  - head: `7d4fcae9b8b2cacb78081e96cbf455e6b3e02f036e99d7adeaff6c326b3db0c7`
- Domain, scope, field, order, and null drift change the correct digest. Exact
  schemas reject missing, unknown, mistyped, malformed, and noncanonical data.
- Provider text normalization is bounded, NFC, line-ending stable, and
  defensive. A1 never accepts or persists raw mailbox input; stored documents
  contain only the one-way user-scope hash rather than the raw verified user
  ID.

## Transaction and lifecycle proof

- Initialization validates all inputs before Firestore activity, reads the
  identity, immutable revision 1, and authority head before writes, then
  creates exactly those three documents atomically. Exact retry is a zero-write
  `existing` result; partial, malformed, or drifted state is ambiguous.
- Location CAS validates the caller-frozen head and reads identity, actual
  head, expected immutable revision, and candidate revision before writes. A
  semantic change performs exactly two operations: create the next immutable
  revision and fully replace the head. Semantic equality ignoring timestamp is
  a zero-write `unchanged` result.
- Exact old-head replay after a durable change is zero-write
  `already_applied`. Identical concurrent workers yield one `advanced` and one
  `already_applied`; different observations yield one `advanced` and one
  conflict. Stale Firestore snapshots retry from fresh reads.
- Pre-apply failure is retryable only after exact before-state readback.
  Apply-then-raise succeeds only after exact identity, prior revision,
  candidate, and head readback. Candidate-only, head-only, immutable drift,
  unreadable, or mixed readback is ambiguous.
- Active, nonviable, deleted, and ambiguous lifecycles preserve immutable
  identity. Deleted is terminal and has first precedence even with malformed
  candidate drift. Duplicate observations append null-geometry ambiguous
  evidence without electing a coordinate. Cross-grid active/nonviable evidence
  conflicts. Coordinate reuse requires a different random row ID.
- State and location revisions advance independently (the adversarial fixture
  proves `7 -> 8` and `1 -> 2`). Timestamp lineage requires identity creation
  to match head creation and
  `identity.createdAt <= currentRevision.observedAt <= head.updatedAt`, while
  allowing later nonlocation head updates.

## Local verification

All test commands used the plan-pinned Python environment. Provider egress was
blackholed, `GOOGLE_APPLICATION_CREDENTIALS` was unset, source coordination was
disabled, and outbound mode matched the gate definition. No external message
or campaign was sent.

- Complete B2 discovery: 100/100 passed in 2.632 seconds.
- Release/auth baseline: 95/95 passed in 0.537 seconds.
- Complete B1 source-authority gate: 606/606 passed in 23.776 seconds.
- Retained M2 gate: 669/669 passed in 22.192 seconds.
- Task 4 focused gate after review fixes: 24/24 passed in 0.043 seconds.
- Initialization/location gate after path hardening: 37/37 passed in 0.046
  seconds.
- `py_compile` for both A1 modules and all three B2 test/harness modules exited
  0.
- `pip check` exited 0 with `No broken requirements found.`
- GitHub Actions YAML parsed and printed `ok`.
- `git diff --check 2b5e785` exited 0 with no output.
- Caffeination remained active through the gate via
  `/usr/bin/caffeinate -dims` (PID 95257).

## Static containment

- `email_automation/row_authority.py` imports only Python standard-library
  modules. `email_automation/row_metadata.py` is the sole allowed application
  importer and contains only pure dictionaries/parsers plus row-ID validation.
- No runtime module imports either A1 module, so the candidate cannot alter
  production behavior.
- The A1 tree has no Google/provider SDK import, network call, credential read,
  runtime flag, campaign path, mailbox input, deployment change, or production
  datastore invocation.
- Both public store APIs validate the verified user ID as one safe Firestore
  document segment before hashing or reference construction. Slash, dot paths,
  reserved IDs, controls, and invalid UTF-8 fail before Firestore activity.
- The B2-only fake layers on the retained B1 fake. It preserves fresh-snapshot
  retries and unknown-commit behavior and refuses writes above the 400-write
  ceiling before any state, event barrier, version, or logical clock changes.

## Independent reviews

- Fresh full-diff spec-compliance review at
  `4c35f941b762975c589ead4a117e98ae79470b5b`: `APPROVED`. No Critical or
  Important finding remained; the reviewer independently reran 100/100 B2
  tests in 2.412 seconds and obtained a clean exact-range diff check.
- Different fresh full-diff correctness/security review at
  `4c35f941b762975c589ead4a117e98ae79470b5b`: `APPROVED`. Its initial
  Important path-segmentation finding produced failing initialization/location
  tests and commit `4c35f941b762975c589ead4a117e98ae79470b5b`; re-review confirmed the fix and
  reported no remaining Critical or Important finding.

## GitHub exact-SHA run

- Branch: `codex/sitesift-production-clearance-20260804`.
- Status: `pending exact-SHA publication`.

## Production posture and next milestone

B2-A1 adds provider-free, runtime-unwired identity/location authority only;
production remains NO-GO.

The next milestone is B2-B ownership/generation authority. No PR, merge,
deployment, production campaign, frontend action, or external communication is
part of B2-A1. Production clearance still requires B2-B, B2-C, B2-D, and the
B3/B4 frontend/runtime acceptance gates.
