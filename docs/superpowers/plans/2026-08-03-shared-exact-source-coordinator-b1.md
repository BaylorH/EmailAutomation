# Shared Exact-Source Coordinator B1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first M3 authority layer so every inbound source has one canonical identity, one frozen classification, one deterministic transition decision, one durable work ledger, and strict settlement before any adopted lane can mark it processed.

**Architecture:** Add a focused `source_coordinator.py` that owns B1 Firestore records and accepts all dependencies explicitly. Introduce it behind a default-disabled runtime mode, prove the state machine first with a transaction-capable fake, then adopt scanner, proposal, retry, replay, pending-response, retention, and health seams in enforced offline tests. Preserve Terminal A as a downstream consumer and leave row ownership, general effect fencing, provider mutation, frontend rules, and production enablement to B2-B4.

**Tech Stack:** Python 3.14, unittest, dataclasses/enums, Firestore transaction interfaces and hermetic fakes, AST-based static inventory tests.

**Plan deliverable:** both (code and production-readiness findings)
**Approved spec:** `docs/superpowers/specs/2026-08-02-shared-exact-source-coordinator-design.md`
**Baseline:** `2b5e785bbc46754de16ca439e463793653e45f84`
**Safety boundary:** Local/offline only. No credentials, network, Graph, OpenAI, Sheets, Drive, mailbox, campaign, production data, deployment, push, or merge.
**Execution authorization update (2026-08-04):** A later explicit user
instruction authorized milestone pushes to the owned GitHub branch. It did not
authorize production enablement, deployment, provider effects, external
contact, or bypassing the B2-B4 clearance gates.

---

## File map

- Create `email_automation/source_coordinator.py`: B1 enums, typed results/errors,
  canonical hashing/alias normalization, Firestore path helpers, identity,
  classification, selection, ledger, thread-head, queue, and settlement APIs.
- Create `tests/source_coordinator_fakes.py`: deterministic Firestore document,
  transaction, clock, UUID, apply-then-raise, and barrier fakes.
- Create `tests/test_source_coordinator.py`: focused B1 state-machine tests.
- Create `tests/test_source_coordinator_inventory.py`: mutation inventory,
  forbidden direct-writer, provider-import, and mode-containment static tests.
- Create `tests/fixtures/graph_draft_delete_callers.json`: exact deferred/M2-owned
  Graph draft-cleanup caller manifest.
- Modify `email_automation/processing.py`: mode gate, source admission/recovery,
  proposal freeze/election, scanner ordering, strict settlement, and retry use.
- Modify `email_automation/messaging.py`: strict canonical marker compatibility
  APIs while preserving disabled-mode legacy behavior.
- Modify `email_automation/operator_replay.py`: canonical replay claim and
  coordinator-owned completion.
- Modify `email_automation/pending_responses.py`: exact-source pending identity
  and no-overwrite validation; no Graph authority changes.
- Modify `email_automation/system_health.py`: bounded B1 health counts.
- Modify `scheduler_runner.py`: enforced-mode duplicate-writer quarantine.
- Modify `main.py`: coordinator-authority retention exclusion.
- Modify focused tests named in the tasks below.

## Task dependency map

Tasks 1-5 are sequential because each adds authority consumed by the next task.
Task 0 is independent and may run beside documentation review. Tasks 6-9 are
sequential integration slices. Task 10 is independent verification/review after
all implementation tasks are green.

### Task 0: Freeze the deferred Graph DELETE and direct-writer inventories

**Files:**
- Create: `tests/fixtures/graph_draft_delete_callers.json`
- Create: `tests/test_source_coordinator_inventory.py`
- Read: `email_automation/email.py`
- Read: `email_automation/followup.py`
- Read: `email_automation/processing.py`
- Read: `email_automation/messaging.py`
- Read: `email_automation/operator_replay.py`
- Read: `scheduler_runner.py`

- [x] **Step 1: Write the failing Graph draft caller inventory test**

Add a test that parses application Python with `ast`, records the containing
function for each `_delete_graph_reply_draft` call, and compares the result
to this exact multiset:

```python
EXPECTED_DRAFT_DELETE_CALLERS = {
    ("email_automation/email.py", "_send_outbox_as_reply"): 2,
    ("email_automation/email.py", "send_and_index_email"): 1,
    ("email_automation/followup.py", "_send_followup_email"): 13,
    ("email_automation/processing.py", "send_reply_in_thread"): 5,
}
```

The first test asserts that
`tests/fixtures/graph_draft_delete_callers.json` exists before attempting to
load it, then returns from that test when absent. This makes the initial RED a
discriminating `AssertionError`, not an import/file error.

The test must separately assert deferred count `3 + 13 == 16`, M2-owned count
`5`, total production callers `21`, and exactly one `requests.delete` call
implementation inside `_delete_graph_reply_draft`.

- [x] **Step 2: Run the inventory test and verify RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator_inventory -v
```

Expected: FAIL with `graph draft delete caller manifest is missing`; no import
or file-read error is accepted as RED.

- [x] **Step 3: Add the exact JSON manifest and AST scanner**

The fixture schema is:

```json
{
  "schemaVersion": 1,
  "deferred": [
    {"path": "email_automation/email.py", "function": "_send_outbox_as_reply", "count": 2},
    {"path": "email_automation/email.py", "function": "send_and_index_email", "count": 1},
    {"path": "email_automation/followup.py", "function": "_send_followup_email", "count": 13}
  ],
  "m2Owned": [
    {"path": "email_automation/processing.py", "function": "send_reply_in_thread", "count": 5}
  ]
}
```

Implement an AST visitor that tracks the current `FunctionDef`/
`AsyncFunctionDef`, counts only direct calls whose function name or attribute is
`_delete_graph_reply_draft`, and separately counts `requests.delete` in the
helper. Do not count tests, vendored files, wrappers, or string tokens.

- [x] **Step 4: Add the initial direct-writer inventory test**

Assert the pre-B1 inventory contains these authority writers/readers so later
tasks must deliberately remove or quarantine them:

```python
EXPECTED_LEGACY_MARKER_SYMBOLS = {
    "email_automation/messaging.py": {"has_processed", "mark_processed"},
    "scheduler_runner.py": {"has_processed", "mark_processed"},
    "email_automation/operator_replay.py": {"_begin_replay_claim", "_complete_replay_claim"},
}
```

The inventory is a baseline assertion, not an allowlist that authorizes those
writers after enforced adoption.

- [x] **Step 5: Run GREEN and commit**

Run the command from Step 2. Expected: all inventory tests pass with deferred
`16`, M2-owned `5`, total `21`.

Commit:

```bash
git add tests/fixtures/graph_draft_delete_callers.json \
  tests/test_source_coordinator_inventory.py
git commit -m "test: freeze B1 mutation inventories"
```

### Task 1: Add mode containment and pure B1 contracts

**Files:**
- Create: `email_automation/source_coordinator.py`
- Create: `tests/test_source_coordinator.py`
- Test: `tests/test_source_coordinator_inventory.py`

- [x] **Step 1: Write failing mode, hash, and alias tests**

Use `importlib.util.find_spec("email_automation.source_coordinator")` in a test
helper, assert the spec is not `None`, and return before dynamic import when it
is absent. Once present, dynamically import and test this public API:

```python
from email_automation.source_coordinator import (
    CoordinatorMode,
    SourceAlias,
    SourceCoordinatorConfigError,
    canonical_json_hash,
    normalize_source_alias,
    resolve_source_coordinator_mode,
    source_alias_key,
)
```

Required assertions:

```python
def test_mode_defaults_disabled_and_unknown_fails_disabled():
    assert resolve_source_coordinator_mode({}) is CoordinatorMode.DISABLED
    assert resolve_source_coordinator_mode({"SITESIFT_SOURCE_COORDINATOR_MODE": "shadow"}) is CoordinatorMode.SHADOW
    assert resolve_source_coordinator_mode({"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"}) is CoordinatorMode.ENFORCED
    assert resolve_source_coordinator_mode({"SITESIFT_SOURCE_COORDINATOR_MODE": "typo"}) is CoordinatorMode.DISABLED

def test_alias_normalization_preserves_opaque_case():
    assert normalize_source_alias("graph", "  AbC+/=  ").value == "AbC+/="
    assert normalize_source_alias("internet_message_id", " <<Case@Example.TEST>> ").value == "Case@Example.TEST"

def test_canonical_hash_rejects_nonfinite_or_mutable_values():
    with self.assertRaises(SourceCoordinatorConfigError):
        canonical_json_hash({"value": float("nan")})
```

Also prove empty, control-character, non-string, unknown-type, and aliases over
`MAX_SOURCE_ALIAS_BYTES=1024` are rejected and a full SHA-256 alias key changes
across user/type/value.

In `tests/test_source_coordinator_inventory.py`, write the static provider-import
gate now, before the production module exists. It first asserts that
`source_coordinator.py` exists and returns from that test when absent, producing
the same explicit assertion RED. Once present, parse imports and reject:

```python
FORBIDDEN_ROOTS = {"requests", "openai", "googleapiclient"}
FORBIDDEN_RELATIVE = {"email", "sheets", "sheet_operations", "file_handling", "ai_processing"}
```

- [x] **Step 2: Run RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_source_coordinator_inventory -v
```

Expected: FAIL with `source coordinator module is missing`; a
`ModuleNotFoundError` is an instrument error and must be corrected before
implementation.

- [x] **Step 3: Implement the minimal pure contracts**

Create these exact types and functions:

```python
class CoordinatorMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"

class SourceCoordinatorError(RuntimeError):
    code = "source_coordinator_error"

class SourceCoordinatorRetryable(SourceCoordinatorError):
    code = "source_coordinator_retryable"

class SourceCoordinatorAmbiguous(SourceCoordinatorError):
    code = "source_coordinator_ambiguous"

class SourceCoordinatorConflict(SourceCoordinatorError):
    code = "source_coordinator_conflict"

class SourceCoordinatorConfigError(SourceCoordinatorError):
    code = "source_coordinator_config"

@dataclass(frozen=True)
class SourceAlias:
    alias_type: str
    value: str
    key: str = ""

```

Add the exact functions `resolve_source_coordinator_mode(environ: Mapping[str,
str]) -> CoordinatorMode`, `canonical_json_hash(value: Any) -> str`,
`normalize_source_alias(alias_type: str, value: str) -> SourceAlias`, and
`source_alias_key(user_id: str, alias: SourceAlias) -> str` with the smallest
implementation that satisfies the tests. `canonical_json_hash` must use
`json.dumps(value, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("utf-8")` and full SHA-256.
Do not import `requests`, Graph, OpenAI, Sheets, Drive, or application effect
modules.

- [x] **Step 4: Verify the prewritten static zero-provider-import gate**

Run the inventory test alone and confirm the now-present module passes the
prewritten AST assertions. Firestore types and `SERVER_TIMESTAMP` are allowed;
network/effect clients are not. Do not add or weaken a test after observing the
implementation.

- [x] **Step 5: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_source_coordinator_inventory -v
```

Expected: all tests pass.

Commit:

```bash
git add email_automation/source_coordinator.py \
  tests/test_source_coordinator.py tests/test_source_coordinator_inventory.py
git commit -m "feat: add B1 coordinator contracts"
```

### Task 2: Implement alias-stable exact-source identity

**Files:**
- Create: `tests/source_coordinator_fakes.py`
- Modify: `email_automation/source_coordinator.py`
- Modify: `tests/test_source_coordinator.py`
- Modify: `tests/test_source_coordinator_inventory.py`

- [x] **Step 1: Build the deterministic transaction fake**

Implement fake document references, snapshots, transactions, and a client with:

```python
class FakeFirestore:
    def __init__(self):
        self.data = {}
        self.events = []
        self.fail_next_commit = None
        self.apply_then_raise_next_commit = None

```

Implement `FakeFirestore.collection(name)` by returning the fake collection
reference rooted at `(name,)`, and `FakeFirestore.transaction()` by returning a
new buffered fake transaction bound to the store. Transactions buffer `create`,
`set`, `update`, and `delete`, enforce create
preconditions atomically, and optionally fail before apply or after full apply.
the fake must never import production coordinator code.

- [x] **Step 2: Write identity RED tests**

Test the wished-for interface:

```python
coordinator = SourceCoordinator(
    fake_fs,
    uuid_factory=lambda: "source-0001",
    now_factory=lambda: FROZEN_NOW,
)
result = coordinator.admit_or_repair_source_identity(
    user_id="user-1",
    hydrated_message=HYDRATED_GRAPH_ENVELOPE,
    evidence_kind="graph_hydration",
    thread_id="thread-1",
)
```

Required RED cases:

- Graph-first then Graph+RFC enrichment retains `source-0001`.
- RFC-first then RFC+Graph enrichment retains `source-0001`.
- An already-bound alias wins over UUID allocation and attaches the late alias.
- A late alias is accepted only when the same coordinator-parsed hydrated
  message also supplies an already-owned Graph/RFC alias.
- Supplied aliases mapped to two owners raise `source_alias_conflict` with zero
  writes.
- Rebinding one alias to another owner fails with zero writes.
- Conversation IDs and internal thread IDs never appear in the alias namespace;
  replay cannot merge identities by conversation ID.
- Once non-empty, an identity's internal thread binding is immutable; a
  different thread raises `source_thread_conflict` with zero writes.
- A fail-before-apply commit is retryable and creates no partial identity.
- Apply-then-raise succeeds only after strict readback of identity and all alias
  paths.
- Two separately admitted disjoint aliases without overlap are never guessed
  together.
- A proof-only disjoint merge returns `source_alias_bridge_required` with zero
  writes; B1 exposes no raw-alias repair API.
- identity admission with alias 9 fails before mutation; the retained maximum is
  `MAX_SOURCE_ALIASES=8`.
- Production imports/construction of the module-private admission-envelope type
  fail the static inventory; admission API call sites are limited to the exact
  processing/replay adapters.

Write that AST gate in `tests/test_source_coordinator_inventory.py` now: reject
production imports/construction of `_SourceAdmissionEnvelope` and permit calls
to `admit_or_repair_source_identity` only from the reviewed processing/replay
adapter functions.

- [x] **Step 3: Run RED and confirm the missing API is the cause**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator.SourceIdentityTests \
  tests.test_source_coordinator_inventory -v
```

Expected: an explicit `hasattr` assertion FAIL stating that
`SourceCoordinator` or `admit_or_repair_source_identity` is absent. Do not
accept an `AttributeError` as RED.

- [x] **Step 4: Implement identity paths and strict readback**

Add:

```python
@dataclass(frozen=True)
class SourceIdentityResult:
    canonical_source_id: str
    aliases: Sequence[SourceAlias]
    created: bool
    repaired: bool

@dataclass(frozen=True)
class _SourceAdmissionEnvelope:
    aliases: Sequence[SourceAlias]
    evidence_kind: str
    evidence_hash: str

class SourceCoordinator:
```

Add `SourceCoordinator.__init__(firestore_client, *, uuid_factory, now_factory)`
and `SourceCoordinator.admit_or_repair_source_identity(*, user_id: str,
hydrated_message: Mapping[str, Any], evidence_kind: str,
thread_id: str | None) ->
SourceIdentityResult` with the behavior below.

The method canonicalizes the exact hydrated message, validates the reviewed
hydration/replay evidence kinds, extracts only typed Graph/RFC aliases, computes
its own evidence hash, and constructs `_SourceAdmissionEnvelope` internally.
No public API accepts aliases or an evidence hash.
Use the exact Firestore paths in the spec. The transaction reads every alias,
chooses zero/one/conflict owner, creates or verifies the retained identity, and
creates late alias projections only when an already-bound alias occurs in the
same trusted envelope. Disjoint proof-only repair is quarantined. It stores an
immutable non-empty internal thread binding and treats conversation/thread IDs
as routing evidence, never aliases. Catch commit errors once, perform one
strict readback, and accept only a fully matching state; otherwise raise typed
retryable/ambiguous errors. Do not retry the transaction automatically.

- [x] **Step 5: Run GREEN, invariants, and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator.SourceIdentityTests -v
```

Expected: all identity tests pass with no provider/effect events.

Commit:

```bash
git add email_automation/source_coordinator.py \
  tests/source_coordinator_fakes.py tests/test_source_coordinator.py \
  tests/test_source_coordinator_inventory.py
git commit -m "feat: add canonical source identity"
```

### Task 3: Fence classification and freeze one immutable snapshot

**Files:**
- Modify: `email_automation/source_coordinator.py`
- Modify: `tests/source_coordinator_fakes.py`
- Modify: `tests/test_source_coordinator.py`
- Modify: `tests/test_source_coordinator_inventory.py`

- [x] **Step 1: Write classification state-machine RED tests**

Exercise these APIs:

```python
claim = coordinator.claim_source_classification(
    user_id="user-1", canonical_source_id="source-0001", lease_seconds=60
)
started = coordinator.record_classification_request_started(
    user_id="user-1",
    canonical_source_id="source-0001",
    classification_epoch=claim.classification_epoch,
    classification_claim_id=claim.classification_claim_id,
    model_request_key="model-request-1",
    classification_input=CLASSIFICATION_INPUT,
)
snapshot = coordinator.persist_complete_classification_snapshot(
    user_id="user-1",
    canonical_source_id="source-0001",
    classification_epoch=claim.classification_epoch,
    classification_claim_id=claim.classification_claim_id,
    complete_proposal=COMPLETE_PROPOSAL,
    proposal_evidence=MODEL_PROPOSAL_EVIDENCE,
)
```

Required cases:

- first claim has epoch `1`, unique claim ID, positive lease, and no model start;
- verified takeover before request start increments epoch and may classify;
- request start commits before an injected classifier callback is invoked;
- the canonical classifier input hash commits with request intent; input drift
  blocks before any second callback;
- expiry after request start authorizes zero second callbacks and returns
  `classification_request_ambiguous`;
- snapshot apply-then-raise exact readback returns the same snapshot;
- a different proposal/hash returns `classification_snapshot_conflict` with
  zero writes;
- a canonical snapshot over `614400` bytes fails before mutation;
- `snapshot_ready` recovery performs zero callbacks;
- verified deterministic hard opt-out records
  `classificationInputHash` and `modelRequestState=not_applicable`, freezes its
  snapshot, and performs zero classifier/model callbacks;
- caller-supplied proposal/model evidence cannot assert verified hard opt-out;
  only the coordinator's injected verifier can select that lane;
- snapshot commit writes no owner, ledger, marker, thread, queue, or provider
  record.

Name the two-worker callback barrier exactly
`ClassificationTests.test_two_workers_call_classifier_once`.
Also add the AST/signature assertion now to
`tests/test_source_coordinator_inventory.py`: reject public construction of the
private verified-evidence type and reject `deterministic_evidence`,
`owner_kind`, or `winner` parameters on `classify_source_once`.

- [x] **Step 2: Run RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator.ClassificationTests \
  tests.test_source_coordinator_inventory -v
```

Expected: explicit `hasattr` assertion failures for the absent classification
API. Do not accept an `AttributeError` as RED.

- [x] **Step 3: Implement explicit classification types and transitions**

Add immutable result types:

```python
@dataclass(frozen=True)
class ClassificationClaim:
    canonical_source_id: str
    classification_epoch: int
    classification_claim_id: str
    lease_expires_at: datetime

@dataclass(frozen=True)
class ClassificationSnapshot:
    canonical_source_id: str
    complete_proposal: Mapping[str, Any]
    complete_proposal_hash: str
    selection_snapshot: Mapping[str, Any]
    selection_hash: str
    snapshot_immutable_hash: str
```

Implement `claim_source_classification`,
`record_classification_request_started`,
`persist_complete_classification_snapshot`, and
`persist_deterministic_classification_snapshot`, and
`require_authoritative_classification_snapshot`. Every transition verifies the
identity path, exact current epoch/claim, legal state, and immutable hashes.
`record_classification_request_started` persists the exact canonical
`classificationInputHash` and stable model request key. After
`modelRequestState=started`, no code path changes it back to `not_started`.
`persist_deterministic_classification_snapshot` accepts the classification
input but no evidence/winner argument: it invokes the coordinator's injected
pure `hard_optout_verifier`, requires its module-private verified result, hashes
the input/evidence, and atomically writes `not_applicable` plus `snapshot_ready`.
When the verifier is absent or returns no verified result, this API cannot elect
hard opt-out.

- [x] **Step 4: Implement the one-call orchestration helper**

Implement the pretested `classify_source_once` helper with explicit user ID, canonical source
ID, lease duration, immutable classification input, and classifier callback
parameters. It must never accept a winner or deterministic-evidence argument.
The coordinator constructor receives an optional injected pure
`hard_optout_verifier`; B1 production wiring leaves it absent, hermetic tests use
a reviewed fake, and B4 owns the real adapter. The helper must:

1. returns an existing strict snapshot without calling `classifier`;
2. obtains the classification claim;
3. when verified evidence requires hard opt-out, commits the deterministic
   `not_applicable -> snapshot_ready` lane without `request_started` or a model;
4. otherwise records request start and invokes `classifier()` exactly once;
5. persists the complete snapshot; and
6. surfaces ambiguity without a second invocation.

The prewritten test uses a counter callback and two simulated workers; total
callback count must be one.

- [x] **Step 5: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_source_coordinator_inventory -v
```

Expected: all Task 3 and prior coordinator tests pass.

Commit:

```bash
git add email_automation/source_coordinator.py \
  tests/source_coordinator_fakes.py tests/test_source_coordinator.py \
  tests/test_source_coordinator_inventory.py
git commit -m "feat: freeze authoritative source classification"
```

### Task 4: Elect one transition owner and materialize the source-work ledger

**Files:**
- Modify: `email_automation/source_coordinator.py`
- Modify: `tests/test_source_coordinator.py`

- [x] **Step 1: Write deterministic selection and ledger RED tests**

Create proposal permutations containing ordinary updates plus combinations of
hard opt-out, terminal, human-decision, generic reply, new property, and
informational work. Assert:

```python
selection = coordinator.elect_transition_owner_from_snapshot(
    user_id="user-1", canonical_source_id="source-0001"
)
ledger = coordinator.create_or_verify_source_work_ledger(
    user_id="user-1", canonical_source_id="source-0001"
)
```

- hard opt-out wins over terminal and human;
- terminal wins over human;
- all reviewed human candidates aggregate into one `human_decision` owner;
- ordinary-only snapshots persist an explicit `ownerKind=none` decision with a
  null `ownerKey`; record absence never means none;
- model-only/unverified opt-out normalizes to `needs_user_input` with
  `unverified_optout_review` and never elects hard opt-out;
- event order permutations produce identical selection/ledger hashes;
- caller-supplied expected owner is assertion-only and cannot self-elect;
- unknown transition-shaped work blocks selection;
- dominance outcomes match the spec table;
- ordinary field/new-property/informational work stays preserved;
- duplicate semantic work receives deterministic occurrence ordinals before
  full-hash `workKey` derivation and no item overwrites another;
- ledgers above 128 entries, 600 KiB canonical bytes, or 400 transaction writes
  fail before materialization;
- differing retry payload/hash cannot replace the owner or ledger.

- [x] **Step 2: Run RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator.SelectionAndLedgerTests -v
```

Expected: explicit `hasattr` assertion failures for missing selection/ledger
APIs, not import or attribute errors.

- [x] **Step 3: Implement pure selection normalization**

Add `build_selection_snapshot(complete_proposal: Mapping[str, Any]) ->
Mapping[str, Any]` with deterministic normalization that sorts candidates by
canonical work hash, validates the reviewed human set, applies
`contact_optout > terminal > human_decision`, and emits explicit dominance for
every work item. It stores exact version
`source-candidate-taxonomy-v1` and the normalized type sets from the spec;
allowlisted unknown informational work is preserved while unknown
transition-shaped work blocks.
It must not accept a caller-provided winner.

- [x] **Step 4: Persist owner and immutable ledger from the stored snapshot**

Implement `elect_transition_owner_from_snapshot` and
`create_or_verify_source_work_ledger`. The transaction reads only the retained
`snapshot_ready` record, persists one explicit decision including `none`, and
freezes ordered ledger entries with deterministic occurrence ordinals and
full-hash `workKey` values. Enforce the spec's entry, canonical-byte, and
transaction-write bounds before mutation. Exact retries are no-ops after strict
readback; mismatches fail closed.

- [x] **Step 5: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_source_coordinator_inventory -v
```

Expected: all coordinator and static-scope tests pass.

Commit:

```bash
git add email_automation/source_coordinator.py tests/test_source_coordinator.py
git commit -m "feat: elect source transition owner"
```

### Task 5: Add thread-head blocking, durable wake, and strict marker settlement

**Files:**
- Modify: `email_automation/source_coordinator.py`
- Modify: `tests/source_coordinator_fakes.py`
- Modify: `tests/test_source_coordinator.py`

- [x] **Step 1: Write two-worker thread-head RED barriers**

Use deterministic barriers to race two distinct sources on one thread. Assert
`claim_or_block_thread_transition` leaves exactly one active head; the
loser has one authoritative pending admission and one same-transaction blocked
projection with immutable blocker evidence; the loser has zero
processed/cursor/domain events.

Add idempotent same-source retry, conflicting-head tamper, apply-then-raise, and
queue-bound cases. Exactly 100 blocked sources are representable for a thread;
source 101 fails closed with zero partial admission/history/index/projection or
head writes.

- [x] **Step 2: Write wake-order and handoff RED tests**

Create three blocked sources with different received/sent/canonical ordering.
`release_generation_and_wake_oldest` must produce one monotonic wake token
on the oldest authoritative admission. Two workers racing
`claim_wake_and_rebind_generation` with that token leave one claimant and one
complete next-generation rebind. A claimed source that creates a new hold
settles as `settled_as_new_blocker` and rebinds all remaining records without
waking another source. Release/token creation is one atomic transaction;
compare-and-set claim/rebind/next-head creation is a second atomic transaction.
Both use the durable admission set rather than unread/age-filtered scans and
strictly read back apply-then-raise outcomes.

- [x] **Step 3: Write strict settlement RED tests**

`settle_source_markers_if_ready` must reject pending/applying ledger work,
invalid delegation, partial dominance, absent/malformed identity, unreadable
state, missing/conflicting snapshot, absent/nonmatching explicit decision,
missing/nonsettled admission, absent/conflicting required thread-head outcome,
marker fail-before-apply, and partial readback. A fully settled source
atomically creates one immutable `sourceSettlements` record plus all known
Graph/RFC alias projections with canonical source, settlement hash, and
revision. A naked legacy marker never authorizes settlement. Late alias repair
adds one matching processed projection from retained identity and canonical
settlement without reclassification or settlement mutation.

Also assert legal ledger transitions and exact completion/delegation evidence;
`applying` grants no external-effect authority.
Name the partial marker/cursor barrier exactly
`LedgerTransitionAndSettlementTests.test_marker_partial_commit_blocks_cursor`.

- [x] **Step 4: Run the new Task 5 tests RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator.ThreadHeadAndWakeTests \
  tests.test_source_coordinator.LedgerTransitionAndSettlementTests -v
```

Expected: explicit `hasattr` assertion failures naming the first absent head,
wake, ledger-transition, or settlement API. Import/attribute errors are not an
accepted RED.

- [x] **Step 5: Implement minimal head, queue, ledger, and marker APIs**

Add the exact methods `claim_or_block_thread_transition`,
`admit_pending_inbound`, `enqueue_blocked_source`,
`release_generation_and_wake_oldest`, `claim_wake_and_rebind_generation`,
`create_or_verify_deferred_work`, `record_source_work_applying`,
`complete_source_work_entry`, `delegate_source_work_entry`,
`dominate_source_work_entry_from_selection`, and
`settle_source_markers_if_ready` with
transaction implementations matching the spec. Store wake generation, token,
state, and claim ID on the authoritative pending admission. Enforce
`MAX_BLOCKED_SOURCES_PER_THREAD=100` before mutation and treat
`inboundPendingAdmissions` as authority; `blockedSources` is a projection only.
No API returns a swallowed boolean; typed results or typed exceptions are
mandatory. All apply-then-raise paths perform one strict readback and never
replay an unknown transaction.

The work-entry methods require exact source/work/ledger hashes. Completion takes
a typed versioned completion record validated for the work kind; delegation
atomically creates/verifies `sourceDeferredWork` with matching owner and payload
hash; dominance is derived only from the stored selection and accepts no caller
winner. Legal transitions are exactly those in the spec.

- [x] **Step 6: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator -v
```

Expected: identity, classification, selection, ledger, head, queue, and
settlement tests all pass.

Commit:

```bash
git add email_automation/source_coordinator.py \
  tests/source_coordinator_fakes.py tests/test_source_coordinator.py
git commit -m "feat: add B1 source queue and settlement"
```

### Task 6: Add disabled-mode containment and strict messaging compatibility

**Files:**
- Modify: `email_automation/messaging.py`
- Modify: `main.py`
- Modify: `scheduler_runner.py`
- Modify: `tests/test_source_coordinator_inventory.py`
- Modify: `tests/test_cleanup_retention.py`
- Create or modify: `tests/test_source_coordinator_integration.py`

- [x] **Step 1: Write disabled-mode zero-effect RED tests**

Patch coordinator construction to raise if called, unset the environment mode,
and run representative `has_processed`, `mark_processed`, and scanner entry
controls. Assert the exact legacy Firestore call sequence/result is unchanged
and coordinator construction count is zero. Repeat with an invalid mode value.
Under `shadow`, assert pure in-memory proposal computation is allowed but every
coordinator, marker, cursor, domain, and provider write/call counter stays zero.

- [x] **Step 2: Write enforced strict-marker and retention RED tests**

Under `enforced`, assert direct `messaging.mark_processed` and duplicate
`scheduler_runner.mark_processed` cannot authorize source completion. They must
delegate to a coordinator settlement context or raise a typed error. Assert
`main.auto_cleanup_firestore` never deletes coordinator authority collections
or B1-owned processed projections.

- [x] **Step 3: Run RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator_integration \
  tests.test_cleanup_retention tests.test_source_coordinator_inventory -v
```

Expected: enforced cases fail because legacy writers remain active.

- [x] **Step 4: Implement mode-routed compatibility**

In `messaging.py`, preserve legacy bodies behind `disabled`; in `enforced`,
require a canonical settlement context and invoke coordinator methods. In
`shadow`, compute pure alias proposals only and return a structured no-effect
disposition before marker/cursor/domain/provider work. Quarantine the scheduler
duplicate symbols in enforced mode by importing the same messaging
compatibility functions or raising before direct writes.

Change cleanup to skip any B1 authority collection and any processed projection
carrying `canonicalSourceId`/`settlementRevision`; preserve existing legacy
retention behavior for records without B1 ownership.

- [x] **Step 5: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator_integration \
  tests.test_cleanup_retention tests.test_source_coordinator_inventory \
  tests.test_rubric_core_inbox_matching_duplicate_retry -v
```

Expected: all pass in disabled, shadow, and enforced modes.

Commit:

```bash
git add email_automation/messaging.py main.py scheduler_runner.py \
  tests/test_source_coordinator_inventory.py \
  tests/test_source_coordinator_integration.py tests/test_cleanup_retention.py
git commit -m "feat: gate strict source markers"
```

### Task 7: Integrate source admission, proposal freeze, and scanner ordering

**Files:**
- Modify: `email_automation/source_coordinator.py`
- Modify: `email_automation/processing.py`
- Modify: `tests/test_processing_retryability.py`
- Modify: `tests/test_source_coordinator.py`
- Modify: `tests/test_event_processing_order.py`
- Modify: `tests/test_compound_nonviable_processing.py`
- Modify: `tests/test_source_coordinator_integration.py`

- [x] **Step 1: Write the same-thread source-loss RED**

Under enforced mode, scan two ordinary messages in one thread. Assert both have
canonical identity, classification, and ledger records; the earlier message is
not merely history-saved/marked processed; model callbacks execute once per
source unless it is durably blocked; failure of source one leaves both sources
unprocessed and enumerable.
Name this regression exactly
`SourceCoordinatorScannerTests.test_two_same_thread_sources_are_independently_settled`.

- [x] **Step 2: Write freeze-before-effects RED instrumentation**

Instrument proposal callback and every pre-election forbidden write/effect
seam: thread
status/timestamp/reactivation, client recovery, follow-up state, message-order
test writes, Sheet logging/apply, asset upload, notification/counters,
handled-event state, terminal/opt-out/human sagas, pending queue, reply/draft,
processed/read/cursor writes, and provider mutations. Inject read-only hydration
and classifier inputs; assert their canonical input hash commits before a
request-start-fenced classifier callback. Non-deterministic terminal and human
fixtures assert:

```text
identity -> classification_claimed -> classification_request_started
-> classifier -> snapshot_ready -> transition_decision
-> source_work_ledger -> required_thread_head -> downstream_consumer
```

Verified deterministic hard opt-out instead asserts:

```text
identity -> classification_claimed -> model_not_applicable -> snapshot_ready
-> transition_decision -> source_work_ledger -> required_thread_head
-> downstream_consumer
```

Its classifier callback count is exactly zero.
Name the ordering tripwires under `SourceCoordinatorAuthorityOrderTests` in
`tests/test_source_coordinator_integration.py`.

No forbidden event may occur before strict snapshot/explicit-decision/ledger
and required-head readback. Before that barrier, only identity/aliases,
immutable history/index, pending admission/block projection, classification
state, and source-bound failure visibility may write. Losing families have zero
events.

- [x] **Step 3: Run RED**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest \
  tests.test_source_coordinator_integration.SourceCoordinatorScannerTests \
  tests.test_source_coordinator_integration.SourceCoordinatorAuthorityOrderTests -v
```

Expected: earlier source is still marked processed and Sheet/event work still
precedes snapshot authority.

- [x] **Step 4: Split `process_inbox_message` at the proposal seam**

Add a small context object:

```python
@dataclass(frozen=True)
class SourceProcessingAuthority:
    canonical_source_id: str
    snapshot_hash: str
    selection_hash: str
    owner_kind: str
    owner_key: str | None
    ledger_hash: str
```

In enforced mode:

1. admit identity after exact message hydration;
2. recover snapshot/explicit decision/ledger when present;
3. otherwise acquire inputs read-only, canonicalize/hash them, and call the
   classifier only through `classify_source_once`;
4. elect/materialize and claim any required thread head before every forbidden
   business-state or provider seam;
5. dispatch terminal only when persisted owner is terminal;
6. durably block opt-out/human winners until their B4 adapters exist;
7. settle only through coordinator evidence.

Disabled mode must retain the original path. Do not change Terminal A Sheet
attempt or execution fencing. Move existing early thread, follow-up,
message-order, Sheet-log, and related writes behind the authority barrier in
enforced mode rather than treating them as harmless preparation. Split
`fetch_and_log_sheet_for_thread` so its read-only input acquisition is distinct
from logging/mutation; the latter remains behind the barrier.

- [x] **Step 5: Replace scanner last-message batching**

In enforced mode, admit/process each message oldest-first. If a head blocks a
source, persist pending/history/index/blocked evidence and continue only as the
queue contract permits. Remove every enforced path that marks an earlier source
processed solely because it was saved for history. Stop same-thread advancement
after the first retryable/ambiguous source failure.

- [x] **Step 6: Run GREEN, adjacent terminal tests, and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator_integration \
  tests.test_processing_retryability tests.test_event_processing_order \
  tests.test_compound_nonviable_processing -v
```

Expected: all pass; provider callbacks remain mocked/local.

Commit:

```bash
git add email_automation/source_coordinator.py email_automation/processing.py \
  tests/test_source_coordinator.py \
  tests/test_source_coordinator_integration.py \
  tests/test_processing_retryability.py tests/test_event_processing_order.py \
  tests/test_compound_nonviable_processing.py
git commit -m "feat: enforce exact-source inbox admission"
```

### Task 8: Route retry, operator replay, and pending responses through B1

**Files:**
- Modify: `email_automation/source_coordinator.py`
- Modify: `email_automation/processing.py`
- Modify: `email_automation/operator_replay.py`
- Modify: `email_automation/pending_responses.py`
- Modify: `tests/test_operator_message_replay.py`
- Modify: `tests/test_pending_responses.py`
- Modify: `tests/test_processing_retryability.py`
- Modify: `tests/test_source_coordinator.py`
- Modify: `tests/test_source_coordinator_inventory.py`

- [x] **Step 1: Write retry/replay ownership RED tests**

Under enforced mode prove failure retry and operator replay:

- resolve the existing canonical source from Graph/RFC aliases;
- cannot create separate processed claims per alias;
- cannot call the classifier after `snapshot_ready` or `request_started`;
- cannot replace a differing snapshot/owner/ledger;
- delegate completion to `settle_source_markers_if_ready`;
- refuse conflicting aliases before processing/domain effects;
- pass active or settled Terminal A only through the validated
  retained-authority loader bound to `_terminal_retry_disposition`, and perform
  no fresh classification;
- create `legacy_terminal_quarantined` with exact retained evidence hashes but
  no fabricated B1 snapshot, decision, ledger, or settlement;
- return `legacy_terminal_authority_retained` with zero marker/cursor/domain
  effects, and reject conflicting retained evidence with zero writes;
- quarantine in-progress legacy replay claims until reconciled; and
- block on legacy marker-only evidence instead of fabricating a canonical
  settlement.

Write the final direct-writer/evidence-boundary AST tests now, before production
edits: enforced scanner/retry/replay/pending calls cannot invoke legacy marker
writers; public construction/import of private retained-terminal evidence is
forbidden; `quarantine_retained_terminal_authority` cannot accept evidence/hash
arguments; and production loader wiring must be the exact
`_terminal_retry_disposition` adapter.

- [x] **Step 2: Write pending-response source-binding RED tests**

Create pending work for source A, then attempt source B on the same thread-keyed
legacy path. Assert B cannot overwrite A, send A's body, or become A's ledger
completion. Exact A retry requires `canonicalSourceId`, proposal/selection
hashes, and `workKey`. Existing M2 Graph permit behavior remains unchanged.
Exact enqueue/require/claim/clear operations verify canonical source and
`workKey`; clearing A cannot delete, settle, or alter B.

- [x] **Step 3: Run RED**

Run coordinator, operator replay, pending response, and retry test modules.
Expected: retained-authority API, direct-marker, and thread-key overwrite
assertions fail.

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_operator_message_replay tests.test_pending_responses \
  tests.test_processing_retryability tests.test_source_coordinator_inventory -v
```

- [x] **Step 4: Implement coordinator-routed recovery**

Add canonical authority parameters to internal retry/replay calls. Replace
`_begin_replay_claim`/`_complete_replay_claim` direct processed ownership in
enforced mode with canonical admission/settlement. Preserve disabled legacy
behavior. Extend pending schema validation with exact B1 binding fields and
reject absent/mismatched fields before Graph work; add exact-source/work-key
clear semantics.

Implement these exact enforced APIs in `pending_responses.py`:

```python
queue_pending_response(..., canonical_source_id: str, work_key: str,
                       proposal_hash: str, selection_hash: str)
require_pending_response_exact(user_id: str, thread_id: str,
                               canonical_source_id: str, work_key: str)
claim_pending_response_for_send_exact(user_id: str, thread_id: str,
                                      canonical_source_id: str, work_key: str,
                                      expected_revision: int)
clear_pending_response_exact(user_id: str, thread_id: str,
                             canonical_source_id: str, work_key: str,
                             expected_revision: int) -> PendingResponseClearResult
```

Every method returns a typed record/result or raises a typed conflict/retryable
error. The legacy `clear_pending_response(user_id, thread_id) -> bool` remains
callable only in disabled compatibility mode; enforced mode raises before any
read/write and all adopted callers use `clear_pending_response_exact`.

Add `SourceCoordinator.quarantine_retained_terminal_authority(...)` with exact
user, canonical source, thread, Graph ID, and RFC ID parameters—but no evidence
record/hash parameter. Extend the coordinator constructor with an injected
`retained_terminal_authority_loader`; production enforced wiring binds only the
reviewed processing adapter over `_terminal_retry_disposition`, while tests use
a strict fake. The method invokes that loader, internally wraps its validated
result in a module-private evidence type, verifies it against source identity,
writes the create-only classification quarantine and hashes, and returns a
structured retained-authority disposition. Exact replay is idempotent; drift is
a conflict. B1 exposes the
`legacy_terminal_quarantined -> migrated_b1 | retained_terminal_complete`
lifecycle to health but does not implement a resolver. Add equivalent
operator-visible dispositions for legacy marker-only ambiguity and in-progress
replay quarantine. Do not alter B3 send permits.

- [x] **Step 5: Verify the prewritten direct-writer inventory**

Run the inventory assertions written in Step 1. Fix only the production call
sites they identify; do not relax the exact disabled-compatibility or trusted
loader boundaries.

- [x] **Step 6: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_source_coordinator \
  tests.test_operator_message_replay tests.test_pending_responses \
  tests.test_processing_retryability tests.test_source_coordinator_inventory -v
```

Expected: all pass.

Commit:

```bash
git add email_automation/source_coordinator.py email_automation/processing.py \
  tests/test_source_coordinator.py \
  email_automation/operator_replay.py \
  email_automation/pending_responses.py tests/test_operator_message_replay.py \
  tests/test_pending_responses.py tests/test_processing_retryability.py \
  tests/test_source_coordinator_inventory.py
git commit -m "feat: route source recovery through B1"
```

### Task 9: Expose bounded B1 health and complete static adoption gates

**Files:**
- Modify: `email_automation/system_health.py`
- Modify: `tests/test_system_health.py`
- Modify: `tests/test_source_coordinator_inventory.py`
- Modify: `tests/test_source_coordinator_integration.py`

- [x] **Step 1: Write health RED tests**

Add bounded count tests for active/ambiguous classification, blocked sources,
nonsettled pending admissions, unsettled ledgers, alias conflicts, and marker
or canonical-settlement ambiguity. Count `legacy_terminal_quarantined`, legacy
marker-only ambiguity, and in-progress replay quarantine separately.
Missing/unreadable/malformed/over-500 scans must set the established health
fail-closed/error shape rather than report zero.

Before production health edits, also add the final static closure assertions:

- `source_coordinator.py` has no forbidden effect imports/calls;
- all ten B1 collections appear only in coordinator path helpers, health reads,
  rules fixtures/tests, and explicit compatibility checks;
- scanner, retry, replay, pending, and settlement lanes contain no direct
  processed writes; handled writes are limited to an exact enumerated
  post-barrier M2 Terminal A compatibility manifest and are never read as B1
  decision/settlement authority;
- private admission/hard-opt-out/retained-terminal evidence construction and
  dependency wiring match the Task 2/3/8 gates;
- the Graph draft DELETE manifest remains deferred `16`, M2-owned `5`;
- no runtime default enables enforced mode; and
- no B2 `rowBindings`/stable-row owner or B3 general execution claim was added.

- [x] **Step 2: Run RED**

Run `tests.test_system_health`, coordinator integration health nodes, and
`tests.test_source_coordinator_inventory`. Expected: B1 health fields are absent
or an exact closure assertion identifies an unadopted writer; an import/error is
not an accepted RED.

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_system_health \
  tests.test_source_coordinator_integration \
  tests.test_source_coordinator_inventory -v
```

- [x] **Step 3: Implement bounded health projections**

Use server-filtered queries where the fake/runtime supports them and a hard
`HEALTH_SCAN_LIMIT = 500`. Count only unresolved B1 states. Never include raw
aliases, proposal bodies, addresses, recipients, or customer data in health
payloads/logs.

- [x] **Step 4: Resolve the prewritten static closure gates**

Run the inventory test alone, fix only concrete production call sites or scoped
manifests identified by its prewritten assertions, and do not weaken the gates
to make the implementation pass.

- [x] **Step 5: Run GREEN and commit**

Run:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest tests.test_system_health \
  tests.test_source_coordinator_inventory \
  tests.test_source_coordinator_integration -v
```

Expected: all pass.

Commit:

```bash
git add email_automation/system_health.py tests/test_system_health.py \
  tests/test_source_coordinator_inventory.py \
  tests/test_source_coordinator_integration.py
git commit -m "feat: expose B1 coordinator health"
```

### Task 10: Verify, independently review, and freeze the B1 candidate

**Files:**
- Modify: this plan only to record exact evidence after verification
- Create: `docs/superpowers/evidence/2026-08-03-shared-exact-source-coordinator-b1.md`
- Read: approved spec and every changed file

- [x] **Step 1: Run the complete B1 focused suite**

Run all new coordinator, inventory, integration, retry, replay, pending,
retention, and health modules in one import-order command with credentials
unset, empty OpenAI key, and unreachable local Firestore emulator address.
Expected: exit `0`, exact test count recorded from output, zero network/provider
calls.

```bash
env -u GOOGLE_APPLICATION_CREDENTIALS \
  OPENAI_API_KEY= FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
  /Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest \
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
```

- [x] **Step 2: Run retained M2 regression suites**

Run the 23-module changed-surface suite used to checkpoint M2, including
compound terminal, send permits, immutable Graph identity, pending APIs,
completion obligations, retryability, and system health. Expected: exit `0`.

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest \
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
```

- [x] **Step 3: Compile every changed Python file and inspect the diff**

Run:

```bash
{ git diff --name-only --diff-filter=ACMR 2b5e785 -- '*.py'; \
  git ls-files --others --exclude-standard -- '*.py'; } | \
  LC_ALL=C sort -u | xargs \
  /Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m py_compile
git diff --check 2b5e785
```

Expected: both commands exit `0` with no output.

- [x] **Step 4: Prove the original source-loss symptom is fixed**

Cross-reference the captured Task 7 RED for the exact two-message source-loss
test, then run these named barriers GREEN on the candidate:

```bash
/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/.venv/bin/python \
  -m unittest \
  tests.test_source_coordinator_integration.SourceCoordinatorScannerTests.test_two_same_thread_sources_are_independently_settled \
  tests.test_source_coordinator.LedgerTransitionAndSettlementTests.test_marker_partial_commit_blocks_cursor \
  tests.test_source_coordinator.ClassificationTests.test_two_workers_call_classifier_once -v
```

The evidence file records the Task 7 RED assertion text/commit and the Task 10
GREEN output. Never modify production code merely to manufacture RED.

- [x] **Step 5: Request independent spec-compliance review**

Give a fresh reviewer only the approved spec, this plan, baseline SHA, head SHA,
and diff. Resolve every Critical/Important finding, rerun affected tests, and
request re-review until approved.

- [x] **Step 6: Request independent code-quality/security review**

After spec approval, give a separate fresh reviewer the same immutable diff and
test evidence. Resolve every Critical/Important finding and rerun the affected
and full focused suites. Do not proceed with an open Important issue.

- [x] **Step 7: Freeze evidence and leave production NO-GO**

Record head SHA, sorted changed-file aggregate, commands/counts/durations,
compile/diff results, review findings, and zero-effect boundary in this plan and
`docs/superpowers/evidence/2026-08-03-shared-exact-source-coordinator-b1.md`.
Do not push, merge, deploy, enable enforced mode,
call providers, send mail, create campaigns, access production data, or claim
M3/production clearance. B1 completion advances the board only to B2 planning.

Record the candidate head and reproducible sorted-file aggregate with:

```bash
git rev-parse HEAD
git diff --name-only --diff-filter=ACMR 2b5e785 | LC_ALL=C sort | \
  while IFS= read -r path; do shasum -a 256 "$path"; done | shasum -a 256
```

- [x] **Step 8: Commit the local verification record**

Commit the plan/evidence record only after Steps 1-7 are complete:

```bash
git add docs/superpowers/plans/2026-08-03-shared-exact-source-coordinator-b1.md \
  docs/superpowers/evidence/2026-08-03-shared-exact-source-coordinator-b1.md
git commit -m "docs: freeze B1 verification evidence"
```

The evidence records the verified code head before this documentation-only
commit; report both SHAs in the final handoff.

## Execution record — 2026-08-04

- Verified code head:
  `a3fcdf51a9b721b4b61be857476942498a292495`
- Remote branch readback: exact match on
  `codex/sitesift-m3-b1-source-authority-20260803`
- Sorted changed-file aggregate:
  `018fb05dd4bec033075c7cf9d70bf65aced9abd9f93ee21e79308d9e2b6ec5fe`
- Complete focused suite: 606/606 in 23.779 seconds under offline
  containment.
- Retained M2 suite: 669/669 in 22.101 seconds under offline containment.
- Source-loss/concurrency barriers: 3/3 in 0.053 seconds.
- Changed-Python compile and baseline diff check: clean.
- Independent spec review: APPROVED, no open Critical/Important.
- Independent code-quality/security review: APPROVED, no open
  Critical/Important.
- GitHub Actions query: no workflow run exists for the branch; local gates are
  the recorded authority.
- Production status: **NO-GO**. Enforced mode, deployment, campaigns,
  providers, and production data were not touched. B1 advances the board to
  B2 only.

Full commands, evidence limitations, resolved findings, and the zero-effect
boundary are frozen in
`docs/superpowers/evidence/2026-08-03-shared-exact-source-coordinator-b1.md`.

## Plan self-review checklist

- [x] Every B1 spec invariant maps to at least one task and test.
- [x] B2 row ownership, B3 general effect fencing, and B4 real cutover/rules are
  absent from implementation tasks.
- [x] Every production-code step has a preceding verified RED.
- [x] Disabled mode preserves the baseline behavior and is the runtime default.
- [x] No task authorizes automatic Graph draft DELETE.
- [x] No unresolved marker language or unspecified error handling remains.
- [x] Exact file paths, public symbols, commands, expected outcomes, and commits
  are present for every task.
