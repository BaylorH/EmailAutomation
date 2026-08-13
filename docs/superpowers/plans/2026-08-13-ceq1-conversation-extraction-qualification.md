# CE-Q1 Conversation and Extraction Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the effect-free CE-Q1A qualification instrument, execute it against the reviewed `b400ee5` product baseline, and record an honest structured baseline finding without repairing product behavior.

**Architecture:** A host supervisor creates capability-separated SUT, scorer, mutation, audit-proxy, and Firestore-emulator processes. The SUT receives only a generated product-source projection, one minimal descriptor, a synthetic input bundle, and a hash-bound frozen response bundle; it never receives the oracle. L1 drives real deterministic extraction seams, L2 drives the real `process_inbox_message()` orchestration with strict in-memory adapters, and L3 replaces only Firestore with a task-owned loopback emulator behind a namespace wrapper and an independently reconciled gRPC audit proxy.

**Tech Stack:** CPython 3.12.13, `unittest`/`pytest`, standard-library JSON/dataclasses/hashing/process control, PyMuPDF/pdfplumber for native PDFs, `google-cloud-firestore` and `grpcio` pinned through `requirements.lock`, macOS `/usr/bin/sandbox-exec`, a task-owned verified OpenJDK 25.0.2 copy, and a task-owned verified Firestore emulator 1.19.8 JAR.

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
| `requirements-ceq1.in` | Qualification-only pytest input constrained by the production lock |
| `requirements-ceq1.lock` | Offline hash-pinned qualification test dependencies |
| `docs/release-safety/ceq1-wheelhouse-manifest.json` | Reviewed derived-wheel input/output manifest; source RECORD/member digests and derived wheel hashes only, never an upstream-equivalence claim |
| `docs/release-safety/ceq1-execution-manifest.json` | Public scenario registry and input/response/owner hashes; no oracle or expected verdict |
| `docs/release-safety/ceq1-execution-schedule.json` | Public oracle-free scenario/variant/layer schedule and input/response hashes |
| `docs/release-safety/ceq1-toolchain-manifest.json` | Closed full-tree Python/JDK/JAR/venv dependency manifest and digests |
| `docs/release-safety/evidence/ceq1/README.md` | Evidence semantics and non-claims |
| `docs/release-safety/evidence/ceq1/baseline-report.json` | Final sanitized machine-readable baseline finding |
| `docs/release-safety/evidence/ceq1/baseline-report.md` | Final sanitized operator summary |
| `tests/fixtures/ceq1/schemas/` | Closed JSON Schemas for public, sealed, fixture, and runtime records |
| `tests/fixtures/ceq1/inputs/` | Synthetic runtime bundles plus generation-provenance declaration |
| `tests/fixtures/ceq1/responses/` | Hash-addressed frozen model response bundles |
| `tests/fixtures/ceq1/oracles/` | Sealed expected records and `coverage-contract.json` |
| `tests/fixtures/ceq1/runtime-binding-contract.json` | Reviewed transitive by-value/effect alias inventory |
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
| `tests/ceq1/voice.py` | Closed frozen-draft packet and blinded-review contract |
| `tests/test_ceq1_manifest.py` | Contracts, closure, privacy, capability separation, calibration, dependency direction |
| `tests/test_ceq1_sandbox.py` | Filesystem/network/process capability and cleanup proof |
| `tests/test_ceq1_semantic_replay.py` | L1 replay and exact semantic scoring |
| `tests/test_ceq1_stateful_replay.py` | L2 real-entrypoint state/effect/replay scoring |
| `tests/test_ceq1_emulator_replay.py` | L3 preflight, namespace, transport audit, persistence, interruption, cleanup |
| `tests/test_ceq1_voice.py` | Frozen-draft eligibility and blinded-review instrument calibration |
| `scripts/bootstrap_ceq1_runtime.py` | Exact Task 1 orchestrator for the sandboxed double build, derived-lock verification, and sealed local venv |
| `scripts/build_ceq1_wheelhouse.py` | Deterministically reconstructs reviewed pure-Python wheel bytes from exact uv-cache RECORD members without mutating the cache |
| `scripts/run_ceq1.py` | Thin CLI over the host supervisor |
| `scripts/run_ceq1_env.py` | Empty-environment Python/test launcher used before the full supervisor exists |

Do not create a production package for CE-Q1. No production module may import `tests.ceq1`, `scripts.run_ceq1`, or `tests/fixtures/ceq1`.

## Canonical local test environment

Run every command from the worktree root. Never use the symlinked environment
from another worktree and never copy the ambient environment then unset a
partial variable list. Task 1 creates a local ignored sealed runtime bundle at
`.ceq1-venv/` offline from the already-installed CPython 3.12.13 tree. The
bundle contains `python/` (the complete copied CPython base) and `venv/` (the
installed dependency payload):

```text
/Users/baylorharrison/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12
launcher sha256 e2605291e058fdbe3102e8185d0ac5fe0e063398de617010a6af3a42a78f05e3
```

The pinned `uv` executable is `/Users/baylorharrison/.local/bin/uv`, SHA-256
`4424f8430c3cb3990daaa68268af640bdc61190f2e5c276197e3473358b1e4e8`.
It is invoked with `--offline --no-python-downloads --require-hashes` only.
`requirements.lock` pins product dependencies; `requirements-ceq1.lock` pins
pytest and its constrained transitive test dependencies. The preflight hashes
the interpreter tree, both locks, `pyvenv.cfg`, every installed distribution
`RECORD`, native library, and executable in `.ceq1-venv`; the canonical report
binds that frozen environment digest.

The ignored `.ceq1-venv` is a sealed source runtime bundle. Direct Task 1 tests
execute `.ceq1-venv/python/bin/python3.12`, never the venv launcher; the wrapper
adds only the manifest-bound `.ceq1-venv/venv` site-packages after startup. A canonical
run does not execute from the user-writable source runtime in place. Task 5
first validates the closed Task 1 toolchain manifest, then copies the sealed
`.ceq1-venv`, JDK, Firestore JAR, and validated derived wheelhouse into the
task-owned runtime root without following undeclared links. It verifies exact
manifest equality, preserves the read-only seal, and hashes `pyvenv.cfg`, every
distribution `RECORD`, executable, and native library before and after the run.
Task 5 performs no install or rebuild and never reads the original uv cache.
The source and copied manifests, code-signature status where present, ownership,
realpaths, and aggregate digests are recorded. Source reads and copies use
`openat(O_NOFOLLOW)` plus matching pre/post `fstat`; hard links, special files,
absolute/escaping links, size/content/mode drift, and an unexpected executable
bit are rejected. Runtime copying itself runs inside a no-network Seatbelt
profile with an empty environment, read access only to the sealed Task 1
artifacts and committed manifests, and write access only to the task runtime.
Any drift is `BLOCKED` before a
product or emulator child starts. The emulator JAR is executed only from the
verified task-owned copy.

Every direct Python/pytest command below is executed through this literal
empty-environment wrapper; “canonical pytest prefix” means the wrapper refuses
to start unless `-I -S -B` are present exactly as shown:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  -m pytest -q \
  -p no:cacheprovider <test-paths>
```

`run_ceq1_env.py` calls `os.execve()` with a newly constructed environment
mapping whose common Python keys are exactly `HOME`, `TMPDIR`,
`XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `PATH`, `LANG`, `LC_ALL`,
`PYTHONDONTWRITEBYTECODE`, `PYTHONNOUSERSITE`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD`, `E2E_TEST_MODE`,
`SITESIFT_OUTBOUND_MODE`, `CEQ1_TASK_ROOT`, `FIREBASE_BUCKET`,
`FRONTEND_EMAIL_ACCESS_URL`, and `OPENAI_ASSISTANT_MODEL`. Values point only to
`.ceq1-runtime/direct/*`, `/usr/bin`, and `/bin`; outbound mode is `paused`;
the bucket, frontend URL, and model are explicit synthetic `.invalid`/frozen
sentinels. `PYTHONPATH` and every `DYLD_*`, `JAVA_TOOL_OPTIONS`, proxy, SSL
override, SSH agent, Cloud SDK, provider, mailbox, credential, token, key,
user-config, `.env`, or ambient API variable are absent. Tests assert exact
key/value equality in the child even when the parent contains conflicting
garbage. After Seatbelt is active, the bootstrap inserts only the exact hashed
venv and role projection paths into `sys.path`. L3 roles add only their exact
synthetic project and emulator target keys described in Task 9.

The exact synthetic values are
`FIREBASE_BUCKET=demo-ceq1.invalid`,
`FRONTEND_EMAIL_ACCESS_URL=https://ceq1.invalid/email-access`, and
`OPENAI_ASSISTANT_MODEL=ceq1-frozen-proposal`; locale is `C.UTF-8`.
`E2E_TEST_MODE=true` is a declared fixture-mode switch, not a credential, and
any attempt to use a generated sentinel at a client boundary is fatal.

### Task 1: Freeze the qualification-only dependency boundary

**Files:**
- Create: `tests/ceq1/__init__.py`
- Create: `tests/test_ceq1_manifest.py`
- Create: `requirements-ceq1.in`
- Create: `requirements-ceq1.lock`
- Create: `docs/release-safety/ceq1-wheelhouse-manifest.json`
- Create: `scripts/run_ceq1_env.py`
- Create: `scripts/bootstrap_ceq1_runtime.py`
- Create: `scripts/build_ceq1_wheelhouse.py`
- Create: `docs/release-safety/ceq1-toolchain-manifest.json`
- Create: `docs/release-safety/evidence/ceq1/README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Write the failing one-way dependency and runtime-artifact tests**

Add tests that scan `email_automation/**/*.py`, `main.py`, `service.py`, `scheduler_runner.py`, and `app.py` with `ast`. They must reject imports whose module begins with `tests.ceq1` or `scripts.run_ceq1`, and reject literal references to `tests/fixtures/ceq1`. Add assertions that `.ceq1-runtime/` and `.ceq1-venv/` are ignored while committed evidence files are not ignored. Add a fresh-child test for `run_ceq1_env.py` that prints only sorted environment key names and require exact equality with the closed allowlist above, even when the parent injects `OBSIDIAN_REST_API_KEY`, `SSH_AUTH_SOCK`, proxy, credential, and token variables.

Task 1 RED also covers the wheelhouse manifest's exact closed schema and cache
source identities; builder/reconstructor/CPython/`zipfile.py` binding; exact
RECORD-member closure; rejection of cache/archive-ID, byte, mode, size, path,
symlink, hard-link, special, `.data`, native, signature, non-ASCII, traversal,
duplicate, and undeclared-extra drift; allowance of only explicitly
manifest-recorded regular `**/__pycache__/*.pyc` excluded extras; deterministic
double build and byte equality; finished ZIP metadata and RECORD invariants;
derived lock rewrite/validation; output seal/rehash; cache immutability; and the
exact host-pinned sandboxed reconstruction/install command path. These tests use
temporary miniature cache trees for every sabotage and may not mutate the real
uv cache. They also require the sealed bundle's copied Python base and venv
payload, explicit isolated site-packages bootstrap under `-I -S -B`, and reject
any interpreter/stdlib/extension/package realpath outside that bundle.

```python
class Ceq1DependencyDirectionTests(unittest.TestCase):
    def test_production_never_imports_qualification_code(self):
        forbidden = ("tests.ceq1", "scripts.run_ceq1")
        violations = scan_production_imports(REPO_ROOT, forbidden)
        self.assertEqual([], violations)

    def test_runtime_quarantine_is_ignored_but_evidence_is_versioned(self):
        ignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".ceq1-runtime/", ignore_text.splitlines())
        self.assertIn(".ceq1-venv/", ignore_text.splitlines())
        self.assertNotIn("docs/release-safety/evidence/ceq1/", ignore_text.splitlines())
```

- [ ] **Step 2: Run the test to verify RED**

Before the wrapper exists, run the pinned CPython directly with the standard
library `unittest` runner only for this RED bootstrap:

```bash
/Users/baylorharrison/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12 \
  -m unittest tests.test_ceq1_manifest
```

Expected: FAIL because the ignore/evidence/runtime-wrapper contracts are absent.

- [ ] **Step 3: Add the minimal package marker, ignore rule, and evidence contract**

`tests/ceq1/__init__.py` contains only a module docstring. Append exactly
`.ceq1-runtime/` and `.ceq1-venv/` to `.gitignore`. The evidence README must state:

- CE-Q1A is offline deterministic evidence only;
- `baseline-report.*` never certifies production, a model, a mailbox, delivery, Google Sheets persistence, or cross-store atomicity;
- runtime quarantine stays under `.ceq1-runtime/` and is never committed;
- any relevant owner-module or fixture hash change invalidates the report.

Implement `scan_production_imports()` in the test itself for this bootstrap task; move no code into production.

Create `requirements-ceq1.in` containing exactly `pytest==9.1.1`.
`scripts/bootstrap_ceq1_runtime.py` is the only Task 1 orchestrator. It derives
and validates the worktree root from its own resolved `__file__`; it never
accepts or expands `PWD`, reads a shell profile, or copies the parent
environment. Its canonical entry is exactly:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  /Users/baylorharrison/.local/share/uv/python/cpython-3.12.13-macos-aarch64-none/bin/python3.12 \
  -I -S -B scripts/bootstrap_ceq1_runtime.py prepare
```

The mutation-free outer launcher derives only lexical path parameters and a
fresh opaque bootstrap identity, renders the canonical Seatbelt template in
memory, and immediately crosses one true `os.execve()` boundary:
`/usr/bin/env -i <closed-env> /usr/bin/sandbox-exec -p <rendered-policy>
<pinned-python> -I -S -B scripts/bootstrap_ceq1_runtime.py <mode>
--contained --bootstrap <opaque-id>`. It performs no mkdir, copy, hash, cache
read, profile write, or other state mutation before that boundary. The
outer argv and closed child environment carry the exact same rendered-policy
bytes: `sandbox-exec -p` receives the text directly and the child receives its
base64 encoding in `CEQ1_BOOTSTRAP_POLICY_B64`. The contained worker decodes
that value, requires byte equality with its own canonical render, and binds its
hash into the ignored receipt. Before it creates a directory, reads an input,
or emits a receipt, the worker proves that Seatbelt is actually active by
attempting to read the existing worktree `.git` control file, which the
canonical policy deliberately denies while an unsandboxed process can read it.
Only `EPERM`/`EACCES` is accepted. Consequently a directly forged
`--contained --bootstrap ...` invocation fails before mutation even if its
caller forges the expected environment and policy bytes. Tests spy on the
outer `os.execve()` call to prove byte identity between the `-p` argument and
the worker channel, and run the direct-contained bypass as a real subprocess.

After that proof, the contained worker validates its exact environment/flags
and all source and destination roots component-by-component, creates the unique
task root, writes the exact rendered-policy/receipt evidence there, and
performs the entire Task 1 state machine. Every external child begins with a
fresh literal `/usr/bin/env -i` argv and inherits the already-active Seatbelt;
there is no nested `sandbox-exec` and no unsandboxed orchestration between
children. The Task 1 RED is amended before implementation so it requires
exactly one outer sandbox exec, zero pre-boundary filesystem/cache actions,
zero nested sandbox invocations, inherited denial in a contained child, policy
byte identity, and direct-contained refusal; it no longer requires every child
argv to contain `sandbox-exec`.

Inside that boundary, the orchestrator validates the pinned interpreter and
`uv` bytes with Python SHA-256 and creates task directories with no-follow
ownership/mode checks. The template is the exact `BOOTSTRAP_SEATBELT_TEMPLATE` string
constant in `scripts/bootstrap_ceq1_runtime.py`; it is not a separately mutable
file. The committed manifests bind only the portable template bytes/hash
and its exact closed placeholder schema. The ignored runtime receipt binds the
rendered bytes/hash plus the validated logical-ID-to-absolute-realpath parameter
map; no absolute path enters a committed manifest or report. No shell or command
substitution is used. The one outer profile denies all network, and every
contained child inherits it. It reads only the exact reviewed source
cache, interpreter, standard library, `uv`, builder, manifests, and lock
inputs, and writes only under `.ceq1-runtime/bootstrap` and `.ceq1-venv`.

Inside that profile, the orchestrator runs `uv pip compile` only as a diagnostic
resolution step, writing below `.ceq1-runtime/bootstrap` and using all of
`--offline --no-config --no-python-downloads --generate-hashes`, the literal
pinned `--python`, and `--constraint requirements.lock`. It requires exactly
pytest `9.1.1`, pluggy `1.6.0`, iniconfig `2.3.0`, pygments `2.20.0`, and
production-constrained packaging `26.2`. Upstream/cache hashes from that
diagnostic file never enter the canonical qualification lock.

`uv` requires a writable cache for local bookkeeping even in offline mode. The
contained worker therefore takes two closed views of the reviewed source
cache: an identity receipt over path/type/mode/device/inode/link count/size and
mtime/ctime for before/after immutability, and a logical topology receipt over
path/type/mode/size/symlink target for clone comparison. It clones the cache
once with the host-pinned `/bin/cp -cR` into the unique bootstrap root. Through
held no-follow directory descriptors it then rewrites only absolute symlinks
whose targets are strictly below the reviewed source cache to the corresponding
path below the clone. Real-cache characterization found a closed class of uv
build-environment interpreter links whose targets are below the uv-managed
Python-store root but outside both the cache and the manifest-bound CPython
source. Those links may be copied verbatim only when each exact relative path
and target is present in both source receipts; they are classified
`DENIED_EXTERNAL_PYTHON_LINK`, are never followed during clone validation, and
must fail an OS-contained read probe with `EPERM` because the Seatbelt profile
does not grant their target roots. No lock-selected archive or wheel may depend
on one. Any other absolute/escaping link, any changed target, or any readable
denied link is `BLOCKED`. The clone's complete logical topology must equal the
source topology after applying the internal-root substitution and retaining
that exact denied-link class. The worker takes the source identity receipt
again after the clone and after all `uv` work; any source change is `BLOCKED`.
Every `uv` child receives only that unique task-owned clone as `UV_CACHE_DIR`;
no `uv` argv, environment, or resolved cache link may name the reviewed source
cache. The builder remains a read-only RECORD-closed reader of the reviewed
source archive and never invokes `uv`. Tests require the clone command to run
inside the inherited outer Seatbelt, refuse a preexisting or symlinked
destination, prove source-cache immutability, verify exact internal-link
rebasing, exercise a seeded denied-external-link read probe, reject an external
target outside the closed uv-managed Python-store root, and prove that a uv
child pointed at any other cache fails contract validation.

The reviewed uv cache contains extracted exact distributions but not every
original wheel byte needed by hash-required offline installation. Before
creating the venv, `scripts/build_ceq1_wheelhouse.py` therefore reconstructs
only the five reviewed pure-Python wheels into two independent task-owned
staging directories. It never writes to the uv cache. For each package it
resolves the expected cache wheel link under `UV_CACHE_DIR`, requires the
resolved source to stay under `archive-v0`, and matches the committed
`docs/release-safety/ceq1-wheelhouse-manifest.json` package, version, cache
archive ID, original RECORD-byte hash, and complete sorted RECORD-member
path/hash/size set before reading any payload. The manifest also binds the
reconstructor source hash, exact CPython launcher/version hashes, and the exact
standard-library `zipfile.py` hash used to emit and inspect the archives.

The builder resolves source entries through a held directory file descriptor
and `openat(O_NOFOLLOW)`, with matching stable pre/post `fstat`; it rejects hard
links, symlinks, special files, `.data` trees, native binaries, signature files,
non-ASCII paths, traversal, duplicates, and undeclared extras. The sole allowed
extras are regular `**/__pycache__/*.pyc` files individually listed, hashed, and
classified as excluded in the manifest; no other bytecode or extra is allowed.
It accepts only unique safe relative POSIX regular-file paths named by RECORD
and verifies every payload member's exact RECORD SHA-256 and size. The RECORD
self-row is excluded only from those payload hash/size checks; its exact
original bytes are included once as the physically final ZIP member, after all
other RECORD paths have been sorted lexically.
`WHEEL` must say exactly
wheel version `1.0`, `Root-Is-Purelib: true`, and the sole tag `py3-none-any`.
Each derived archive uses `ZIP_STORED`, sorted members, timestamp
`1980-01-01T00:00:00`, empty member extras and archive comment, Unix regular
file mode `0644`, empty member comments, `create_system=3`, ZIP create version
`20`, extract version `20`, zero internal attributes and flag bits, no data
descriptors, no directory entries, and no Zip64. Archive order is exactly
`sorted(non-RECORD members) + RECORD`. The two independent builds
must be byte-identical. The builder then validates the finished ZIP and RECORD
again and matches the manifest's derived filename, member count, byte size, and
new SHA-256, then seals the output read-only and rehashes it. These deterministic
derived hashes are explicitly **not** claimed to equal upstream wheel hashes.

The orchestrator runs the builder twice into disjoint stage A/B roots, validates
both, requires byte identity, copies the validated result into the promoted
task-owned wheelhouse, seals it read-only, and verifies it again. It then
renders a deterministic candidate qualification lock below
`.ceq1-runtime/bootstrap` from the five promoted derived wheel hashes. The
candidate format and dependency order are fixed by the orchestrator and it
must compare byte-for-byte with committed `requirements-ceq1.lock`; canonical
execution never rewrites a repository file. The separate development command
`derive-review-candidate` may emit proposed manifest and lock bytes only below
`.ceq1-runtime/bootstrap/review-candidate`; promotion into version control uses
an explicit reviewed patch, never a tool write.

`requirements-ceq1.lock` therefore pins only the five reviewed derived wheel
hashes. The orchestrator copies the complete manifest-bound CPython base into
`.ceq1-venv/python`, verifies that the copied interpreter's `sys.executable`,
`sys.prefix`, `sys.base_prefix`, stdlib, platstdlib, and loaded extension-module
realpaths are all below that copied root, then creates
`.ceq1-venv/venv` with `uv venv --offline --no-config --no-project
--relocatable --no-python-downloads --python
.ceq1-venv/python/bin/python3.12`. It installs
`requirements.lock` from only the validated task-owned writable cache clone,
whose logical topology is bound to the before/after receipts of the reviewed
read-only source cache, with
`--offline --no-config --no-python-downloads --require-hashes --only-binary
:all: --link-mode copy --exact`, then installs the derived qualification lock
from only the promoted wheelhouse with `--offline --no-config
--no-python-downloads --no-index --find-links <wheelhouse> --require-hashes
--only-binary :all: --link-mode copy --reinstall`. The second install never
uses `--exact`, so it cannot remove product dependencies, and `--reinstall`
forces all five packages—including packaging—to come from the derived
wheelhouse. The reviewed source cache is never any `uv` child's
`UV_CACHE_DIR`. After installation, the orchestrator replaces only the generated
venv interpreter links with exact internal relative targets:
`bin/python -> ../../python/bin/python3.12`, `bin/python3 -> python`, and
`bin/python3.12 -> python`. It renders a closed `pyvenv.cfg` whose `home` is
exactly `../python/bin`; no link or config value names the source worktree. It
copies the completed bundle to a second task path and proves both the venv and
base launchers resolve every prefix, stdlib, extension, and package path inside
that copy. The venv launcher is never a canonical execution entry; the copied
base interpreter runs with `-I -S -B`, and `run_ceq1_env.py` inserts only the
validated copied venv's site-packages. The orchestrator validates the exact five
installed versions and their RECORD members, rejects any bundle link escaping
`.ceq1-venv`, seals the complete Python-plus-venv bundle read-only, and records
its full closed tree manifest. Any input/output or
installed-provenance mismatch is `BLOCKED`, never a cache write, download,
ambient fallback, or weakened hash check.

The load-bearing order is: outer in-memory profile render and true execve;
contained active-Seatbelt proof and policy-byte verification; contained
environment/root validation; create and bind the ignored local receipt;
validate committed inputs; take the source-cache receipt; clone and validate
the unique writable task cache; run diagnostic resolution using only that
clone; run builder stages A and B; compare,
validate, seal, and promote; render and compare the derived lock; create the
copied Python base and venv; install the product lock; force-reinstall the five
derived packages; retake and compare the source-cache receipt; validate and
seal the runtime bundle. Tests assert every literal argv/environment field,
profile content/hash, inherited-sandbox state, cache-clone binding, state
transition, and refusal path. The steps never rely
on a network denial implemented by uv alone.

`scripts/run_ceq1_env.py` resolves and verifies the local interpreter and
worktree root, creates the task-owned direct-run directories with mode `0700`,
sets umask `077`, closes all non-stdio descriptors, constructs the exact new
environment mapping, and uses `os.execve()`; it never reads `.env`, shell
profiles, keychains, or the parent environment values.

Generate `ceq1-toolchain-manifest.json` with a closed schema and symbolic
artifact IDs only—no absolute path. For CPython 3.12.13 and OpenJDK 25.0.2,
compute a deterministic tree digest over the sorted sequence
`{relativePath,type,mode,uidClass,gidClass,symlinkTarget,contentSha256}` for
every entry; reject sockets/devices/FIFOs and links escaping the artifact root.
Record entry count, tree digest, launcher hash, version output hash, and the
digest algorithm version. Record the Firestore 1.19.8 JAR byte hash and the
two lockfile hashes. The toolchain manifest also binds the wheelhouse manifest,
bootstrap-orchestrator source, reconstructor source, canonical parameterized
Seatbelt-template content/hash/schema, every promoted derived wheel hash, and
the sealed Python-plus-venv runtime tree. The ignored runtime receipt, not the
committed manifest, binds the rendered profile and absolute parameters. A
test recomputes exact equality and a one-byte, mode, path,
or symlink-target mutation must fail. Independent review of this manifest is a
canonical preflight prerequisite.

- [ ] **Step 4: Run the test to verify GREEN**

Run the canonical pytest prefix with `tests/test_ceq1_manifest.py`.

Expected: PASS and no provider/network output.

- [ ] **Step 5: Commit the boundary**

```bash
git add .gitignore requirements-ceq1.in requirements-ceq1.lock \
  docs/release-safety/ceq1-wheelhouse-manifest.json \
  docs/release-safety/ceq1-toolchain-manifest.json \
  scripts/bootstrap_ceq1_runtime.py scripts/build_ceq1_wheelhouse.py \
  scripts/run_ceq1_env.py \
  tests/ceq1/__init__.py \
  tests/test_ceq1_manifest.py docs/release-safety/evidence/ceq1/README.md
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
- the public execution schedule has only `schemaVersion`,
  `productionAncestor`, `implementationBase`, and `entries`; each entry has
  exactly `ordinal`, `scenarioId`, `variantId`, `layers`, `inputHash`, and
  `responseHash`;
  it rejects response class, voice eligibility, oracle, sabotage, expected
  outcome, promotion class, and non-claim fields;
- coverage records have exactly `variantId`, `scenarioId`, `layers`,
  `responseClass`, `voiceEligibility`, `oracleHash`, `sabotageId`,
  `promotionClass`, `expectedVerdict`, and `nonClaims`;
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

Define `MANDATORY_VARIANT_IDS` as the exact 55 strings in the approved spec.
Return typed `ValidatedManifest`, `ValidatedExecutionSchedule`, and
`ValidatedCoverage` objects only after exact set equality, path containment,
byte-hash, owner-module-hash, privacy validation, and equality of public
schedule versus sealed coverage `{scenarioId, variantId, layers}` all pass.
Ordinals are unique contiguous integers starting at zero and match the reviewed
Task 7 matrix row order.
The trusted host scheduler may read the public schedule but not the sealed
coverage or oracle. It emits one child descriptor with exactly
`{scenarioId, variantId, layer, inputHash, responseHash}`; the SUT never reads
the public schedule itself.

`privacy.py` must expose `scan_bytes()`, `scan_json()`, `scan_tree()`, and `validate_generation_provenance()`. Errors contain only a rule ID and logical artifact ID, never the matched text. Recognize declared synthetic identities/addresses and `.invalid` domains; do not claim detection of arbitrary copied prose or numbers.

- [ ] **Step 4: Add the generation-provenance declaration**

Create a closed JSON record with a synthetic template version, declared fictional people/properties/domains, scanner rule IDs and hashes only, no raw-source access, and `independentReviewStatus: pending`. Seeded forbidden values exist only in isolated temporary unit-test trees and are never committed. The pending review state is allowed while authoring but makes a canonical gate run `BLOCKED` until an independent reviewer changes it to `approved` after reviewing the exact fixture diff.

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

Expected: all 18 mutation primitives are caught for their intended reason and
the synthetic control remains green. This proves only generic scorer/mutator
mechanics. It cannot authorize a product verdict; Task 7 must separately run
the referenced sabotage against every variant's own control in every declared
layer.

- [ ] **Step 5: Commit scorer calibration**

```bash
git add tests/ceq1/scorer.py tests/ceq1/mutator.py tests/test_ceq1_manifest.py
git commit -m "test: calibrate exact CE-Q1 scoring"
```

### Task 5: Build the deny-default OS sandbox and durable process ownership

**Files:**
- Create: `tests/ceq1/supervisor.py`
- Create: `tests/ceq1/capability_probe.py`
- Create: `tests/test_ceq1_sandbox.py`

- [ ] **Step 1: Write failing deny-default sandbox and ownership tests**

Before any Python guard probe exists, require Seatbelt itself to deny these inert
probes: non-loopback DNS/socket/HTTP, access to the oracle/repository/credential
and keychain/Mach/XPC surfaces, writes outside one task tmp root, and worker
fork/exec. Positive probes may read only an allowlisted projection, write under
the task root, and—only in L3 role—connect to one exact loopback port. Build
every child environment from a new mapping and assert exact key equality; record
and reject any inherited descriptor other than stdio and the named bootstrap,
release, result, and ledger descriptors.

Add lifecycle tests requiring a durably fsynced `PREPARING` receipt before
spawn, a child stopped on an inherited readiness pipe, PID/PGID/start-time and
ancestry capture, atomically fsynced `STARTED`, and release only afterward.
Workers must have exactly stdio plus declared one-shot control descriptors
until release and exactly stdio afterward. TERM cleanup uses bounded TERM→KILL,
closes all parent control descriptors and quarantine files, proves group/
descendant absence and socket/port closure, and only then removes temp/receipt. An unproved cleanup or outer
SIGKILL retains a durable receipt and blocks the next run pending exact
reconciliation.

Add boundary tests that copy the exact sealed Task 1 Python-plus-venv bundle
into a temporary task runtime and require byte/mode/link-manifest equality,
relocatability, and zero escaping links. A released child must prove that
`sys.executable`, `sys.prefix`, `sys.base_prefix`, stdlib, platstdlib, imported
extension modules, and inserted package paths all resolve under its task
runtime while a direct source-Python read is OS-denied. Mutants for a changed
byte/mode/link target, an unsealed source, an attempted uv-cache/source-runtime
read, and any venv create/install/rebuild invocation must all fail before
worker release.

- [ ] **Step 2: Run sandbox tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_sandbox.py`.

Expected: FAIL because the supervisor and capability probes are absent.

- [ ] **Step 3: Implement the minimal Seatbelt supervisor and start barrier**

Generate role profiles with `deny default`, exact read/write subpaths, explicit
Mach/XPC/keychain denials, no general network, and no worker fork/exec after the
single initial worker exec. L1/L2 roles receive no network permission. The
single task-owned worker bootstrap starts normally from the outer supervisor,
closes undeclared FDs, loads and applies its one final role profile directly via
the macOS sandbox API **before** reading case data or importing harness/product
code, reports `STRICT_READY`, and blocks on the release pipe. Do not nest
`sandbox_init()` under `sandbox-exec`; this host rejects nested initialization.
The final profile denies all later fork/exec. The parent independently verifies
the PID/PGID/start identity and strict-profile receipt, atomically writes and
fsyncs `STARTED`, then sends one release byte. Tests deliberately attempt
`fork`, same-interpreter exec, shell exec, and a spawned descendant after
`STRICT_READY`; all must be OS-denied.

Before profile generation, validate the closed toolchain manifest and copy the
already sealed Task 1 Python-plus-venv `.ceq1-venv` bundle, JDK tree, emulator JAR, and validated
derived wheelhouse into the task root. Task 5 does not reinstall either lock
and never reads the original uv cache. Verify exact entry-set/digest equality,
reject escaping links/special files, preserve read-only modes, and require the
copied dependency payload to remain relocatable within the task runtime. Every
worker executes `<task>/runtime/ceq1/python/bin/python3.12 -I -S -B`, then the
reviewed bootstrap inserts only `<task>/runtime/ceq1/venv` site-packages. Before
release, assert `sys.executable`, `sys.prefix`, `sys.base_prefix`, stdlib,
platstdlib, every extension-module realpath, and every inserted package path
are below `<task>/runtime/ceq1`; OS-deny all reads from the source CPython tree.
Hash the copied interpreter, `pyvenv.cfg`, every installed distribution
`RECORD`, executable, and native library before release and after cleanup. Grant every role read-only
access; writable HOME/tmp/cache/output live outside `runtime/`.
Mutation, write-attempt, and post-run digest tests must prove the sealed runtime
did not change. No role reads the original user-writable runtime/JDK/JAR after
this copy step.

The fixed runtime-preparation bootstrap runs once via `/usr/bin/sandbox-exec`
under a no-network profile before copied role runtimes exist. It receives an
empty environment and may read only the manifest-bound Task 1 sealed venv,
JDK/JAR, derived wheelhouse, committed manifests, and locks; it may write only
`<task>/runtime`. It copies and independently rehashes those artifacts, then
compares the complete task-runtime manifest with the Task 1 source manifest.
A missing entry, escaping link, mode drift, or digest mismatch is `BLOCKED`,
never a rebuild, install, download, original-cache read, or ambient fallback.

The outer unsandboxed supervisor never consumes case data or oracles. It owns
only process setup, profiles, receipts, quarantine, and cleanup. It writes and
fsyncs receipts through an open directory FD with atomic rename. It independently
snapshots the task process tree before release and after termination. Receipt
states are exactly `PREPARING`, `STARTED`, `CLEANING`, and `CLEAN`; every
transition is a same-directory atomic rename followed by file and directory
`fsync`. The parent records start identity and ancestry, not PID alone. Unknown
cleanup retains the task tree and receipt and blocks the next run.

The task-owned role command is exactly
`<task-python> -I -S -B <verified-bootstrap.py> ...`. The bootstrap disables
core dumps and uses only the standard library before applying its final role
profile. `PREPARING` is durably created before `Popen`; it contains no invented
child identity. Launch uses `start_new_session=True`, `close_fds=True`,
`stdin=/dev/null`, pre-opened `O_NOFOLLOW` regular quarantine files for stdout
and stderr, and `pass_fds` limited to readiness/release plus a declared one-shot
service-ready FD for infrastructure roles. After Seatbelt, the child inventories
its FDs and requires that exact set, emits a bounded `STRICT_READY` receipt,
closes readiness, and blocks for exactly byte `0x01`. The parent independently
verifies kernel PID/PGID/start identity and ancestry, fsyncs `STARTED`, and then
releases it. EOF, extra bytes, timeout, or parent death exits before imports.
The child closes the release FD before case/product import, leaving only
descriptors 0/1/2; infrastructure closes service-ready after its receipt.

- [ ] **Step 4: Run sandbox tests to verify GREEN**

Expected: every inert forbidden probe is blocked by Seatbelt with
`SANDBOX_BACKSTOP_BLOCKED`; allowed projection/tmp operations pass; no probe
payload is displayed before quarantine scan; receipts and cleanup are exact.

- [ ] **Step 5: Commit the containment prerequisite**

```bash
git add tests/ceq1/supervisor.py tests/ceq1/capability_probe.py \
  tests/test_ceq1_sandbox.py
git commit -m "test: contain CE-Q1 child processes"
```

### Task 6: Add temporal guards inside the proven OS backstop

**Files:**
- Create: `tests/ceq1/guards.py`
- Create: `tests/ceq1/sut_worker.py`
- Create: `tests/ceq1/score_worker.py`
- Create: `tests/ceq1/mutation_worker.py`
- Modify: `tests/ceq1/supervisor.py`
- Modify: `tests/ceq1/capability_probe.py`
- Modify: `tests/test_ceq1_sandbox.py`

- [ ] **Step 1: Write failing dual-layer boundary tests**

Run every forbidden-effect probe inside the already-proven deny-default role
sandbox. Install the Python temporal guard before product imports. Probes cover
Firestore/Firebase/OpenAI/MSAL/discovery construction; DNS/socket/requests/
urllib/http.client/httpx; credential/keychain paths; outside writes; manual-live
imports/subprocesses; Drive/Tasks/follow-up/outbox/send/pending boundaries; and
guard replacement/deletion. Use inert synthetic addresses/data, while relying
on Seatbelt—not inertness—as the backstop.

For each probe require `GUARD_BLOCKED` and one named guard ledger entry. Then run
the same probe with the Python guard deliberately disabled and require
`SANDBOX_BACKSTOP_BLOCKED`. A guard miss that Seatbelt catches is a test RED,
not a pass. Positive adapter calls run in separate children.

Build capability-separation tests from temporary projections and bundles. The
closed role/action reasons include:

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
The worker receives only
`{scenarioId, variantId, layer, inputHash, responseHash}`.

- [ ] **Step 2: Run guard/capability tests to verify RED**

Run the canonical pytest prefix with:

```bash
tests/test_ceq1_manifest.py tests/test_ceq1_sandbox.py
```

Expected: FAIL because `guards.install_temporal_guard` and workers are absent.

- [ ] **Step 3: Implement execution-long guards and capability-separated workers**

Expose these closed interfaces:

Implement the frozen `ChildMounts` record with `projection`, `descriptor`,
`inputs`, `responses`, and `output` `Path` fields. Implement
`build_source_projection(repo_root, destination, relative_paths)` to return a
sorted `relative_path -> sha256` mapping after copying only regular allowlisted
files and making them read-only. Implement keyword-only
`run_sandboxed_child(role, argv, mounts, task_root,
unix_socket_paths=(), loopback_ports=())` to return
a closed `ChildReceipt`; it raises `CapabilityError` with one of the stable
reason codes above for denied or ambiguous outcomes.

The supervisor builds child environments from the exact empty-environment
builder in Task 1 and asserts key equality, FD inventory, umask, cwd, and task
paths before releasing each child. No parent environment is copied.

`guards.py` generalizes the proven collection guard: preload SDK imports before
the socket-construction measurement boundary while network is already denied,
install audit hooks, protect watched module/class attributes, write argument-free
attempt records, and verify identity through `atexit`. Registered local adapter
identities are exact and count-bound; guards never uninstall.

Workers read only their own capability paths. `sut_worker.py` installs the
temporal guard before importing the projected harness. `score_worker.py`
imports only `tests.ceq1.contracts`, `privacy`, and `scorer` from a separate
scoring projection. `mutation_worker.py` imports only contracts and mutator.
All stdout/stderr/result bytes remain quarantined until `privacy.scan_tree()`
passes; the parent returns only an opaque quarantine ID on failure.

Quarantine promotion is fd-based and race-closed: `lstat` accepts only regular
single-link files under the task root, rejects symlinks/hardlinks/devices/FIFOs,
opens with `O_NOFOLLOW`, hashes before and after scanning the same FD, and
atomically promotes only those exact bytes through a fsynced directory FD.

- [ ] **Step 4: Run sandbox tests to verify GREEN**

Run the Task 6 command again. Expected: every canonical probe is
`GUARD_BLOCKED`; disabled-guard mutants are `SANDBOX_BACKSTOP_BLOCKED`; positive
capability operations succeed; cleanup is proven; terminal output contains no
probe payload.

- [ ] **Step 5: Commit the containment layer**

```bash
git add tests/ceq1/guards.py tests/ceq1/supervisor.py \
  tests/ceq1/capability_probe.py tests/ceq1/sut_worker.py \
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
- Create: `docs/release-safety/ceq1-execution-schedule.json`
- Create: `tests/fixtures/ceq1/inputs/*.json`
- Create: `tests/fixtures/ceq1/schemas/*.json`
- Create: `tests/fixtures/ceq1/inputs/ceq-pdf-01.pdf`
- Create: `tests/fixtures/ceq1/responses/*.json`
- Create: `tests/fixtures/ceq1/oracles/*.json`
- Create: `tests/fixtures/ceq1/oracles/coverage-contract.json`
- Create: `tests/fixtures/ceq1/runtime-binding-contract.json`
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
`processing._sheets_client`, `processing.requests`, and
`processing.queue_pending_response`, plus `ai_processing.client`,
`ai_processing._fs`, `ai_processing._sheets_client`, and
`ai_processing.build_conversation_payload`. `processing.py` has no
`build_conversation_payload` alias.

Do not rely on this illustrative list as the binding inventory. Before writing
`RuntimeBindings`, generate `tests/fixtures/ceq1/runtime-binding-contract.json`
from an AST/call-graph inventory rooted at `process_inbox_message`,
`propose_sheet_updates`, `apply_proposal_to_sheet`,
`build_conversation_payload`, and `process_pdf_for_ai`. The closed records are
`{module, attribute, ownerModule, effectClass, allowedReplacementKind}` and
must include every by-value/global provider, Firestore, Sheets, HTTP, Drive,
Tasks, follow-up, notification, logging, message-index, pending-response,
outbox/send, attachment, and dynamic-import boundary transitively reachable in
the declared cases. At minimum this includes provider/effect aliases in
`processing`, `ai_processing`, `messaging`, `clients`, `sheets`,
`sheet_operations`, `notifications`, `followup`, `file_handling`,
`campaign_safety`, and dynamically imported follow-up functions. The test
recomputes exact set equality from the frozen owner-module hashes. An uncovered
effect-capable alias is `INSTRUMENT_FAILURE`; it may not be discovered and
patched ad hoc during execution. In L3, every Firestore alias in this contract
must be the exact same identity-pinned namespace-wrapper root (or a bound child
derived from it); no owner module may retain or receive a raw emulator client.
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
closed table in this task. First create only closed JSON Schemas for the public
manifest, public schedule, input bundle, frozen response queue, oracle,
coverage record, sabotage receipt, and runtime result. Every schema sets
`additionalProperties: false`, rejects duplicate JSON keys and non-finite
numbers at parse time, constrains logical IDs to closed patterns, and forbids
filesystem paths. Write RED validator tests for one complete temporary row and
every missing/extra/type/hash/layer failure. Only after those tests are GREEN
author committed fixture content. Every identity uses a declared `.invalid` domain and every
property/person/value is newly fictional. Input, response, and oracle are in
separate files. The public manifest contains no layer, expected state/verdict,
oracle hash, or sabotage mapping. The public execution schedule contains only
scenario/variant/layer and input/response hashes. The sealed coverage contract
owns all oracle, sabotage, expected-outcome, voice, promotion, and non-claim
fields. Exact schedule-versus-coverage equality is validated before a child is
created.

Each authored input bundle has a closed common envelope—synthetic clock,
scenario/variant ID, source identity, chronological message records, target and
sibling rows/formulas, thread/messages/indexes, configured field modes,
attachments, expected runtime path class, and declared fictional identities—
plus only family-specific typed payload permitted by its JSON Schema. Frozen
response bundles are ordered closed `responses.create` returns with exact
prompt/config/call hashes and typed proposal shapes. Oracles enumerate every
expected and forbidden fact with canonical field/value/unit/basis, exact
supporting source segment/message identity, target property/suite, freshness
rule, allowed transform, response obligations, operation order, complete first-
run/replay state, allowed/forbidden effects, and sabotage reason. No fixture
uses prose-only or substring expectations.

The table below is also the exact authoring assignment. Each row receives a
positive and near-miss case within its named scenario bundle, its own frozen
response call(s), oracle section, and sabotage control; no implementer may
collapse two variant rows into one undifferentiated assertion. The Schemas and
matrix are reviewed and committed before bulk fixture bytes are authored, and
the fixture diff receives its separate privacy/provenance review before any
canonical product run.

Each sealed coverage record is exactly
`{variantId, scenarioId, layers, responseClass, voiceEligibility, oracleHash,
sabotageId, promotionClass, expectedVerdict, nonClaims}`. The validator must
reject a missing/extra field and must verify that `oracleHash` matches the
separately mounted oracle bytes. These fields never enter the public manifest
or SUT descriptor.

The closed response classes are exactly `missing_field_reply`, `terminal_reply`,
`review_no_reply`, `correction_close_reply`, `alternate_reply`, `no_reply`,
`monitored_continuation_reply`, `reply_all_draft`, `launch_draft`,
`missing_field_draft`, `correction_close_draft`, `followup_draft`, and
`continuation_draft`.
`voiceEligibility` is `false` for every current row: non-voice rows are outside
blinded review, and voice rows lack a shared production finalizer. Every
required row's sealed oracle `expectedVerdict` is `PASS_OFFLINE`; diagnostic
rows use `UNVERIFIED`. The authored `sabotageId`, reason, and response class are
closed data and must be reviewed before canonical execution. The `baseline`
column is plan-only review metadata and is never copied into a fixture,
schedule, SUT descriptor, or scorer oracle: `VERIFY` means
no baseline failure is predeclared, while `FAIL` or `UNVERIFIED` must still be
executed and an unexpected pass triggers adversarial review rather than silent
promotion.

| variantId | scenarioId | layers | sabotageId | expected sabotage reason | responseClass | voiceEligibility | promotion | baseline |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `known-filled` | `CEQ-MEM-01` | `L1+L2+L3` | `SAB-EXT01-01` | `KNOWN_FACT_REASKED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `explicit-decline` | `CEQ-MEM-01` | `L1+L2+L3` | `SAB-EXT01-02` | `DECLINED_FACT_REASKED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `correction-after-window` | `CEQ-LONG-01` | `L1+L2+L3` | `SAB-EXT01-03` | `STALE_CORRECTION_WON` | `correction_close_reply` | `false` | `required` | `FAIL` |
| `acknowledgement-not-question` | `CEQ-MEM-01` | `L1+L2+L3` | `SAB-EXT01-04` | `ACK_MISCLASSIFIED_AS_QUESTION` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `fresh-target-terminal` | `CEQ-TERM-01` | `L1+L2+L3` | `SAB-EXT02-01` | `CITED_TERMINAL_NOT_APPLIED` | `terminal_reply` | `false` | `required` | `FAIL` |
| `stale-quoted-terminal` | `CEQ-TERM-02` | `L1+L2+L3` | `SAB-EXT02-02` | `QUOTED_ONLY_TERMINAL_ACCEPTED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `wrong-property-terminal` | `CEQ-WRONG-01` | `L1+L2+L3` | `SAB-EXT02-03` | `CROSS_ENTITY_TERMINAL_ACCEPTED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `addressless-terminal` | `CEQ-TERM-02` | `L1+L2+L3` | `SAB-EXT02-04` | `UNCITED_TERMINAL_ACCEPTED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `ambiguous-terminal` | `CEQ-TERM-02` | `L1+L2+L3` | `SAB-EXT02-05` | `AMBIGUOUS_TERMINAL_ACCEPTED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `same-address-two-suites` | `CEQ-SUITE-01` | `L1+L2+L3` | `SAB-EXT03-01` | `CROSS_SUITE_FACT_ACCEPTED` | `review_no_reply` | `false` | `required` | `FAIL` |
| `mixed-property-pdf` | `CEQ-PDF-01` | `L1+L2+L3` | `SAB-EXT03-02` | `CROSS_PROPERTY_PDF_FACT_ACCEPTED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `mixed-suite-pdf` | `CEQ-SUITE-01` | `L1+L2+L3` | `SAB-EXT03-03` | `CROSS_SUITE_PDF_FACT_ACCEPTED` | `review_no_reply` | `false` | `required` | `FAIL` |
| `exact-target-attachment` | `CEQ-PDF-01` | `L1+L2+L3` | `SAB-EXT03-04` | `SUPPORTED_TARGET_FACT_DROPPED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `rent14-opex4` | `CEQ-OPEX-01` | `L1+L2+L3` | `SAB-EXT04-01` | `RENT_OPEX_CONFLATED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `monthly-annual` | `CEQ-OPEX-01` | `L1+L2+L3` | `SAB-EXT04-02` | `BASIS_CONVERSION_WRONG` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `latest-correction` | `CEQ-OPEX-01` | `L1+L2+L3` | `SAB-EXT04-03` | `STALE_NUMERIC_VALUE_WON` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `numeric-range` | `CEQ-OPEX-01` | `L1+L2+L3` | `SAB-EXT04-04` | `NUMERIC_RANGE_TRANSFORM_WRONG` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `digit-decoy` | `CEQ-OPEX-01` | `L1+L2+L3` | `SAB-EXT04-05` | `DIGIT_DECOY_ACCEPTED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `unsupported-opex` | `CEQ-OPEX-02` | `L1+L2+L3` | `SAB-EXT04-06` | `INVENTED_OPEX_ACCEPTED` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `ordered-success` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-01` | `TERMINAL_OPERATION_ORDER_WRONG` | `terminal_reply` | `false` | `required` | `FAIL` |
| `move-failure` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-02` | `MOVE_FAILURE_HIDDEN` | `terminal_reply` | `false` | `required` | `FAIL` |
| `comment-failure` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-03` | `COMMENT_FAILURE_HIDDEN` | `terminal_reply` | `false` | `required` | `FAIL` |
| `highlight-failure` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-04` | `HIGHLIGHT_FAILURE_HIDDEN` | `terminal_reply` | `false` | `required` | `FAIL` |
| `audit-write-failure` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-05` | `AUDIT_FAILURE_HIDDEN` | `terminal_reply` | `false` | `required` | `FAIL` |
| `terminal-state-failure` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-06` | `FALSE_TERMINAL_COMPLETION` | `terminal_reply` | `false` | `required` | `FAIL` |
| `column-beyond-z` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-07` | `COMMENT_COLUMN_ADDRESS_TRUNCATED` | `terminal_reply` | `false` | `required` | `FAIL` |
| `retry-after-partial-attempt` | `CEQ-TERM-01` | `L2+L3` | `SAB-EXT05-08` | `PARTIAL_RETRY_DUPLICATED_EFFECT` | `terminal_reply` | `false` | `required` | `FAIL` |
| `viable-alternate` | `CEQ-ALT-01` | `L1+L2+L3` | `SAB-EXT06-01` | `ALTERNATE_ACTION_MISSING` | `alternate_reply` | `false` | `required` | `FAIL` |
| `alternate-unavailable` | `CEQ-ALT-01` | `L1+L2+L3` | `SAB-EXT06-02` | `UNAVAILABLE_ALTERNATE_ACTIONED` | `terminal_reply` | `false` | `required` | `FAIL` |
| `two-alternates` | `CEQ-ALT-01` | `L1+L2+L3` | `SAB-EXT06-03` | `ALTERNATE_CARDINALITY_WRONG` | `alternate_reply` | `false` | `required` | `FAIL` |
| `same-event-replay` | `CEQ-ALT-01` | `L1+L2+L3` | `SAB-EXT06-04` | `DUPLICATE_ALTERNATE_ACTION` | `alternate_reply` | `false` | `required` | `FAIL` |
| `direct-broker-question` | `CEQ-IN-09` | `L1+L2+L3` | `SAB-IN09-01` | `UNSAFE_BROKER_QUESTION_ANSWERED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `confidential-identity-question` | `CEQ-IN-09` | `L1+L2+L3` | `SAB-IN09-02` | `CONFIDENTIAL_IDENTITY_DISCLOSED` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `question-plus-partial-specs` | `CEQ-IN-09` | `L1+L2+L3` | `SAB-IN09-03` | `SAFE_FACTS_DROPPED_ON_REVIEW` | `review_no_reply` | `false` | `required` | `FAIL` |
| `unrelated-mail` | `CEQ-IN-10` | `L2+L3` | `SAB-IN10-01` | `UNTRACKED_MAIL_MUTATED_STATE` | `no_reply` | `false` | `required` | `VERIFY` |
| `quoted-cre-nearmiss` | `CEQ-IN-10` | `L2+L3` | `SAB-IN10-02` | `QUOTED_CRE_NEARMISS_PROCESSED` | `no_reply` | `false` | `required` | `VERIFY` |
| `tracked-reply-nearmiss` | `CEQ-IN-10` | `L2+L3` | `SAB-IN10-03` | `TRACKED_NEARMISS_PROCESSED` | `no_reply` | `false` | `required` | `VERIFY` |
| `thirteen-message-window` | `CEQ-LONG-01` | `L1+L2+L3` | `SAB-CHR-01` | `HISTORY_WINDOW_BYPASSED` | `correction_close_reply` | `false` | `required` | `FAIL` |
| `delayed-inbound-order` | `CEQ-LONG-01` | `L1+L2+L3` | `SAB-CHR-02` | `DELAYED_INBOUND_ORDER_WRONG` | `missing_field_reply` | `false` | `required` | `FAIL` |
| `pause-hold` | `CEQ-LONG-01` | `L2+L3` | `SAB-CHR-03` | `PAUSED_THREAD_CONTINUED` | `no_reply` | `false` | `required` | `VERIFY` |
| `monitored-resume` | `CEQ-LONG-01` | `L2+L3` | `SAB-CHR-04` | `UNSUPPORTED_RESUME` | `monitored_continuation_reply` | `false` | `required` | `FAIL` |
| `settled-replay` | `CEQ-LONG-01` | `L2+L3` | `SAB-CHR-05` | `SETTLED_REPLAY_STATE_DELTA` | `no_reply` | `false` | `required` | `FAIL` |
| `dated-ooo` | `CEQ-OOO-01` | `L2+L3` | `SAB-AUTO-01` | `OOO_EXTRACTED_OR_REPLIED` | `no_reply` | `false` | `required` | `VERIFY` |
| `generic-auto-ack` | `CEQ-OOO-01` | `L2+L3` | `SAB-AUTO-02` | `AUTOACK_EXTRACTED_OR_REPLIED` | `no_reply` | `false` | `required` | `VERIFY` |
| `quoted-cre-ooo` | `CEQ-OOO-01` | `L2+L3` | `SAB-AUTO-03` | `QUOTED_CRE_OOO_PROCESSED` | `no_reply` | `false` | `required` | `VERIFY` |
| `copied-party-reply-all` | `CEQ-AUDIENCE-01` | `L2+L3` | `SAB-AUD-01` | `CC_DROPPED_OR_MISROUTED` | `reply_all_draft` | `false` | `required` | `UNVERIFIED` |
| `display-name-ambiguity` | `CEQ-AUDIENCE-01` | `L2+L3` | `SAB-AUD-02` | `AMBIGUOUS_AUDIENCE_GUESSED` | `reply_all_draft` | `false` | `required` | `UNVERIFIED` |
| `wrong-tenant-signature-decoy` | `CEQ-AUDIENCE-01` | `L2+L3` | `SAB-AUD-03` | `SIGNATURE_IDENTITY_DRIFT` | `reply_all_draft` | `false` | `required` | `UNVERIFIED` |
| `native-text-three-page` | `CEQ-PDF-01` | `L1+L2+L3` | `SAB-PDF-01` | `NATIVE_PDF_PAGE_BINDING_WRONG` | `review_no_reply` | `false` | `required` | `VERIFY` |
| `image-only-explicitly-unverified` | `CEQ-PDF-01` | `L1` | `SAB-PDF-02` | `OCR_CAPABILITY_OVERCLAIMED` | `no_reply` | `false` | `diagnostic` | `UNVERIFIED` |
| `launch` | `VOICE-LAUNCH` | `L1` | `SAB-VOICE-01` | `VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER` | `launch_draft` | `false` | `diagnostic` | `UNVERIFIED` |
| `missing-field` | `VOICE-MISSING` | `L1` | `SAB-VOICE-02` | `VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER` | `missing_field_draft` | `false` | `diagnostic` | `UNVERIFIED` |
| `correction-close` | `VOICE-CORRECTION-CLOSE` | `L1` | `SAB-VOICE-03` | `VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER` | `correction_close_draft` | `false` | `diagnostic` | `UNVERIFIED` |
| `followup` | `VOICE-FOLLOWUP` | `L1` | `SAB-VOICE-04` | `VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER` | `followup_draft` | `false` | `diagnostic` | `UNVERIFIED` |
| `continuation` | `VOICE-CONTINUATION` | `L1` | `SAB-VOICE-05` | `VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER` | `continuation_draft` | `false` | `diagnostic` | `UNVERIFIED` |

Each variant contains a positive/near-miss execution and a sabotage ID. For
every `{scenarioId, variantId, layer}` record, calibration executes that
sabotage against the variant's own synthetic known-good control and requires
its exact stable reason. Missing, extra, or duplicate calibration tuples are
`INSTRUMENT_FAILURE`. Image-only PDF and all five voice variants execute as
diagnostics with exact `UNVERIFIED` non-claims. Voice scoring rejects raw
`proposal.response_email` and the current missing-field selected template as
proof of a shared final rendered draft.

Run `fixture_builder.py` only over newly authored templates to create the
native three-page PDF. The provenance receipt starts as `pending`; after a
fresh independent reviewer verifies the exact fixture diff contains no copied
customer content or PII, change only `independentReviewStatus` and reviewer role
to `approved` in a separate commit before a canonical baseline run.

- [ ] **Step 7: Run the full L1/L2 deck and exact closure checks**

Run:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  -m pytest -q -p no:cacheprovider \
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
  docs/release-safety/ceq1-execution-manifest.json \
  docs/release-safety/ceq1-execution-schedule.json tests/fixtures/ceq1
git commit -m "test: exercise CE-Q1 semantic and state replays"
```

### Task 8: Build the frozen-draft voice review instrument

**Files:**
- Create: `tests/ceq1/voice.py`
- Create: `tests/test_ceq1_voice.py`
- Modify: `tests/ceq1/contracts.py`
- Modify: `tests/ceq1/manifest.py`

- [ ] **Step 1: Write failing voice eligibility and blinded-review tests**

Define a closed `FinalDraft` record with exactly `subject`, `plainBody`,
`htmlBody`, `to`, `cc`, `replyMode`, `signatureIdentity`, `scenarioId`,
`variantId`, `productionFinalizer`, and `productionFinalizerHash`. Tests reject
raw `proposal.response_email`, `_select_automatic_response_body()` output, a
missing-field template, harness-reconstructed metadata, or a draft without one
of the five production-owned finalizer identities required by the spec.

Define the exact five scored dimensions—natural flow, professional tone,
context continuity, concision, and absence of obvious AI tells—and hard
faults for semantic/grounding error, known/declined-field re-ask, invented
commitment, audience/signature drift, duplicate greeting/signoff, and AI-tell
or broken punctuation. A synthetic instrument-only packet tests exact scores,
two blinded independent reviewers, a third reviewer when any dimension differs
by more than one point, zero hard faults, and **both reviewers scoring every
dimension at least 4/5**. Any draft below 3/5, any hard semantic fault, or any
safety disagreement fails rather than being averaged. At least one
reviewer must have role `human_operator`; reviewer IDs are opaque role-local
codes and contain no name, email, or other PII.

Tests require the closed statuses `review_ready`, `partial`,
`pass_frozen_drafts`, and `fail`; a voice status never upgrades or suppresses a
hard-safety verdict. The current `b400ee5` cases must all be ineligible because
the shared production finalizers do not exist, produce no blinded packet, and
remain diagnostic `UNVERIFIED`—not a fake voice failure or pass.

- [ ] **Step 2: Run voice tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_voice.py`.

Expected: FAIL because the closed voice instrument is absent.

- [ ] **Step 3: Implement only the qualification-side instrument**

`voice.py` validates eligible production-owned `FinalDraft` records, generates
a randomized opaque packet without scenario expectation/oracle/failure reason,
validates reviewer forms, applies the fixed rubric and tie-break rule, and
emits a redacted structured receipt. It imports no product module and cannot
render, rewrite, repair, or choose product copy. Calibration uses newly authored
synthetic `FinalDraft` controls only to prove the rubric detects each hard fault
and score threshold; those controls are labeled `INSTRUMENT_ONLY` and never
count toward product voice evidence.

- [ ] **Step 4: Run voice tests to verify GREEN**

Expected: instrument controls PASS, every product `VOICE-*` context is exactly
`UNVERIFIED_NO_SHARED_FINALIZER`, and no reviewer packet requiring human work is
generated for the current baseline.

- [ ] **Step 5: Commit the voice instrument**

```bash
git add tests/ceq1/voice.py tests/test_ceq1_voice.py \
  tests/ceq1/contracts.py tests/ceq1/manifest.py
git commit -m "test: add CE-Q1 frozen draft review contract"
```

### Task 9: Prove L3 persistence with a pinned task-owned Firestore emulator

**Files:**
- Create: `tests/ceq1/firestore_audit_proxy.py`
- Create: `tests/ceq1/firestore_emulator.py`
- Create: `tests/test_ceq1_emulator_replay.py`
- Modify: `tests/ceq1/adapters.py`
- Modify: `tests/ceq1/harness.py`
- Modify: `tests/ceq1/supervisor.py`

- [ ] **Step 1: Write failing pinned-prerequisite and lifecycle tests**

Require the exact closed toolchain manifest from Task 1 and reject
entry-set/type/symlink/mode/owner/content/version drift before spawning
anything. The current source prerequisites include:

```text
/opt/homebrew/Cellar/openjdk/25.0.2/libexec/openjdk.jdk/Contents/Home/bin/java
sha256 370ef109f74f859afc8cfe0300b2da782d60698160b8a48f19731d6d2e3012ea

/Users/baylorharrison/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.8.jar
sha256 9d43599ed6151199e8d604dc87fac51218e49e5f3a48519b1ae560bbe5e3382d
```

Verify the full JDK tree, not only `bin/java`; verify the JAR byte hash; copy
both to the task root; verify the copies; make them read-only; and run
`java -jar <copied-jar> --version` inside the emulator sandbox without
download/update behavior. Test a fresh task-owned loopback
proxy/emulator process group, startup receipt, bounded TERM-to-KILL cleanup,
quarantine-file closure, port/socket closure, and temp retention on unproved cleanup. No
test may invoke Firebase CLI or download an emulator/JDK.

- [ ] **Step 2: Write failing namespace-wrapper and independent-audit tests**

Require a fluent wrapper over collection/document/query/batch/transaction
references. It canonicalizes and checks every read/query/create/update/delete
path before transport. Batch and transaction child references remain wrapped.

Run a separate gRPC forwarding process between the Python SDK and the emulator.
The SUT-to-proxy hop uses an exact task-owned Unix-domain socket; the proxy-to-
emulator hop uses one exact loopback TCP port. The SUT-facing `RPC_SPEC_V1`
registers exactly `BatchGetDocuments` (unary-stream), `RunQuery`
(unary-stream), `BeginTransaction`, `Commit`, and `Rollback` (unary-unary).
Complete-inventory routines inside the proxy may separately call
`ListCollectionIds` and `ListDocuments` through its private upstream channel
when commanded over the supervisor-only control FD; those methods are not
registered on the SUT socket. `GetDocument`, direct create/update/delete,
`BatchWrite`, `Write`, `Listen`, aggregation/partition/pipeline/long-running
operations, and every unknown RPC are rejected before upstream transport.

Each registered method binds the generated Firestore 2.28 request/response
classes, serializers, and cardinality. After parse, clone the protobuf, discard
unknown fields, deterministically serialize both copies, and reject any
difference. Extract and validate database/name/parent, batch-get documents and
found/missing responses, query parent/from collections, Commit update/delete/
transform document names, field masks, preconditions, transaction IDs, and
response document names. Record method, canonical paths, attempt ordinal,
stream request/response/close cardinalities, status code, and transaction/token
hashes without field values or raw error bodies. Unknown fields, methods, stream
shapes, resource-bearing responses, or cardinalities are
`INSTRUMENT_FAILURE`.

A client interceptor injects one opaque `x-ceq1-operation-id` from the wrapper
`ContextVar`; the proxy strips it before upstream forwarding. Every RPC must
reconcile to one wrapper ledger operation by operation ID, method, ordered
attempt, and canonical resource set. Transaction retries reuse the operation ID
with an exact attempt ordinal; one wrapper operation need not equal one RPC.
Independently of wrapper attribution, the proxy validates every parsed request
and response against the exact `(default)` database root and task namespace
document prefix **before** forwarding or returning it. Collection-group/all-
descendant queries, malformed names, missing attribution, or any resource
outside the prefix are rejected and mark `INSTRUMENT_FAILURE`.

The topology is exact and independently attacked:

- SUT Seatbelt permits file read/write-connect only to the exact task-owned
  proxy Unix socket and denies all TCP networking;
- proxy Seatbelt permits only the exact Unix socket plus remote TCP to
  `localhost:<emulatorPort>`;
- emulator Seatbelt permits local TCP on `<emulatorPort>` and no outbound
  network;
- SDK endpoint is exactly `unix:<proxySocket>` with `grpc.insecure_channel`,
  `AnonymousCredentials`, synthetic project `demo-ceq1-<taskId>`, and database
  `(default)`; TLS, ADC, default endpoint discovery, alternate targets, and
  channel reconstruction are rejected;
- a SUT `connect_ex` directly to `<emulatorPort>` must fail with OS `EPERM`,
  while the same SUT reaches the proxy Unix socket successfully.

The generated profiles use literal Unix-socket and port filters, never a broad
loopback allow. The SUT allows only `network-outbound` to the proxy
`remote unix-socket path-literal`; proxy allows only inbound on that exact
`local unix-socket` and outbound `remote ip localhost:<emulatorPort>`;
emulator allows only inbound `local ip localhost:<emulatorPort>`. All TCP binds
use numeric `127.0.0.1`; IPv6 and DNS remain denied. Mutants cover every other
TCP/Unix destination and emulator outbound access.

The proxy alone creates the Unix socket at a nonce-bearing path inside its
private task directory with mode `0600`; the parent verifies lstat type,
ownership, mode, inode, and proxy PID/start identity before releasing SUT. For
Java, the supervisor reserves the selected loopback port until immediately
before the gated emulator spawn, then proves the listener belongs to the exact
recorded Java PID/start identity and the unique synthetic project responds
before it releases the proxy or SUT. Any race, owner ambiguity, unexpected
listener/socket replacement, or readiness mismatch is `INSTRUMENT_FAILURE`;
no random check-then-use port is accepted as proof. Python gRPC inherited-TCP-
listener FD handoff is not assumed or required.

Only the L3 SUT environment adds
`FIRESTORE_EMULATOR_HOST=unix:<proxySocket>`,
`GOOGLE_CLOUD_PROJECT=demo-ceq1-<taskId>`, and the equal `GCLOUD_PROJECT`.
Java alone adds task-owned `JAVA_HOME`. Proxy/emulator coordinates otherwise
arrive through bounded control records, not ambient environment.

Calibration uses its own fresh proxy/emulator/process group. Raw Commit messages
containing out-of-namespace update, delete, and transform paths must be parsed,
recorded, and rejected `PERMISSION_DENIED` **before** upstream transport;
missing operation metadata is `UNATTRIBUTED_RPC`, an unknown method is
`UNIMPLEMENTED`, and direct SUT-to-emulator transport is OS-denied. Upstream
mutation count and before/after inventory remain zero-delta—do not create then
delete an outside document to prove detection. Tear down calibration completely
and start a fresh canonical proxy/emulator with no calibration capability. The
ordinary out-of-namespace mutant remains rejected by the wrapper before proxy
transport.

- [ ] **Step 3: Run L3 tests to verify RED**

Run the canonical pytest prefix with `tests/test_ceq1_emulator_replay.py`.

Expected: FAIL because emulator/proxy/wrapper implementations are absent.

- [ ] **Step 4: Implement the pinned launcher, proxy, and namespace wrapper**

The launcher uses the task-owned copied Java/JAR with direct argv only:

```text
<java> -XX:-UsePerfData -Djava.io.tmpdir=<task>/tmp \
  -jar <jar> --host 127.0.0.1 --port <emulatorPort>
  --project_id demo-ceq1-<taskId> --single_project_mode true
  --single_project_mode_error true
```

It stores exact argv, copied-tree/JAR hashes, PID/PGID/start identity, child
tree, Unix-socket identity, port owner identities, strict-profile receipt, and
pipe state. The SUT talks only to the proxy Unix socket. The proxy talks only to
the emulator loopback port.
Sandbox profiles allow those exact directions and deny every other network
endpoint.

Use two independent raw inventories before the first mutation, after each
transition, and after replay. Reconcile wrapper ledger, proxy audit ledger, and
the final complete emulator inventory by path/method/cardinality. A missing or
unattributed proxy request, ledger disagreement, or path outside the task
namespace is `INSTRUMENT_FAILURE` even if final state is empty.

- [ ] **Step 5: Run mandatory L3 scenarios, interruption, replay, and cleanup**

Run the Task 9 test again. Expected: required state scenarios exercise actual
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

### Task 10: Orchestrate the fixed schedule and commit the honest baseline finding

**Files:**
- Create: `scripts/run_ceq1.py`
- Create: `docs/release-safety/evidence/ceq1/baseline-report.json`
- Create: `docs/release-safety/evidence/ceq1/baseline-report.md`
- Modify: `tests/ceq1/supervisor.py`
- Modify: `tests/test_ceq1_manifest.py`
- Modify: `tests/test_ceq1_semantic_replay.py`
- Modify: `tests/test_ceq1_stateful_replay.py`
- Modify: `tests/test_ceq1_emulator_replay.py`
- Modify: `tests/test_ceq1_voice.py`

- [ ] **Step 1: Write failing CLI, schedule, and report-schema tests**

Require exact subcommands `preflight`, `calibrate`, `run`, `verify-report`, and
`assemble-evidence`. `run` has only declared tiers `l1`, `l2`, `l3`, or `all`.
The frozen full schedule is required variants forward once, required variants
in reverse once, three fresh-process repetitions, then declared diagnostic
variants. No retry-until-green, case filter, xfail, skip, or best-of-N option
exists.

“Forward” is the Task 7 matrix filtered to `promotionClass=required` in
ascending `ordinal`, expanding each row's layers in `L1`, `L2`, `L3` order and
omitting undeclared layers. “Reverse” is that exact required tuple sequence in
reverse. Repetitions 1, 2, and 3 each restart at the required forward first
tuple with a new task ID, process tree, adapter state, and L3 namespace. Only
after those required tuples, diagnostic rows execute in ascending ordinal and
declared-layer order. The public schedule stores each matrix ordinal explicitly;
the validator recomputes it from the matrix and rejects reordering, duplication,
or omission.

Canonical promotion mode stops on the first non-pass and seals that authoritative
gate verdict. Because the approved `b400ee5` baseline is expected to fail,
Task 10 runs that full canonical schedule with stop-on-first-nonpass. Only a
fully green future candidate may finish the schedule and reach `PASS_OFFLINE`.
After a non-pass report is sealed, a separate explicit diagnostic continuation
may execute the unexecuted remainder; it reports coverage and future work but
cannot replace, widen, or issue a gate verdict. Tests require the canonical
verdict source and diagnostic coverage receipt to be separately labeled and
prove the report reducer never derives a verdict from diagnostic attempts.
The canonical and diagnostic reports are distinct immutable closed records.
The diagnostic report contains the canonical report hash and exact next tuple,
contains no gate-verdict field, and covers only the unexecuted suffix.
`assemble-evidence` accepts only two already-verified immutable reports (or one
fully complete green canonical report), proves their schedule partitions are
ordered/disjoint/complete, preserves the canonical verdict byte-for-byte, and
deterministically produces the sole combined evidence view.

Report tests require `executionHead` plus non-self-referential
`evidenceCarrierParent`, product
source/production ancestor, toolchain/dependency/public-manifest/public-schedule/
sealed-coverage/fixture/oracle/owner/projection hashes; and a path-free
`sandboxPolicyReceiptDigest` over the rendered-profile hash plus a canonical
logical-ID-to-realpath-parameter digest. The raw parameter map and absolute
paths remain only in the ignored receipt; canonical, diagnostic, assembled,
and committed reports carry the same opaque receipt digest, and
`verify-report` must recompute it while the ignored receipt is available and
otherwise verify its chain from the already verified parent report. Any digest
mismatch or missing receipt is `INSTRUMENT_FAILURE`. Reports also require the planned exact 19
scenarios/55 variants; canonical attempted cardinality through its first
non-pass; diagnostic full closure and attempt cardinality; separate
historical/current source labels; per-layer structured results;
before/after/replay hashes; and exact zero counts for successful external
effects. Forbidden **attempts** are separately counted and must be zero in
product execution; deliberate calibration attempts appear only in the named
contained calibration ledger with `GUARD_BLOCKED` or
`SANDBOX_BACKSTOP_BLOCKED` and zero successful effect.

Reports also include per-field exact-accuracy and exact-abstention counts by
family; named counts for wrong-row/cross-entity writes, known/declined re-asks,
uncited terminal decisions, duplicate actions, forbidden events, provider
construction, network, pending/outbox/send/follow-up, and replay deltas; voice
eligibility/status, dimension scores, hard-fault counts, reviewer-role
provenance, and tie-break status; non-claims; deterministic verdict precedence;
privacy scan receipt; complete process/port/toolchain cleanup receipt; and an
exact next-gate eligibility record. Raw messages, fixture bodies, recipient
values, credentials, absolute paths, and unredacted failure payloads are
forbidden. The report may say only that the profiled CE-Q1 processes were
restricted and their wrapper/proxy/inventory ledgers reconciled; it may not
claim a macOS network namespace or exclusion of unrelated host processes.

- [ ] **Step 2: Run CLI/report tests to verify RED**

Run the canonical pytest prefix with all six CE-Q test modules.

Expected: FAIL because `scripts/run_ceq1.py` and report assembly are absent.

- [ ] **Step 3: Implement the thin CLI and fixed supervisor schedule**

`scripts/run_ceq1.py` only parses the closed CLI and calls supervisor methods;
it imports no product module. `preflight` verifies clean exact candidate HEAD,
ancestry from `6caa8ec`, implementation-base product-file equality with
`b400ee5`, owner/transitive projection/dependency hashes, fixture hashes,
sandbox probes, pinned Java/JAR, and no unreconciled task receipt.

The execution source identity is
`{executionHead, productSourceBase, productionAncestor}`; never claim execution
HEAD equals `b400ee5`. The later report-only commit is
verified externally after creation. Inside the report,
`evidenceCarrierParent` equals `executionHead`; neither the later commit SHA nor
a digest of the blob containing itself is stored in the report. The carrier commit may
differ only by the two generated evidence files, and final review records its
exact commit/tree/blob SHAs outside those files.
Any product-file drift requires a separately reviewed successor identity.

`calibrate` first runs the known-good synthetic control and all 18 generic
mutation primitives in separate scorer/mutator children. It then runs every
sealed coverage record's own sabotage against that variant's own known-good
control in every declared layer and requires exact set equality over
`{scenarioId, variantId, layer, sabotageId, expectedReason}`. Guard, provider,
network, filesystem, process, and transport sabotages are actual OS-contained
probes, never ledger-only mutations. `run --tier all` uses only the supervisor,
new process/state/namespace per attempt, and a synthetic clock. Canonical mode
follows the full frozen schedule and stops at the first non-pass; the explicit
diagnostic continuation runs only the unexecuted suffix after a non-pass
canonical report. Typed digests omit only declared volatile process/time/path
receipt fields.

`verify-report` validates canonical, diagnostic, and assembled schemas without
rewriting them. `assemble-evidence` reads a verified canonical report plus its
hash-bound verified diagnostic continuation, recomputes complete cardinality,
and emits `assembled-report.json`/`.md`; its verdict field is copied only from
the canonical report. Tests mutate either input, overlap/drop/reorder a tuple,
add a verdict to diagnostics, or change the canonical verdict and require exact
rejection.

- [ ] **Step 4: Run fresh preflight and calibration**

Run:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py preflight \
  --output .ceq1-runtime/preflight.json
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py calibrate \
  --output .ceq1-runtime/calibration.json
```

Expected: preflight `PASS`; the generic calibration control is `VERIFIED`; all
18 primitive mutants and every variant/layer sabotage are `REFUTED` for their
exact intended reasons with exact tuple-set equality; deliberate forbidden
attempts are contained and zero effects succeed; quarantined artifacts are
privacy-clean. If a local prerequisite is absent, preflight returns
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
  tests/test_ceq1_stateful_replay.py tests/test_ceq1_emulator_replay.py \
  tests/test_ceq1_voice.py
git commit -m "test: orchestrate the CE-Q1 qualification gate"
test -z "$(git status --short)"
```

- [ ] **Step 6: Execute the canonical offline baseline once**

Run:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py run --tier all \
  --mode canonical --output .ceq1-runtime/canonical
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py verify-report \
  .ceq1-runtime/canonical/report.json
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py run --tier all \
  --mode diagnostic-continuation --canonical-report \
  .ceq1-runtime/canonical/report.json --output .ceq1-runtime/diagnostic
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py verify-report \
  .ceq1-runtime/diagnostic/report.json --canonical-report \
  .ceq1-runtime/canonical/report.json
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py assemble-evidence \
  --canonical-report .ceq1-runtime/canonical/report.json \
  --diagnostic-report .ceq1-runtime/diagnostic/report.json \
  --output .ceq1-runtime/assembled
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  scripts/run_ceq1.py verify-report \
  .ceq1-runtime/assembled/report.json
```

Expected canonical product outcome on `b400ee5`: stop at the first required
non-pass and seal `FAIL` with its exact evidence. Expected diagnostic outcome:
all remaining declared cases/attempts execute with stable digests and zero
forbidden effects, exposing the paused-send pending projection and other
promotion-required gaps plus declared `UNVERIFIED` voice/image-only/non-atomicity
records, but issue no gate verdict. `PASS_OFFLINE` would be unexpected at this
baseline and triggers adversarial review rather than promotion.

- [ ] **Step 7: Generate sanitized committed evidence from the verified report**

Only after canonical, diagnostic, and assembled reports independently verify
and the quarantine/output trees pass `privacy.scan_tree()`, copy the assembled
`report.json`/`.md` as `baseline-report.json`/`.md`. Include stable
reason codes and redacted diffs, never raw fixture bodies. Before rendering,
`lstat` every candidate output, reject links/special files/multiple hard links,
open with `O_NOFOLLOW`, scan and hash through that FD, rehash without closing,
then atomically copy the exact scanned bytes through a fsynced evidence-
directory FD. Run `verify-report` again on the committed form and require exact
semantic equality to the quarantined assembled report, canonical-report hash,
diagnostic-report hash, and unchanged canonical verdict.

- [ ] **Step 8: Run the final affected and broad regression gates**

Run:

```bash
/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  -m pytest -q -p no:cacheprovider \
  tests/test_ceq1_manifest.py tests/test_ceq1_sandbox.py \
  tests/test_ceq1_semantic_replay.py tests/test_ceq1_stateful_replay.py \
  tests/test_ceq1_emulator_replay.py tests/test_ceq1_voice.py \
  tests/test_test_collection_contract.py \
  tests/test_runtime_provider_initialization.py \
  tests/test_process_user_service.py

/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  -m pytest --noconftest --collect-only -q \
  -p no:cacheprovider

/usr/bin/env -i PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  ./.ceq1-venv/python/bin/python3.12 -I -S -B scripts/run_ceq1_env.py \
  -m py_compile \
  tests/ceq1/*.py scripts/bootstrap_ceq1_runtime.py \
  scripts/build_ceq1_wheelhouse.py scripts/run_ceq1.py scripts/run_ceq1_env.py
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

After Task 10, perform independent reviews in this order:

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
