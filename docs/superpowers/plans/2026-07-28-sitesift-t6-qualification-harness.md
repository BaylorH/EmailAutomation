# SiteSift T6 Qualification Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Deliverable:** Both — committed qualification-harness code plus a verified,
zero-effect T6a finding. No L3/L4 live result is claimed by this plan.

**Goal:** Build and prove offline the non-production harness that binds the
frozen SiteSift candidate to a bounded real-provider L3 gate and a separately
authorized, test-owned L4 `1 -> 3 -> 10 -> 22` worker ladder.

**Architecture:** Keep all harness behavior on the qualification-only branch,
verify and extract the immutable candidate artifacts, and launch their code in
an isolated child rather than importing the branch copy. A strict local
admission layer, atomic qualification ledger, encrypted recovery capsule,
privacy-safe evidence contract, and synthetic fixture corpus surround the
candidate without weakening or replacing its effect gateway.

**Tech Stack:** Python 3.12.13, standard-library dataclasses/JSON/tar/zip/HMAC,
`openpyxl==3.1.5`, `cryptography==49.0.0`, pinned `jsonschema`, `unittest`,
macOS `sandbox-exec`, Google/Firebase/Microsoft SDKs inherited from the frozen
`requirements.lock` through a separate qualification lock, and the existing
SiteSift effect gateway/provider replay contracts.

---

## Immutable inputs

| Input | Required identity |
|---|---|
| Frontend/Functions source | `b4636e8276db18cb633d8c9e27b5e05fa9dc21a9` |
| Backend source | `f104b5f4cfc7574188e47efaadbf72df219e19a5` |
| Combined manifest SHA-256 | `fb1b23c27525aa405e16f35ed71599c7887023e90d41677049b7c3097214cbaa` |
| Worker manifest SHA-256 | `b6ebb974ed4c0ddaa618f7f9d165c207cc569b862d9d27e8724f64e0871b4abc` |
| Worker archive SHA-256 | `909c4cdd6d9bfc3c1e276a313dc1b52781e6708913547f27e19fd63b1a9b5138` |
| Worker archive size | `23,941,120` bytes |
| Extracted-tree algorithm | `tree-v1-regular-files` |
| Extracted regular-file tree SHA-256 | `8859b9b27ea7861220b1e48c0c46eb342aa6756ff044945cd7485dc34ade8d5c` |
| Worker regular-file count | `514` |
| Dependency-lock SHA-256 | `55eb86c3569e41a4fb92a5090c63887def006aebec8ab3926cdd794c052864c6` |
| Dockerfile SHA-256 | `dbe2ad9309a3dc2b53c20e13592489f6f5234b4961a3c650192dafdf2ec72160` |
| Base image | `python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf` |

Canonical local artifact paths for build verification:

```text
/Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/sitesift-turn2-f104b5f/release-manifest.json
/Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-release-manifest.json
/Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-source.tar
```

These paths are T6a build inputs, not committed configuration. They establish
the current candidate's exact refutation. A future T6b must provide explicit
paths for a newly frozen candidate and pass that candidate's independently
recorded hashes.

## T6a effect boundary

Every command in this plan must run without live credentials and without
provider, Firestore, Drive, Sheets, Graph, mailbox, campaign, queue, scheduler,
worker, deployment, IAM, or production activity. Tests use fakes, temporary
directories, and an OS network/process sandbox.

## Planning-audit verdict

The frozen candidate is currently **REFUTED for live T6b admission**. The
verified source audit found:

1. the frozen OpenAI replay transport has no durable idempotency key or
   queryable receipt after an ambiguous response;
2. the worker reaches direct OpenAI, Graph draft/attachment, Drive
   create/permission, and Firebase Storage mutations outside the frozen final
   gateway;
3. the gateway cannot enforce or enumerate the exact approved
   stage/recipient/effect plan by run; and
4. the worker returns no typed counters and has additional mutation surfaces
   that are not yet in an exact snapshot/restoration contract.

Tasks 1–10 therefore build the reusable offline apparatus and encode these
checks as objective live-admission blockers. They do not construct live clients
or issue provider/mail/storage effects. Task 11 records the zero-effect harness
build plus refutation. The next product phase closes the seams, freezes a new
candidate, and reruns its L1/L2 evidence before this harness can seek live
authorization.

The audit has already established that this exact candidate cannot route every
reachable provider/mail/storage mutation through its frozen effect gateway or
reconcile every ambiguous provider/send outcome through a queryable receipt.
Archive execution and technically isolated test credentials are not assumed
either way; Tasks 3, 5, and 6 still test them independently and may add further
closed refutation or `UNAVAILABLE` reasons.

The implementation must preserve completed generic harness work, return the
closed refutation reasons from canonical `verify`/live-admission commands,
record the finding, and never manufacture a passing fake lane.

## File responsibility map

```text
.sitesift-qualification-only
    branch marker consumed by the release-builder refusal
requirements-qualification.in / requirements-qualification.lock
    harness-only dependencies; frozen product requirements.lock stays byte-exact

qualification/canonical.py
    strict duplicate-free JSON, canonical bytes, SHA-256, keyed HMAC helpers
qualification/contracts.py
    immutable candidate, policy, approval, plan, stage, result, and cap types
qualification/candidate.py
    exact manifest/archive verification and safe read-only extraction
qualification/candidate_seams.py
    frozen-source provider/worker mutation capability audit and refutation codes
qualification/fixtures.py
    deterministic XLSX/PDF corpus generation, validation, and runtime mapping
qualification/isolation.py
    sanitized child environment, sandbox backend, bounded IPC and log capture
qualification/candidate_child.py
    minimal extracted-candidate L3/L4 launcher; no product replacement
qualification/approval.py
    strict approval/predecessor parsing and static/read-only admission checks
qualification/evidence.py
    allowlisted evidence reports and schema/chain verification
qualification/adapters.py
    parent protocols, fail-on-call sentinels, and in-memory test doubles
qualification/ledger.py
    atomic run+namespace claim and per-call L3 reservations
qualification/recovery.py
    encrypted recovery capsule and effect-disabled recovery state machine
qualification/reconcile.py
    exact plan/provider/mailbox/Firestore/Sheet/worker comparison
qualification/l3_runner.py
    one-call plan plus current provider-receipt admission refutation
qualification/l4_runner.py
    future stage state machine plus current worker-effect admission refutation
qualification/cli.py
    verify, fixtures, run, and recover commands
qualification/sandbox_profiles/macos-deny-effects.sb
    deny-network/deny-child-process offline child profile

scripts/run_sitesift_qualification.sh
    pinned `uv` wrapper for `python -m qualification.cli`
scripts/run_test_level.py
    canonical L3/L4 dispatch after existing environment preflight
scripts/release/build-worker.sh
    qualification-marker refusal
scripts/deploy_process_user.sh
    qualification-marker refusal before any apply-mode preflight or gcloud call
Dockerfile
    final build-context refusal, including archives made by external packagers

tests/fixtures/sitesift_qualification/
    committed stage-01/03/10/22 workbooks, four synthetic PDFs, and manifest
tests/test_qualification_*.py
    focused zero-effect contract, failure-injection, isolation, L3, and L4 tests

docs/release-safety/sitesift-product-candidate-binding.json
    immutable product identities only
docs/release-safety/sitesift-qualification-policy.json
    fixture/evidence schemas, ladder, provider/runtime allowlists, hard ceilings
docs/release-safety/sitesift-qualification-evidence.schema.json
    closed privacy-safe report topology
docs/release-safety/scenario-registry.json
    canonical L3/L4 harness registration while live results remain not_run
```

## Parallel execution map

```text
Task 1 branch boundary
        |
Task 2 canonical contracts
        |
        +--> Task 3 candidate verifier ----+
        +--> Task 4 synthetic corpus ------+--> Task 5 child isolation
        +--> Task 6 approval/evidence -----+
                                              |
                         Task 7 ledger/recovery
                                  |
                    +-------------+-------------+
                    |                           |
               Task 8 L3                   Task 9 L4
                    |                           |
                    +-------------+-------------+
                                  |
                      Task 10 canonical CLI
                                  |
                    Task 11 zero-effect release proof
```

Tasks 3, 4, and 6 may be implemented by separate subagents after Task 2 is
reviewed. Tasks 8 and 9 may then proceed in parallel after Task 7 is reviewed.
Task 10 integrates only reviewed outputs. Task 11 is deliberately serial.

These eleven engineering tasks are **not eleven user turns**. Target three
building turns:

1. core boundary/contracts/artifacts/corpus/admission/ledger (Tasks 1–7, with
   Tasks 3/4/6 parallelized);
2. candidate refutation lanes and canonical integration (Tasks 8–10, with L3
   and L4 work parallelized); and
3. full verification, independent review, pushes, Brain finding, and next
   planning handoff (Task 11).

If the first two waves integrate cleanly, they may collapse into one long
building turn. Milestones are announced when each wave's objective evidence is
complete.

### Task 1: Make the qualification branch structurally non-deployable

**Files:**

- Create: `.sitesift-qualification-only`
- Modify: `Dockerfile`
- Modify: `scripts/deploy_process_user.sh`
- Modify: `scripts/release/build-worker.sh`
- Modify: `tests/test_process_user_production_deploy_contract.py`
- Modify: `tests/test_ws_b_dockerignore_contract.py`
- Modify: `tests/test_source_rollback_release_contract.py`
- Modify: `tests/test_graph_send_inventory.py`
- In a separate `email-admin-ui` support worktree:
  - Modify: `scripts/release/build-release.mjs`
  - Modify: `scripts/release/release-contract.test.mjs`

- [ ] **Step 1: Write the failing release-refusal test**

Add this method to `WorkerSourceRollbackContractTests`:

```python
def test_build_refuses_qualification_only_source(self):
    _write(
        self.repo / ".sitesift-qualification-only",
        "SiteSift qualification harness; never package or deploy.\n",
    )
    self._git("add", ".sitesift-qualification-only")
    self._git("commit", "-qm", "mark qualification-only source")

    result = self._build()

    self.assertEqual(result.returncode, 69, result.stderr)
    self.assertIn("qualification-only", result.stderr)
    self.assertFalse(self.output.exists())
```

- [ ] **Step 2: Run the focused test and prove the guard is missing**

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -m unittest \
  tests.test_source_rollback_release_contract.WorkerSourceRollbackContractTests.test_build_refuses_qualification_only_source \
  -v
```

Expected: `FAIL` because the current builder proceeds past the marker instead of
returning `69`.

- [ ] **Step 3: Add the marker and fail before artifact-directory creation**

Create `.sitesift-qualification-only` with exactly:

```text
SiteSift qualification harness; never package or deploy.
```

In `scripts/release/build-worker.sh`, immediately after resolving `repo` and
before the dirty-tree or output-directory checks, add:

```bash
qualification_marker="$repo/.sitesift-qualification-only"
if [[ -e "$qualification_marker" ]]; then
  printf 'Refusing worker artifact build: qualification-only source cannot be packaged or deployed.\n' >&2
  exit 69
fi
```

This ordering matters: a qualification branch must refuse even when clean, and
must not create the requested output directory.

- [ ] **Step 4: Guard both direct deployment and externally archived Docker builds**

First add the deploy and Docker marker-refusal assertions described below, run
`tests.test_process_user_production_deploy_contract` and
`tests.test_ws_b_dockerignore_contract`, and require the new cases to fail while
all existing cases remain green. Only then edit the script and Dockerfile.

Add this block to `scripts/deploy_process_user.sh` after argument parsing and
before `process_user_gcloud_preflight local`:

```bash
qualification_marker="$REPO_ROOT/.sitesift-qualification-only"
if [[ "$mode" == "apply" && -e "$qualification_marker" ]]; then
  printf 'Refusing process-user deployment: qualification-only source cannot be deployed.\n' >&2
  exit 69
fi
```

The dry-run remains usable because it makes zero provider calls. Add a
`DeployScriptContractTests` case that invokes `--apply` from this marked
checkout and proves exit `69`, an empty fake-gcloud log, and an empty fake-git
log. Update the normal apply-path fixture to execute copied scripts from a
temporary, marker-free repository so the existing production-deploy contract
continues to be tested rather than being hidden by this branch's marker.

Add this immediately after the `FROM` line in `Dockerfile`:

```dockerfile
RUN test ! -e /app/.sitesift-qualification-only
```

If the current Dockerfile copies the source after the `FROM` line, place the
check immediately after the first whole-context `COPY` instead. Add a
`tests/test_ws_b_dockerignore_contract.py` test proving the marker is not
ignored and the Dockerfile refuses when it is present. This final build-context
guard covers release archives created by out-of-repository packagers: the
archive may be preserved as evidence, but it cannot become a runnable worker
image.

- [ ] **Step 5: Guard the supported combined-release packager**

Create a separate support worktree from the untouched frontend candidate:

```bash
git -C /Users/baylorharrison/Documents/GitHub.nosync/email-admin-ui \
  worktree add \
  /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/worktrees/email-admin-ui/t6-qualification-packaging-guard \
  -b codex/sitesift-t6-qualification-packaging-guard-20260728 \
  b4636e8276db18cb633d8c9e27b5e05fa9dc21a9
```

In `scripts/release/release-contract.test.mjs`, add a worker repository marker,
run `build-release.mjs`, and prove it exits nonzero before creating the output.
Run the focused suite under the pinned runtime and require the new assertion to
fail because the packager still accepts the marked worker:

```bash
mise exec node@20.20.2 -- \
  node --test scripts/release/release-contract.test.mjs
```

In `scripts/release/build-release.mjs`, after resolving `workerRepo` and before
`cleanCommit()` or any archive/output operation, reject
`join(workerRepo, ".sitesift-qualification-only")` when it exists.

Run and commit:

```bash
mise exec node@20.20.2 -- \
  node --test scripts/release/release-contract.test.mjs
git add scripts/release/build-release.mjs scripts/release/release-contract.test.mjs
git commit -m "test: refuse qualification-only worker packaging"
```

Record this support commit in the qualification policy and push its branch with
the backend qualification branch. The structural claim is explicitly limited
to the three supported entry points plus the marker-carrying Docker build
context; arbitrary custom packaging is outside the release system and is not
called supported.

- [ ] **Step 6: Keep qualification Python outside the production-source baseline only while the marker exists**

In `tests/test_graph_send_inventory.py`, add:

```python
QUALIFICATION_MARKER_PATH = REPO_ROOT / ".sitesift-qualification-only"


def _is_nondeployable_qualification_source(
    relative_path: Path,
    *,
    marker_path: Path = QUALIFICATION_MARKER_PATH,
) -> bool:
    return (
        bool(relative_path.parts)
        and relative_path.parts[0] == "qualification"
        and marker_path.is_file()
    )
```

Then add this guard inside `_repo_python_files()` before yielding:

```python
if _is_nondeployable_qualification_source(rel):
    continue
```

Add the exact contract test:

```python
def test_qualification_source_exclusion_requires_release_refusal_marker(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
        marker = Path(tmp_dir) / ".sitesift-qualification-only"
        source = Path("qualification") / "runner.py"
        self.assertFalse(
            _is_nondeployable_qualification_source(
                source,
                marker_path=marker,
            )
        )
        marker.write_text("qualification-only\n", encoding="utf-8")
        self.assertTrue(
            _is_nondeployable_qualification_source(
                source,
                marker_path=marker,
            )
        )
```

Also import `tempfile`. This is not a general baseline exemption: removing the
release-refusal marker makes every `qualification/*.py` file re-enter the
production AST baseline automatically.

- [ ] **Step 7: Run the boundary suites**

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -m unittest \
  tests.test_source_rollback_release_contract \
  tests.test_process_user_production_deploy_contract \
  tests.test_ws_b_dockerignore_contract \
  tests.test_graph_send_inventory \
  -v
```

Expected: exit `0`; all source-rollback and outbound-inventory tests pass.

- [ ] **Step 8: Commit the structural boundary**

```bash
git add \
  .sitesift-qualification-only \
  Dockerfile \
  scripts/deploy_process_user.sh \
  scripts/release/build-worker.sh \
  tests/test_process_user_production_deploy_contract.py \
  tests/test_ws_b_dockerignore_contract.py \
  tests/test_source_rollback_release_contract.py \
  tests/test_graph_send_inventory.py
git commit -m "test: isolate qualification harness from worker release"
```

### Task 2: Add strict canonical contracts and the frozen product binding

**Files:**

- Create: `qualification/__init__.py`
- Create: `qualification/canonical.py`
- Create: `qualification/contracts.py`
- Create: `docs/release-safety/sitesift-product-candidate-binding.json`
- Create: `tests/test_qualification_contracts.py`

- [ ] **Step 1: Write failing tests for duplicate-free JSON, HMACs, enums, and the exact candidate**

Create `tests/test_qualification_contracts.py` with tests containing these exact
assertions:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qualification.canonical import (
    CanonicalDataError,
    canonical_json_bytes,
    identity_hmac,
    load_strict_json,
    sha256_bytes,
)
from qualification.contracts import (
    CandidateBinding,
    L4Stage,
    QualificationLevel,
    RunStatus,
    load_candidate_binding,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BINDING_PATH = (
    REPO_ROOT
    / "docs"
    / "release-safety"
    / "sitesift-product-candidate-binding.json"
)


class QualificationContractTests(unittest.TestCase):
    def test_strict_json_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "duplicate.json"
            path.write_text('{"schemaVersion":1,"schemaVersion":2}\n')
            with self.assertRaisesRegex(CanonicalDataError, "duplicate key"):
                load_strict_json(path)

    def test_canonical_bytes_and_hmac_are_stable(self):
        value = {"z": [2, 1], "a": True}
        self.assertEqual(
            canonical_json_bytes(value),
            b'{"a":true,"z":[2,1]}',
        )
        self.assertEqual(
            sha256_bytes(canonical_json_bytes(value)),
            sha256_bytes(canonical_json_bytes({"a": True, "z": [2, 1]})),
        )
        self.assertEqual(
            identity_hmac(b"k" * 32, "  Test-Owned-ID  "),
            identity_hmac(b"k" * 32, "  Test-Owned-ID  "),
        )
        self.assertNotEqual(
            identity_hmac(b"k" * 32, "Test-Owned-ID"),
            identity_hmac(b"k" * 32, "test-owned-id"),
        )

    def test_level_stage_and_status_vocabularies_are_closed(self):
        self.assertEqual([item.value for item in QualificationLevel], ["L3", "L4"])
        self.assertEqual([item.value for item in L4Stage], [1, 3, 10, 22])
        self.assertEqual(
            [item.value for item in RunStatus],
            ["PASS", "FAIL", "AMBIGUOUS", "UNAVAILABLE"],
        )

    def test_committed_candidate_binding_is_exact(self):
        binding = load_candidate_binding(BINDING_PATH)
        self.assertEqual(binding.frontend_commit, "b4636e8276db18cb633d8c9e27b5e05fa9dc21a9")
        self.assertEqual(binding.worker_commit, "f104b5f4cfc7574188e47efaadbf72df219e19a5")
        self.assertEqual(binding.combined_manifest_sha256, "fb1b23c27525aa405e16f35ed71599c7887023e90d41677049b7c3097214cbaa")
        self.assertEqual(binding.worker_manifest_sha256, "b6ebb974ed4c0ddaa618f7f9d165c207cc569b862d9d27e8724f64e0871b4abc")
        self.assertEqual(binding.worker_archive_sha256, "909c4cdd6d9bfc3c1e276a313dc1b52781e6708913547f27e19fd63b1a9b5138")
        self.assertEqual(binding.worker_archive_size, 23941120)
        self.assertEqual(binding.source_tree_algorithm, "tree-v1-regular-files")
        self.assertEqual(binding.source_tree_sha256, "8859b9b27ea7861220b1e48c0c46eb342aa6756ff044945cd7485dc34ade8d5c")
        self.assertEqual(binding.regular_file_count, 514)
```

Add structural negative tests which pass a temporary mapping with one unknown
key, a short digest, `clean: false`, a malformed commit, a nonpositive archive
size/file count, or an unsupported tree algorithm to
`CandidateBinding.from_mapping()` and require `CanonicalDataError`. A different
well-formed commit is structurally valid; Task 3 must instead reject it with
`CandidateMismatch` when comparing supplied artifacts to the committed binding.

- [ ] **Step 2: Run the contract test and prove the package is absent**

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -m unittest tests.test_qualification_contracts -v
```

Expected: import failure for `qualification`.

- [ ] **Step 3: Implement the canonical primitives**

Create `qualification/canonical.py` with this complete public surface:

```python
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Iterable


class CanonicalDataError(ValueError):
    pass


def _no_duplicate_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalDataError(f"duplicate key: {key}")
        result[key] = value
    return result


def load_strict_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CanonicalDataError(f"non-finite JSON value: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanonicalDataError(f"invalid JSON: {path.name}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise CanonicalDataError("value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity_hmac(key: bytes, exact_value: str) -> str:
    if len(key) < 32:
        raise CanonicalDataError("identity HMAC key must be at least 32 bytes")
    if not isinstance(exact_value, str) or not exact_value:
        raise CanonicalDataError("identity value must be a non-empty exact string")
    return hmac.new(key, exact_value.encode("utf-8"), hashlib.sha256).hexdigest()
```

- [ ] **Step 4: Implement closed enums and strict candidate binding**

Create `qualification/contracts.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Mapping

from .canonical import CanonicalDataError, load_strict_json


class QualificationLevel(str, Enum):
    L3 = "L3"
    L4 = "L4"


class L4Stage(IntEnum):
    ONE = 1
    THREE = 3
    TEN = 10
    TWENTY_TWO = 22


class RunStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise CanonicalDataError(
            f"{label} keys differ: missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CanonicalDataError(f"{label} must be SHA-256")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CanonicalDataError(f"{label} must be SHA-256") from exc
    return value


def _commit_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40:
        raise CanonicalDataError(f"{label} must be a full commit")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CanonicalDataError(f"{label} must be a full commit") from exc
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CanonicalDataError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class CandidateBinding:
    frontend_commit: str
    worker_commit: str
    combined_manifest_sha256: str
    worker_manifest_sha256: str
    worker_archive_sha256: str
    worker_archive_size: int
    source_tree_algorithm: str
    source_tree_sha256: str
    regular_file_count: int
    dependency_lock_sha256: str
    dockerfile_sha256: str
    base_image: str
    worker_python: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CandidateBinding":
        expected = frozenset(
            {
                "schemaVersion",
                "frontendCommit",
                "workerCommit",
                "combinedManifestSha256",
                "workerManifestSha256",
                "workerArchiveSha256",
                "workerArchiveSize",
                "sourceTreeAlgorithm",
                "sourceTreeSha256",
                "regularFileCount",
                "dependencyLockSha256",
                "dockerfileSha256",
                "baseImage",
                "workerPython",
            }
        )
        _exact_keys(value, expected, "candidate binding")
        if value["schemaVersion"] != 1:
            raise CanonicalDataError("candidate binding schemaVersion must be 1")
        if value["sourceTreeAlgorithm"] != "tree-v1-regular-files":
            raise CanonicalDataError("candidate sourceTreeAlgorithm is unsupported")
        return cls(
            frontend_commit=_commit_sha(value["frontendCommit"], "frontendCommit"),
            worker_commit=_commit_sha(value["workerCommit"], "workerCommit"),
            combined_manifest_sha256=_sha256(value["combinedManifestSha256"], "combinedManifestSha256"),
            worker_manifest_sha256=_sha256(value["workerManifestSha256"], "workerManifestSha256"),
            worker_archive_sha256=_sha256(value["workerArchiveSha256"], "workerArchiveSha256"),
            worker_archive_size=_positive_int(
                value["workerArchiveSize"], "workerArchiveSize"
            ),
            source_tree_algorithm=value["sourceTreeAlgorithm"],
            source_tree_sha256=_sha256(value["sourceTreeSha256"], "sourceTreeSha256"),
            regular_file_count=_positive_int(
                value["regularFileCount"], "regularFileCount"
            ),
            dependency_lock_sha256=_sha256(value["dependencyLockSha256"], "dependencyLockSha256"),
            dockerfile_sha256=_sha256(value["dockerfileSha256"], "dockerfileSha256"),
            base_image=str(value["baseImage"]),
            worker_python=str(value["workerPython"]),
        )


def load_candidate_binding(path: Path) -> CandidateBinding:
    value = load_strict_json(path)
    if not isinstance(value, Mapping):
        raise CanonicalDataError("candidate binding must be an object")
    return CandidateBinding.from_mapping(value)
```

During implementation, split long constructor lines to the repository's normal
line length without changing this contract.

- [ ] **Step 5: Commit the exact immutable binding**

Create `docs/release-safety/sitesift-product-candidate-binding.json`:

```json
{
  "schemaVersion": 1,
  "frontendCommit": "b4636e8276db18cb633d8c9e27b5e05fa9dc21a9",
  "workerCommit": "f104b5f4cfc7574188e47efaadbf72df219e19a5",
  "combinedManifestSha256": "fb1b23c27525aa405e16f35ed71599c7887023e90d41677049b7c3097214cbaa",
  "workerManifestSha256": "b6ebb974ed4c0ddaa618f7f9d165c207cc569b862d9d27e8724f64e0871b4abc",
  "workerArchiveSha256": "909c4cdd6d9bfc3c1e276a313dc1b52781e6708913547f27e19fd63b1a9b5138",
  "workerArchiveSize": 23941120,
  "sourceTreeAlgorithm": "tree-v1-regular-files",
  "sourceTreeSha256": "8859b9b27ea7861220b1e48c0c46eb342aa6756ff044945cd7485dc34ade8d5c",
  "regularFileCount": 514,
  "dependencyLockSha256": "55eb86c3569e41a4fb92a5090c63887def006aebec8ab3926cdd794c052864c6",
  "dockerfileSha256": "dbe2ad9309a3dc2b53c20e13592489f6f5234b4961a3c650192dafdf2ec72160",
  "baseImage": "python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf",
  "workerPython": "3.12.13"
}
```

Keep `qualification/__init__.py` side-effect free:

```python
"""Non-deployable SiteSift qualification harness."""
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -m unittest tests.test_qualification_contracts -v
```

Expected: exit `0`; all strict-contract tests pass.

Commit:

```bash
git add \
  qualification/__init__.py \
  qualification/canonical.py \
  qualification/contracts.py \
  docs/release-safety/sitesift-product-candidate-binding.json \
  tests/test_qualification_contracts.py
git commit -m "test: bind qualification harness to frozen candidate"
```

### Task 3: Verify and safely extract the exact candidate

**Files:**

- Create: `qualification/candidate.py`
- Create: `tests/test_qualification_candidate.py`

- [ ] **Step 1: Write failing candidate-verifier tests**

Cover these cases with temporary manifests and tar archives:

1. the three canonical artifact paths above verify successfully;
2. a missing artifact raises `CandidateUnavailable` without attempting any
   credential or network discovery;
3. a manifest, archive, dependency-lock, Dockerfile, commit, base-image, Python,
   source-build-identity, file-count, or tree-digest mismatch raises
   `CandidateMismatch`;
4. absolute paths, `..`, duplicate members, links, devices, FIFOs, sockets,
   non-regular members other than directories, and members outside the declared
   source tree are rejected before extraction;
5. the destination must not exist, extraction creates no file through a
   symlink, and every extracted source file becomes non-writable; and
6. a post-extraction mutation is detected by a second tree-digest check.

The positive test must assert the exact frozen values from the immutable-input
table, including regular-file count `514`. Do not reconstruct a tarball for the
positive case; consume the real artifact.

- [ ] **Step 2: Prove the verifier is absent**

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -B -m unittest tests.test_qualification_candidate -v
```

Expected: import failure for `qualification.candidate`.

- [ ] **Step 3: Implement typed verification and safe extraction**

Implement:

```python
class CandidateUnavailable(RuntimeError):
    """A required local artifact is absent or unreadable."""


class CandidateMismatch(RuntimeError):
    """A supplied artifact does not equal the frozen candidate."""


@dataclass(frozen=True)
class CandidateArtifactPaths:
    combined_manifest: Path
    worker_manifest: Path
    worker_archive: Path


@dataclass(frozen=True)
class VerifiedCandidate:
    import_root: Path
    binding: CandidateBinding
    combined_manifest_digest: str
    worker_manifest_digest: str
    worker_archive_digest: str
    source_tree_digest: str
    regular_file_count: int


def verify_and_extract_candidate(
    *,
    binding_path: Path,
    artifacts: CandidateArtifactPaths,
    destination: Path,
) -> VerifiedCandidate:
    ...
```

Use strict JSON from `qualification.canonical`. Hash raw files before parsing.
Cross-check both manifest schemas and their internal identities, including the
combined manifest's worker source-build identity and the worker manifest's own
source-build identity; these are distinct expected values, not interchangeable
digests. Validate the complete member list before creating `destination`, use
`os.open`/`dir_fd` or an equivalently race-safe extraction loop rather than
`TarFile.extractall`, and set directories `0555` and regular files `0444`.

Recompute the source tree with the frozen algorithm:

```text
for each sorted regular relative path:
    sha256.update(len(utf8(path)).to_bytes(8, "big"))
    sha256.update(utf8(path))
    sha256.update(len(content).to_bytes(8, "big"))
    sha256.update(content)
```

This is versioned as `tree-v1-regular-files`, the exact framing used to derive
`8859b9b27ea7861220b1e48c0c46eb342aa6756ff044945cd7485dc34ade8d5c`;
the positive real-artifact test is the compatibility oracle. The raw archive
digest independently binds directory entries, modes, and the complete tar
framing. Archive validation rejects any directory not implied by a regular
member, so the file-only extracted-tree digest cannot hide an extra empty
directory.

- [ ] **Step 4: Run focused tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements.lock \
  python -B -m unittest tests.test_qualification_candidate -v
git add qualification/candidate.py tests/test_qualification_candidate.py
git commit -m "test: verify frozen qualification candidate"
```

Expected: exit `0`; all positive and tamper cases pass.

### Task 4: Build the deterministic synthetic qualification corpus

**Files:**

- Modify: `.gitignore`
- Modify: `CLAUDE.md`
- Create: `requirements-qualification.in`
- Create: `requirements-qualification.lock`
- Create: `qualification/fixtures.py`
- Create: `docs/release-safety/sitesift-qualification-policy.json`
- Create: `tests/test_qualification_fixtures.py`
- Create: `tests/fixtures/sitesift_qualification/stage-01.xlsx`
- Create: `tests/fixtures/sitesift_qualification/stage-03.xlsx`
- Create: `tests/fixtures/sitesift_qualification/stage-10.xlsx`
- Create: `tests/fixtures/sitesift_qualification/stage-22.xlsx`
- Create: `tests/fixtures/sitesift_qualification/attachments/synthetic-flyer-alpha.pdf`
- Create: `tests/fixtures/sitesift_qualification/attachments/synthetic-floorplan-beta.pdf`
- Create: `tests/fixtures/sitesift_qualification/attachments/synthetic-flyer-gamma.pdf`
- Create: `tests/fixtures/sitesift_qualification/attachments/synthetic-floorplan-delta.pdf`
- Create: `tests/fixtures/sitesift_qualification/fixture-manifest.json`

- [ ] **Step 1: Record the narrow synthetic-XLSX exception and harness lock**

The repository instruction that normally forbids generating XLSX files is aimed
at customer-data fixtures. Add a narrow `CLAUDE.md` exception that permits only
the four wholly synthetic T6 files under
`tests/fixtures/sitesift_qualification/`, while continuing to forbid generated
customer workbooks. The user's approved T6 design is the authority for this
exception.

Add to `.gitignore`:

```gitignore
!tests/fixtures/sitesift_qualification/*.xlsx
```

Create `requirements-qualification.in`:

```text
-r requirements.lock
jsonschema==4.25.1
```

Compile a separate lock so `requirements.lock` remains byte-identical to the
frozen product candidate:

```bash
UV_OFFLINE=1 uv pip compile --offline requirements-qualification.in \
  --python-version 3.12 \
  --generate-hashes \
  --output-file requirements-qualification.lock
```

No online fallback is allowed in T6a. If the pinned package or transitive wheel
metadata is absent from the local cache, record `UNAVAILABLE/dependency_cache`
and stop rather than using the network during build/verify.

- [ ] **Step 2: Write failing byte-reproducibility and rejection tests**

Tests must prove:

- four independently loadable workbooks contain exactly `1`, `3`, `10`, and
  `22` data rows;
- every workbook has title row 1, the exact production header row 2, and data
  beginning at row 3;
- `sendMode` is exactly `separate`, so expected irreversible mail effects are
  exactly the row count even when every logical recipient is runtime-mapped to
  one approved test mailbox;
- regenerating into two separate temporary directories yields identical bytes
  for every XLSX, PDF, policy, and manifest;
- `check_corpus(committed_root)` compares against regenerated temporary bytes
  and never writes to the committed tree;
- both pinned PDF readers extract more than 100 characters from every PDF, so
  the production upload path does not take its extraction-fallback branch; and
- formulas, macros, external links, hidden sheets/rows/columns, comments,
  drawings/objects, unexpected OOXML parts, unknown columns, invalid types,
  duplicate case IDs, non-`example.com` fixture addresses, secret-like strings,
  and values outside the closed synthetic vocabulary are rejected.

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest tests.test_qualification_fixtures -v
```

Expected: import failure because `qualification.fixtures` does not exist.

- [ ] **Step 3: Implement deterministic builders and the scanner**

Expose:

```python
def generate_corpus(output_dir: Path) -> Mapping[str, str]:
    """Create the closed synthetic corpus and return path -> SHA-256."""


def load_fixture_manifest(path: Path) -> FixtureManifest:
    """Strictly parse and validate the corpus manifest."""


def scan_fixture_tree(root: Path) -> None:
    """Fail closed on non-synthetic or structurally unsafe fixture content."""


def check_corpus(committed_root: Path) -> None:
    """Regenerate elsewhere and compare every declared byte and digest."""
```

Use the exact production columns:

```text
Property Address
City
Leasing Contact
Email
Total SF
Rent/SF /Yr
Ops Ex /SF
Drive Ins
Docks
Ceiling Ht
Power
Flyer / Link
Gross Rent
```

Use only fictitious `Synthetic ...` names/addresses and reserved `example.com`
addresses. Give every row a stable `synthetic-property-NNN` logical case ID in
the manifest. Workbook recipient cells remain synthetic `example.com` values;
the later live materializer maps them to the single approved mailbox in a
test-owned copy without changing the committed fixtures.

`openpyxl.Workbook.save()` is not a byte-stability boundary. Set fixed workbook
creation/modification metadata, save to memory, then rewrite sorted OOXML
members with fixed ZIP timestamps, permissions, and `ZIP_STORED`. Generate PDFs
with a small deterministic standard-library writer and fixed metadata. The
manifest hashes every committed file and binds:

- schema/seed version;
- exact row count and `sendMode: "separate"` for each stage;
- expected effect count equal to stage size;
- the four attachment digests;
- scenario tags, all of which must exist in the scenario registry;
- evidence schema digest; and
- the only ladder `[1, 3, 10, 22]`.

The policy also binds the L3 smoke case, all eight final case IDs, `25` maximum
calls, zero retries, output-token ceiling, total token ceiling, micro-USD
ceiling, per-stage worker/email caps, and allowed provider/runtime labels.

- [ ] **Step 4: Generate, check, and commit the corpus**

```bash
UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m qualification.fixtures build
UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m qualification.fixtures check
UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest tests.test_qualification_fixtures -v
git add \
  .gitignore CLAUDE.md \
  requirements-qualification.in requirements-qualification.lock \
  qualification/fixtures.py \
  docs/release-safety/sitesift-qualification-policy.json \
  tests/test_qualification_fixtures.py \
  tests/fixtures/sitesift_qualification
git commit -m "test: add deterministic SiteSift qualification corpus"
```

Expected: both generation checks and all fixture tests exit `0`.

### Task 5: Isolate candidate execution from the harness and host

**Files:**

- Create: `qualification/isolation.py`
- Create: `qualification/candidate_child.py`
- Create: `qualification/sandbox_profiles/macos-deny-effects.sb`
- Create: `tests/test_qualification_isolation.py`

- [ ] **Step 1: Write hostile child-boundary tests**

Drive a real subprocess through the public launcher and prove:

- the candidate import root is the verified read-only extraction;
- the process working directory is a separate writable, mode-`0700` run
  directory, because frozen `main.py` writes `msal_token_cache.bin` relative to
  its working directory;
- neither the qualification worktree nor user-site packages appear on
  `sys.path`;
- `HOME`, cloud SDK homes, caches, and config roots are private temporary
  directories;
- inherited variables containing `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`,
  `API_KEY`, `BEARER`, Firebase, Google, Graph, Azure, or provider identity are
  absent unless explicitly allowlisted for an admitted live run;
- offline socket, DNS, subprocess, fork/exec, out-of-run-directory write,
  symlink escape, and ambient-config reads fail under the actual OS sandbox;
- stdout/stderr cannot spoof the result envelope, and each captured stream is
  mode `0600` and byte-capped; and
- missing or unproved isolation support returns `UNAVAILABLE`, never a degraded
  unsandboxed pass.

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest tests.test_qualification_isolation -v
```

Expected: import failure because the isolation and child modules do not exist.

- [ ] **Step 2: Implement the parent/child protocol**

Implement:

```python
@dataclass(frozen=True)
class ChildRequest:
    level: QualificationLevel
    mode: str
    run_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class ChildResult:
    status: RunStatus
    code: str
    counts: Mapping[str, int]
    digests: Mapping[str, str]


def run_candidate_child(
    *,
    candidate: VerifiedCandidate,
    request: ChildRequest,
    run_dir: Path,
    explicit_environment: Mapping[str, str],
) -> ChildResult:
    ...
```

The parent supplies canonical request bytes on stdin and reads only the
dedicated result FD. For L3 live mode it also owns a dedicated reservation
request/ack pipe described in Task 8. Candidate stdout/stderr go to private
bounded files and never to evidence or console.

Launch Python with `-I -S` only if the pinned third-party import path is then
added explicitly; otherwise use `-I`, `PYTHONNOUSERSITE=1`, no `PYTHONPATH`, and
an explicit child bootstrap path. The bootstrap adds only
`candidate.import_root` to the product import path. Its CWD is `run_dir`, not
the extraction. It loads:

- L3: frozen claim/provider modules plus the frozen script transport; or
- L4: frozen `main.py`, followed by exactly one
  `refresh_and_process_user(exact_test_uid)` call.

Do not call `run_all_users`, monkey-patch product functions, replace product
clients, or import the branch's product code.

- [ ] **Step 3: Implement and test the macOS sandbox**

The checked-in profile must deny network and process creation, deny reads of
home/cloud credential locations, allow reads of the system Python/runtime and
verified candidate root, and allow writes only in the private run root. Bind
absolute paths through `sandbox-exec -D` parameters; never interpolate
untrusted strings into the profile.

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest tests.test_qualification_isolation -v
git add \
  qualification/isolation.py \
  qualification/candidate_child.py \
  qualification/sandbox_profiles/macos-deny-effects.sb \
  tests/test_qualification_isolation.py
git commit -m "test: isolate frozen candidate execution"
```

Expected: all hostile attempts fail for the intended reason and the legitimate
offline request succeeds.

### Task 6: Seal approval, predecessor, and privacy-safe evidence contracts

**Files:**

- Create: `qualification/approval.py`
- Create: `qualification/evidence.py`
- Create: `docs/release-safety/sitesift-qualification-evidence.schema.json`
- Create: `tests/test_qualification_approval.py`
- Create: `tests/test_qualification_evidence.py`

- [ ] **Step 1: Write failing closed-schema tests**

Test duplicate keys, non-finite numbers, invalid UTF-8, noncanonical bytes,
unknown/missing fields, wrong types, expired or wrong-stage approvals, candidate
or harness mismatch, policy/corpus mismatch, replayed run ID, incorrect
predecessor stage, non-`PASS` predecessor, incomplete reconciliation or
restoration, broken evidence digest/chain, and identity mismatch.

Privacy tests recursively reject raw email addresses, UIDs, client IDs, thread,
message, sheet, Drive, property, secret, body, prompt, response, exception, and
stack values anywhere in evidence. Only keyed HMAC identities and closed status,
count, duration, digest, provider/runtime-label, and reason-code fields are
allowed.

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_approval \
  tests.test_qualification_evidence -v
```

Expected: import failure because approval/evidence modules do not exist.

- [ ] **Step 2: Implement static and read-only admission**

Expose:

```python
def load_approval(path: Path) -> QualificationApproval:
    """Parse canonical bytes with an exact closed schema."""


def admit_static(
    *,
    approval: QualificationApproval,
    binding: CandidateBinding,
    harness_commit: str,
    policy: QualificationPolicy,
    fixture_manifest: FixtureManifest,
    predecessor_path: Path | None,
) -> StaticAdmission:
    """Perform only local/read-only checks; construct no external client."""


def admit_environment(
    *,
    static: StaticAdmission,
    observed: ReadOnlyEnvironmentObservation,
) -> AdmittedPlan:
    """Bind HMAC identities and exact test-owned capabilities."""
```

Approval bytes name the exact clean harness commit, candidate hashes, level,
mode, stage/case plan, run ID, namespace, test project/mailbox/Drive/Sheet HMACs,
caps, expiry, and predecessor evidence digest where applicable. Use separate
keys for evidence identity HMACs and recovery encryption.

Stage `1` requires no predecessor. Stages `3`, `10`, and `22` require the exact
previous stage evidence, with `PASS`, complete reconciliation, complete
reversible restoration, the same immutable candidate/harness/policy/corpus and
test-owned identities, and an unbroken chain digest.

- [ ] **Step 3: Build and validate closed evidence**

Use Draft 2020-12 with `additionalProperties: false` at every object. Runtime
validation uses the separately pinned `jsonschema`. Evidence contains no raw
provider request/response or customer/test identity; it records only:

- exact candidate/harness/policy/corpus digests;
- stage, run status, closed reason codes, timestamps, and durations;
- HMAC identities;
- admitted caps and actual counts;
- child exit/output digests;
- per-call reservation states;
- exact reconciliation/restoration summaries;
- irreversible sent-mail count;
- predecessor and current chain digests; and
- whether the recovery capsule was destroyed or retained.

The evidence writer must validate first, serialize canonical bytes, open the
destination exclusively, use mode `0600`, `fsync` the file and parent
directory, and refuse overwrite.

- [ ] **Step 4: Run tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_approval \
  tests.test_qualification_evidence -v
git add \
  qualification/approval.py qualification/evidence.py \
  docs/release-safety/sitesift-qualification-evidence.schema.json \
  tests/test_qualification_approval.py tests/test_qualification_evidence.py
git commit -m "test: seal qualification approval and evidence"
```

### Task 7: Make claims, reservations, and recovery crash-safe

**Files:**

- Create: `qualification/adapters.py`
- Create: `qualification/ledger.py`
- Create: `qualification/recovery.py`
- Create: `tests/test_qualification_ledger.py`
- Create: `tests/test_qualification_recovery.py`

- [ ] **Step 1: Write failing protocol, atomicity, and interruption tests**

Prove:

- two processes racing for one namespace yield exactly one successful atomic
  run+namespace claim;
- run and namespace claim are created in one transaction or neither exists;
- the transaction is the first external write after local/read-only admission
  and after the recovery capsule has been durably written;
- every L3 call has a unique reservation, and only the winner may receive a
  start acknowledgement;
- a started reservation is never retried after timeout, crash, malformed
  result, provider error, or missing response;
- stage replay and cross-run namespace reuse fail;
- recovery never invokes provider, Graph send, worker, scheduler, or queue
  methods;
- expiration does not prevent exact recovery of an already claimed run;
- wrong key, tampered capsule, symlink destination, partial write, and
  restoration ambiguity fail closed while preserving evidence; and
- successful exact restoration deletes sensitive capsule material and leaves a
  privacy-safe tombstone outside the product `users` tree.

- [ ] **Step 2: Run the tests red**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_ledger \
  tests.test_qualification_recovery -v
```

Expected: import failure because the adapter, ledger, and recovery modules do
not yet exist.

- [ ] **Step 3: Implement protocols, the atomic ledger, and encrypted recovery**

Define narrow protocols for read-only identity discovery, atomic ledger
transactions, test-state setup/snapshot/restore, mailbox reads, and explicit
provider/mail effects. Provide in-memory implementations whose unexpected
methods fail. No Google, Firebase, Microsoft, or provider SDK may import when
these modules are imported.

Implement:

```python
def claim_run_and_namespace(
    store: QualificationLedgerStore,
    admitted: AdmittedPlan,
) -> ClaimedRun:
    """Atomic create-if-absent; first external write."""


def reserve_provider_call(
    store: QualificationLedgerStore,
    *,
    run: ClaimedRun,
    case_id_hmac: str,
    repeat_index: int,
) -> ProviderReservation:
    """Create a unique STARTED reservation before call acknowledgement."""


def write_recovery_capsule(
    path: Path,
    *,
    key: bytes,
    snapshot: ReversibleSnapshot,
) -> RecoveryCapsuleDigest:
    """Encrypt, exclusive-create, chmod 0600, fsync file and directory."""


def recover_only(
    *,
    capsule_path: Path,
    key: bytes,
    adapters: RecoveryAdapters,
) -> RecoveryResult:
    """Reconcile/restore exact owned scope; never execute product effects."""
```

Use `cryptography.fernet.Fernet` with a separately supplied key. The capsule
contains the raw test-owned identifiers needed for exact restoration; evidence
does not. Firestore's implementation uses a transaction and create-if-absent
preconditions for two independently unique immutable documents: the run claim
and namespace claim. A single composite document cannot reject duplicate run
and duplicate namespace independently. If transaction commit acknowledgement
is ambiguous, read those exact two claim documents and reconcile their sealed
digests; never blindly retry the claim.

- [ ] **Step 4: Run tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_ledger \
  tests.test_qualification_recovery -v
git add \
  qualification/adapters.py qualification/ledger.py qualification/recovery.py \
  tests/test_qualification_ledger.py tests/test_qualification_recovery.py
git commit -m "test: make qualification recovery crash safe"
```

### Task 8: Build the one-call L3 lane and encode the provider-receipt refutation

**Files:**

- Create: `qualification/candidate_seams.py`
- Modify: `qualification/candidate_child.py`
- Create: `qualification/l3_runner.py`
- Create: `tests/test_qualification_candidate_seams.py`
- Create: `tests/test_qualification_l3_runner.py`

- [ ] **Step 1: Write failing capability-audit and one-call-lane tests**

The static-audit tests use the real verified extraction and prove the currently
expected facts:

- `OpenAIClaimReplayTransport` pins the provider/model/timeout, sets SDK
  `max_retries=0`, calls `responses.create` once, and uses `store=False`;
- it exposes neither a durable idempotency key nor response-retrieval method;
- it discards the provider response ID from its typed result; and
- `BudgetedProviderTransport` reservations are process-local only.

The audit returns closed code `provider_receipt_unqueryable`; any unrecognized
shape returns `provider_capability_unknown`. Both prevent live client
construction. Tests must use the real frozen archive and tampered miniature
sources so a future source change cannot accidentally inherit this expected
refutation.

Using a fake one-call child transport only in harness unit tests, also specify:

- smoke is exactly `unavailable-optout-suppression` once;
- a passing smoke is followed by exactly three repeats of these eight frozen
  case IDs and no others:
  `fresh-suite-closeout`, `split-suite-isolation`,
  `attachment-alternate-isolation`, `complete-facts-closeout`,
  `workflow-intents-visible`, `rent-correction-closeout`,
  `repeated-information-request`, and
  `unavailable-optout-suppression`;
- the total ceiling is `25`, with zero retries;
- failed/ambiguous smoke stops the final set;
- every call has one durable reservation before child launch; and
- all product persistence/mail/campaign sentinels remain untouched.

The real-provider test requires `FAIL/candidate_refuted` with
`provider_receipt_unqueryable` before reading `OPENAI_API_KEY`, importing the
SDK client, claiming a run, or making a network call.

- [ ] **Step 2: Run the tests red**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_candidate_seams \
  tests.test_qualification_l3_runner -v
```

Expected: import failure because the seam and L3 runner modules do not exist.

- [ ] **Step 3: Implement the audit and one admitted case/repeat per child**

The frozen multi-case `run_claim_replay()` loop cannot satisfy a parent-side
reservation immediately before every effect. Implement the reusable correct
shape:

1. parent validates the exact smoke/final order;
2. parent writes the capsule and claims run+namespace;
3. parent atomically reserves one `{caseIdHmac, repeatIndex}` key;
4. parent launches a fresh child naming only that case/repeat;
5. child selects a one-case frozen catalog, sets repeats to one, constructs the
   frozen pinned adapter/transport, and invokes the frozen replay/oracle once;
6. child exits and the parent finalizes or leaves the reservation ambiguous;
7. no started key is ever launched again.

This uses stdin plus the dedicated result FD and does not inject a transport,
monkey-patch the candidate, or let a child make multiple provider calls.

Implement the static audit first so the real-provider path fails before the
generic one-call lane can construct a live transport. The lane remains ready
for a future candidate that passes the capability audit.

- [ ] **Step 4: Run tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_candidate_seams \
  tests.test_qualification_l3_runner -v
git add \
  qualification/candidate_seams.py qualification/candidate_child.py \
  qualification/l3_runner.py tests/test_qualification_candidate_seams.py \
  tests/test_qualification_l3_runner.py
git commit -m "test: encode L3 candidate receipt gate"
```

### Task 9: Encode the full-worker refutation and future L4 state machine

**Files:**

- Modify: `qualification/candidate_seams.py`
- Create: `qualification/reconcile.py`
- Create: `qualification/l4_runner.py`
- Create: `tests/test_qualification_reconcile.py`
- Create: `tests/test_qualification_l4_runner.py`

- [ ] **Step 1: Write failing worker-seam, reconciliation, and admission tests**

Against the real verified extraction, require the static verifier to report
these known closed refutation codes:

- `provider_mutation_bypasses_gateway` for direct OpenAI inference/file calls;
- `graph_mutation_bypasses_gateway` for draft create/patch/delete and attachment
  mutations before final send;
- `drive_mutation_bypasses_gateway` for Drive file/permission creation;
- `storage_mutation_bypasses_gateway` for Firebase token-cache upload;
- `effect_plan_not_enforceable` because the gateway lacks stage/recipient/row
  membership enforcement and run-wide receipt enumeration;
- `send_receipt_unqueryable` because Graph ambiguity has no queryable provider
  idempotency receipt; and
- `worker_result_untyped` because `refresh_and_process_user()` returns no typed
  counters.

Also prove the usable seam: direct invocation can name one UID and avoids
`run_all_users`, Cloud Scheduler, and the outer service/queue wrapper. Preserve
this positive fact for the future candidate, but it cannot override any
refutation.

Without constructing live adapters, use in-memory observations to specify the
future state machine:

- only `1 -> 3 -> 10 -> 22` is legal;
- later stages require exact prior `PASS` evidence;
- only one stage can run per approval/process;
- planned `sendMode: separate` counts are exact;
- missing, extra, duplicate, delayed, cross-run, or ambiguous effects fail;
- Firestore, Sheet, Drive, Storage token-cache, Graph/mail, effect receipts, and
  typed worker outcomes all require exact plan membership;
- reversible restoration and irreversible sent-test-mail reporting are
  distinct; and
- recovery-only mode is structurally unable to run a worker or effect.

Require `FAIL/candidate_refuted` with the complete sorted reason-code set before:

- reading any credential environment variable;
- importing Firebase, Google, Microsoft, OpenAI, or candidate `main.py`;
- writing a capsule/claim;
- touching a test or production service; or
- starting the child.

Tests set hostile credential/client constructors and prove zero calls. A
miniature future-candidate fixture with every capability present may advance
only to `UNAVAILABLE/test_environment_not_supplied`; it must never execute live
in T6a.

- [ ] **Step 2: Run the tests red**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_reconcile \
  tests.test_qualification_l4_runner -v
```

Expected: import failure because reconciliation and L4 runner modules do not
exist and the L4 seam inventory is incomplete.

- [ ] **Step 3: Implement inventory, pure reconciliation, and early refusal**

Implement the static verifier over the extracted candidate. Implement
`reconcile_l4(...)` as a pure set/count comparison over typed HMAC/digest
observations. `qualification.l4_runner` performs only local candidate
verification and the seam audit for this binding.

Do not implement `qualification/live_adapters.py` for this candidate; doing so
after static refutation would create risk without producing admissible evidence.
The refusal must happen before credentials, SDK imports, capsule/claim writes,
external services, or child startup.

- [ ] **Step 4: Run tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_reconcile \
  tests.test_qualification_l4_runner -v
git add \
  qualification/candidate_seams.py qualification/reconcile.py \
  qualification/l4_runner.py tests/test_qualification_reconcile.py \
  tests/test_qualification_l4_runner.py
git commit -m "test: encode L4 candidate effect gate"
```

### Task 10: Integrate the explicit CLI, canonical levels, and registry

**Files:**

- Create: `qualification/cli.py`
- Create: `scripts/run_sitesift_qualification.sh`
- Modify: `scripts/run_test_level.py`
- Modify: `docs/release-safety/scenario-registry.json`
- Modify: `tests/test_test_level_runner.py`
- Modify: `tests/test_scenario_registry.py`
- Create: `tests/test_qualification_cli.py`

- [ ] **Step 1: Write failing CLI and dispatch tests**

Specify these commands:

```text
python -m qualification.cli fixtures build
python -m qualification.cli fixtures check
python -m qualification.cli verify --combined-manifest "$SITESIFT_COMBINED_MANIFEST" --worker-manifest "$SITESIFT_WORKER_MANIFEST" --worker-archive "$SITESIFT_WORKER_ARCHIVE"
python -m qualification.cli run --level L3 --mode verify --combined-manifest "$SITESIFT_COMBINED_MANIFEST" --worker-manifest "$SITESIFT_WORKER_MANIFEST" --worker-archive "$SITESIFT_WORKER_ARCHIVE"
python -m qualification.cli run --level L3 --mode live --approval "$SITESIFT_QUALIFICATION_APPROVAL" --combined-manifest "$SITESIFT_COMBINED_MANIFEST" --worker-manifest "$SITESIFT_WORKER_MANIFEST" --worker-archive "$SITESIFT_WORKER_ARCHIVE"
python -m qualification.cli run --level L4 --mode verify --combined-manifest "$SITESIFT_COMBINED_MANIFEST" --worker-manifest "$SITESIFT_WORKER_MANIFEST" --worker-archive "$SITESIFT_WORKER_ARCHIVE"
python -m qualification.cli run --level L4 --mode live --stage 1 --approval "$SITESIFT_QUALIFICATION_APPROVAL" --combined-manifest "$SITESIFT_COMBINED_MANIFEST" --worker-manifest "$SITESIFT_WORKER_MANIFEST" --worker-archive "$SITESIFT_WORKER_ARCHIVE"
python -m qualification.cli recover --capsule "$SITESIFT_RECOVERY_CAPSULE" --approval "$SITESIFT_QUALIFICATION_APPROVAL"
```

`PASS -> 0`, `FAIL -> 1`, `AMBIGUOUS -> 1`, and `UNAVAILABLE -> 3`.
Unexpected exceptions are closed `FAIL`, with no stack or secret printed.

Canonical level dispatch uses an `argparse.REMAINDER` after `--`:

```bash
./scripts/run_test_level.sh --level L3
./scripts/run_test_level.sh --level L4
./scripts/run_test_level.sh --level L3 -- verify
./scripts/run_test_level.sh --level L4 -- verify
```

Bare L3/L4 remain `UNAVAILABLE` exit `3` before importing qualification modules
or constructing adapters. Reject passthrough for L1/L2. L3/L4 verify use the
credential-free/offline isolation boundary; live mode does not pretend to be
credential-free.

Run:

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_cli \
  tests.test_test_level_runner \
  tests.test_scenario_registry -v
```

Expected: the new CLI/dispatch assertions fail before implementation; all
pre-existing runner/registry assertions remain green.

- [ ] **Step 2: Implement the wrapper and dispatch**

Create `scripts/run_sitesift_qualification.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"
export UV_OFFLINE=1
exec uv run --offline --isolated --no-project \
  --python 3.12.13 \
  --with-requirements requirements-qualification.lock \
  python -B -m qualification.cli "$@"
```

The CLI must complete strict local admission and candidate verification before
any live-module import. `verify` runs candidate extraction, fixture check,
closed-schema check, sandbox hostile smoke, static effect-path audit, and no
external call. For the frozen binding it must return exit `1` with
`candidate_refuted` and the closed L3/L4 seam codes; detecting the refutation is
the successful harness behavior, but it is not a product `PASS`.

- [ ] **Step 3: Update registry without claiming live evidence**

Preserve all `59` scenario IDs and family counts. Change L3/L4 profile
availability to explicit `mode_required` dispatch with runner/mode metadata.
Add T6 automated paths to `DEP-04`, fixture scenario tags, and validation that
every tag exists and supports its declared level.

Keep `DEP-04.latestResult.status` exactly `not_run`; its summary must state that
the harness is built, the frozen candidate is statically refuted, and no
controlled L4 stage is admissible or has run. Do not set Gate 2 ready or claim
an L3/L4 pass during T6a.

- [ ] **Step 4: Run integration tests and commit**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_cli \
  tests.test_test_level_runner \
  tests.test_scenario_registry -v
set +e
./scripts/run_test_level.sh --level L3
l3_status=$?
./scripts/run_test_level.sh --level L4
l4_status=$?
set -e
test "$l3_status" -eq 3
test "$l4_status" -eq 3
git add \
  qualification/cli.py scripts/run_sitesift_qualification.sh \
  scripts/run_test_level.py docs/release-safety/scenario-registry.json \
  tests/test_qualification_cli.py tests/test_test_level_runner.py \
  tests/test_scenario_registry.py
git commit -m "test: integrate SiteSift qualification levels"
```

Because `set -e` would stop on expected exit `3`, run the two bare-level checks
in a shell block that captures each status before asserting it.

### Task 11: Prove T6a is zero-effect, review it independently, and publish it

**Files:**

- Modify only if verification finds an issue: files introduced in Tasks 1–10
- Create in Brain after product verification:
  `projects/email-automation/findings/FDR-026-sitesift-t6a-harness-and-candidate-refutation.md`
- Create in Brain after product verification:
  `projects/email-automation/handoffs/2026-07-28-sitesift-candidate-effect-closure-planning.md`

- [ ] **Step 1: Require a clean committed qualification worktree**

```bash
git status --short
git log -1 --format='%H %s'
```

Expected: no status output and the current commit is the reviewed harness head.
If fixes are needed in later steps, commit them deliberately and restart this
task from Step 1.

- [ ] **Step 2: Run the complete focused qualification proof offline**

```bash
UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m unittest \
  tests.test_qualification_contracts \
  tests.test_qualification_candidate \
  tests.test_qualification_fixtures \
  tests.test_qualification_isolation \
  tests.test_qualification_approval \
  tests.test_qualification_evidence \
  tests.test_qualification_ledger \
  tests.test_qualification_recovery \
  tests.test_qualification_candidate_seams \
  tests.test_qualification_l3_runner \
  tests.test_qualification_reconcile \
  tests.test_qualification_l4_runner \
  tests.test_qualification_cli \
  tests.test_test_level_runner \
  tests.test_scenario_registry -v
```

Expected: exit `0`; no skip, credential request, or network access.

- [ ] **Step 3: Verify corpus, artifact binding, and hostile sandbox**

```bash
UV_OFFLINE=1 ./scripts/run_sitesift_qualification.sh fixtures check
set +e
UV_OFFLINE=1 ./scripts/run_sitesift_qualification.sh verify \
  --combined-manifest /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/sitesift-turn2-f104b5f/release-manifest.json \
  --worker-manifest /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-release-manifest.json \
  --worker-archive /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-source.tar
verify_status=$?
set -e
test "$verify_status" -eq 1
```

Expected: fixture check exits `0`; candidate verify exits `1` with
`candidate_refuted` and the complete expected closed reason-code set. Output
contains only closed status/reason codes and the exact
candidate/harness/policy/corpus digests—no path-derived identity, secret,
customer data, provider call, or product mutation.

- [ ] **Step 4: Run the canonical L1 and unavailable-level contracts**

```bash
UV_OFFLINE=1 ./scripts/run_test_level.sh --level L1
```

Expected: exit `0` with failures `0` and errors `0`.

Then capture and assert:

```bash
set +e
./scripts/run_test_level.sh --level L3
l3_status=$?
./scripts/run_test_level.sh --level L4
l4_status=$?
set -e
test "$l3_status" -eq 3
test "$l4_status" -eq 3
```

Expected: both bare live levels remain explicitly unavailable.

- [ ] **Step 5: Reproduce the pinned Node and complete support-branch gates**

Run from the `email-admin-ui` qualification-packaging support worktree:

```bash
mise exec node@20.20.2 -- node --version
mise exec node@20.20.2 -- npm --version
mise exec node@20.20.2 -- \
  node --test scripts/release/release-contract.test.mjs
mise exec node@20.20.2 -- \
  node --test functions/*.test.js functions/lib/*.test.js
mise exec node@20.20.2 -- \
  env CI=true npm test -- --runInBand
mise exec node@20.20.2 -- \
  env CI=true npm run build:base-v1
```

Expected:

- runtime is exactly Node `v20.20.2`, npm `10.8.2`;
- the release-contract suite passes with the new marker refusal;
- Functions remain **456/456**, with zero fail/cancel/skip/todo;
- frontend remains **94 suites / 676 tests**;
- production build and Base-v1 verifier pass; and
- the support worktree is clean after ignored build output is removed by its
  normal build tooling.

No `npx`, package install, lock mutation, or network fallback is allowed.

- [ ] **Step 6: Run static safety and immutability checks**

```bash
UV_OFFLINE=1 uv run --offline --python 3.12.13 --isolated --no-project \
  --with-requirements requirements-qualification.lock \
  python -B -m compileall -q qualification tests
rg -n -i \
  'jill|fiftyflowers|@gmail\.com|@fiftyflowers\.com|AIza|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|refresh[_ -]?token' \
  qualification tests/fixtures/sitesift_qualification \
  docs/release-safety/sitesift-qualification-policy.json \
  docs/release-safety/sitesift-qualification-evidence.schema.json
shasum -a 256 \
  /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/sitesift-turn2-f104b5f/release-manifest.json \
  /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-release-manifest.json \
  /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/release-artifacts/worker-f104b5f/worker-source.tar
git diff --check
git status --short
```

Expected: the privacy scan has no matches; artifact hashes equal the immutable
input table; diff check is clean; worktree is clean. Also verify the frozen
backend and frontend product worktrees still resolve to
`f104b5f4cfc7574188e47efaadbf72df219e19a5` and
`b4636e8276db18cb633d8c9e27b5e05fa9dc21a9` with no qualification edits.

- [ ] **Step 7: Request independent adversarial review**

Ask a fresh reviewer to inspect the complete branch diff against the approved
design, focusing on:

- any deploy/package escape;
- any branch-copy product import;
- any ambient credential or production fallback;
- provider-call reservation timing;
- replay/namespace races;
- crash/recovery resend risk;
- L4 direct worker/effect-gateway fidelity;
- fixture determinism and effect-count meaning;
- evidence privacy/schema closure; and
- any path that can claim `PASS` without exact reconciliation/restoration.

Resolve every blocker with a failing regression test, commit the fix, and rerun
Steps 1–6. A refuted candidate seam is a valid T6a finding; weakening the
contract is not.

- [ ] **Step 8: Push the qualification branch**

```bash
git push -u origin codex/sitesift-t6-qualification-harness-20260728
git status --short
git rev-parse HEAD
git rev-parse @{upstream}
git -C /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/worktrees/email-admin-ui/t6-qualification-packaging-guard \
  push -u origin codex/sitesift-t6-qualification-packaging-guard-20260728
git -C /Users/baylorharrison/Documents/Codex/2026-07-26/read-users-baylorharrison-documents-github-nosync-4/work/worktrees/email-admin-ui/t6-qualification-packaging-guard \
  status --short
```

Expected: both pushes succeed, both statuses are empty, and each local/upstream
full SHA matches.

- [ ] **Step 9: Record the T6a finding and create the product-closure handoff**

In Brain, record exact commands/results, candidate and harness commits, test
counts, artifact hashes, zero-effect proof, independent review disposition, the
remaining `not_run` L3/L4 status, and the complete candidate refutation. Update
the project, backlog, and digest without changing unrelated items.

Create a **planning-brainstorming** handoff for product candidate effect
closure. It must name:

- the exact clean harness and packaging-guard commits;
- every frozen-source bypass/refutation code and source seam;
- the required durable/queryable provider and Graph receipt contracts;
- exact run/stage/recipient/effect-plan enforcement and enumeration;
- typed worker outcome and complete Firestore/Sheet/Drive/Storage/Graph snapshot
  boundaries;
- the need to create and fully rerun L1/L2 on a new candidate;
- the rule that Jill/customer/production data remains outside qualification;
  and
- the fact that live T6b authorization is premature until the new candidate
  passes the static gate.

Commit and push the Brain record. T6a completion means **the qualification
harness is built, verified with zero effects, and correctly refuses the current
candidate**. It does not mean the product is fully qualified or ready for Jill.
