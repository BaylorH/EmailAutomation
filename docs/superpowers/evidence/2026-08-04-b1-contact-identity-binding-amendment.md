# B1 Contact Identity Binding Amendment Evidence

## Candidate and scope

- Approved B2-B baseline:
  `48a23dbf31e2b3c04f8e745239768f6f264c9e0b`.
- Approved amendment/design publication:
  `778e50208e42c91aab501340efdbeb8f88939202`.
- Recorded plan-publication checkpoint:
  `193dd6d051023a3158b25849c8ab0c4abe1ceee6`.
- Reviewed and exact-SHA-proven code candidate:
  `29dc7003fc596e3aab72d3ef24b91308255ecefe`.
- Final baseline-to-code binary diff SHA-256 reviewed independently by both
  approvers:
  `bd3d6d99035140f5b30a8ce7eee5bc18da1ea4d91d88cd953706b3b45558969a`.
- Deliverable: both provider-free B1/B2 bridge code and production-clearance
  findings.
- The exact baseline-to-code range changes the five approved design/plan
  documents, `source_coordinator.py`, `row_authority.py`, and their five named
  test modules: 12 files, 2,438 insertions, and 49 deletions.

This evidence publication changes only this file, the amendment plan status,
and the one roadmap checkbox.

## Published milestones

- `778e50208e42c91aab501340efdbeb8f88939202` — approve and publish the B1
  contact-identity amendment and child plan.
- `193dd6d051023a3158b25849c8ab0c4abe1ceee6` — record exact plan-publication
  proof.
- `74f2370140c55d831688ad4ddfd10cfc3c2ad8d2` — bind newly verified B1 hard
  opt-outs to exact and canonical user-scoped contact hashes.
- `29dc7003fc596e3aab72d3ef24b91308255ecefe` — carry bound contact authority
  through a domain-separated v2 B1 link while retaining v1 bytes.

Each substantive milestone was committed and pushed on the owned
`codex/sitesift-production-clearance-20260804` branch. No commit was pushed or
merged directly to `main`.

## Frozen authority behavior

- The injected hard-opt-out verifier's trusted response remains the exact v1
  three-field proof. Identity fields returned by a caller or verifier are not
  accepted.
- Only after a strict non-local hard-opt-out proof succeeds does B1 derive the
  exact and plus-stripped canonical mailbox hashes from the frozen,
  source-matched classification input and verified user scope.
- Every newly claimed verified hard opt-out persists exact nested v2 evidence
  with the original evidence hash and both complete identity hashes. Existing
  candidate, selection, owner, ledger, and B2-link hashes transitively bind the
  complete five-field evidence.
- An existing v1 hard snapshot remains immutable history: retry reconstructs
  v1, bypasses identity derivation, performs no upgrade, and compares exact
  stored material. An existing v2 retry re-derives and exact-compares both
  identity hashes.
- B1 independently implements B2's UTF-8, canonical-JSON, Unicode-control,
  user-scope, mailbox-normalization, and domain-hash bytes. Cross-module parity
  is test-only; neither runtime module imports the other.
- New bound contact authority emits the exact contact-only v2 B1-link shape
  under `sitesift.row.b1_authority_link.v2`. Terminal, human-decision, and
  legacy contact authority retain the exact v1 shape and domain.
- The private contact-fan-out planner validates the complete link and rejects
  v1 before generic request-ID derivation or row-state planning. B2-C remains
  responsible for the later contact-settlement/fan-out mutations.
- Persisted authority contains hashes only. No raw mailbox or verified user ID
  was added to snapshots, links, logs, this evidence, or Brain.

## TDD and refutation evidence

Task 1 began with a selected 12-test RED that produced 19 discriminating
failures for the absent identity helpers, v1/v2 schema distinction, v2
persistence, source/sender binding, retry/readback/race behavior, legacy
replay, integration, and inventory. A later resource-bound RED errored exactly
because the B2-compatible depth constant was absent. The minimum implementation
then made the final 270-test focused source gate green.

Task 2's unchanged implementation ran 222 focused tests with 3 failures and 18
errors because the v2 domain/builder were absent and legacy fan-out reached the
generic planner. The new literal tests of the pre-existing v1 vectors passed in
that RED. The minimum discriminated builder/validator and early v1 gate made
all 222 focused tests and all 299 discovered row-authority tests green.

Reviewer A directly proved representative new helper, v2-persistence, and
v2-link tests fail against the frozen baseline for their intended missing
behavior, then loaded the baseline module independently and proved the new
literal v1 link and downstream-vector test remains green. Reviewer B audited
the committed test discrimination, frozen vectors, and exact final behavior
without claiming to rerun those baseline probes.

Frozen link vectors:

- v1 terminal: `1ac2f1ebdaeb99c59d1b3c8a7084d66165a05fee4c91c02ca8f479379c34c1c3`
- v1 human decision: `27583f112239d94468f657ee57fabd1b72f2ca97420899087a7ce1702b770fbf`
- v1 legacy contact: `051c1cda498e0a3d08c168e10f80ac54f0e29493a1b77e42b53895e92e1147fe`
- v2 bound contact: `e23bbb1dafe6c155d0781c2ea90600cc1f02ba8bb7a523f0852d97420079cb8f`

The v1 test also freezes the existing direct-claim request, claim-set,
generation, settlement, and source-settlement-link hashes byte-for-byte.

## Final local verification

Every behavioral command used the plan-pinned Python 3.12 environment with
Firestore pointed to `127.0.0.1:9`, blank OpenAI credentials, source
coordination disabled, outbound paused, and every provider proxy blackholed.
The retained M2 command enabled live semantics only inside hermetic fakes;
provider egress remained blackholed. No external message or campaign was sent.

- Release/auth/Jill regressions: 95/95 passed in 0.384 seconds.
- Complete B1 gate: 617/617 passed in 24.632 seconds.
- Complete B2 discovery: 299/299 passed in 8.454 seconds.
- Retained M2 gate: 669/669 passed in 22.492 seconds.
- Final Task 1 focused gate: 270/270 passed in 7.083 seconds.
- Final Task 2 focused gate: 222/222 passed in 6.916 seconds.
- All unittest summaries were plain `OK` with zero skips, failures, or errors.
- `compileall` for `email_automation`, `scripts`, and `tests` exited 0.
- `pip check` exited 0 with `No broken requirements found.`
- Ruby parsed all three GitHub Actions YAML files successfully.
- `git diff --check` exited 0. The index and worktree were clean after the one
  tracked bytecode artifact refreshed by `compileall` was restored to its exact
  committed bytes.
- Caffeination remained active through verification via
  `/usr/bin/caffeinate -dims` (PID 95257).

## Independent full-diff reviews

Reviewer A (`b2b_plan_executor_review`, Epicurus the 2nd) approved exact range
`48a23db...29dc700` and digest `bd3d6d99...` with no Critical or Important
finding. Its blackholed rerun passed 95 release/auth, 617 B1, 299 B2, and 669 M2
tests with zero skips. It verified the unchanged verifier trust boundary,
post-verification derivation, exact v1/v2 retry behavior, complete readback,
hash-only persistence, production-verifier absence, runtime containment, RED
discrimination, and frozen v1 downstream bytes.

Reviewer B (`task7_contract_audit`, Huygens the 2nd) independently approved the
same exact range and digest with no Critical or Important finding. Its
blackholed rerun passed 617 B1 and 299 B2 tests. It verified independent B2
parity and cross-user vectors, exact stored-evidence/link correlation,
cross-scope refusal, defensive validation, early legacy fan-out rejection,
malformed/partial/unreadable readback refusal, swapped-identity retry refusal,
and absence of runtime/provider/writer expansion.

Both reviewers rechecked local HEAD, binary diff digest, clean worktree, and
range diff before and after review. Reviewer A also rechecked live remote
equality; Reviewer B checked the exact remote-tracking ref, and root separately
verified live remote equality. Neither reviewer edited a file, contacted a
product provider or external human, or performed an external write or
communication; GitHub access was read-only verification.

## Exact GitHub proof

- Plan/design SHA `778e50208e42c91aab501340efdbeb8f88939202`:
  [Production Clearance CI run 31001047514](https://github.com/BaylorH/EmailAutomation/actions/runs/31001047514),
  [job 92289728426](https://github.com/BaylorH/EmailAutomation/actions/runs/31001047514/job/92289728426),
  successful at that exact SHA.
- Plan-record SHA `193dd6d051023a3158b25849c8ab0c4abe1ceee6`:
  [Production Clearance CI run 31001220079](https://github.com/BaylorH/EmailAutomation/actions/runs/31001220079),
  [job 92290298636](https://github.com/BaylorH/EmailAutomation/actions/runs/31001220079/job/92290298636),
  successful at that exact SHA.
- Task 1 SHA `74f2370140c55d831688ad4ddfd10cfc3c2ad8d2`:
  [Production Clearance CI run 31002953759](https://github.com/BaylorH/EmailAutomation/actions/runs/31002953759),
  [job 92295977495](https://github.com/BaylorH/EmailAutomation/actions/runs/31002953759/job/92295977495),
  successful at that exact SHA.
- Final code SHA `29dc7003fc596e3aab72d3ef24b91308255ecefe`:
  [Production Clearance CI run 31003133246](https://github.com/BaylorH/EmailAutomation/actions/runs/31003133246),
  [job 92296561651](https://github.com/BaylorH/EmailAutomation/actions/runs/31003133246/job/92296561651),
  successful at that exact SHA.
- The final GitHub job passed 95 release/auth tests in 0.755 seconds, 617 B1
  tests in 31.744 seconds, 299 B2 tests in 14.758 seconds, and 669 M2 tests in
  24.733 seconds, then compiled changed Python and passed the exact release
  diff check.
- Local HEAD, the owned remote branch, and the workflow head SHA all matched
  the final code SHA before evidence publication.

## Production posture and next gate

This amendment is provider-free and runtime-unwired. It changed no provider
client, route, worker, frontend, Firestore rule, deployment, environment flag,
campaign, or production record. It was not merged to `main`, deployed, or
enabled. No production campaign or external communication occurred.

Production and Jill's return remain **NO-GO**. The next milestone is to draft,
review, independently approve, and publish the B2-C contact-compliance child
plan before executing it. That is followed by B2-D, B3 provider-effect
authority, B4 frontend/rules/runtime adoption, deployment, and an explicitly
authorized production canary using an exact self-owned recipient named by
Baylor in the current turn.
