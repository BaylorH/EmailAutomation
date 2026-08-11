# Production Readiness Evidence Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a versioned readiness registry and deterministic views that distinguish safe user return from broader quality coverage without touching runtime behavior.

**Architecture:** A pure-stdlib Python module validates one hand-authored JSON overlay against the existing feature registry, gradebook, and fixture map. It derives stale gate states at a caller-supplied UTC time and renders two committed Markdown views. A sanitized evidence note carries bounded live claims; the renderer never reads production systems or external output folders.

**Tech Stack:** Python 3 standard library, `unittest`, JSON, Markdown, existing SiteSift release-safety artifacts.

---

## File structure

- Create `scripts/generate_readiness_views.py`: strict loader, validator, freshness logic, renderer, and `--check` CLI.
- Create `tests/test_readiness_registry.py`: schema, cross-reference, freshness, PII, rendering, and CLI regressions.
- Create `docs/release-safety/readiness-registry.json`: only hand-authored current readiness/evidence source.
- Create `docs/release-safety/evidence/2026-08-11-controlled-reopen.md`: sanitized bounded proof summary.
- Create `docs/release-safety/current-user-readiness.md`: generated gate view.
- Create `docs/release-safety/full-quality-coverage.md`: generated all-feature view.
- Modify `docs/release-safety/system-audit-packet.md`: point reviewers to the new current decision source and remove blanket-gate ambiguity.

### Task 1: Strict registry validation and freshness

**Files:**
- Create: `scripts/generate_readiness_views.py`
- Create: `tests/test_readiness_registry.py`

- [ ] **Step 1: Write failing schema and cross-reference tests**

Create a minimal valid in-memory registry fixture and tests for exact top-level keys, fixed enums, unique IDs, known feature/event references, resolvable evidence/blocker IDs, existing repo-relative artifact paths, and no email/absolute-path content. The test imports the script through `importlib.util`:

```python
SPEC = importlib.util.spec_from_file_location(
    "generate_readiness_views",
    REPO_ROOT / "scripts" / "generate_readiness_views.py",
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

def test_valid_registry_resolves_all_references(self):
    result = module.validate_registry(
        self.registry,
        self.feature_registry,
        self.gradebook,
        self.fixture_map,
        repo_root=REPO_ROOT,
    )
    self.assertEqual({"login_view", "supervised_campaign_use", "autonomous_campaign_use"}, result.gate_ids)

def test_unknown_feature_and_blocker_fail_with_stable_ids(self):
    bad = copy.deepcopy(self.registry)
    bad["evidence"][0]["featureIds"] = ["core.unknown"]
    with self.assertRaisesRegex(module.RegistryError, "core.unknown"):
        module.validate_registry(bad, self.feature_registry, self.gradebook, self.fixture_map, repo_root=REPO_ROOT)
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
python3 -B -m unittest -v tests.test_readiness_registry
```

Expected: import failure because `scripts/generate_readiness_views.py` does not exist.

- [ ] **Step 3: Implement the minimal validator**

Define these public units:

```python
class RegistryError(ValueError):
    pass

@dataclass(frozen=True)
class ValidatedRegistry:
    registry: dict[str, Any]
    feature_by_id: dict[str, dict[str, Any]]
    fixture_matrix: dict[str, dict[str, Any]]
    gate_ids: frozenset[str]
    evidence_by_id: dict[str, dict[str, Any]]
    quality_by_id: dict[str, dict[str, Any]]

def validate_registry(
    registry: Mapping[str, Any],
    feature_registry: Mapping[str, Any],
    gradebook: Mapping[str, Any],
    fixture_map: Mapping[str, Any],
    *,
    repo_root: Path,
) -> ValidatedRegistry:
    required_top = {
        "schemaVersion", "updatedAt", "releaseIdentity",
        "rolloutGates", "evidence", "qualityItems",
    }
    if set(registry) != required_top or registry.get("schemaVersion") != 1:
        raise RegistryError("registry_schema")
    feature_by_id = {
        feature["id"]: feature for feature in feature_registry["features"]
    }
    known_scenarios = set(gradebook["eventTaxonomy"]) | set(
        gradebook["featureScenarios"]
    )
    gate_ids = {gate["id"] for gate in registry["rolloutGates"]}
    evidence_by_id = {item["id"]: item for item in registry["evidence"]}
    quality_by_id = {item["id"]: item for item in registry["qualityItems"]}
    if len(gate_ids) != len(registry["rolloutGates"]):
        raise RegistryError("duplicate_gate_id")
    if len(evidence_by_id) != len(registry["evidence"]):
        raise RegistryError("duplicate_evidence_id")
    if len(quality_by_id) != len(registry["qualityItems"]):
        raise RegistryError("duplicate_quality_id")
    for item in [*registry["evidence"], *registry["qualityItems"]]:
        unknown_features = set(item["featureIds"]) - set(feature_by_id)
        if unknown_features:
            raise RegistryError(f"unknown_feature:{sorted(unknown_features)[0]}")
        unknown_scenarios = set(item.get("scenarioIds", [])) - known_scenarios
        if unknown_scenarios:
            raise RegistryError(f"unknown_scenario:{sorted(unknown_scenarios)[0]}")
    for gate in registry["rolloutGates"]:
        missing_evidence = set(gate["evidenceIds"]) - set(evidence_by_id)
        missing_blockers = set(gate["blockerIds"]) - set(quality_by_id)
        if missing_evidence:
            raise RegistryError(f"unknown_evidence:{sorted(missing_evidence)[0]}")
        if missing_blockers:
            raise RegistryError(f"unknown_blocker:{sorted(missing_blockers)[0]}")
    return ValidatedRegistry(
        registry=dict(registry),
        feature_by_id=feature_by_id,
        fixture_matrix=dict(fixture_map["featureFixtureMatrix"]),
        gate_ids=frozenset(gate_ids),
        evidence_by_id=evidence_by_id,
        quality_by_id=quality_by_id,
    )
```

Use exact authored enums:

```python
GATE_DECISIONS = {"go", "ready_for_canary", "hold"}
PROOF_LEVELS = {"live_production", "production_readback", "deterministic_test", "source_review", "historical"}
EVIDENCE_RESULTS = {"pass", "partial", "fail"}
QUALITY_STATES = {"proven_live", "source_only", "partial", "open", "ready_for_live"}
```

Require every `go` gate to have passing evidence and zero blockers. Require every `ready_for_canary` gate to name nonempty scope, `forbids`, `nextAction`, blocker IDs, and rollback. Validate scenario IDs against the union of `eventTaxonomy` and `featureScenarios` keys. Reject strings matching email syntax, `/Users/`, `file://`, secrets, or raw message fields anywhere in the registry.

- [ ] **Step 4: Add and implement deterministic freshness tests**

Add:

```python
def test_expired_go_evidence_renders_gate_stale(self):
    at = module.parse_utc("2026-08-12T01:52:01Z")
    statuses = module.effective_gate_decisions(self.validated, at=at)
    self.assertEqual("stale", statuses["login_view"])

def test_ready_for_canary_does_not_promote_from_prior_live_proof(self):
    statuses = module.effective_gate_decisions(
        self.validated,
        at=module.parse_utc("2026-08-11T07:00:00Z"),
    )
    self.assertEqual("ready_for_canary", statuses["supervised_campaign_use"])
    self.assertEqual("hold", statuses["autonomous_campaign_use"])
```

Implement strict `Z`-timestamp parsing and derive `stale` only in generated state; never rewrite authored JSON.

- [ ] **Step 5: Run Task 1 tests and commit**

Run:

```bash
python3 -B -m unittest -v tests.test_readiness_registry
python3 -B -m py_compile scripts/generate_readiness_views.py tests/test_readiness_registry.py
```

Expected: all Task 1 tests pass, compilation exits zero.

Commit:

```bash
git add scripts/generate_readiness_views.py tests/test_readiness_registry.py
git commit -m "test: validate production readiness evidence"
```

### Task 2: Deterministic views and CLI

**Files:**
- Modify: `scripts/generate_readiness_views.py`
- Modify: `tests/test_readiness_registry.py`

- [ ] **Step 1: Write failing renderer and CLI tests**

Add tests asserting:

```python
def test_current_view_states_exact_capability_boundary(self):
    rendered = module.render_current_readiness(self.validated, at=self.at)
    self.assertIn("Login / view | GO", rendered)
    self.assertIn("Supervised campaign use | READY FOR CANARY", rendered)
    self.assertIn("Autonomous campaign use | HOLD", rendered)
    self.assertIn("follow-ups off", rendered)

def test_full_view_separates_mapped_fixtures_from_live_proof(self):
    rendered = module.render_full_quality_coverage(self.validated, at=self.at)
    self.assertIn("Mapped fixtures", rendered)
    self.assertIn("Live/source evidence", rendered)
    self.assertNotIn("Mapped fixtures = proven live", rendered)
```

Use a temporary repository copy to prove default mode writes both files and `--check` exits `2` on byte drift without writing.

- [ ] **Step 2: Run renderer tests and confirm RED**

Run the two named tests. Expected: missing renderer functions.

- [ ] **Step 3: Implement renderers and CLI**

Implement:

```python
def render_current_readiness(validated: ValidatedRegistry, *, at: datetime) -> str:
    """Render three gates, their allows/forbids, blockers, guardrails, evidence age, and next action."""

def render_full_quality_coverage(validated: ValidatedRegistry, *, at: datetime) -> str:
    """Render every production_v1_core feature with mapped fixture counts, evidence, quality items, and retest triggers."""

def render_outputs(repo_root: Path, *, at: datetime) -> dict[Path, str]:
    """Load, validate, and return the two deterministic output payloads."""
```

The full view iterates sorted core feature IDs. Fixture cells count as `mapped fixtures` only when their fixture-map status is `covered`. Evidence status precedence is current `fail`, current `pass`, `partial`, deterministic/source only, then `unproven`. A single evidence record affects only its listed feature/scenario claims.

CLI:

```text
python3 scripts/generate_readiness_views.py [--check] [--at 2026-08-11T07:00:00Z]
```

Default writes both views atomically through temporary sibling files. `--check` performs no writes and exits `2` with relative mismatched paths. Validation errors exit `2` with a stable ID and no data dump.

- [ ] **Step 4: Run Task 2 tests and commit**

Run the focused suite and compilation. Expected: all pass.

Commit:

```bash
git add scripts/generate_readiness_views.py tests/test_readiness_registry.py
git commit -m "feat: render production readiness views"
```

### Task 3: Seed bounded evidence and generated views

**Files:**
- Create: `docs/release-safety/readiness-registry.json`
- Create: `docs/release-safety/evidence/2026-08-11-controlled-reopen.md`
- Create: `docs/release-safety/current-user-readiness.md`
- Create: `docs/release-safety/full-quality-coverage.md`
- Modify: `tests/test_readiness_registry.py`

- [ ] **Step 1: Write failing repository-artifact tests**

Require the committed registry to contain exactly three gate IDs and all sixteen core features to appear in the generated full view. Assert bounded claims:

```python
def test_committed_gate_decisions_match_authoritative_boundary(self):
    registry = json.loads(REGISTRY_PATH.read_text())
    decisions = {gate["id"]: gate["decision"] for gate in registry["rolloutGates"]}
    self.assertEqual({
        "login_view": "go",
        "supervised_campaign_use": "ready_for_canary",
        "autonomous_campaign_use": "hold",
    }, decisions)

def test_one_row_canary_does_not_clear_followups(self):
    autonomous = next(g for g in registry["rolloutGates"] if g["id"] == "autonomous_campaign_use")
    self.assertIn("autonomous_followups", autonomous["forbids"])
    self.assertIn("autonomous-followups-current-live-gap", autonomous["blockerIds"])
```

Also scan the registry, proof summary, and generated views for email addresses, absolute paths, raw addresses, message IDs, and secrets.

- [ ] **Step 2: Run committed-artifact tests and confirm RED**

Expected: registry and evidence files are absent.

- [ ] **Step 3: Create the sanitized evidence note**

Record only these bounded facts:

- immutable backend release `831478fc42fae40bf95b79ee4dcd935cadb7e1a1` and production revision `process-user-00092-som`;
- one ten-row live launch, ten unique initial sends/indexes, queue drain, counters, and zero scoped residue;
- five simple/correction extraction rows with same-row formulas and no repeated ask observed;
- two unavailable rows terminalized with follow-ups stopped;
- Account B login/view containment readback;
- non-claims for copied-party CC, ambiguous multi-suite PDFs, hard repeat suppression, voice variety, autoresponders, and autonomous follow-ups.

Do not include recipients, names, addresses, document IDs, raw bodies, or local paths.

- [ ] **Step 4: Create the readiness registry**

Use `schemaVersion: 1`, `updatedAt: 2026-08-11T01:52:18Z`, the three exact gates, and evidence records linked to the proof note. The login/view evidence expires at `2026-08-12T01:52:18Z`; behavioral evidence expires by explicit `retestOn` triggers.

Seed quality items for:

- `returning-user-canary-unrun` (blocks supervised and autonomous);
- `autonomous-followups-current-live-gap` (blocks autonomous);
- `reply-all-cc-multiparty-live-gap` (blocks autonomous; supervised guardrail avoids complex copied-party threads);
- `pdf-multi-suite-ambiguity` (blocks autonomous document extraction; manual-review guardrail);
- `hard-repeat-ask-rejection-gap` (blocks autonomous);
- `natural-voice-variety` (no gate block; tracked quality);
- `long-multiturn-ordering-gap` (blocks autonomous);
- `account-b-historical-cleanup` (no gate block; post-return hygiene).

Every item must link existing feature and gradebook IDs, name `nextProof`, and use sanitized stable legacy references only.

- [ ] **Step 5: Generate and verify both views**

Run:

```bash
python3 scripts/generate_readiness_views.py --at 2026-08-11T07:00:00Z
python3 scripts/generate_readiness_views.py --check --at 2026-08-11T07:00:00Z
python3 -B -m unittest -v tests.test_readiness_registry
```

Expected: generator and `--check` exit zero; all tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add docs/release-safety/readiness-registry.json \
  docs/release-safety/evidence/2026-08-11-controlled-reopen.md \
  docs/release-safety/current-user-readiness.md \
  docs/release-safety/full-quality-coverage.md \
  tests/test_readiness_registry.py
git commit -m "docs: record controlled user return evidence"
```

### Task 4: Make the readiness source discoverable and verify the branch

**Files:**
- Modify: `docs/release-safety/system-audit-packet.md`
- Modify: `tests/test_readiness_registry.py`

- [ ] **Step 1: Write the failing packet-pointer test**

Require the packet to name all three new source/view files and contain the sentence:

```text
Priority alone never blocks a rollout gate; only an explicit blocksGates link does.
```

Require it to say the current view is authoritative for capability clearance while the packet remains the test-selection contract.

- [ ] **Step 2: Run the pointer test and confirm RED**

Expected: the packet does not yet name the readiness registry.

- [ ] **Step 3: Add the minimal packet section**

Add `## Current capability clearance` immediately before `## Evidence Required Before Normal Users Return`. Link the registry and both views, explain the three gate decisions, and state that mapped fixtures and P0/P1 labels do not automatically equal live proof or a rollout block.

- [ ] **Step 4: Run complete verification**

Run:

```bash
python3 scripts/generate_readiness_views.py --check --at 2026-08-11T07:00:00Z
python3 -B -m unittest -v \
  tests.test_readiness_registry \
  tests.test_release_feature_registry \
  tests.test_system_audit_packet \
  tests.test_production_v1_fixture_map
python3 -B -m py_compile scripts/generate_readiness_views.py tests/test_readiness_registry.py
git diff --check
```

Expected: generator check exits zero; all readiness and 22 pre-existing release-safety tests pass; compilation and diff check exit zero.

- [ ] **Step 5: Commit the integration pointer**

```bash
git add docs/release-safety/system-audit-packet.md tests/test_readiness_registry.py
git commit -m "docs: route release reviews through readiness gates"
```

- [ ] **Step 6: Review the final commit range**

Run:

```bash
git status --short
git log --oneline 5227200..HEAD
git diff --stat 5227200..HEAD
```

Expected: clean worktree; only the planned registry, evidence, renderer, generated views, tests, and packet pointer changed. No runtime, provider, Firestore, mailbox, dashboard, or user-data file is in the diff.
