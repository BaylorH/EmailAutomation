# Stable Row Authority B2-A0 Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the isolated B2 test harness and complete canonical identity/hash primitives that every later row-authority record depends on.

**Architecture:** Keep B1 fakes untouched by adding a B2-only bounded subclass. Create a standard-library-only `row_authority.py` containing exact canonical JSON, domain-prefix hashing, verified-user scope hashing, strict UUIDv4 row IDs, and mailbox normalization/hashing; no datastore or runtime adapter is introduced.

**Tech Stack:** Python 3.12 standard library, `unittest`, AST inventory tests, existing hermetic Firestore fake subclassing, GitHub Actions.

**Plan deliverable:** both (provider-free code and B2-A0 clearance evidence)
**Approved spec:** `docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`
**Program roadmap:** `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
**Baseline:** `bbd739abb3c272443e35fcd356c6a820871e027e`
**Safety boundary:** No provider/client import, Firestore authority record, runtime adoption, production read/write, deploy, `main` merge, campaign, frontend/rules change, or external communication. Remote writes are limited to reviewed milestone commits on Baylor's owned release branch: this approved plan, the B2-A0 candidate/evidence checkpoint, and its remote-evidence follow-up.

---

## File map

- Create `tests/row_authority_fakes.py`: B2-only 400-write-ceiling transaction
  and Firestore fake subclasses.
- Create `tests/test_row_authority_contracts.py`: complete bounded-fake,
  canonical JSON, domain hash, user scope, UUIDv4 row ID, mailbox, and error
  tests.
- Modify `tests/test_source_coordinator_inventory.py`: replace the global B2/B3
  literal prohibition with an exact per-path/per-token allowlist.
- Create `email_automation/row_authority.py`: standard-library-only B2 primitive
  contracts.
- Modify `.github/workflows/production-clearance-ci.yml`: permanently discover
  all `test_row_authority*.py` tests.
- Create
  `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a0.md`: exact
  local/remote verification and review evidence.

## Task order

Task 0 isolates the test harness. Task 1 adds canonical/domain/user/row-ID
primitives. Task 2 adds mailbox primitives and the permanent CI test step. Task
3 reviews, verifies, publishes, and freezes evidence.

Use this interpreter for every Python command:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python
```

### Mandatory plan-publication gate — complete before Task 0

The implementation executor may not create or modify B2 code until this child
plan and the program roadmap have two independent `APPROVED` verdicts and are
published at a green exact SHA. Complete this protocol first:

1. Run fenced Python/Bash parse checks, placeholder/whitespace scans, and
   `git diff --check --no-index /dev/null` against both plan files. Obtain one
   design-compliance approval and one fresh-executor/TDD approval. Any
   Critical or Important finding resets the corresponding approval.
2. Mark only the roadmap's `B2-A0 child plan` status complete. Stage exactly
   the roadmap and this child plan, inspect `git diff --cached --stat`, run
   `git diff --cached --check`, and commit with
   `docs: split B2 into executable milestones`.
3. Publish only that commit to
   `codex/sitesift-production-clearance-20260804`, then prove the remote branch
   equals local HEAD and select CI only by that exact head SHA:

```bash
git push origin codex/sitesift-production-clearance-20260804
B2_A0_PLAN_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_A0_PLAN_SHA"

B2_A0_PLAN_RUN_ID=""
for attempt in {1..30}; do
  B2_A0_PLAN_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_A0_PLAN_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_A0_PLAN_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_A0_PLAN_RUN_ID" && break
  sleep 2
done
test -n "$B2_A0_PLAN_RUN_ID"
gh run watch "$B2_A0_PLAN_RUN_ID" --exit-status
test "$(gh run view "$B2_A0_PLAN_RUN_ID" \
  --json headSha --jq .headSha)" = "$B2_A0_PLAN_SHA"
test "$(gh run view "$B2_A0_PLAN_RUN_ID" \
  --json conclusion --jq .conclusion)" = success
test -z "$(git status --porcelain)"
```

Record the plan SHA and run URL in the implementation log. A PR, `main` merge,
deployment, runtime flag, campaign, or external message remains forbidden.
Task 0 starts only after every command above succeeds.

### Task 0: Isolate the B2 write ceiling and replace the literal gate

**Files:**
- Create: `tests/row_authority_fakes.py`
- Create: `tests/test_row_authority_contracts.py`
- Modify: `tests/test_source_coordinator_inventory.py`

- [ ] **Step 1: Replace the global literal expectation with the exact allowlist test**

Add this constant immediately after
`B2_B3_FORBIDDEN_OWNERSHIP_LITERALS`:

```python
B2_B3_OWNERSHIP_LITERAL_ALLOWLIST = {
    "email_automation/row_authority.py": frozenset(
        {"rowBindings", "stableRowOwner"}
    ),
}
```

Replace
`test_b2_b3_ownership_literals_are_absent_from_runtime` completely with:

```python
def test_b2_b3_ownership_literals_are_bounded_to_reviewed_b2_files(self):
    violations = Counter(
        {
            (path, literal): count
            for (path, literal), count in (
                self.static_closure_inventory.forbidden_ownership_literals.items()
            )
            if literal
            not in B2_B3_OWNERSHIP_LITERAL_ALLOWLIST.get(path, frozenset())
        }
    )
    self.assertEqual(Counter(), violations)
```

This replacement keeps `executionEpoch`, `executionClaimId`, and
`providerIntent` forbidden everywhere because they do not appear in the
allowlist. It permits only `rowBindings` and `stableRowOwner`, only in the pure
B2 authority module.

- [ ] **Step 2: Create the complete failing bounded-fake test file**

Create `tests/test_row_authority_contracts.py` with exactly:

```python
"""Focused contracts for provider-free B2 row authority."""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_AUTHORITY_FAKES_PATH = REPO_ROOT / "tests" / "row_authority_fakes.py"


class BoundedRowAuthorityFakeTests(unittest.TestCase):
    def _load_fakes(self):
        self.assertTrue(
            ROW_AUTHORITY_FAKES_PATH.exists(),
            "row authority fakes module is missing",
        )
        return importlib.import_module("tests.row_authority_fakes")

    def test_invalid_write_ceilings_are_rejected(self):
        module = self._load_fakes()
        for value in (True, False, 0, -1, 1.5, "400", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.BoundedFakeFirestore(max_writes_per_commit=value)

    def test_exactly_400_writes_commit(self):
        module = self._load_fakes()
        store = module.BoundedFakeFirestore(max_writes_per_commit=400)
        transaction = store.transaction()
        for index in range(400):
            transaction.create(
                store.collection("bounded").document(str(index)),
                {"index": index},
            )
        transaction.commit()
        self.assertEqual(400, len(store.data))
        self.assertIn(("commit_applied", 400), store.events)

    def test_401_writes_fail_before_any_apply(self):
        module = self._load_fakes()
        store = module.BoundedFakeFirestore(max_writes_per_commit=400)
        transaction = store.transaction()
        for index in range(401):
            transaction.create(
                store.collection("bounded").document(str(index)),
                {"index": index},
            )
        with self.assertRaisesRegex(RuntimeError, "400-write ceiling"):
            transaction.commit()
        self.assertEqual({}, store.data)
        self.assertIn(
            ("commit_refused_write_ceiling", 401, 400),
            store.events,
        )
        self.assertNotIn(("commit_applied", 401), store.events)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the exact RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator_inventory \
  tests.test_row_authority_contracts -v
```

Expected: the inventory suite passes and exactly three bounded-fake tests fail
with `row authority fakes module is missing`. A `ModuleNotFoundError` or B1 test
failure is an invalid RED.

- [ ] **Step 4: Create the complete B2-only bounded fake**

Create `tests/row_authority_fakes.py` with exactly:

```python
"""B2-only fakes layered on the retained B1 Firestore fake."""

from __future__ import annotations

from tests.source_coordinator_fakes import FakeFirestore, FakeTransaction


class BoundedFakeTransaction(FakeTransaction):
    """Reject an oversized B2 transaction before barriers or writes apply."""

    def _apply_buffered(self):
        ceiling = self._store.max_writes_per_commit
        write_count = len(self._operations)
        if write_count > ceiling:
            self._store.events.append(
                ("commit_refused_write_ceiling", write_count, ceiling)
            )
            raise RuntimeError(
                f"fake transaction exceeds {ceiling}-write ceiling"
            )
        return super()._apply_buffered()


class BoundedFakeFirestore(FakeFirestore):
    """FakeFirestore with a required positive per-commit write ceiling."""

    def __init__(self, *, max_writes_per_commit=400):
        if (
            isinstance(max_writes_per_commit, bool)
            or type(max_writes_per_commit) is not int
            or max_writes_per_commit < 1
        ):
            raise ValueError(
                "fake write ceiling must be a positive integer"
            )
        super().__init__()
        self.max_writes_per_commit = max_writes_per_commit

    def transaction(self, max_attempts=5):
        return BoundedFakeTransaction(self, max_attempts=max_attempts)
```

Do not modify `tests/source_coordinator_fakes.py`.

- [ ] **Step 5: Run GREEN and retained B1 fake regressions**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts \
  tests.test_source_coordinator_inventory \
  tests.test_source_coordinator \
  tests.test_source_coordinator_integration -v
git diff --check
```

Expected: every test passes and `git diff --check` has no output.

- [ ] **Step 6: Commit Task 0**

Mark Task 0 checkboxes complete, then run:

```bash
git add tests/row_authority_fakes.py \
  tests/test_row_authority_contracts.py \
  tests/test_source_coordinator_inventory.py \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md
git commit -m "test: isolate B2 authority write bounds"
```

### Task 1: Add canonical JSON, byte-prefix domains, user scope, and row IDs

**Files:**
- Create: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contracts.py`

- [ ] **Step 1: Append the complete failing primitive test class**

Before the file's final `if __name__ == "__main__":` block, add exactly:

```python
import ast
import hashlib
import json
from types import MappingProxyType
from uuid import UUID


ROW_AUTHORITY_PATH = REPO_ROOT / "email_automation" / "row_authority.py"
ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS = frozenset(
    {"__future__", "hashlib", "json", "re", "unicodedata", "uuid"}
)
ROW_AUTHORITY_IMPORTER_ALLOWLIST = frozenset()
_APPLICATION_SCAN_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "tests",
        "vendor",
        "venv",
    }
)


def _direct_import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("<relative>")
            elif node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def _literal_dynamic_imports(tree):
    targets = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not node.args
            or not isinstance(node.args[0], ast.Constant)
            or type(node.args[0].value) is not str
        ):
            continue
        call_name = None
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr
        if call_name in {"__import__", "import_module"}:
            targets.append(node.args[0].value)
    return targets


def _tree_imports_row_authority(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "email_automation.row_authority"
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module in {
                "email_automation.row_authority",
                "row_authority",
            }:
                return True
            if (
                node.module == "email_automation"
                or (node.level and node.module is None)
            ) and any(alias.name == "row_authority" for alias in node.names):
                return True
    return "email_automation.row_authority" in _literal_dynamic_imports(tree)


def _application_python_paths():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if (
            relative == Path("email_automation/row_authority.py")
            or any(
                part in _APPLICATION_SCAN_EXCLUDED_PARTS
                for part in relative.parts
            )
        ):
            continue
        yield relative


class CanonicalRowAuthorityPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = None
        if ROW_AUTHORITY_PATH.exists():
            cls.module = importlib.import_module("email_automation.row_authority")

    def test_row_authority_module_exists(self):
        self.assertTrue(
            ROW_AUTHORITY_PATH.exists(),
            "row authority module is missing",
        )

    def test_row_authority_imports_are_standard_library_only(self):
        self.assertTrue(
            ROW_AUTHORITY_PATH.exists(),
            "row authority module is missing",
        )
        tree = ast.parse(
            ROW_AUTHORITY_PATH.read_text(encoding="utf-8"),
            filename=str(ROW_AUTHORITY_PATH),
        )
        self.assertEqual(
            set(),
            _direct_import_roots(tree)
            - ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS,
        )
        self.assertEqual([], _literal_dynamic_imports(tree))

    def test_row_authority_is_not_imported_by_runtime(self):
        importers = [
            relative.as_posix()
            for relative in _application_python_paths()
            if relative.as_posix() not in ROW_AUTHORITY_IMPORTER_ALLOWLIST
            and _tree_imports_row_authority(
                ast.parse(
                    (REPO_ROOT / relative).read_text(encoding="utf-8"),
                    filename=str(relative),
                )
            )
        ]
        self.assertEqual([], importers)

    def _require_module(self):
        if self.module is None:
            self.skipTest("row authority module is missing")
        return self.module

    @staticmethod
    def _reference_hash(domain, material):
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(
            domain.encode("utf-8") + b"\0" + encoded
        ).hexdigest()

    def test_canonical_json_is_exact_utf8_sorted_compact_and_tuple_normalized(self):
        module = self._require_module()
        self.assertEqual(
            b'{"a":[true,1,"\xc3\xa9"],"z":null}',
            module.canonical_json_bytes(
                {"z": None, "a": (True, 1, "é")}
            ),
        )

    def test_canonical_json_rejects_unsupported_unsafe_and_cyclic_values(self):
        module = self._require_module()
        cyclic = []
        cyclic.append(cyclic)
        invalid_values = (
            float("nan"),
            float("inf"),
            1.25,
            9007199254740992,
            b"bytes",
            {"set"},
            MappingProxyType({"a": 1}),
            {1: "non-string-key"},
            chr(0xD800),
            cyclic,
        )
        for value in invalid_values:
            with self.subTest(value=type(value).__name__), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.canonical_json_bytes(value)

    def test_domain_hash_uses_byte_prefix_and_frozen_expected_digest(self):
        module = self._require_module()
        scope_hash = "a" * 64
        payload = {"nullable": None, "value": 1}
        expected = (
            "773aefc65ea24cf28562eb10df940fc5fec5a6e9e520e2b64536e0957398568d"
        )
        actual = module.domain_hash(
            "sitesift.test.payload.v1",
            payload,
            user_scope_hash=scope_hash,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(
            expected,
            self._reference_hash(
                "sitesift.test.payload.v1",
                {
                    **payload,
                    "schemaVersion": 1,
                    "userScopeHash": scope_hash,
                },
            ),
        )
        legacy_inside_json = hashlib.sha256(
            json.dumps(
                {
                    "domain": "sitesift.test.payload.v1",
                    **payload,
                    "schemaVersion": 1,
                    "userScopeHash": scope_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(actual, legacy_inside_json)

    def test_domain_hash_changes_for_domain_scope_null_and_value_drift(self):
        module = self._require_module()
        base = module.domain_hash(
            "sitesift.test.payload.v1",
            {"nullable": None, "value": 1},
            user_scope_hash="a" * 64,
        )
        variants = (
            module.domain_hash(
                "sitesift.test.other.v1",
                {"nullable": None, "value": 1},
                user_scope_hash="a" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": None, "value": 1},
                user_scope_hash="b" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": "", "value": 1},
                user_scope_hash="a" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": None, "value": 2},
                user_scope_hash="a" * 64,
            ),
        )
        self.assertEqual(4, len(set(variants)))
        self.assertNotIn(base, variants)

    def test_user_scope_hash_is_exact_untransformed_and_frozen(self):
        module = self._require_module()
        self.assertEqual(
            "48fafc848b44ae7b0414309666dcb54208b7867700240a0f343ec02c53eb0cf2",
            module.user_scope_hash("uid-1"),
        )
        self.assertNotEqual(
            module.user_scope_hash("uid"),
            module.user_scope_hash(" uid "),
        )
        self.assertNotEqual(
            module.user_scope_hash("UID"),
            module.user_scope_hash("uid"),
        )

    def test_user_scope_rejects_empty_control_oversize_and_non_string(self):
        module = self._require_module()
        for value in (
            "",
            "uid\n1",
            "x" * 513,
            chr(0xD800),
            None,
            1,
            True,
        ):
            with self.subTest(value=repr(value)), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.user_scope_hash(value)

    def test_row_id_requires_rfc4122_uuid4_and_preserves_all_hex(self):
        module = self._require_module()
        value = UUID("123e4567-e89b-42d3-a456-426614174000")
        self.assertEqual(
            "sr1_123e4567e89b42d3a456426614174000",
            module.new_row_id(uuid_factory=lambda: value),
        )
        self.assertEqual(
            "sr1_123e4567e89b42d3a456426614174000",
            module.validate_row_id(
                "sr1_123e4567e89b42d3a456426614174000"
            ),
        )

    def test_row_id_rejects_wrong_version_variant_shape_and_factory_type(self):
        module = self._require_module()
        invalid_factories = (
            lambda: UUID("123e4567-e89b-12d3-a456-426614174000"),
            lambda: UUID("123e4567-e89b-42d3-7456-426614174000"),
            lambda: "123e4567-e89b-42d3-a456-426614174000",
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.new_row_id(uuid_factory=factory)
        for value in (
            "",
            "sr1_123e4567e89b12d3a456426614174000",
            "sr1_123e4567e89b42d37456426614174000",
            "sr1_123E4567E89B42D3A456426614174000",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.validate_row_id(value)

    def test_domain_and_scope_hash_validation_is_exact(self):
        module = self._require_module()
        for domain in (
            "",
            "sitesift.test",
            "sitesift.TEST.v1",
            "sitesift.test.v0",
            "sitesift." + chr(0xD800) + ".v1",
            "x" * 129,
            None,
        ):
            with self.subTest(domain=domain), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    domain,
                    {},
                    user_scope_hash="a" * 64,
                )
        for scope_hash in ("a" * 63, "A" * 64, "g" * 64, None):
            with self.subTest(scope_hash=scope_hash), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    "sitesift.test.payload.v1",
                    {},
                    user_scope_hash=scope_hash,
                )
        for payload in (
            None,
            [],
            {"schemaVersion": 1},
            {"userScopeHash": "a" * 64},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    "sitesift.test.payload.v1",
                    payload,
                    user_scope_hash="a" * 64,
                )

    def test_error_codes_are_stable_and_specific(self):
        self.assertIsNotNone(self.module, "row authority module is missing")
        module = self.module
        self.assertEqual("row_authority_error", module.RowAuthorityError.code)
        self.assertEqual(
            "row_authority_retryable", module.RowAuthorityRetryable.code
        )
        self.assertEqual(
            "row_authority_ambiguous", module.RowAuthorityAmbiguous.code
        )
        self.assertEqual(
            "row_authority_conflict", module.RowAuthorityConflict.code
        )
        self.assertEqual(
            "row_authority_config_error",
            module.RowAuthorityConfigError.code,
        )
```

Move the existing final `if __name__ == "__main__": unittest.main()` block to
the end of the file after this class.

- [ ] **Step 2: Run the exact RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests -v
```

Expected: the module-existence, standard-library import, and error-code tests
fail because the module is missing; the runtime non-adoption test passes; the
nine behavior tests skip. A test-module import error is not an accepted RED.

- [ ] **Step 3: Create the importable error/constant skeleton and make its contracts GREEN**

Create `email_automation/row_authority.py` with exactly:

```python
"""Provider-free primitive contracts for B2 stable row authority."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from uuid import RFC_4122, UUID, uuid4


SCHEMA_VERSION = 1
MAX_ROW_BINDINGS = 128
MAX_ROW_AUTHORITY_PLANNED_WRITES = 400
MAX_OPAQUE_BYTES = 512
MAX_JSON_SAFE_INTEGER = 9007199254740991

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_PATTERN = re.compile(
    r"^sitesift\.[a-z0-9][a-z0-9_.-]*\.v[1-9][0-9]*$"
)
_ROW_ID_PATTERN = re.compile(
    r"^sr1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
)


class RowAuthorityError(RuntimeError):
    code = "row_authority_error"


class RowAuthorityRetryable(RowAuthorityError):
    code = "row_authority_retryable"


class RowAuthorityAmbiguous(RowAuthorityError):
    code = "row_authority_ambiguous"


class RowAuthorityConflict(RowAuthorityError):
    code = "row_authority_conflict"


class RowAuthorityConfigError(RowAuthorityError):
    code = "row_authority_config_error"
```

Run only the skeleton contracts:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_row_authority_module_exists \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_row_authority_imports_are_standard_library_only \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_row_authority_is_not_imported_by_runtime \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_error_codes_are_stable_and_specific -v
```

Expected: four tests pass. This is the minimal GREEN for the initial RED; none
of the behavior functions exist yet.

- [ ] **Step 4: Run canonical-JSON RED, append its implementation, and make it GREEN**

Run the two canonical tests before adding code:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_canonical_json_is_exact_utf8_sorted_compact_and_tuple_normalized \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_canonical_json_rejects_unsupported_unsafe_and_cyclic_values -v
```

Expected RED: both tests error only because `canonical_json_bytes` is absent.

Append exactly:

```python
def _utf8_bytes(value, *, field_name):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RowAuthorityConfigError(
            f"{field_name} must contain valid UTF-8 text"
        ) from exc


def _canonical_json_value(value, *, path, seen):
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        _utf8_bytes(value, field_name=path)
        return value
    if type(value) is int:
        if abs(value) > MAX_JSON_SAFE_INTEGER:
            raise RowAuthorityConfigError(
                f"{path} exceeds the JSON safe-integer bound"
            )
        return value
    if type(value) is float:
        raise RowAuthorityConfigError(f"{path} cannot be a float")
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in seen:
            raise RowAuthorityConfigError(f"{path} contains a cycle")
        seen.add(identity)
        try:
            return [
                _canonical_json_value(
                    item,
                    path=f"{path}[{index}]",
                    seen=seen,
                )
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    if type(value) is dict:
        identity = id(value)
        if identity in seen:
            raise RowAuthorityConfigError(f"{path} contains a cycle")
        seen.add(identity)
        try:
            normalized = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise RowAuthorityConfigError(
                        f"{path} contains a non-string key"
                    )
                _utf8_bytes(key, field_name=f"{path} key")
                normalized[key] = _canonical_json_value(
                    item,
                    path=f"{path}.{key}",
                    seen=seen,
                )
            return normalized
        finally:
            seen.remove(identity)
    raise RowAuthorityConfigError(
        f"{path} contains unsupported type {type(value).__name__}"
    )


def canonical_json_bytes(value):
    normalized = _canonical_json_value(value, path="$", seen=set())
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
```

Rerun the same two-test command. Expected GREEN: 2/2 pass.

- [ ] **Step 5: Run domain-hash RED, append its implementation, and make it GREEN**

Run the domain-focused tests before adding code:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_domain_hash_uses_byte_prefix_and_frozen_expected_digest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_domain_hash_changes_for_domain_scope_null_and_value_drift \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_domain_and_scope_hash_validation_is_exact -v
```

Expected RED: all three methods error only because `domain_hash` is absent.

Append exactly:

```python


def _require_sha256(value, *, field_name):
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise RowAuthorityConfigError(
            f"{field_name} must be a complete lowercase SHA-256 hash"
        )
    return value


def _require_domain(domain):
    if type(domain) is not str:
        raise RowAuthorityConfigError(
            "domain must be a bounded versioned sitesift domain"
        )
    encoded = _utf8_bytes(domain, field_name="domain")
    if len(encoded) > 128 or _DOMAIN_PATTERN.fullmatch(domain) is None:
        raise RowAuthorityConfigError(
            "domain must be a bounded versioned sitesift domain"
        )
    return domain


def domain_hash(domain, payload, *, user_scope_hash):
    checked_domain = _require_domain(domain)
    checked_scope = _require_sha256(
        user_scope_hash,
        field_name="user_scope_hash",
    )
    if type(payload) is not dict:
        raise RowAuthorityConfigError(
            "domain hash payload must be an exact field dictionary"
        )
    reserved_fields = {"schemaVersion", "userScopeHash"} & payload.keys()
    if reserved_fields:
        raise RowAuthorityConfigError(
            "domain hash payload cannot replace canonical envelope fields"
        )
    material = {
        **payload,
        "schemaVersion": SCHEMA_VERSION,
        "userScopeHash": checked_scope,
    }
    return hashlib.sha256(
        _utf8_bytes(checked_domain, field_name="domain")
        + b"\0"
        + canonical_json_bytes(material)
    ).hexdigest()
```

Rerun the same three-test command. Expected GREEN: 3/3 pass and both frozen
domain vectors match independently computed values.

- [ ] **Step 6: Run verified-user scope RED, append its implementation, and make it GREEN**

Run the user-scope tests before adding code:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_user_scope_hash_is_exact_untransformed_and_frozen \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_user_scope_rejects_empty_control_oversize_and_non_string -v
```

Expected RED: both tests error only because `user_scope_hash` is absent.

Append exactly:

```python
def _contains_control(value):
    return any(
        unicodedata.category(character).startswith("C")
        for character in value
    )


def user_scope_hash(verified_user_id):
    if type(verified_user_id) is not str:
        raise RowAuthorityConfigError(
            "verified_user_id must be an exact string"
        )
    encoded = _utf8_bytes(
        verified_user_id,
        field_name="verified_user_id",
    )
    if (
        not encoded
        or len(encoded) > MAX_OPAQUE_BYTES
        or _contains_control(verified_user_id)
    ):
        raise RowAuthorityConfigError(
            "verified_user_id must be nonempty, bounded, and control-free"
        )
    material = {"verifiedUserId": verified_user_id}
    return hashlib.sha256(
        b"sitesift.user.scope.v1\0" + canonical_json_bytes(material)
    ).hexdigest()
```

Rerun the same two-test command. Expected GREEN: 2/2 pass and the exact user
scope is neither trimmed nor case-normalized.

- [ ] **Step 7: Run UUIDv4 row-ID RED, append its implementation, and make it GREEN**

Run the row-ID tests before adding code:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_row_id_requires_rfc4122_uuid4_and_preserves_all_hex \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests.test_row_id_rejects_wrong_version_variant_shape_and_factory_type -v
```

Expected RED: both tests error because `new_row_id`/`validate_row_id` are
absent.

Append exactly:

```python


def validate_row_id(value):
    if type(value) is not str or _ROW_ID_PATTERN.fullmatch(value) is None:
        raise RowAuthorityConfigError(
            "row_id must be sr1_ followed by RFC4122 UUIDv4 hex"
        )
    return value


def new_row_id(*, uuid_factory=uuid4):
    value = uuid_factory()
    if (
        not isinstance(value, UUID)
        or value.version != 4
        or value.variant != RFC_4122
    ):
        raise RowAuthorityConfigError(
            "row ID factory must return an RFC4122 UUIDv4"
        )
    return validate_row_id(f"sr1_{value.hex}")
```

Rerun the same two-test command. Expected GREEN: 2/2 pass.

- [ ] **Step 8: Run complete Task 1 GREEN and containment**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.CanonicalRowAuthorityPrimitiveTests \
  tests.test_source_coordinator_inventory -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m py_compile \
  email_automation/row_authority.py tests/test_row_authority_contracts.py
git diff --check
```

Expected: every test passes, compilation exits 0, and diff check has no output.

- [ ] **Step 9: Commit Task 1**

Mark Task 1 checkboxes complete, then run:

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_contracts.py \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md
git commit -m "feat: add canonical row authority primitives"
```

### Task 2: Add mailbox normalization, contact hashes, and permanent B2 CI

**Files:**
- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contracts.py`
- Modify: `.github/workflows/production-clearance-ci.yml`

- [ ] **Step 1: Append the complete failing mailbox test class**

Before the test file's final `if __name__ == "__main__":` block, add exactly:

```python
class ContactIdentityPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def test_normalization_nfc_trims_lowers_and_strips_only_first_plus_suffix(self):
        exact, canonical = self.module.normalize_contact_mailbox(
            "  First.La\u0301st+Tour+Second@Example.COM  "
        )
        self.assertEqual("first.lást+tour+second@example.com", exact)
        self.assertEqual("first.lást@example.com", canonical)

    def test_normalization_preserves_dots_and_has_no_domain_specific_rules(self):
        exact, canonical = self.module.normalize_contact_mailbox(
            "First.Last+Tag@Example.com"
        )
        self.assertEqual("first.last+tag@example.com", exact)
        self.assertEqual("first.last@example.com", canonical)
        self.assertNotEqual("firstlast@example.com", canonical)

    def test_normalization_rejects_invalid_or_overbound_mailboxes(self):
        invalid = (
            "",
            "plain-address",
            "a@@example.com",
            "@example.com",
            "+tag@example.com",
            "a@",
            "a\n@example.com",
            "a@exam\u0000ple.com",
            chr(0xD800) + "@example.com",
            "a" * 310 + "@example.com",
            None,
            1,
            True,
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.normalize_contact_mailbox(value)

    def test_contact_identity_hash_has_frozen_prefixed_digest(self):
        actual = self.module.contact_identity_hash(
            "first.last@example.com",
            user_scope_hash="a" * 64,
        )
        self.assertEqual(
            "0929de5bfcbb44acae6c72bcafbd62c0587ee42c12aab305883a723b5639515c",
            actual,
        )

    def test_plus_variants_have_distinct_exact_hashes_and_one_canonical_hash(self):
        first_exact, first_canonical = self.module.normalize_contact_mailbox(
            "first.last+one@example.com"
        )
        second_exact, second_canonical = self.module.normalize_contact_mailbox(
            "first.last+two@example.com"
        )
        self.assertEqual(first_canonical, second_canonical)
        self.assertNotEqual(
            self.module.contact_identity_hash(
                first_exact,
                user_scope_hash="a" * 64,
            ),
            self.module.contact_identity_hash(
                second_exact,
                user_scope_hash="a" * 64,
            ),
        )
        self.assertEqual(
            self.module.contact_identity_hash(
                first_canonical,
                user_scope_hash="a" * 64,
            ),
            self.module.contact_identity_hash(
                second_canonical,
                user_scope_hash="a" * 64,
            ),
        )

    def test_contact_hash_rejects_untrimmed_mixed_case_and_non_nfc_input(self):
        invalid = (
            " first.last@example.com",
            "First.Last@example.com",
            "first.la\u0301st@example.com",
            None,
        )
        for value in invalid:
            with self.subTest(value=repr(value)), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.contact_identity_hash(
                    value,
                    user_scope_hash="a" * 64,
                )

    def test_contact_hash_output_contains_no_mailbox_material(self):
        value = "private.mailbox+tag@example.com"
        exact, canonical = self.module.normalize_contact_mailbox(value)
        for normalized in (exact, canonical):
            digest = self.module.contact_identity_hash(
                normalized,
                user_scope_hash="a" * 64,
            )
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotIn("private", digest)
            self.assertNotIn("example", digest)
```

Move the final `if __name__ == "__main__": unittest.main()` block after this
class.

- [ ] **Step 2: Run the exact RED**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts.ContactIdentityPrimitiveTests -v
```

Expected: seven errors naming missing
`normalize_contact_mailbox`/`contact_identity_hash`. No unrelated test may fail.

- [ ] **Step 3: Append the complete mailbox implementation**

Add these constants after `MAX_OPAQUE_BYTES`:

```python
MAX_MAILBOX_BYTES = 320
CONTACT_NORMALIZATION_VERSION = "sitesift-mailbox-v1"
```

Append these functions to `email_automation/row_authority.py`:

```python
def normalize_contact_mailbox(mailbox):
    if type(mailbox) is not str:
        raise RowAuthorityConfigError("mailbox must be a string")
    normalized = unicodedata.normalize("NFC", mailbox).strip().lower()
    encoded = _utf8_bytes(normalized, field_name="mailbox")
    if (
        not encoded
        or len(encoded) > MAX_MAILBOX_BYTES
        or _contains_control(normalized)
        or normalized.count("@") != 1
    ):
        raise RowAuthorityConfigError(
            "mailbox must be bounded, control-free, and contain one @"
        )
    local_part, domain = normalized.split("@", 1)
    if not local_part or not domain:
        raise RowAuthorityConfigError(
            "mailbox local part and domain must be nonempty"
        )
    canonical_local = local_part.split("+", 1)[0]
    if not canonical_local:
        raise RowAuthorityConfigError(
            "mailbox canonical local part must be nonempty"
        )
    return normalized, f"{canonical_local}@{domain}"


def contact_identity_hash(normalized_mailbox, *, user_scope_hash):
    exact, _canonical = normalize_contact_mailbox(normalized_mailbox)
    if exact != normalized_mailbox:
        raise RowAuthorityConfigError(
            "contact identity hash requires a normalized mailbox"
        )
    payload = {
        "normalizationVersion": CONTACT_NORMALIZATION_VERSION,
        "normalizedMailboxIdentity": normalized_mailbox,
    }
    return domain_hash(
        "sitesift.contact.identity.v1",
        payload,
        user_scope_hash=user_scope_hash,
    )
```

- [ ] **Step 4: Run mailbox GREEN and complete focused regression**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator_inventory \
  tests.test_source_coordinator \
  tests.test_source_coordinator_integration -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m py_compile \
  email_automation/row_authority.py tests/row_authority_fakes.py \
  tests/test_row_authority_contracts.py
git diff --check
```

Expected: every test passes, compilation exits 0, and diff check has no output.

- [ ] **Step 5: Add permanent B2 test discovery to GitHub Actions**

Insert this exact step after `Run complete B1 focused suite` in
`.github/workflows/production-clearance-ci.yml`:

```yaml
      - name: Run complete B2 focused suite
        run: >-
          python -m unittest discover
          -s tests
          -p 'test_row_authority*.py'
          -v
```

Verify syntax and the exact local equivalent:

```bash
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/production-clearance-ci.yml"); puts "ok"'
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest discover \
  -s tests -p 'test_row_authority*.py' -v
```

Expected: YAML prints `ok` and every B2 test passes.

- [ ] **Step 6: Commit Task 2**

Mark Task 2 checkboxes complete, then run:

```bash
git add email_automation/row_authority.py \
  tests/test_row_authority_contracts.py \
  .github/workflows/production-clearance-ci.yml \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md
git commit -m "feat: add contact identity primitives"
```

### Task 3: Review, verify, publish, and freeze B2-A0 evidence

**Files:**
- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md`
- Create: `docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a0.md`

- [ ] **Step 1: Run the complete local B2-A0 gate with provider egress unavailable**

```bash
B2_A0_PY=../codex-release-a-medium-recovery-20260714/.venv/bin/python
B2_A0_OFFLINE_ENV=(
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

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A0_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A0_PY" -m unittest discover \
  -s tests -p 'test_row_authority*.py' -v

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A0_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A0_PY" -m unittest \
  auth_service.test_auth_service_isolation \
  tests.test_jill_live_campaign_regressions \
  tests.test_full_campaign_e2e -v

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A0_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=paused "$B2_A0_PY" -m unittest \
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

env -u GOOGLE_APPLICATION_CREDENTIALS "${B2_A0_OFFLINE_ENV[@]}" \
  SITESIFT_OUTBOUND_MODE=live "$B2_A0_PY" -m unittest \
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

"$B2_A0_PY" -m py_compile \
  email_automation/row_authority.py tests/row_authority_fakes.py \
  tests/test_row_authority_contracts.py
"$B2_A0_PY" -m pip check
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/production-clearance-ci.yml"); puts "ok"'
git diff --check 2b5e785
```

Expected: every command exits 0; B2-A0 is 23/23, release/auth is 95/95,
complete B1 is 606/606, retained M2 is 669/669, `pip check` reports no broken
requirements, YAML prints `ok`, and the diff check has no output. Record exact
counts and durations from the actual run rather than copying these expected
baselines.

- [ ] **Step 2: Obtain independent spec-compliance and code-quality approvals**

Dispatch a fresh read-only reviewer against the B2 design plus the B2-A0 diff.
Dispatch a different fresh read-only reviewer for correctness, standard-library
containment, test discrimination, maintainability, and security boundaries.
Critical/Important findings block publication. Fix with a new failing test,
rerun the focused and retained gates, commit, and request re-review.

Expected: both reviewers return `APPROVED`.

- [ ] **Step 3: Create the exact evidence document and mark plans**

Create the evidence document with exactly these headings:

```markdown
# Stable Row Authority B2-A0 Evidence

## Candidate and scope
## Commits
## Canonical-domain and identity proof
## Local verification
## Static containment
## Independent reviews
## GitHub exact-SHA run
## Production posture and next milestone
```

Record full commit SHAs, exact commands/counts/durations, frozen digest values,
changed-file inventory, and review verdicts. Before the push, leave the GitHub
run section marked `pending exact-SHA publication`; this exact phrase is
replaced after Step 5 readback. Mark only the child-plan steps already executed
as `[x]` and confirm the roadmap's plan-publication item remains complete. The
production posture
must state: `B2-A0 adds provider-free primitives only; production remains
NO-GO.`

- [ ] **Step 4: Commit the pre-publication evidence state**

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a0.md
git commit -m "docs: freeze B2-A0 local evidence"
```

- [ ] **Step 5: Push once and prove the exact remote SHA and CI conclusion**

```bash
git push origin codex/sitesift-production-clearance-20260804
B2_A0_SHA="$(git rev-parse HEAD)"
B2_A0_REMOTE_SHA="$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)"
test "$B2_A0_SHA" = "$B2_A0_REMOTE_SHA"
```

Use this bounded poll, then print the exact run/job URLs and count/duration log
lines needed by the evidence document:

```bash
B2_A0_RUN_ID=""
for attempt in {1..30}; do
  B2_A0_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_A0_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_A0_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_A0_RUN_ID" && break
  sleep 2
done
test -n "$B2_A0_RUN_ID"
gh run watch "$B2_A0_RUN_ID" --exit-status
test "$(gh run view "$B2_A0_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_A0_SHA"
test "$(gh run view "$B2_A0_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
gh run view "$B2_A0_RUN_ID" --json url,jobs \
  --jq '{runUrl: .url, jobs: [.jobs[] | {name, url, startedAt, completedAt, conclusion}]}'
gh run view "$B2_A0_RUN_ID" --log | \
  rg 'Ran [0-9]+ tests in|OK$|No broken requirements|(^| )ok$'
```

Expected: local/remote SHA equality and successful exact-SHA workflow containing
release/auth, B1, B2 discovery, retained M2, compile, and diff steps. Do not
open a PR, merge, or deploy.

- [ ] **Step 6: Append remote evidence, commit, push, and reverify**

Replace the pending phrase with the exact run/job URLs, remote SHA, B2 count,
release/auth count, B1 count, retained M2 count, and durations. Mark the
roadmap's `B2-A0 code` item complete. Then run:

```bash
git add docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md \
  docs/superpowers/evidence/2026-08-04-stable-row-authority-b2-a0.md
git commit -m "docs: freeze B2-A0 remote evidence"
git push origin codex/sitesift-production-clearance-20260804
B2_A0_EVIDENCE_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_A0_EVIDENCE_SHA"

B2_A0_EVIDENCE_RUN_ID=""
for attempt in {1..30}; do
  B2_A0_EVIDENCE_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_A0_EVIDENCE_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_A0_EVIDENCE_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_A0_EVIDENCE_RUN_ID" && break
  sleep 2
done
test -n "$B2_A0_EVIDENCE_RUN_ID"
gh run watch "$B2_A0_EVIDENCE_RUN_ID" --exit-status
test "$(gh run view "$B2_A0_EVIDENCE_RUN_ID" \
  --json headSha --jq .headSha)" = "$B2_A0_EVIDENCE_SHA"
test "$(gh run view "$B2_A0_EVIDENCE_RUN_ID" \
  --json conclusion --jq .conclusion)" = success
gh run view "$B2_A0_EVIDENCE_RUN_ID" --json url,jobs \
  --jq '{runUrl: .url, jobs: [.jobs[] | {name, url, startedAt, completedAt, conclusion}]}'
gh run view "$B2_A0_EVIDENCE_RUN_ID" --log | \
  rg 'Ran [0-9]+ tests in|OK$|No broken requirements|(^| )ok$'
test "$(git rev-parse HEAD)" = "$B2_A0_EVIDENCE_SHA"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_A0_EVIDENCE_SHA"
test -z "$(git status --porcelain)"
```

Expected: the second exact-SHA CI run is green and the branch is clean and
synchronized. Stop at the B2-A1 boundary; do not merge or deploy.
