# CE-Q1 Conversation and Extraction Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the effect-free CE-Q1A qualification instrument, execute it against the reviewed `b400ee5` product baseline, and record an honest structured baseline finding without repairing product behavior.

**Architecture:** A host supervisor creates capability-separated SUT, scorer, mutation, audit-proxy, and Firestore-emulator processes. The SUT receives only a generated product-source projection, one minimal descriptor, a synthetic input bundle, and a hash-bound frozen response bundle; it never receives the oracle. L1 drives real deterministic extraction seams, L2 drives the real `process_inbox_message()` orchestration with strict in-memory adapters, and L3 replaces only Firestore with a task-owned loopback emulator behind a namespace wrapper and an independently reconciled gRPC audit proxy.

**Tech Stack:** Python 3.14 test interpreter, `unittest`/`pytest`, standard-library JSON/dataclasses/hashing/process control, PyMuPDF/pdfplumber for native PDFs, `google-cloud-firestore` and `grpcio` already present through `requirements.lock`, macOS `/usr/bin/sandbox-exec`, pinned OpenJDK 25.0.2, and cached Firestore emulator 1.19.8.

**Deliverable:** both

**Approved specification:** `docs/superpowers/specs/2026-08-13-ceq1-conversation-extraction-qualification-design.md`

**Production-source ancestor:** `6caa8ec14cc525299cfb8ed13bdd219f35c4322b`

**Implementation base:** `b400ee5ad55ac75203da6a53730c4a134cad79e5`

---

## Scope and stop line

This plan builds the qualification instrument and records what the current product does. It must not change any file under `email_automation/`, `main.py`, `service.py`, `scheduler_runner.py`, or `app.py`. In particular, this plan does **not** add durable decline memory, per-fact product provenance, suite identity, shared voice finalizers, or the paused-send terminal outcome. Those are separate product TDD changes only after CE-Q1 reproduces them.

The implementation and every command in this plan are offline. Do not run `scripts/standalone.py`, `scripts/e2e.py`, `scripts/campaign_lifecycle.py`, `tests/outlook_helper.py`, `tests/e2e_helpers.py`, `/process-user`, a scheduler, a mailbox helper, or any provider-backed benchmark. Do not read or mutate production, campaign switches, mailboxes, Graph, Google Sheets, OpenAI, Drive, Cloud Tasks, or any external endpoint. Do not push, merge, deploy, create a campaign, create an outbox item, or send/draft mail.

The expected initial outcome is a trustworthy instrument with a product verdict of `FAIL` and diagnostic `UNVERIFIED` records. A truthful red product finding is completion for this plan; changing fixtures, oracles, or scorers to make the baseline green is forbidden.

## Existing production seams that must remain real

| Claim | Real symbol | Harness rule |
| --- | --- | --- |
| Ten-message history | `email_automation.messaging.build_conversation_payload(..., limit=10)` | Seed strict Firestore/Graph adapters; never inject richer history into a history-qualified case |
| Frozen proposal path | `email_automation.ai_processing.propose_sheet_updates()` | Replace the entire `ai_processing.client` alias with an identity-pinned queue client; `dry_run=True` still invokes that queue |
| Deterministic guards | post-processors inside `propose_sheet_updates()` | Do not duplicate their logic in CE-Q1 |
| Native PDF parser | `email_automation.file_handling.process_pdf_for_ai()` | Feed generated local bytes; never call Drive/OpenAI fallback |
| Sheet write planning | `email_automation.ai_processing.apply_proposal_to_sheet()` | Bind strict Sheets adapter; preserve real guards, AI_META behavior, formula refresh, and returned snapshots |
| Pipeline authority | `email_automation.processing.process_inbox_message()` | Required scenarios enter here with `allow_outbound_reply=True` |
| Final body selection | `email_automation.processing._select_automatic_response_body()` | Grade the selected body, not convenient model prose |
| Mail chokepoint | `email_automation.processing.send_reply_in_thread()` | Leave real; `SITESIFT_OUTBOUND_MODE=paused` must stop it at entry |
| Known paused defect | `processing._queue_response_retry_or_reconciliation()` | Bind only `processing.queue_pending_response` to a strict recorder and report the one fallthrough as baseline `FAIL` |

Important import detail: `processing.py` and `ai_processing.py` import dependencies by value. Runtime binding must replace each exact module alias used by the real function, then verify identity at exit. Accessing `ai_processing.client.responses` before replacing `ai_processing.client` constructs the lazy OpenAI provider and is a hard instrument failure.

## Planned file structure

| Path | Responsibility |
| --- | --- |
| `.gitignore` | Ignore task-owned runtime/quarantine state only |
| `docs/release-safety/ceq1-execution-manifest.json` | Public scenario registry and input/response/owner hashes; no oracle or expected verdict |
| `docs/release-safety/evidence/ceq1/README.md` | Evidence semantics and non-claims |
| `docs/release-safety/evidence/ceq1/baseline-report.json` | Final sanitized machine-readable baseline finding |
| `docs/release-safety/evidence/ceq1/baseline-report.md` | Final sanitized operator summary |
| `tests/fixtures/ceq1/inputs/` | Synthetic runtime bundles plus generation-provenance declaration |
| `tests/fixtures/ceq1/responses/` | Hash-addressed frozen model response bundles |
| `tests/fixtures/ceq1/oracles/` | Sealed expected records and `coverage-contract.json` |
| `tests/ceq1/contracts.py` | Closed schemas, canonical JSON/hashes, statuses, verdict precedence |
| `tests/ceq1/manifest.py` | Closed manifest/coverage validation and owner-hash verification |
| `tests/ceq1/privacy.py` | Mechanical privacy/credential scanner and provenance validation |
| `tests/ceq1/mutator.py` | Result-schema mutations without product code or oracle access |
| `tests/ceq1/scorer.py` | Oracle-side exact comparison and failure reasons; no product imports |
| `tests/ceq1/guards.py` | Execution-long constructor/network/process/file/effect tripwire |
| `tests/ceq1/frozen_provider.py` | Strict queue-backed `responses.create` replacement |
| `tests/ceq1/adapters.py` | Closed in-memory Firestore, Sheets, Graph, PDF, pending-response, and effect ledgers |
| `tests/ceq1/runtime_bindings.py` | Identity-pinned binding/restoration of exact imported aliases |
| `tests/ceq1/harness.py` | SUT child coordinator over real product seams |
| `tests/ceq1/firestore_audit_proxy.py` | Separate loopback gRPC proxy and transport audit ledger |
| `tests/ceq1/firestore_emulator.py` | Pinned-Java/JAR lifecycle and namespace-enforcing Firestore wrapper |
| `tests/ceq1/supervisor.py` | Preflight, source projection, sandbox profiles, children, cleanup, report assembly |
| `tests/ceq1/sut_worker.py` | SUT-only child entrypoint; installs guards before projected product imports |
| `tests/ceq1/score_worker.py` | Product-free scorer child entrypoint |
| `tests/ceq1/mutation_worker.py` | Oracle-free/product-free mutation child entrypoint |
| `tests/ceq1/fixture_builder.py` | Deterministically renders authored native PDF bytes and verifies fixture hashes |
| `tests/test_ceq1_manifest.py` | Contracts, closure, privacy, capability separation, calibration, dependency direction |
| `tests/test_ceq1_sandbox.py` | Filesystem/network/process capability and cleanup proof |
| `tests/test_ceq1_semantic_replay.py` | L1 replay and exact semantic scoring |
| `tests/test_ceq1_stateful_replay.py` | L2 real-entrypoint state/effect/replay scoring |
| `tests/test_ceq1_emulator_replay.py` | L3 preflight, namespace, transport audit, persistence, interruption, cleanup |
| `scripts/run_ceq1.py` | Thin CLI over the host supervisor |

Do not create a production package for CE-Q1. No production module may import `tests.ceq1`, `scripts.run_ceq1`, or `tests/fixtures/ceq1`.

## Canonical local test environment

Run every Python command from the worktree root. Use the already-installed isolated interpreter:

```bash
CEQ_PY=/Users/baylorharrison/.config/superpowers/worktrees/EmailAutomation/backend-lazy-init-implementation-20260813/.venv/bin/python
```

For direct pytest commands, remove ambient credentials and disable third-party plugin loading:

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    -u FIREBASE_API_KEY \
    -u GOOGLE_OAUTH_CLIENT_ID \
    -u GOOGLE_OAUTH_CLIENT_SECRET \
    -u GOOGLE_REFRESH_TOKEN \
    -u CLOUDSDK_CONFIG \
    -u GMAIL_ADDRESS \
    -u GMAIL_APP_PASSWORD \
    E2E_TEST_MODE=true \
    SITESIFT_OUTBOUND_MODE=paused \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    "$CEQ_PY" -m pytest -q -p no:cacheprovider <test-paths>
```

The host supervisor must build an even smaller child environment from an allowlist; it must not copy the host environment.

### Task 1: Freeze the qualification-only dependency boundary

**Files:**
- Create: `tests/ceq1/__init__.py`
- Create: `tests/test_ceq1_manifest.py`
- Create: `docs/release-safety/evidence/ceq1/README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing one-way dependency and runtime-artifact tests**

Add tests that scan `email_automation/**/*.py`, `main.py`, `service.py`, `scheduler_runner.py`, and `app.py` with `ast`. They must reject imports whose module begins with `tests.ceq1` or `scripts.run_ceq1`, and reject literal references to `tests/fixtures/ceq1`. Add a second assertion that `.ceq1-runtime/` is ignored while committed evidence files are not ignored.

```python
class Ceq1DependencyDirectionTests(unittest.TestCase):
    def test_production_never_imports_qualification_code(self):
        forbidden = ("tests.ceq1", "scripts.run_ceq1")
        violations = scan_production_imports(REPO_ROOT, forbidden)
        self.assertEqual([], violations)

    def test_runtime_quarantine_is_ignored_but_evidence_is_versioned(self):
        ignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".ceq1-runtime/", ignore_text.splitlines())
        self.assertNotIn("docs/release-safety/evidence/ceq1/", ignore_text.splitlines())
```

- [ ] **Step 2: Run the test to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: FAIL because `tests.ceq1` and `.ceq1-runtime/` do not exist.

- [ ] **Step 3: Add the minimal package marker, ignore rule, and evidence contract**

`tests/ceq1/__init__.py` contains only a module docstring. Append exactly `.ceq1-runtime/` to `.gitignore`. The evidence README must state:

- CE-Q1A is offline deterministic evidence only;
- `baseline-report.*` never certifies production, a model, a mailbox, delivery, Google Sheets persistence, or cross-store atomicity;
- runtime quarantine stays under `.ceq1-runtime/` and is never committed;
- any relevant owner-module or fixture hash change invalidates the report.

Implement `scan_production_imports()` in the test itself for this bootstrap task; move no code into production.

- [ ] **Step 4: Run the test to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: PASS and no provider/network output.

- [ ] **Step 5: Commit the boundary**

```bash
git add .gitignore tests/ceq1/__init__.py tests/test_ceq1_manifest.py docs/release-safety/evidence/ceq1/README.md
git commit -m "test: establish CE-Q1 qualification boundary"
```

### Task 2: Add closed records, canonical hashes, and verdict precedence

**Files:**
- Create: `tests/ceq1/contracts.py`
- Modify: `tests/test_ceq1_manifest.py`

- [ ] **Step 1: Write failing contract tests**

Test all five gate verdicts, all three evidence layers, canonical hashing independent of dictionary insertion order, rejection of non-finite numbers, rejection of extra keys, stable state digests, and exact verdict precedence.

```python
def test_gate_verdict_precedence_is_closed(self):
    self.assertEqual(GateVerdict.BLOCKED, classify_gate(prerequisite_missing=True))
    self.assertEqual(
        GateVerdict.INSTRUMENT_FAILURE,
        classify_gate(instrument_faults=["guard_identity"], required_refutations=["wrong_value"]),
    )
    self.assertEqual(
        GateVerdict.FAIL,
        classify_gate(required_refutations=["wrong_value"], missing_required_evidence=["fact_provenance"]),
    )
    self.assertEqual(
        GateVerdict.UNVERIFIED,
        classify_gate(missing_required_evidence=["fact_provenance"]),
    )
    self.assertEqual(GateVerdict.PASS_OFFLINE, classify_gate())
```

Add a test proving a diagnostic `UNVERIFIED` record does not downgrade an otherwise green hard gate and remains present in `nextGateEligibility`.

- [ ] **Step 2: Run the new tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: FAIL with `ModuleNotFoundError: tests.ceq1.contracts`.

- [ ] **Step 3: Implement the minimal closed contracts**

Define string enums `Layer(L1, L2, L3)`, `GateVerdict(BLOCKED, INSTRUMENT_FAILURE, FAIL, UNVERIFIED, PASS_OFFLINE)`, and `EvidenceResult(VERIFIED, REFUTED, UNVERIFIED)`. Implement:

```python
def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
```

Use dataclasses for `EffectAttempt`, `FactRecord`, `StateSnapshot`, `ExecutionResult`, `ScoreRecord`, and `GateReport`. Each `from_mapping()` must compare `set(value)` to an explicit key set before coercion. `ExecutionResult` must carry `scenarioId`, `variantId`, `layer`, `sourceIdentity`, `facts`, `events`, `draft`, `stateBefore`, `stateAfter`, `effectLedger`, `providerLedger`, `runtimeProjectionDigest`, and `nonClaims`.

Implement `classify_gate()` in the exact order `BLOCKED → INSTRUMENT_FAILURE → FAIL → UNVERIFIED → PASS_OFFLINE`. It must accept only named keyword arguments and return an enum.

- [ ] **Step 4: Run contract tests to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: PASS.

- [ ] **Step 5: Commit the closed contracts**

```bash
git add tests/ceq1/contracts.py tests/test_ceq1_manifest.py
git commit -m "test: add closed CE-Q1 result contracts"
```

### Task 3: Validate public manifest, sealed coverage, and synthetic provenance

**Files:**
- Create: `tests/ceq1/manifest.py`
- Create: `tests/ceq1/privacy.py`
- Create: `tests/fixtures/ceq1/inputs/provenance.json`
- Modify: `tests/test_ceq1_manifest.py`

- [ ] **Step 1: Write failing validator tests over temporary fixture trees**

Tests must prove:

- the public manifest has only `schemaVersion`, `productionAncestor`, `implementationBase`, and `scenarios`;
- each public scenario has only `id`, `family`, `purpose`, `provenanceLabel`, `inputBundle`, `inputHash`, `responseBundle`, `responseHash`, and `ownerModuleHashes`;
- public records reject `expectedVerdict`, `oracleHash`, `expectedState`, and `sabotageId` anywhere;
- coverage records have exactly `variantId`, `scenarioId`, `layers`, `sabotageId`, `promotionClass`, `expectedVerdict`, and `nonClaims`;
- the 19 stable scenario IDs and 55 mandatory variant IDs are exact sets with no duplicate, skip, filter, or xfail field;
- hashes are lowercase 64-character SHA-256 strings and match file bytes;
- absolute paths, `file://`, production-shaped IDs, undeclared identities, and non-`.invalid` mailboxes are rejected without echoing the matched value;
- credential-shaped tokens, seeded forbidden tokens, raw-message IDs, and timestamps outside the declared synthetic clock quarantine the bundle;
- provenance explicitly says `generationMethod: newly_authored_synthetic_template`, `rawCustomerSourcesAccessed: false`, and carries a reviewer status.

Use temporary JSON documents for these tests so the committed deck can remain absent until its dedicated authoring tasks.

- [ ] **Step 2: Run validator tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: FAIL because `manifest.py` and `privacy.py` do not exist.

- [ ] **Step 3: Implement closed validation and redacted errors**

`manifest.py` must expose:

```python
MANDATORY_SCENARIO_IDS = frozenset({
    "CEQ-LONG-01", "CEQ-MEM-01", "CEQ-TERM-01", "CEQ-TERM-02",
    "CEQ-SUITE-01", "CEQ-PDF-01", "CEQ-OPEX-01", "CEQ-OPEX-02",
    "CEQ-ALT-01", "CEQ-IN-09", "CEQ-IN-10", "CEQ-WRONG-01",
    "CEQ-OOO-01", "CEQ-AUDIENCE-01", "VOICE-LAUNCH", "VOICE-MISSING",
    "VOICE-CORRECTION-CLOSE", "VOICE-FOLLOWUP", "VOICE-CONTINUATION",
})
```

Define `MANDATORY_VARIANT_IDS` as the exact 55 strings in the approved spec. Return typed `ValidatedManifest` and `ValidatedCoverage` objects only after exact set equality, path containment, byte-hash, owner-module-hash, and privacy validation all pass.

`privacy.py` must expose `scan_bytes()`, `scan_json()`, `scan_tree()`, and `validate_generation_provenance()`. Errors contain only a rule ID and logical artifact ID, never the matched text. Recognize declared synthetic identities/addresses and `.invalid` domains; do not claim detection of arbitrary copied prose or numbers.

- [ ] **Step 4: Add the generation-provenance declaration**

Create a closed JSON record with a synthetic template version, declared fictional people/properties/domains, seeded forbidden tokens used only by scanner unit tests, no raw-source access, and `independentReviewStatus: pending`. The pending review state is allowed while authoring but makes a canonical gate run `BLOCKED` until an independent reviewer changes it to `approved` after reviewing the exact fixture diff.

- [ ] **Step 5: Run validator tests to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: PASS with temporary valid examples and deliberate invalid examples rejected.

- [ ] **Step 6: Commit manifest/privacy primitives**

```bash
git add tests/ceq1/manifest.py tests/ceq1/privacy.py tests/fixtures/ceq1/inputs/provenance.json tests/test_ceq1_manifest.py
git commit -m "test: validate sealed CE-Q1 fixture contracts"
```

### Task 4: Build the oracle-only scorer and calibrate it with blind mutations

**Files:**
- Create: `tests/ceq1/scorer.py`
- Create: `tests/ceq1/mutator.py`
- Modify: `tests/test_ceq1_manifest.py`

- [ ] **Step 1: Write failing exact-scoring tests**

Create a safe temporary execution result and a separately loaded oracle. Assert exact comparison for field, value, unit/basis, source message, source span, target property/suite, freshness, events, action count, forbidden effects, complete state, replay delta, and draft obligations. Assert scorer imports contain no `email_automation` reference.

Then require every calibration mutation to produce `REFUTED` with its named reason:

```python
REQUIRED_MUTATIONS = {
    "extra-write", "wrong-row", "wrong-field", "wrong-value", "wrong-unit",
    "wrong-basis", "quoted-only-support", "invented-fact", "known-reask",
    "declined-reask", "uncited-terminal", "duplicate-action", "forbidden-event",
    "forbidden-send", "provider-construction", "network-attempt",
    "output-cardinality", "guard-identity",
}
```

Add negative controls proving an exact safe record is `VERIFIED` and a missing product provenance field is `UNVERIFIED`/hard-failing rather than inferred from matching text.

- [ ] **Step 2: Run calibration tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: FAIL because scorer and mutator are absent.

- [ ] **Step 3: Implement the scorer as a product-free process module**

`score_execution(result, oracle)` returns one `ScoreRecord` with a sorted set of stable failure reason codes and a structured redacted diff. It must use typed equality, never substring matching. Product provenance earns credit only when the observed fact itself carries the exact closed evidence reference.

`mutator.py` receives only an unscored result plus one mutation ID. It must not import the scorer, oracle, product, manifest, or fixture loader. Each mutation changes one schema field deterministically and returns a new object without mutating the input.

- [ ] **Step 4: Run calibration tests to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: all 18 mutants are caught for their intended reason and the control remains green.

- [ ] **Step 5: Commit scorer calibration**

```bash
git add tests/ceq1/scorer.py tests/ceq1/mutator.py tests/test_ceq1_manifest.py
git commit -m "test: calibrate exact CE-Q1 scoring"
```

### Task 5: Extend the temporal no-effect guard through interpreter exit

**Files:**
- Create: `tests/ceq1/guards.py`
- Modify: `tests/test_ceq1_manifest.py`

- [ ] **Step 1: Write failing subprocess tests for every forbidden boundary**

Use fresh Python subprocesses and a task-owned JSONL ledger. Install the guard before importing product code. Probes must show the guard records and raises on:

- Firestore/Firebase/OpenAI/MSAL/Google discovery client construction unless the exact object identity is registered as a local adapter;
- DNS, socket construction/connect, `requests`, `urllib`, `http.client`, and `httpx` non-loopback work;
- subprocesses invoking `gcloud`, `firebase`, mailbox helpers, scheduler/manual-live scripts, or any unregistered executable;
- credential/keychain/Cloud SDK path reads;
- writes outside the task temp root;
- imports of effectful manual scripts;
- Drive, Tasks, follow-up claim/send/retry, outbox/send, and pending-send calls outside the one baseline recorder;
- replacement or deletion of a protected guard function.

Add positive probes for ordinary imports, reads from the projection, writes under the task temp root, a registered frozen-response client, and registered loopback Firestore transport in L3 mode.

- [ ] **Step 2: Run guard tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: FAIL because `guards.install_temporal_guard` is absent.

- [ ] **Step 3: Implement the execution-long guard**

Extract and generalize the proven approach in `tests/test_test_collection_contract.py`: preload SDK modules before the socket-construction measurement boundary, add the socket audit hook, replace watched attributes, protect module attributes against reassignment, and verify identity in `atexit`.

The public API is:

```python
@dataclass(frozen=True)
class GuardPolicy:
    task_root: Path
    ledger_path: Path
    allow_loopback: bool
    allowed_constructor_ids: frozenset[int]
    allowed_callable_ids: frozenset[int]


def install_temporal_guard(policy: GuardPolicy) -> "GuardHandle":
    """Install once before product imports and remain active through exit."""
```

Every attempted effect appends `{sequence, boundary, outcome}` without arguments or sensitive values. The handle's `close()` checks protected identity and exact expected allowed-call counts; it never removes the guards.

- [ ] **Step 4: Run guard tests to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: PASS; forbidden probes fail inside their child without making a request, and the test process reports only stable reason codes.

- [ ] **Step 5: Commit the temporal guard**

```bash
git add tests/ceq1/guards.py tests/test_ceq1_manifest.py
git commit -m "test: enforce CE-Q1 no-effect boundaries"
```

### Task 6: Enforce filesystem capability separation and OS sandbox cleanup

**Files:**
- Create: `tests/ceq1/supervisor.py`
- Create: `tests/ceq1/sut_worker.py`
- Create: `tests/ceq1/score_worker.py`
- Create: `tests/ceq1/mutation_worker.py`
- Create: `tests/test_ceq1_sandbox.py`

- [ ] **Step 1: Write failing sandbox and capability tests**

Build each test from a temporary projection and temporary bundle tree. Require
these negative probes before running product cases:

The test table is closed. For each row, call `run_capability_probe()` with the
named role/action and compare the returned stable reason code exactly:

| Role/action | Expected reason |
| --- | --- |
| SUT reads oracle | `DENIED_SUT_ORACLE_READ` |
| SUT reads full manifest | `DENIED_SUT_MANIFEST_READ` |
| SUT reads repository outside projection | `DENIED_SUT_REPOSITORY_READ` |
| scorer imports product | `DENIED_SCORER_PRODUCT_IMPORT` |
| mutator reads oracle | `DENIED_MUTATOR_ORACLE_READ` |
| any role resolves/connects non-loopback | `DENIED_NON_LOOPBACK_NETWORK` |
| any role writes outside task root | `DENIED_OUTSIDE_WRITE` |
| quarantined output contains seeded token | `QUARANTINED_PRIVACY_FINDING` |
| TERM cleanup closes owned tree | `CLEANUP_VERIFIED` |
| prior SIGKILL receipt is unreconciled | `ORPHAN_RECONCILIATION_REQUIRED` |

The SUT projection test must prove exact allowlist generation: every projected
file is a regular file beneath the projection root, matches its recorded hash,
and is read-only. It must not contain `docs/`, the full execution manifest,
coverage contract, oracle, `.git`, a credential file, or a manual/live script.
The worker receives only `{scenarioId, layer, inputHash, responseHash}`.

- [ ] **Step 2: Run sandbox tests to verify RED**

Run the canonical pytest prefix with:

```bash
tests/test_ceq1_manifest.py tests/test_ceq1_sandbox.py
```

Expected: FAIL because `tests.ceq1.supervisor` and worker entrypoints are absent.

- [ ] **Step 3: Implement the minimal host supervisor and workers**

Expose these closed interfaces:

Implement the frozen `ChildMounts` record with `projection`, `descriptor`,
`inputs`, `responses`, and `output` `Path` fields. Implement
`build_source_projection(repo_root, destination, relative_paths)` to return a
sorted `relative_path -> sha256` mapping after copying only regular allowlisted
files and making them read-only. Implement keyword-only
`run_sandboxed_child(role, argv, mounts, task_root, loopback_ports=())` to return
a closed `ChildReceipt`; it raises `CapabilityError` with one of the stable
reason codes above for denied or ambiguous outcomes.

The supervisor builds child environments from an empty mapping and an explicit
name allowlist. It sets task-owned `HOME`, `TMPDIR`, `XDG_*`, Cloud SDK, cache,
and Python cache paths; deliberately sets declared synthetic `E2E_TEST_MODE`
sentinels and `SITESIFT_OUTBOUND_MODE=paused`; removes proxy, credential,
token, keychain, `.netrc`, and provider variables; and hashes non-secret values
without recording them.

Generate one `/usr/bin/sandbox-exec` profile per role. SUT may read only its
projection/descriptor/input/response and Python runtime, write only its output
and tmp roots, and use no network for L1/L2. Scorer may read only sealed result
and oracle and cannot read/import product. Mutator may read only the result
schema and input result. L3 adds only the exact task-owned loopback proxy port.
Profiles deny process execution except the exact Python worker or pinned L3
Java/proxy child registered by the outer supervisor.

Write a durable task receipt before child startup containing schema, random
task ID, parent PID/start identity, child PID/PGID/start identity, realpaths,
hashes, ports, and lifecycle state. On `INT`/`TERM`, use bounded TERM then KILL,
wait for process group absence, close both pipes, prove ports closed, then
remove temp. If cleanup is unknown or interrupted, retain the receipt/temp and
return `INSTRUMENT_FAILURE`. On a later run, refuse an unreconciled receipt.

Workers read only their own capability paths. `sut_worker.py` installs the
temporal guard before importing the projected harness. `score_worker.py`
imports only `tests.ceq1.contracts`, `privacy`, and `scorer` from a separate
scoring projection. `mutation_worker.py` imports only contracts and mutator.
All stdout/stderr/result bytes remain quarantined until `privacy.scan_tree()`
passes; the parent returns only an opaque quarantine ID on failure.

- [ ] **Step 4: Run sandbox tests to verify GREEN**

Run the Task 6 command again. Expected: every deny probe is rejected for its
named reason, positive capability reads/writes succeed, cleanup is proven, and
the parent terminal output contains no probe payload.

- [ ] **Step 5: Commit the containment layer**

```bash
git add tests/ceq1/supervisor.py tests/ceq1/sut_worker.py \
  tests/ceq1/score_worker.py tests/ceq1/mutation_worker.py \
  tests/test_ceq1_sandbox.py
git commit -m "test: isolate CE-Q1 execution capabilities"
```

### Task 7: Author the closed synthetic deck and run real L1/L2 product seams

**Files:**
- Create: `tests/ceq1/frozen_provider.py`
- Create: `tests/ceq1/adapters.py`
- Create: `tests/ceq1/runtime_bindings.py`
- Create: `tests/ceq1/fixture_builder.py`
- Create: `tests/ceq1/harness.py`
- Create: `tests/test_ceq1_semantic_replay.py`
- Create: `tests/test_ceq1_stateful_replay.py`
- Create: `docs/release-safety/ceq1-execution-manifest.json`
- Create: `tests/fixtures/ceq1/inputs/*.json`
- Create: `tests/fixtures/ceq1/inputs/ceq-pdf-01.pdf`
- Create: `tests/fixtures/ceq1/responses/*.json`
- Create: `tests/fixtures/ceq1/oracles/*.json`
- Create: `tests/fixtures/ceq1/oracles/coverage-contract.json`
- Modify: `tests/fixtures/ceq1/inputs/provenance.json`

- [ ] **Step 1: Write failing replay-client and adapter tests**

Before authoring cases, require a closed queue client that validates the exact
prompt/config hash before returning one frozen response, fails on missing or
extra calls, and never sees an oracle. Require in-memory Graph, history,
Firestore, Sheets, pending-response, action/audit, and effect adapters to reject
unknown methods and paths and to emit canonical operation receipts.

The replay-client test creates one `FrozenCall` with a known SHA-256 prompt hash,
invokes `responses.create()` once with the matching production arguments, and
compares the typed frozen response exactly. Separate children change one prompt
byte, call twice, call zero times before `assert_exhausted()`, and attempt to
open an oracle path; require `PROMPT_HASH_DRIFT`, `EXTRA_PROVIDER_CALL`,
`MISSING_PROVIDER_CALL`, and `DENIED_SUT_ORACLE_READ` respectively.

Adapter tests call one allowlisted operation and one unknown method/path. The
unknown cases must return `INSTRUMENT_FAILURE` with
`ADAPTER_METHOD_NOT_ALLOWED` or `ADAPTER_PATH_NOT_ALLOWED`. Constructing a
snapshot without any one required effect surface must return
`INCOMPLETE_STATE_SNAPSHOT`. Replaying the same stable source identity on the
same complete state must preserve the semantic digest and add no operation.

- [ ] **Step 2: Run focused replay tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_semantic_replay.py` and
`tests/test_ceq1_stateful_replay.py`.

Expected: FAIL because replay client, adapters, and harness are absent.

- [ ] **Step 3: Implement the queue client, adapters, and binding context**

The frozen client surface is exactly:

`FrozenProviderClient(expected_calls, ledger)` exposes `responses` as itself.
Its keyword-only `create(model, input, temperature)` canonicalizes the exact
request, compares it with the next `FrozenCall`, records one frozen-provider
ledger entry, and returns a closed `FrozenResponse`. `assert_exhausted()` fails
unless the queue is empty. It has no generic attribute fallback, file access,
oracle path, SDK client, or transport.

Replace the whole `ai_processing.client` alias before any `.responses` access.
`RuntimeBindings` pins every patched module attribute by identity, fails on
replacement, restores it on exit, and records exact use counts. Bind the aliases
used by `processing.py`, not only their owner modules: `processing._fs`,
`processing._sheets_client`, `processing.requests`,
`processing.build_conversation_payload`, and
`processing.queue_pending_response`, plus the exact `ai_processing` aliases.
Leave `process_inbox_message`, `propose_sheet_updates`,
`apply_proposal_to_sheet`, `_select_automatic_response_body`, and
`send_reply_in_thread` real.

The complete state snapshot contains target/sibling rows and formulas; threads,
messages, and indexes; reviews; terminal actions; pending response; audit;
outbox/send/follow-up namespaces; provider/effect ledgers; and action order.
Omitting any required surface is `INSTRUMENT_FAILURE`.

- [ ] **Step 4: Write failing L1/L2 characterization tests against temporary cases**

L1 must call real `propose_sheet_updates()` with the strict frozen response and
real post-processors. History cases must first seed adapters and call real
`build_conversation_payload(..., limit=10)`. The native PDF case must generate
fictional bytes deterministically, pass them through the real local
`file_handling.process_pdf_for_ai()` parser, and never reach Drive/OpenAI
fallback.

Every one of these 12 IDs must call the real
`process_inbox_message(..., allow_outbound_reply=True)` under strict adapters:
`CEQ-LONG-01`, `CEQ-MEM-01`, `CEQ-TERM-01`, `CEQ-TERM-02`,
`CEQ-SUITE-01`, `CEQ-PDF-01`, `CEQ-ALT-01`, `CEQ-IN-09`,
`CEQ-IN-10`, `CEQ-WRONG-01`, `CEQ-OOO-01`, and
`CEQ-AUDIENCE-01`. Tests assert the entrypoint identity and call count.

For a reply-capable baseline case, assert exactly one natural call to real
`send_reply_in_thread`, exact `suppressed_by_kill_switch`, zero transport/client
or Graph-send attempts, exactly one module-local in-memory
`processing.queue_pending_response` call, zero pending storage, and product
verdict `FAIL`. `allow_outbound_reply=False` cannot satisfy this assertion.

Expected REDs must be product-observable, not hard-coded by scenario ID: no
decline ledger, no typed product provenance, no suite identity, and no shared
final draft cause the scorer to return the corresponding stable
`FAIL`/`UNVERIFIED` reason only when the observed record actually lacks it.

- [ ] **Step 5: Implement the minimal L1/L2 harness to make characterization GREEN**

Expose:

Implement three explicit functions returning the closed `ExecutionResult`:
`execute_l1(case, bindings)` for deterministic proposal/post-processing;
`execute_l2(case, bindings)` for state-unit application/readback; and
`execute_pipeline_case(case, bindings)` for the real required entrypoint. Each
function validates its accepted `RuntimeCase.layer` and scenario class, calls
only the real seams assigned to that layer, asserts the frozen queue exhausted,
closes the binding/effect ledgers, and refuses to manufacture an expected value
or verdict.

The harness coordinates production seams and records output; it contains no
expected values, expected verdicts, oracle reads, copied product decision
logic, direct fixture-state assignments in place of transitions, or substring
scoring. A direct full-history proposal diagnostic is labeled
`BYPASSED_HISTORY` and cannot satisfy the history variants.

- [ ] **Step 6: Author and validate the full synthetic deck**

Create the 19 exact scenario IDs and all 55 exact coverage variants from the
approved spec. Every identity uses a declared `.invalid` domain and every
property/person/value is newly fictional. Input, response, and oracle are in
separate files. The public manifest contains no layer, expected state/verdict,
oracle hash, or sabotage mapping. The sealed coverage contract owns those.

Each variant contains a positive/near-miss execution and a sabotage ID. Every
required safety/state variant executes in its named layer. Image-only PDF and
all five voice variants execute as diagnostics with exact `UNVERIFIED`
non-claims. Voice scoring rejects raw `proposal.response_email` and the current
missing-field selected template as proof of a shared final rendered draft.

Run `fixture_builder.py` only over newly authored templates to create the
native three-page PDF. The provenance receipt starts as `pending`; after a
fresh independent reviewer verifies the exact fixture diff contains no copied
customer content or PII, change only `independentReviewStatus` and reviewer role
to `approved` in a separate commit before a canonical baseline run.

- [ ] **Step 7: Run the full L1/L2 deck and exact closure checks**

Run:

```bash
<canonical-env> "$CEQ_PY" -m pytest -q -p no:cacheprovider \
  tests/test_ceq1_manifest.py tests/test_ceq1_semantic_replay.py \
  tests/test_ceq1_stateful_replay.py
```

Expected: test/instrument contracts PASS; exactly 19 scenario IDs and 55
variant IDs execute with no skip/xfail/filter; deterministic safe cases match
their oracles; declared product gaps are reported as the exact expected
`FAIL`/`UNVERIFIED` evidence rather than test failures; forbidden constructor,
network, mailbox, outbox, send, and follow-up counts are zero.

- [ ] **Step 8: Commit the real-seam replay deck**

```bash
git add tests/ceq1/frozen_provider.py tests/ceq1/adapters.py \
  tests/ceq1/runtime_bindings.py tests/ceq1/fixture_builder.py \
  tests/ceq1/harness.py tests/test_ceq1_semantic_replay.py \
  tests/test_ceq1_stateful_replay.py \
  docs/release-safety/ceq1-execution-manifest.json tests/fixtures/ceq1
git commit -m "test: exercise CE-Q1 semantic and state replays"
```

### Task 8: Prove L3 persistence with a pinned task-owned Firestore emulator

**Files:**
- Create: `tests/ceq1/firestore_audit_proxy.py`
- Create: `tests/ceq1/firestore_emulator.py`
- Create: `tests/test_ceq1_emulator_replay.py`
- Modify: `tests/ceq1/adapters.py`
- Modify: `tests/ceq1/harness.py`
- Modify: `tests/ceq1/supervisor.py`

- [ ] **Step 1: Write failing pinned-prerequisite and lifecycle tests**

Require these exact local prerequisites and reject realpath/hash/mode/owner
drift before spawning anything:

```text
/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home/bin/java
sha256 370ef109f74f859afc8cfe0300b2da782d60698160b8a48f19731d6d2e3012ea

/Users/baylorharrison/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.8.jar
sha256 9d43599ed6151199e8d604dc87fac51218e49e5f3a48519b1ae560bbe5e3382d
```

Verify `java -jar <jar> --version` without download/update behavior. Test a
fresh task-owned loopback port/proxy/emulator process group, startup receipt,
bounded TERM-to-KILL cleanup, both pipe terminals, port closure, and temp
retention on unproved cleanup. No test may invoke Firebase CLI or download an
emulator/JDK.

- [ ] **Step 2: Write failing namespace-wrapper and independent-audit tests**

Require a fluent wrapper over collection/document/query/batch/transaction
references. It canonicalizes and checks every read/query/create/update/delete
path before transport. Batch and transaction child references remain wrapped.

Run a separate loopback gRPC forwarding process between the Python SDK and the
emulator. It parses the Firestore request messages used by the harness, records
method plus canonical resource paths without document values, and forwards to
the emulator. Calibration deliberately bypasses the wrapper and attempts a
create/delete outside the namespace through the proxy; the independent audit
must detect it and produce `INSTRUMENT_FAILURE`. The normal out-of-namespace
mutant must be rejected by the wrapper before proxy transport.

- [ ] **Step 3: Run L3 tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_emulator_replay.py`.

Expected: FAIL because emulator/proxy/wrapper implementations are absent.

- [ ] **Step 4: Implement the pinned launcher, proxy, and namespace wrapper**

The launcher uses direct Java argv only:

```text
<java> -jar <jar> --host 127.0.0.1 --port <emulatorPort>
  --project_id demo-ceq1-<taskId> --single_project_mode true
  --single_project_mode_error true
```

It stores exact argv, realpaths/hashes, PID/PGID/start identity, child tree,
ports, and pipe state. The SUT talks only to the proxy port. The proxy talks
only to the emulator port. Sandbox profiles allow those exact directions and
deny every other network endpoint.

Use two independent raw inventories before the first mutation, after each
transition, and after replay. Reconcile wrapper ledger, proxy audit ledger, and
the final complete emulator inventory by path/method/cardinality. A missing or
unattributed proxy request, ledger disagreement, or path outside the task
namespace is `INSTRUMENT_FAILURE` even if final state is empty.

- [ ] **Step 5: Run mandatory L3 scenarios, interruption, replay, and cleanup**

Run the Task 8 test again. Expected: required state scenarios exercise actual
Firestore transactions/timestamps/readbacks, switches remain false in the
synthetic namespace, same source identity replay is zero-delta, injected
transaction/interruption state is visibly retryable, no pending/outbox/send or
outside-namespace document is written, proxy/wrapper inventories reconcile,
and no Java/proxy/port/temp residue remains.

- [ ] **Step 6: Commit L3 persistence**

```bash
git add tests/ceq1/firestore_audit_proxy.py tests/ceq1/firestore_emulator.py \
  tests/test_ceq1_emulator_replay.py tests/ceq1/adapters.py \
  tests/ceq1/harness.py tests/ceq1/supervisor.py
git commit -m "test: prove CE-Q1 emulator persistence"
```

### Task 9: Orchestrate the fixed schedule and commit the honest baseline finding

**Files:**
- Create: `scripts/run_ceq1.py`
- Create: `docs/release-safety/evidence/ceq1/baseline-report.json`
- Create: `docs/release-safety/evidence/ceq1/baseline-report.md`
- Modify: `tests/ceq1/supervisor.py`
- Modify: `tests/test_ceq1_manifest.py`
- Modify: `tests/test_ceq1_semantic_replay.py`
- Modify: `tests/test_ceq1_stateful_replay.py`
- Modify: `tests/test_ceq1_emulator_replay.py`

- [ ] **Step 1: Write failing CLI, schedule, and report-schema tests**

Require exact subcommands `preflight`, `calibrate`, `run`, and
`verify-report`. `run` has only declared tiers `l1`, `l2`, `l3`, or `all`, and
canonical mode always executes forward once, reverse once, then three fresh
process repeats per case. No retry-until-green, case filter, xfail, skip, or
best-of-N option exists. Diagnostic mode may continue after first product
failure but can never issue a gate verdict.

Report tests require candidate/source/dependency/manifest/fixture/owner hashes;
19 scenarios/55 variants and attempt cardinality; separate historical/current
source labels; per-layer structured results; before/after/replay hashes;
effect/constructor/network counters; non-claims; deterministic verdict
precedence; privacy scan receipt; complete cleanup receipt; and an exact
next-gate eligibility record. Raw messages, fixture bodies, recipient values,
credentials, absolute paths, and unredacted failure payloads are forbidden.

- [ ] **Step 2: Run CLI/report tests to verify RED**

Run the canonical pytest prefix with all four CE-Q test modules.

Expected: FAIL because `scripts/run_ceq1.py` and report assembly are absent.

- [ ] **Step 3: Implement the thin CLI and fixed supervisor schedule**

`scripts/run_ceq1.py` only parses the closed CLI and calls supervisor methods;
it imports no product module. `preflight` verifies clean exact candidate HEAD,
ancestry from `6caa8ec`, implementation-base product-file equality with
`b400ee5`, owner/transitive projection/dependency hashes, fixture hashes,
sandbox probes, pinned Java/JAR, and no unreconciled task receipt.

The source identity is `{candidateHead, productSourceBase, productionAncestor}`;
never claim candidate HEAD equals `b400ee5`. Any product-file drift requires a
separately reviewed successor identity.

`calibrate` runs the known-good synthetic control and all 18 mutations in
separate scorer/mutator children before any product verdict. `run --tier all`
uses only the supervisor, new process/state/namespace per attempt, synthetic
clock, forward/reverse/three-repeat schedule, and typed digests that omit only
declared volatile process/time/path receipt fields.

- [ ] **Step 4: Run fresh preflight and calibration**

Run:

```bash
<canonical-env> "$CEQ_PY" scripts/run_ceq1.py preflight \
  --output .ceq1-runtime/preflight.json
<canonical-env> "$CEQ_PY" scripts/run_ceq1.py calibrate \
  --output .ceq1-runtime/calibration.json
```

Expected: preflight `PASS`; calibration control `VERIFIED`; all 18 mutants
`REFUTED` for their exact intended reasons; zero forbidden attempts; quarantined
artifacts privacy-clean. If a local prerequisite is absent, preflight returns
`BLOCKED` before starting product/emulator children and the plan stops without
claiming L3 evidence.

- [ ] **Step 5: Freeze an exact clean instrument commit before canonical execution**

Run the complete affected tests, Python compile, manifest/privacy scan,
`git diff --check`, and fixture independent review. Commit any reviewer-approved
fixture provenance-status change separately. Then commit the runner before
creating canonical evidence:

```bash
git add scripts/run_ceq1.py tests/ceq1/supervisor.py \
  tests/test_ceq1_manifest.py tests/test_ceq1_semantic_replay.py \
  tests/test_ceq1_stateful_replay.py tests/test_ceq1_emulator_replay.py
git commit -m "test: orchestrate the CE-Q1 qualification gate"
test -z "$(git status --short)"
```

- [ ] **Step 6: Execute the canonical offline baseline once**

Run:

```bash
<canonical-env> "$CEQ_PY" scripts/run_ceq1.py run --tier all \
  --output .ceq1-runtime/canonical
<canonical-env> "$CEQ_PY" scripts/run_ceq1.py verify-report \
  .ceq1-runtime/canonical/report.json
```

Expected instrument outcome: all scheduled cases/attempts execute with stable
digests and zero forbidden effects. Expected product outcome on `b400ee5`:
`FAIL`, with exact evidence for the paused-send pending projection and other
promotion-required product gaps, plus declared diagnostic `UNVERIFIED` voice/
image-only/non-atomicity records. `PASS_OFFLINE` would be unexpected at this
baseline and triggers adversarial review rather than promotion.

- [ ] **Step 7: Generate sanitized committed evidence from the verified report**

Only after the quarantine and output tree pass `privacy.scan_tree()`, render
`baseline-report.json` and `.md` from the structured report. Include stable
reason codes and redacted diffs, never raw fixture bodies. Run
`verify-report` again on the committed form and compare its semantic digest to
the quarantined canonical report.

- [ ] **Step 8: Run the final affected and broad regression gates**

Run:

```bash
<canonical-env> "$CEQ_PY" -m pytest -q -p no:cacheprovider \
  tests/test_ceq1_manifest.py tests/test_ceq1_sandbox.py \
  tests/test_ceq1_semantic_replay.py tests/test_ceq1_stateful_replay.py \
  tests/test_ceq1_emulator_replay.py \
  tests/test_test_collection_contract.py \
  tests/test_runtime_provider_initialization.py \
  tests/test_process_user_service.py

<canonical-env> "$CEQ_PY" -m pytest --noconftest --collect-only -q \
  -p no:cacheprovider

"$CEQ_PY" -m py_compile tests/ceq1/*.py scripts/run_ceq1.py
git diff --check b400ee5..HEAD
git status --short
```

Expected: all instrument and regression tests PASS; whole-repo collection is
complete with zero constructor/network ledger entries; compile and diff check
exit 0. The two inherited `test_full_campaign_e2e.py` runtime assertion failures
remain excluded and must not be represented as new CE-Q failures or silently
fixed.

- [ ] **Step 9: Commit the frozen baseline finding**

```bash
git add docs/release-safety/evidence/ceq1/baseline-report.json \
  docs/release-safety/evidence/ceq1/baseline-report.md
git commit -m "test: record CE-Q1 offline baseline"
test -z "$(git status --short)"
```

The commit remains local. Do not push, merge, deploy, enable switches, call a
model/mailbox/provider, or begin a product fix in this implementation plan.

## Final review and completion gate

After Task 9, perform independent reviews in this order:

1. exact-SHA specification/data-flow review against the approved design;
2. exact-SHA security/no-effect/privacy review, including deliberate sandbox,
   guard, wrapper, proxy, and cleanup attacks; and
3. fresh empirical rerun of preflight, calibration, canonical report
   verification, affected tests, collection, compile, diff, SHA, and status.

Any P0/P1/P2 or an instrument failure routes back through a new TDD cycle and
invalidates the prior canonical report. A review follow-up creates a new SHA and
requires the complete affected tier rerun. Completion means a reviewed,
committed, sanitized, honest offline finding; it does not mean the product is
qualified for users.
