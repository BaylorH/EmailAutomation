# Stable Row Authority B2-A0 Evidence

## Candidate and scope

- Approved-plan baseline: `be092ede8cc44d69d3fb9729456481d26c032645`.
- Independently reviewed code candidate: `c05e4521920629ccafd3fb65849b4c363d3066d3`.
- Published candidate: `61592023a53a9ba0f7d3822e477ea540186e68b4`.
- Deliverable: both provider-free B2-A0 code and clearance evidence.
- Changed implementation inventory is exactly:
  - `.github/workflows/production-clearance-ci.yml`
  - `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md`
  - `email_automation/row_authority.py`
  - `tests/row_authority_fakes.py`
  - `tests/test_row_authority_contracts.py`
  - `tests/test_source_coordinator_inventory.py`
- Scope contains canonical JSON, domain/user/contact hashing, UUIDv4 row IDs,
  bounded B2-only fakes, static-containment tests, and permanent B2 CI
  discovery. It contains no datastore authority record, provider/client import,
  runtime adopter, production read/write, deploy, campaign, frontend, or rules
  change.

## Commits

- `be092ede8cc44d69d3fb9729456481d26c032645` — `docs: split B2 into executable milestones`
- `28fa7391de00aa7f9c389e07f770c0688f517b0b` — `test: isolate B2 authority write bounds`
- `1296ec23c77ee82bc63d613f2f1a2654be8198bf` — `test: harden B2 write ceiling discrimination`
- `cdeab09e087733786f3a3cae41e04514d59d9b2a` — `feat: add canonical row authority primitives`
- `901816d55fcc0387eb24aae298e628d6f063f1f0` — `test: distinguish shared canonical containers`
- `8ab98f0f65ea1fdb7c101d4f74ecb5cf694bce8a` — `fix: bound canonical row authority inputs`
- `8f206ba144a81bc29c40601a22143dd5792f7079` — `feat: add contact identity primitives`
- `c05e4521920629ccafd3fb65849b4c363d3066d3` — `fix: make mailbox normalization idempotent`
- `61592023a53a9ba0f7d3822e477ea540186e68b4` — `docs: freeze B2-A0 local evidence`

## Canonical-domain and identity proof

- Canonical JSON accepts only exact JSON-safe primitive/container types,
  rejects floats, unsafe integers, invalid UTF-8, non-string keys, cycles, and
  unsupported subclasses, while allowing shared acyclic containers.
- Canonical input is bounded to depth 64, 4,096 nodes, and 16 MiB of tracked
  UTF-8 material and final encoded output. Adversarial depth fails with the
  stable `RowAuthorityConfigError`, not a raw recursion error.
- Domain hashing uses `UTF8(domain) + NUL + canonical_json(flat_payload)` with
  `schemaVersion` and `userScopeHash` as flat reserved fields.
- Frozen user-scope vector for exact verified user ID `uid-1`:
  `48fafc848b44ae7b0414309666dcb54208b7867700240a0f343ec02c53eb0cf2`.
- Frozen synthetic flat-domain vector for domain
  `sitesift.test.payload.v1`, scope hash `a` repeated 64 times, and payload
  `{ "nullable": null, "value": 1 }`:
  `773aefc65ea24cf28562eb10df940fc5fec5a6e9e520e2b64536e0957398568d`.
- Frozen contact vector for normalized mailbox `first.last@example.com` and
  scope hash `a` repeated 64 times:
  `0929de5bfcbb44acae6c72bcafbd62c0587ee42c12aab305883a723b5639515c`.
- Mailboxes are NFC-normalized, stripped, lowercased, and NFC-normalized again;
  the exact identity preserves dots and plus suffixes, while the canonical
  identity removes only the first plus suffix. The `J` plus combining-caron
  case proves normalization is idempotent, and the exact 320-byte/321-byte
  acceptance boundary is discriminated.
- Row IDs are `sr1_` plus RFC 4122 UUIDv4 lowercase hex and are validated before
  use.

## Local verification

All commands used the plan-pinned Python environment. Provider egress was
blackholed, `GOOGLE_APPLICATION_CREDENTIALS` was unset, source coordination was
disabled, and outbound mode matched the gate definition.

- B2 discovery, `python -m unittest discover -s tests -p 'test_row_authority*.py' -v`:
  23/23 passed in 0.866 seconds.
- Release/auth gate, the three plan-listed modules: 95/95 passed in 0.387
  seconds.
- Complete B1 gate, the ten plan-listed modules: 606/606 passed in 23.996
  seconds.
- Retained M2 gate, the 23 plan-listed modules: 669/669 passed in 22.290
  seconds.
- `py_compile` for the authority module and its two focused test modules exited
  0.
- `pip check` exited 0 with `No broken requirements found.`
- GitHub Actions YAML parsed and printed `ok`.
- `git diff --check 2b5e785` exited 0 with no output.
- Caffeination remained active through the gate via `/usr/bin/caffeinate -dims`.

## Static containment

- The new authority module imports only Python standard-library modules.
- AST inventory permits `rowBindings` and `stableRowOwner` only in
  `email_automation/row_authority.py`; `executionEpoch`, `executionClaimId`, and
  `providerIntent` remain forbidden everywhere in the scanned runtime closure.
- Dynamic-import and provider/client inventory checks remain conservative and
  green.
- The B2-only fake inherits the retained B1 fake without modifying it. Exactly
  400 writes commit; 401 writes produce the refusal event before barriers,
  data, versions, or logical clock can change. Invalid ceilings, including
  integer subclasses, are rejected.
- No runtime module imports or calls the B2 primitive module, so this milestone
  cannot change production behavior.

## Independent reviews

- Fresh full-diff spec-compliance review: `APPROVED`. The reviewer confirmed the
  exact six-file scope, passed 6/6 targeted adversarial checks, obtained a clean
  diff check, and found no Critical or Important spec deviation.
- Different fresh full-diff code-quality/security review: `APPROVED`. No
  Critical or Important correctness, containment, privacy, resource-safety,
  Unicode, fake-atomicity, CI, or maintainability finding was reported.

## GitHub exact-SHA run

- Branch: `codex/sitesift-production-clearance-20260804`.
- Local and remote candidate SHA:
  `61592023a53a9ba0f7d3822e477ea540186e68b4`.
- [Production Clearance CI run 30962659515](https://github.com/BaylorH/EmailAutomation/actions/runs/30962659515)
  completed successfully for that exact head SHA.
- [offline-verification job 92169729024](https://github.com/BaylorH/EmailAutomation/actions/runs/30962659515/job/92169729024)
  completed successfully from `2026-08-05T00:14:35Z` through
  `2026-08-05T00:16:11Z`.
- Release/auth: 95/95 passed in 0.968 seconds.
- Complete B1: 606/606 passed in 31.364 seconds.
- Complete B2 discovery: 23/23 passed in 1.488 seconds.
- Retained M2: 669/669 passed in 25.198 seconds.
- Changed-Python compilation and `git diff --check 2b5e785` both completed
  successfully.

## Production posture and next milestone

B2-A0 adds provider-free primitives only; production remains NO-GO.

The next milestone is B2-A1: row identity/location authority. It requires its
own executable child plan and review gate before implementation. No PR, merge,
deploy, runtime flag, production campaign, or external communication is part of
this checkpoint.
