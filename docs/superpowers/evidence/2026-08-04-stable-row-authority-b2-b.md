# Stable Row Authority B2-B Evidence

## Candidate and scope

- Approved-plan baseline:
  `5676b26ca61ba447e759a36be43d658d1bb8a7a9`.
- Independently approved B2-B child-plan publication:
  `7344e7a5e21651d27d75683fc7e463de4dc469d2`.
- Reviewed and exact-SHA-proven B2-B code candidate:
  `24b9a5fd2b38cb039714c5f22a3fddbf41c392c3`.
- Final baseline-to-code binary diff SHA-256 reviewed by both approvers:
  `69cdc0428d1080c1ccd59f1181dc4769d35292a8a11066ed3482e48fe22bdb32`.
- Deliverable: both provider-free B2-B code and production-clearance findings.
- The exact baseline-to-code file inventory is:
  - `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-b-ownership.md`
  - `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
  - `email_automation/row_authority.py`
  - `tests/source_coordinator_fakes.py`
  - `tests/test_row_authority_contracts.py`
  - `tests/test_row_authority_ownership.py`
  - `tests/test_source_coordinator_inventory.py`
- That range contains 22,969 insertions and 941 deletions. This evidence
  publication additionally changes only this file and the one B2-B roadmap
  checkbox.

## Published implementation commits

- `4c1955fbe8de5499b0ae34e4e95fbfacd86125e2` — freeze B2-B binding contracts.
- `bea6ba9be06d4efcb9cc4c5f9af8f448e4da873d` — add immutable row bindings.
- `dc9ba03ecac69eab1c038bd5abb007730fbc7332` — add stable contact-row evidence.
- `f8640ab25bb60cf65aff7de15749c5fa2857dff0` — freeze ownership contracts.
- `979c43f2a6ba0e006636e23bf8b1ba39668eb526` — add all-or-none row claims.
- `bb006e71d5c3bf9ff93ed15f074922c78d93d708` — fence row-authority leases.
- `332aaad98d4080d36dedabd9d5147abba21d9bc8` — settle fenced row ownership.
- `ecd018a73dead1a1e2851d976d762ade8b002140` — link row outcomes to B1 evidence.
- `24b9a5fd2b38cb039714c5f22a3fddbf41c392c3` — correlate current B1 threads
  before direct-source linking while preserving split-thread contact fan-out.

The owned GitHub branch was pushed and proved at each substantive milestone.
B2 discovery grew from 184 tests at `f8640ab`, to 239 at `979c43f`, 250 at
`bb006e7`, 275 at `332aaad`, 290 at `ecd018a`, and 292 at `24b9a5f`. The
release/auth, complete B1, and retained M2 gates stayed at 95, 606, and 669
passing tests respectively throughout those exact-SHA runs.

## Persistence and API inventory

B2-B creates immutable records in `threadRowBindings`, `rowThreadBindings`,
`contactRowBindings`, `contactRowBindingEvidence`, `rowClaimSets`,
`rowOwnerGenerations`, `rowOwnerSettlements`, `rowOperatorActions`, and
`rowSourceSettlementLinks`. It creates or fully replaces mutable CAS records in
`contactRowBindingHeads` and `rowAuthorityHeads`. Existing `rowIdentities` and
row heads are read as B2-A authority; `contactOptOutHeads` is only a fail-closed
B2-C-owned existence sentinel.

The exact B1 post-settlement read set is `sourceIdentities`,
`sourceClassifications`, `sourceTransitionOwners`, `sourceWorkLedgers`, and
`sourceSettlements`. B2-B stages no mutation to any B1 collection.

New B2-B public store operations are:

- `bind_thread_rows`
- `record_contact_row_association`
- `claim_row_set`
- `take_over_expired_lease`
- `settle_owner_generation`
- `record_operator_decline`
- `link_b1_source_settlement`

Private transaction-composable planners cover contact association,
authenticated-operator/contact-fan-out claims, settlement, and decline without
opening nested transactions. Pure builders and validators freeze thread/contact
bindings, B1 links, operator actions, claim sets, generations, settlements, and
source-settlement links.

## TDD and transaction proof

- Binding and contact-association REDs discriminated missing pure schemas,
  immutable reverse edges, deterministic read ordering, exact 3/1/0 write
  dispositions, B2-C suppression, race replay, and executor readback before
  their minimum store paths were added.
- Ownership-contract REDs froze exact domains, schemas, correlated nulls,
  derived priority, canonical ordering, timestamp lineage, and the 400-write
  ceiling before claim code existed.
- Claim REDs covered exact B1 derivation, immutable thread resolution,
  first-commit equal-priority arbitration, all-or-none multi-row behavior,
  128-row/385-write bounds, dominated predecessor settlements, replay, and
  apply-then-raise. They turned green at `979c43f`.
- Lease REDs covered expiry, epoch/fence advancement, stale fences, later-head
  replay, and strict failure readback. They turned green at `bb006e7`.
- Settlement and operator-decline REDs covered outcome derivation, exact fence,
  provider-free evidence, 257/386-write formulas, mixed-state refusal, replay,
  and atomic action/claim/settlement/head changes. They turned green at
  `332aaad`.
- The original Task 7 focused RED ran 40 tests and failed all 15 source-link
  cases because the method was absent. The first implementation reached 40/40
  green, then review-generated REDs exposed and fixed same-generation unrelated
  head acceptance, missing claim/generation timestamp and priority bounds,
  duplicate query appearance before executor readback, and insufficient pointer
  direction/result proof.
- The Task 8 full-diff review reproduced a coherently rehashed B1 identity whose
  current thread resolved a different row set. The 41-test focused gate failed
  in exactly two places: the required binding read was absent and the unsafe
  link was accepted. The minimum transaction read/correlation fix made 41/41
  pass.
- Independent review then exposed that an unconditional row-set equality check
  incorrectly blocked legitimate `contact_fanout`, whose B1 source thread and
  stable contact target may be different rows. A two-test RED retained direct
  drift refusal while reproducing the split-thread contact error. The
  origin-discriminated minimum fix made both tests and the final 42-test focused
  gate pass.

All transaction callbacks reset captured state on SDK retry, perform every
fresh read before the first staged mutation, use immutable `create`, and replace
mutable heads with `set(..., merge=False)`. Immediate executor failure accepts
only the exact complete before-image or disposition-specific after-image.
Multi-row claim/decline arithmetic remains bounded at 385/386 writes, and the
fake refuses 401 before any logical state changes.

## Final local verification

Every behavioral command used the plan-pinned Python 3.12 environment with
Firestore pointed to `127.0.0.1:9`, blank OpenAI credentials, source coordination
disabled, outbound paused, and all provider proxies blackholed. The retained M2
command enabled live semantics only inside hermetic fakes; provider egress
remained blackholed. No external message or campaign was sent.

- Release/auth/Jill regressions: 95/95 passed in 0.378 seconds; 1.57 seconds
  wall time.
- Complete B1 gate: 606/606 passed in 24.532 seconds; 25.76 seconds wall time.
- Complete B2 discovery: 292/292 passed in 8.496 seconds; 8.63 seconds wall
  time.
- Retained M2 gate: 669/669 passed in 22.566 seconds; 23.73 seconds wall time.
- Final source-link plus primitive-contract gate: 42/42 passed in 1.216
  seconds; 1.28 seconds wall time.
- Final source-link-only reviewer rerun: 17/17 passed in 0.332 seconds.
- `compileall` for `email_automation`, `scripts`, and `tests` exited 0.
- `pip check` exited 0 with `No broken requirements found.`
- Every GitHub Actions YAML file parsed successfully with the pinned Python and
  cached PyYAML 6.0.3 and independently with Ruby. PyYAML is not in the project
  lock, so the first unaugmented local parser invocation correctly reported
  `ModuleNotFoundError`; no production dependency was added for this dev-only
  check. GitHub also parsed and executed the exact workflow successfully.
- `git diff --check` exited 0, the index was empty, and the code worktree was
  clean before publication.
- Caffeination remained active through verification via
  `/usr/bin/caffeinate -dims` (PID 95257).

## Independent reviews

Reviewer A (`b2b_plan_executor_review`, Epicurus the 2nd) initially found the
Important direct-source relational gap described above. On the final exact
`69cdc042...` diff, the reviewer returned `APPROVED` with no Critical or
Important finding after the complete design/roadmap/plan pass, 292/292 B2
tests, 17/17 focused source-link tests, compile, diff, transaction, cross-user,
B1, and runtime-containment checks.

Reviewer B (`task7_contract_audit`, Huygens the 2nd) found the Important
contact-fan-out overconstraint in the first fix. On the final exact
`69cdc042...` diff, the reviewer returned `APPROVED` with no Critical or
Important finding after 292/292 B2, 669/669 M2, 17/17 source-link, and 84/84
contract/containment tests. Its executor probe confirmed exact apply-then-raise
success, strict readback of the B1 thread binding, and ambiguity on post-apply
binding drift.

## Static containment

- `email_automation/row_authority.py` imports only Python standard-library
  modules and accepts injected Firestore-shaped and transaction-executor
  dependencies.
- The runtime import audit proves only the pure `row_metadata.py` module imports
  `row_authority.py`; no route, scheduler, worker, provider, or effect path
  imports B2.
- B2-B changed no provider module, runtime route, frontend, Firestore rules,
  deployment, workflow, or production environment setting.
- `test_source_link_performs_zero_writes_to_every_b1_collection` proves the
  exact B1 documents are byte-for-byte unchanged and absent from the mutation
  log. The B1 inventory still recognizes only reviewed owners.
- Settlement schemas contain no send, draft, message ID, provider, or Sheet
  effect field. No B2-B operation issues a permit or claims that a provider
  effect occurred.

## Exact GitHub code run

- Branch: `codex/sitesift-production-clearance-20260804`.
- Local HEAD, remote branch, and workflow head SHA all matched
  `24b9a5fd2b38cb039714c5f22a3fddbf41c392c3`.
- [Production Clearance CI run 30997990266](https://github.com/BaylorH/EmailAutomation/actions/runs/30997990266)
  completed successfully for that exact code SHA.
- [offline-verification job 92279642542](https://github.com/BaylorH/EmailAutomation/actions/runs/30997990266/job/92279642542)
  completed successfully from `2026-08-05T10:34:54Z` through
  `2026-08-05T10:36:39Z`.
- GitHub release/auth: 95/95 in 1.059 seconds.
- GitHub complete B1: 606/606 in 28.813 seconds.
- GitHub complete B2: 292/292 in 12.041 seconds.
- GitHub retained M2: 669/669 in 23.504 seconds.
- Changed-Python compilation and exact release diff checks completed in the
  same job.

## Production posture and next milestone

B2-B is provider-free and runtime-unwired. It was not merged to `main`,
deployed, enabled, or exercised against production Firestore or the production
frontend. No production campaign or external communication occurred.
Production and Jill's return remain **NO-GO**.

The next gate is independent approval and publication of the B2-C
contact-compliance child plan. Production clearance still requires B2-C, B2-D,
B3 atomic provider-effect authority, B4 frontend/rules/runtime adoption, and an
explicit production-canary decision using an exact self-owned recipient named
by Baylor in the current turn.
