# SiteSift T6 Qualification Harness Design

**Date:** 2026-07-28
**Status:** Proposed for written review
**Deliverable:** Both — qualification-harness code and a verified qualification finding
**Product candidate:** Frontend/Functions `b4636e8276db18cb633d8c9e27b5e05fa9dc21a9`; backend `f104b5f4cfc7574188e47efaadbf72df219e19a5`

## Goal

Build the missing non-production harness required to qualify the frozen
SiteSift release candidate through:

- L3: bounded real-provider semantic validation; and
- L4: a test-owned, full-worker `1 -> 3 -> 10 -> 22` campaign ladder.

The build must make the future controlled run reproducible, attributable,
strictly bounded, privacy-safe, and fail-closed. Building and testing the
harness itself must have zero external effects.

This work does not deploy, send mail, create a campaign, touch Jill or any
customer, use live credentials, or claim that L3/L4 passed. A later,
separately authorized T6b turn supplies test-owned infrastructure and
credentials, approves each effectful stage, and records the actual result.

## Release State Preserved

The already verified product candidate remains immutable:

| Component | Frozen identity |
|---|---|
| Frontend and Functions source | `b4636e8276db18cb633d8c9e27b5e05fa9dc21a9` |
| Backend worker source | `f104b5f4cfc7574188e47efaadbf72df219e19a5` |
| Combined release manifest SHA-256 | `fb1b23c27525aa405e16f35ed71599c7887023e90d41677049b7c3097214cbaa` |
| Worker manifest SHA-256 | `b6ebb974ed4c0ddaa618f7f9d165c207cc569b862d9d27e8724f64e0871b4abc` |
| Canonical backend L1 baseline | `2444/2444` |
| Real Firestore L2 | `10/10`, twice |

The harness is developed on a separate qualification-only branch based on
the backend candidate. The candidate branch and its artifacts are not changed
or rebuilt.

The harness must verify and extract the frozen worker source archive into an
isolated temporary directory, then execute product code from that extraction.
It must never silently substitute the qualification branch's copy of product
modules. If implementing the harness proves that the product candidate itself
must change, T6 stops and a new candidate is created through the normal
build-and-test sequence.

## Decision

Use a separate, non-deployable qualification branch that binds an independent
test orchestrator to exact candidate artifacts.

This preserves the already qualified candidate while allowing tests,
synthetic fixtures, approval validation, live-adapter boundaries, evidence
generation, and restoration logic to be reviewed and versioned.

### Rejected alternatives

1. **Modify the frozen candidate branch.** This would change its source and
   artifact identity, invalidate existing L1/L2 evidence, and require a new
   candidate.
2. **Use a manual operator runbook only.** Manual assembly cannot prove exact
   inputs, stage order, effect caps, no-replay behavior, or privacy-safe
   evidence repeatably.
3. **Use Jill's account or campaigns as the ladder.** Customer activity is not
   deterministic test infrastructure and cannot be deliberately failed,
   restored, or bounded. Jill remains a product user, never a test harness.

## Authorization Boundary

There are two deliberately separate phases.

### T6a — build and offline verification

Authorized effects:

- create and test qualification-only source, schemas, fixtures, and adapters;
- generate deterministic local synthetic files;
- use in-memory or filesystem fakes under blocked-network tests;
- commit and push the qualification branch.

Forbidden effects:

- production, staging, Firestore, Drive, Sheets, Graph, mailbox, Firebase,
  provider, campaign, queue, scheduler, worker, deployment, or IAM activity;
- loading live credentials;
- using Jill or customer identifiers or data.

### T6b — controlled qualification

T6b requires a new authorization that names the exact harness commit,
candidate hashes, project, test identities, sender, recipient, test client,
test thread namespace, stage, expiry, and effect caps.

Authorization is one stage at a time. Approval of the 1-row stage does not
authorize 3, 10, or 22 rows. The next stage requires a new approval bound to
the prior stage's verified evidence digest.

The approval digest is an accident-prevention and attribution control. It is
not authentication and does not replace normal cloud/provider credentials or
access controls.

## Architecture

The qualification branch adds a top-level `qualification/` package and narrow
scripts. It does not add live behavior to `email_automation`, the scheduler, or
the deployed worker entry point.

```text
qualification/
  candidate.py          frozen artifact and source-extraction verification
  approval.py           strict authorization document and digest validation
  fixtures.py           deterministic synthetic corpus and manifest validation
  contracts.py          immutable run, stage, effect, and evidence contracts
  admission.py          all fail-closed checks performed before external I/O
  adapters.py           protocols plus offline fakes; no default live client
  l3_runner.py           bounded one-shot provider semantic runner
  l4_runner.py           one-stage full-worker state machine
  reconcile.py           exact cross-surface accounting
  restore.py             reversible test-state restoration and verification
  evidence.py            privacy-safe report construction and schema validation
scripts/
  build_sitesift_qualification_fixtures.py
  run_sitesift_qualification.py
tests/
  test_qualification_*.py
tests/fixtures/sitesift_qualification/
  stage-01.xlsx
  stage-03.xlsx
  stage-10.xlsx
  stage-22.xlsx
  attachments/
  fixture-manifest.json
docs/release-safety/
  sitesift-product-candidate-binding.json
  sitesift-qualification-policy.json
  sitesift-qualification-evidence.schema.json
```

Exact file boundaries may be consolidated during implementation when doing so
keeps the same contracts and testability. The architectural boundaries are
fixed: candidate verification, admission, external adapters, reconciliation,
restoration, and evidence remain independently testable.

### Qualification-only deployment guard

The branch carries a conspicuous qualification-only marker. The normal worker
release builder must fail when that marker exists. A test proves the guard.
The harness can consume the frozen product archive but cannot itself produce a
deployable product archive.

## Frozen Candidate Binding

`sitesift-product-candidate-binding.json` contains only immutable product
identities:

- frontend/Functions source commit;
- backend source commit;
- combined manifest digest;
- worker manifest digest;
- expected worker archive digest read from the verified worker manifest;
- expected source-tree digest;
- dependency-lock digest; and
- supported product Python/runtime identity.

`sitesift-qualification-policy.json` independently binds the harness fixture
schema, fixture manifest digest, evidence schema, allowed provider/runtime
labels, hard call/effect/spend ceilings, and the only supported ladder:
`[1, 3, 10, 22]`. Updating qualification policy never changes or appears to
change the frozen product identity.

Before any live adapter can be constructed, the runner must:

1. Require explicit paths to the combined manifest, worker manifest, and
   worker archive.
2. Hash every supplied file and compare it with the committed binding.
3. Parse manifests with strict schemas and compare their internal source and
   artifact identities.
4. Reject symlinks, path traversal, unexpected archive members, duplicate
   paths, and files outside the expected candidate source tree.
5. Extract into a new temporary directory with read-only permissions.
6. Recompute the extracted candidate source-tree digest.
7. Launch candidate execution in an isolated subprocess whose product import
   root is the extracted archive, not the qualification worktree.
8. Record the harness commit and require the qualification worktree to be
   clean for a live run.

The harness commit is recorded at runtime rather than embedded inside its own
commit. T6b authorization names that exact clean commit.

A missing required artifact produces `UNAVAILABLE`; an identity or digest
mismatch produces `FAIL`. Both happen before credential discovery or network
access.

### Candidate process boundary

The qualification parent never imports `email_automation` or root `main.py`
from its own branch. Candidate behavior runs in a child process with the
verified extraction as its working directory and only product dependencies plus
the standard library on its import path.

The parent writes a canonical request to the child's standard input and reads
the typed result envelope from a dedicated inherited result pipe so product log
output cannot be mistaken for protocol data. A minimal launcher, hashed as part
of the harness identity, does only the following:

- for L3, import the frozen candidate claim/proposal/provider APIs, construct
  their existing pinned provider adapter, evaluate the supplied synthetic cases,
  and return typed semantic results; or
- for L4, load the frozen root `main.py` and call its existing
  `refresh_and_process_user(exact_test_uid)` once.

The L4 call deliberately selects one already admitted dedicated test user
instead of calling `run_all_users`. It executes the complete candidate
per-user worker body—including its real clients and final outbound effect
gateway—but does not exercise the outer Cloud Scheduler trigger, user
enumeration, or scheduler lease. Those outer controls remain covered by L1/L2;
T6 validates the campaign worker against a test-owned account.

The launcher does not monkey-patch product functions, replace clients, alter
return values, or add the qualification worktree to `sys.path`. Qualification
adapters prepare and read the test-owned environment from the parent; they are
not injected into the candidate child. The live child uses the frozen
candidate's real service modules and exact effect gateway under the admitted
environment and caps.

Before candidate import, the child environment is rebuilt from an allowlist.
It has:

- a temporary `HOME`, cloud/config homes, cache directory, and working
  directory;
- `PYTHONNOUSERSITE=1`, `PYTHONDONTWRITEBYTECODE=1`, and no inherited
  `PYTHONPATH`;
- no inherited cloud, Firebase, Graph, Google, provider, bearer, token, secret,
  or credential variable;
- in `live` mode only, the exact approved variables and credentials supplied
  explicitly by the parent; and
- exact run, UID, recipient, retry, provider, and effect-cap variables from the
  sealed admission plan.

Offline candidate checks run inside an enforceable OS boundary before import:
the macOS implementation uses a checked-in `sandbox-exec` profile that denies
network access, process creation, and filesystem writes outside the run
directory; CI may use an equivalent network-none, read-only container. A
host without a tested isolation backend returns `UNAVAILABLE`. Hostile tests
must prove socket, subprocess, ambient-credential, home/config, and
out-of-scope-write attempts fail inside the actual child process.

Live execution necessarily permits the approved network services, but retains
the sanitized filesystem, import path, process, environment, and explicit
credential boundary. No ambient credential fallback is allowed.

Candidate stdout/stderr is bounded and captured to a mode-`0600` transient
sink; it is never streamed into the console or evidence report. The report may
record only the child exit code, output digest, closed result codes, and
reconciled counts. The raw sink is destroyed after successful reconciliation;
on a stopped run, access or retention requires a separate sensitive-diagnostic
decision.

## Synthetic Qualification Corpus

The repository commits four deterministic `.xlsx` workbooks representing
exactly 1, 3, 10, and 22 campaign rows, plus the bounded synthetic attachments
referenced by those rows.

The generator is deterministic: the same schema version and seed must produce
byte-identical files and manifest digests. `--check` regenerates into a
temporary directory and proves that committed outputs match.

Every fact is fictitious. Names, properties, companies, phone numbers,
addresses, message text, and attachment contents use an explicit synthetic
vocabulary. Email-like fixture values use reserved `example.com` domains.
Stable case IDs such as `synthetic-property-007` replace customer identifiers.

The fixture generator and scanner accept only the explicit synthetic vocabulary
and reject:

- any name, identifier, property, company, contact value, or prose outside the
  declared synthetic catalog;
- real configured sender or recipient values;
- non-reserved email domains;
- unknown workbook columns or attachment types;
- formulas, macros, external links, hidden data, comments, or embedded objects;
- duplicate case IDs, rows outside the declared stage, or references to a case
  not declared in the fixture manifest; and
- raw secrets or credential-shaped strings.

The stage workbooks are independent datasets, not mutations of one shared
campaign. This makes every stage attributable and restorable.

The fixture manifest records only safe case IDs, structural counts, expected
effect counts, workbook and attachment digests, and scenario tags. It never
contains live identities.

## Approval Contract

The runner consumes a canonical JSON approval document and requires its
SHA-256 through `SITESIFT_QUALIFICATION_APPROVAL_SHA256`. Unknown fields,
duplicate keys, noncanonical serialization, missing values, and extra scope
are rejected.

Required fields:

- schema version and approval ID;
- exact harness commit;
- exact candidate source, manifest, archive, and fixture digests;
- level (`L3` or `L4`) and exact L4 stage;
- run ID and a fresh, unique test namespace;
- cloud project and database identities;
- opaque dedicated test UID, client, thread, Sheet, sender, and recipient
  labels plus keyed runtime-value digests;
- allowed provider/model and runtime identities;
- maximum provider calls, output operations, recipients, rows, and spend;
- issued-at and expires-at timestamps;
- for L4 stages after 1, the immediately preceding stage and its evidence
  digest; and
- operator authorization label.

Static admission receives every claimed runtime identity through explicit
inputs and compares its keyed HMAC-SHA-256 with the approval. After static
admission, credential-backed read-only discovery obtains the actual authenticated
project, tenant, mailbox, sender, recipient, UID, client, thread, and Sheet
identities and compares their HMACs again. No effect is enabled unless claimed,
approved, and actual identities all match.

A fresh evidence key is supplied separately for the run, is never logged or
persisted, and is not an authentication credential. Clear identity values never
enter the approval or evidence report.

Approval expires closed. It cannot authorize a stage larger than its fixture,
multiple stages, multiple recipients, retries, scheduler execution, or a run
ID already present in the test-owned receipt store.

The runner accepts only identities in the exact dedicated test allowlist and
requires read-only metadata to independently prove that the project, database,
UID, client, mailboxes, thread namespace, and Sheet are qualification-owned.
The L4 credential set must have no access to the production Firestore project,
customer mailboxes, or customer Drive/Sheets. Anything not positively proved
test-owned is rejected regardless of approval; Jill and customer identities
therefore cannot enter the lane. This design never permits customer-owned data
or a production project for L4.

## Admission Before External I/O

The CLI has two execution classes:

- `verify`: deterministic local fixture, schema, candidate, state-machine,
  privacy, and fake-adapter checks with network blocked; and
- `live`: a separately authorized L3 or single L4 stage.

For `live`, admission has a local phase followed by a bounded read-only
environment phase. The following order is mandatory:

1. Parse strict CLI arguments and approval without importing cloud/provider
   SDK clients.
2. Verify clean harness source and exact harness commit.
3. Verify candidate manifests, archive, extracted source, and runtime.
4. Verify fixture digests and scan all synthetic content.
5. For an L4 stage after 1, load the actual predecessor evidence file, parse it
   against the strict evidence schema, hash its canonical bytes, and require:
   the immediately previous stage; `PASS`; completed reconciliation and
   restoration; identical candidate, harness, policy, and fixture identities;
   and an unbroken predecessor-digest chain. Stage 1 requires that no
   predecessor be supplied.
6. Validate approval scope, expiry, the verified predecessor digest, claimed
   identity HMACs, and caps.
7. Compute the complete setup, provider/mail, expected-state, reconciliation,
   and restoration plan and prove it is within every cap.
8. Prove the run ID is absent from the local evidence directory and emit a
   local-admission digest containing no live identity values.
9. Only then construct read-capable adapters for the explicitly claimed
   identities and verify that their actual authenticated identity HMACs match
   the approval.
10. Read the narrowly scoped qualification ledger to verify that the run ID and
    namespace appear unused. This read is advisory; atomic ownership is acquired
    later.
11. Read and capture the exact test-owned starting state, prove that it is
    empty or matches the approved fixture baseline, and prove that it is
    restorable.
12. Freeze the complete admission digest and atomically write the encrypted
    recovery capsule locally.
13. As the first external write, atomically create-if-absent one qualification
    ledger claim keyed by both run ID and namespace. If it already exists or
    the atomic result is ambiguous, stop before setup or any provider/product
    effect.
14. Seal the acquired ledger claim with the admission digest, then enable only
    the parent methods and candidate child described by the admitted plan.
15. Revalidate expiry, actual identity HMACs, prior-state hashes, and caps
    immediately before every external effect.

No credential lookup, SDK client construction, DNS lookup, or external metadata
read occurs before the local phase in steps 1–8 passes. No provider/product,
setup, or restoration write occurs before the bounded read-only phase and
atomic ledger claim in steps 9–14 pass. The ledger claim is always the first
external write.

There is no generic production service locator and no ambient-default project,
user, mailbox, Sheet, or credential fallback. Missing explicit configuration
returns `UNAVAILABLE`.

## External Adapter Boundary

The qualification parent accesses external systems only through narrow
protocols injected into the runners:

- provider inference;
- test-owned Firestore state and receipts;
- test-owned workbook/Sheet state;
- test-owned mailbox send/read;
- test-owned attachment storage if required; and
- clock/runtime identity.

The default implementation used by unit and verification tests is in-memory
and network-disabled. Live adapter modules are imported lazily only after local
admission. Their effect-capable methods remain sealed until the bounded
read-only environment phase completes.

Every mutating qualification-parent adapter call requires an immutable admitted
effect containing the run ID, stage, sequence, operation class, idempotency key,
expected prior state, and a case ID when the operation is case-scoped. Parent
adapters reject calls not present in the admitted plan.

The frozen candidate child is a separate trust boundary. It does not expose
dependency injection for its internal Firestore and Sheet clients, so the
harness does not pretend that parent protocols intercept those calls. Instead:

- the live child receives credentials that can reach only the dedicated
  qualification project, mailbox, and Drive/Sheets;
- all state starts inside the fresh approved test namespace;
- a static candidate seam audit proves every provider/mail mutation reachable
  from `refresh_and_process_user` passes through the existing final effect
  gateway;
- the gateway receives exact plan-derived run, attempt, provider, user, and
  recipient caps; and
- pre/post snapshots reconcile candidate Firestore and Sheet changes against a
  closed expected-state transition contract.

Direct candidate Firestore/Sheet writes are reversible sandbox state, not
claimed to be gateway-intercepted. An unexpected state change fails the stage
even inside the sandbox. Any provider/mail bypass found by the seam audit
refutes this design before a live run. If test-only credential isolation cannot
be proved, L4 is `UNAVAILABLE`; broader credentials are never accepted as a
substitute.

The existing outbound effect gateway, receipt, retry, and cap controls are
reused rather than bypassed. The qualification runner may make their bounds
tighter; it may not weaken Phase B/C authentication, recipient, idempotency,
retry, or kill-switch controls.

## L3: Real-provider Semantic Gate

L3 tests real provider semantics against synthetic evidence without running a
campaign, scheduling a worker, or creating customer-visible effects.

Properties:

- one direct, foreground invocation;
- a dedicated test identity and the frozen candidate provider path;
- exact pinned provider, model, prompt, schema, timeout, and token bounds;
- an approved maximum call count and spend reservation before the first call;
- SDK and harness retries set to zero;
- a smoke case before the remaining bounded corpus;
- exact semantic oracles and repeatability rules;
- provider usage reconciled to the admitted call plan; and
- immediate stop on transport ambiguity, missing usage, schema mismatch,
  semantic variance outside the oracle, or any attempted non-provider effect.

L3 never starts the scheduler or full worker. Its live adapters expose provider
inference only; every product-persistence, Sheet, mailbox, and campaign adapter
is a fail-on-call sentinel. The qualification control-plane ledger is the one
allowed persistence surface.

The run-level atomic ledger claim is created before the smoke call. Immediately
before each admitted case/repeat call, the parent atomically reserves its unique
provider idempotency key in that claimed ledger. A started reservation is never
called again. Success records the provider request/usage digest; timeout,
process interruption, or missing response leaves the reservation
reconciliation-required.

L3 uses the same recovery capsule and `recover` command as L4. Recovery may
query provider/usage metadata when supported and finalize the evidence, but it
cannot issue a provider inference call. If acceptance cannot be proved, the
result remains `AMBIGUOUS` and the reservation/tombstone permanently prevents a
retry.

## L4: Controlled Full-worker Ladder

L4 proves the frozen worker across controlled, wholly test-owned campaign
sizes `1 -> 3 -> 10 -> 22`.

### State machine

- One CLI invocation can execute exactly one stage.
- Stage 1 requires no predecessor but requires a new run namespace.
- Stage 3 requires a passing, reconciled stage-1 evidence digest.
- Stage 10 requires stage 3.
- Stage 22 requires stage 10.
- A failed, unavailable, ambiguous, unreconciled, expired, or restored-with-
  error predecessor cannot authorize the next stage.
- No flag skips a stage, reuses a prior namespace, or auto-promotes after
  success.

Each stage uses its own dedicated synthetic workbook/campaign namespace. The
worker is invoked directly in the foreground for exactly the admitted scope.
No scheduler, recurring trigger, cross-user enumeration, or background queue
is started. The candidate's normal per-user Inbox and SentItems scans do run,
so the dedicated test mailbox must be empty except for the active stage's
synthetic artifacts. The test UID likewise contains only the active stage's
synthetic campaign state, so whole-user queries remain bounded to test-owned
records.

The admitted effect plan sets exact expected counts. The external-effect
gateway hard-stops any operation beyond the lower of the approval cap and
fixture expectation. The maximum stage contains 22 rows and one explicitly
approved recipient identity; no dynamic recipient expansion is permitted.

### Stop and no-resend rule

Any ambiguous provider, send, persistence, or reconciliation outcome stops the
stage. The runner must not retry or resend an operation merely because a
response was lost. It first queries the test-owned receipt/provider surface by
idempotency key. If the result cannot be proved, the stage is `AMBIGUOUS`, not
failed-and-retried.

## Reconciliation Contract

A stage can pass only when all applicable independent views agree exactly:

- admitted effect-plan count and idempotency keys;
- effect-gateway receipts and counters;
- provider accepted/returned operations and usage;
- test mailbox accepted and observed messages;
- Firestore test records and terminal state;
- Sheet/workbook test rows and expected state transitions; and
- worker result and error counters.

Comparison uses safe case IDs and hashes. Missing, extra, duplicated,
out-of-order, cross-run, or cross-stage effects are blockers. A successful API
response alone is never proof of a completed effect.

The evidence report states one of:

- `PASS`: exact outcome and reconciliation, followed by verified restoration;
- `FAIL`: an attributable expected-vs-actual difference;
- `AMBIGUOUS`: effect outcome cannot be proven and no resend occurred; or
- `UNAVAILABLE`: admission or required test infrastructure was unavailable
  before effects.

Only `PASS` can become an L4 predecessor.

## Restoration Contract

Before any live L3 provider call or L4 setup/product effect, the harness records
the complete recovery plan. For L4 it also captures the reversible starting
state for the fresh, test-owned namespace and proves the namespace contains no
unrelated records. It atomically writes a mode-`0600`,
authenticated-encrypted recovery capsule to an explicit operator path. The
separately supplied recovery key is never written or logged. The capsule
contains the exact run scope, starting snapshot when applicable, idempotency
keys, approved cleanup operations, and enough test-owned raw identifiers to
resume reconciliation after a parent or child crash. It is operational
recovery state, not privacy-safe evidence.

After the capsule exists, admission atomically claims the run ID and namespace
as its first external write in a dedicated qualification ledger outside
`users/{uid}` and every worker-visible query. This prevents replay even when
the runner disappears. The test UID's campaign state still contains only the
active stage; historical qualification tombstones do not enter candidate
queries.

Normal completion, `FAIL`, `AMBIGUOUS`, signal interruption, child crash,
parent crash, and authorization expiry after effects begin all enter the same
recovery state machine:

1. disables further effects for the run;
2. reconciles every admitted idempotency key before considering cleanup;
3. preserves any receipt, mailbox record, or provider observation that is the
   only proof of an ambiguous operation;
4. for L4, restores only the explicitly approved reversible test-owned
   Firestore, Sheet, and, if the plan used it, Drive state to the captured
   starting snapshot;
5. queries the same bounded namespace to prove restoration;
6. proves the run cannot be replayed by retaining its qualification-ledger
   tombstone;
7. reconciles irreversible provider and mail effects rather than pretending
   they were undone; and
8. reports restoration, preserved-evidence, and irreversible-effect counts
   separately.

Queue and scheduler adapters are fail-on-call sentinels. Because L4 invokes the
per-user worker directly and starts no trigger or queue, recovery never changes
queue or scheduler state; reconciliation proves those surfaces remained
untouched.

Sent mail cannot be unsent. It remains only in dedicated test mailboxes and is
reported as a reconciled irreversible test effect. Raw message IDs required
during cleanup remain transient and do not enter the privacy-safe report.

`run_sitesift_qualification.py recover` accepts an existing recovery capsule
and exposes no method that can create a new provider, mail, campaign, or worker
effect. It may read exact receipts, reconcile, quarantine preserved evidence,
restore approved reversible state, and finalize the tombstone. Expiry blocks
new effects but never blocks this exact-scope containment path.

Signal/exception handlers attempt recovery immediately, but correctness never
depends on handlers running. A fresh process can resume from the capsule.

If restoration cannot be proved, the stage is not `PASS`, the next stage is
blocked, and the test-owned namespace is quarantined for explicit inspection.
If cleanup would destroy the only evidence that could settle an ambiguous
effect, that artifact remains quarantined and the run remains `AMBIGUOUS`.
The harness never broadens cleanup beyond the exact run namespace.

## Privacy-safe Evidence

The committed JSON schema uses an explicit allowlist. A report may contain:

- schema, level, stage, status, and closed reason/error codes;
- candidate, harness, fixture, approval, admitted-plan, predecessor, and result
  digests;
- safe synthetic case IDs and scenario tags;
- provider/model/runtime labels;
- timestamps and durations;
- expected/observed/reconciled/restored counts;
- token, call, spend, receipt, retry, and cap totals;
- boolean admission, isolation, reconciliation, and restoration gates; and
- keyed HMAC digests of live test-owned identities.

It may not contain:

- message or attachment bodies;
- prompts, provider responses, extracted facts, addresses, property data, or
  workbook cell values;
- email addresses, UIDs, client IDs, thread IDs, Sheet IDs, Drive IDs, message
  IDs, access tokens, secrets, credentials, or authorization headers;
- exception stacks or SDK request/response dumps; or
- Jill/customer data or identifiers in any form.

The evidence builder rejects unknown fields and scans every string for email,
address, secret, bearer, credential, and known-customer patterns. The final
report is built from typed allowlisted values, not by redacting an unrestricted
log after execution.

Console output follows the same allowlist. Live SDK debug logging is disabled.

## Canonical Test-level Integration

`scripts/run_test_level.py` gains real L3 and L4 dispatchers instead of the
current unconditional “no canonical suite” response.

- Without the required explicit live configuration and approval, L3/L4 return
  `UNAVAILABLE` before credential or network access.
- With `verify` mode, the qualification contracts run against fakes under the
  L1 network/credential block and cannot produce an external effect.
- With `live` mode, the dispatcher invokes the canonical L3 runner or exactly
  one approved L4 stage.
- Result counts and status map deterministically to the existing passed,
  failed, and unavailable exit codes.

The separate `run_sitesift_qualification.py` CLI exposes detailed operator
inputs and evidence paths; the test-level wrapper remains the canonical release
gate entry point.

## Test Strategy

Implementation follows test-driven development. Tests are written to fail
before each behavior is added.

### Pure contract and fixture tests

- strict schema parsing, canonical JSON, duplicate-key rejection, and stable
  digests;
- byte-deterministic 1/3/10/22 workbooks and attachments;
- fixture privacy scanner positives and negatives;
- exact stage sizes, case uniqueness, attachment references, and effect plans;
- evidence allowlist and prohibited-value rejection.

### Candidate and isolation tests

- correct frozen artifacts extract and match;
- product-candidate and qualification-policy bindings are independent;
- manifest, archive, source-tree, runtime, symlink, traversal, duplicate-path,
  dirty-worktree, and wrong-commit failures;
- candidate subprocess imports product modules only from the extraction;
- the candidate seam inventory proves every reachable provider/mail effect
  passes through the frozen final gateway and fails if a bypass is introduced;
- harness build and verify modes block socket, credential, SDK, and subprocess
  escape paths;
- a host without the tested OS isolation backend is unavailable;
- candidate stdout/stderr cannot enter protocol or privacy-safe evidence;
- qualification branch cannot produce a normal release artifact.

### Approval and admission tests

- wrong candidate, harness, fixture, approval-declared project/identity, level,
  stage, model, cap, expiry, or predecessor digest rejects before adapter
  creation;
- predecessor evidence with the wrong schema, bytes, stage, status,
  reconciliation/restoration state, identity binding, or chain continuity is
  rejected;
- an actual external project/identity mismatch or reused run namespace rejects
  after bounded reads but before any provider call or write;
- missing and ambient-default configuration is unavailable;
- known Jill/customer identity is always rejected;
- cap arithmetic is conservative and checked again immediately before effect;
- concurrent attempts for the same run or namespace yield exactly one atomic
  ledger claimant, and every loser stops before setup/effects.

### L3 tests

- smoke-first ordering and zero calls after smoke failure;
- exact provider/model/prompt/schema/timeout binding;
- no retry, call/token/spend caps, usage reconciliation, semantic oracles, and
  fail-on-call sentinels for every non-provider adapter;
- the qualification ledger is the only allowed L3 persistence surface;
- timeout, interruption, and unknown provider outcome enter recovery without
  another call; and
- a started provider reservation can never be issued twice.

### L4 state-machine and failure-injection tests

- only `1 -> 3 -> 10 -> 22` is valid;
- one stage per invocation and a fresh approval per stage;
- every non-first stage requires the exact prior evidence digest;
- exact plan/gateway/provider/mailbox/Firestore/Sheet/worker reconciliation;
- extra, missing, duplicate, delayed, or cross-run effects block;
- failures and ambiguous results at every adapter seam stop without resend;
- reversible state restores exactly, irreversible mail is reported honestly,
  and failed restoration blocks progression;
- signal interruption, parent crash, child crash, and post-effect expiry resume
  through recovery-only mode;
- recovery preserves the sole proof of an ambiguous effect and never enables a
  new effect;
- queue and scheduler sentinels remain untouched; and
- repeated or crashed runs cannot replay admitted effects.

### Full verification

- focused qualification tests;
- deterministic fixture `--check`;
- compile/import checks;
- canonical credential-free/network-blocked L1;
- canonical L3/L4 unavailable behavior without authorization; and
- the full backend suite remains green.

No T6a test supplies live credentials or enables a real adapter.

## Acceptance Criteria for T6a

T6a is complete only when:

1. the harness exists only on the separate qualification branch;
2. candidate binding proves the exact frozen product artifacts and extracted
   product imports;
3. a seam audit proves every reachable candidate provider/mail effect uses the
   frozen final gateway, and live credential capability is restricted to
   test-owned infrastructure;
4. committed synthetic 1/3/10/22 fixtures regenerate byte-identically and pass
   privacy scans;
5. approval, predecessor, atomic-claim, stage, cap, no-retry, reconciliation,
   recovery, restoration, and evidence
   contracts are covered by failure-injection tests;
6. read-capable live adapters are unreachable before local admission, and all
   effect-capable methods are unreachable before complete admission;
7. the recovery capsule is durable before an atomic run/namespace claim becomes
   the first external write, and no started L3/L4 effect can replay;
8. build/verify execution performs zero network, credential, provider,
   persistence, campaign, queue, scheduler, or mail activity;
9. canonical L3/L4 entry points exist and fail closed when not separately
   authorized;
10. the qualification-only branch cannot be packaged as the product worker;
11. the pinned Node/Python and full backend test suites remain green; and
12. the verified branch commit and test evidence are recorded in Brain with a
    T6b handoff that clearly states no live qualification has run yet.

## Refutation Conditions

Stop and redesign rather than weakening the gate if:

- exact candidate code cannot be exercised without modifying or rebuilding it;
- any provider/mail mutation reachable in the L4 candidate bypasses the frozen
  final effect gateway;
- the candidate cannot run with credentials that are technically isolated from
  production/customer Firestore, mail, and Drive/Sheets;
- the full-worker scope cannot be bounded to dedicated test-owned state;
- any provider/send outcome lacks a queryable idempotency receipt;
- the intended 1/3/10/22 effect counts cannot be known before execution;
- restoration would require a broad or customer-scoped delete;
- a report must expose raw user, property, mail, Sheet, or credential data;
- a read-capable live adapter must initialize before local admission, or an
  effect-capable method must be enabled before complete admission;
- canonical L3/L4 would depend on an operator remembering an unenforced manual
  step; or
- the existing product safety gateway must be weakened to make the harness
  work.

## Milestone Meaning

Passing T6a means “the controlled qualification apparatus is built and proved
safe offline.” It does not mean the product is fully qualified or ready for
Jill.

Passing the separately authorized T6b L3 and all four L4 stages means the exact
candidate has completed the planned provider and full-worker qualification
ladder. Only then does the project advance to the coordinated T7 rollout and
objective all-clear. Jill's normal product use resumes after that all-clear;
her activity is monitored as real use, not used to prove the release.
