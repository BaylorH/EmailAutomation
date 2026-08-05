# B1 Contact Identity Binding Amendment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans to
> implement this plan task by task. Use superpowers:test-driven-development for
> every behavior change, superpowers:systematic-debugging for every unexpected
> failure, superpowers:requesting-code-review at every frozen review gate, and
> superpowers:verification-before-completion before every publication claim.

**Goal:** Close the B1-to-B2 contact provenance gap by freezing user-scoped
exact/canonical mailbox hashes into newly verified deterministic hard-opt-out
evidence and carrying that authority only through a discriminated v2 B1 link,
while preserving every existing v1 terminal/human and legacy link byte.

**Architecture:** `source_coordinator.py` independently derives the approved
mailbox hashes from the verifier-approved frozen `message.from` only after the
existing strict verifier succeeds. New hard opt-outs persist a nested v2
evidence object whose existing snapshot/selection/owner/ledger chain binds both
hashes. `row_authority.py` accepts the exact legacy v1 link shape/domain and a
new exact contact-only v2 shape/domain; builders emit v1 for terminal/human or
legacy evidence and v2 only for bound hard opt-out. Both modules remain
independent and provider-free, with cross-module parity proved in tests rather
than runtime imports.

**Tech stack:** Python 3.12, existing injected Firestore-shaped fakes,
standard-library hashing/Unicode/JSON, `unittest`, AST/static inventory, and
GitHub Actions.

**Plan deliverable:** both (provider-free B1/B2 bridge code and immutable
production-clearance evidence)

**Normative amendment:**
`docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md`

**B1 design:**
`docs/superpowers/specs/2026-08-02-shared-exact-source-coordinator-design.md`

**B2 design:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`

**Program roadmap:**
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`

**Baseline:** `48a23dbf31e2b3c04f8e745239768f6f264c9e0b`

**Publication checkpoint:** `B1/B2 contact identity bridge`

**Safety boundary:** No provider/API client/import/call, send, campaign,
notification, reply, Sheet write, production Firestore mutation, runtime
enablement, deploy, `main` merge, frontend/rules/workflow change, B2-C contact
write, or external communication. Tests blackhole provider networking. Remote
writes are limited to reviewed milestone commits on Baylor's owned
`codex/sitesift-production-clearance-20260804` branch. Production and Jill's
return remain NO-GO.

## Frozen implementation decisions

1. The hard-opt-out verifier signature and exact result remain unchanged:
   `{schemaVersion: 1, evidenceKind: str, evidenceHash: h64}`. Extra identity
   fields returned by the verifier remain invalid. B1 derives identity only
   after accepting that result.
2. Only the verified hard-opt-out branch requires
   `classification_input["message"]["from"]`. A verifier returning `None`,
   local-policy evidence, and model classification retain their current input
   behavior.
3. Mailbox normalization and user/contact hashes are byte-for-byte the B2-A0
   contract. The new contact-only JSON helper uses `ensure_ascii=False` and
   cannot reuse B1's existing escaping canonical helper; contact control checks
   reject every Unicode category beginning with `C`; verified user IDs are
   exact control-free strings of 1–512 UTF-8 bytes; normalized mailboxes are at
   most 320 UTF-8 bytes. Raw case/whitespace variants normalize rather than
   fail. A private B1 helper named
   `_hard_optout_contact_identity_hashes(*, user_id, canonical_source_id,
   classification_input)` requires the v2 input's exact source ID, returns
   `(exact_hash, canonical_hash)`, and never returns normalized raw strings.
4. New non-local hard-opt-out deterministic evidence is exactly version 2 with
   `schemaVersion`, `evidenceKind`, `evidenceHash`, `exactIdentityHash`, and
   `canonicalMailboxIdentityHash`. Local policy and legacy unbound evidence are
   exact version 1 with the original three fields.
5. `_build_classification_snapshot_material` validates exact deterministic
   evidence shapes. Existing v1 snapshot retry validates the historical input
   hash and current strict verifier result, then rebuilds v1 without invoking
   any mailbox/user-scope helper and compares byte-for-byte; it never upgrades.
   A new claimed hard opt-out always writes v2. V2 retry re-derives both hashes
   from the exact input and requires exact equality.
6. The existing deterministic candidate remains exactly
   `{type: contact_optout, evidenceHash: deterministicEvidenceHash}`. No new
   candidate, owner, ledger, or settlement field is added; their existing hash
   chain transitively binds v2 evidence.
7. `B1LinkV1` retains the current exact keys and
   `sitesift.row.b1_authority_link.v1`. `B1LinkV2` adds non-null
   `exactIdentityHash` and `canonicalMailboxIdentityHash`, is valid only for
   `contact_optout`, and hashes under
   `sitesift.row.b1_authority_link.v2`.
8. `build_b1_authority_link(...)` keeps its signature. It emits v1 for every
   terminal/human classification and legacy v1 contact evidence. It emits v2
   only after strict v2 evidence, candidate, owner, and ledger validation.
   `validate_b1_authority_link(...)` accepts only one complete exact shape and
   recomputes only its matching domain.
9. Existing v1 claim/generation/settlement/source-link vectors remain
   byte-identical. B2-C later requires v2 before creating aliases, a contact
   settlement/head, or fan-out. The already-existing private
   `_plan_contact_fanout_row_claim` rejects v1 before request-ID derivation or
   row planning. This milestone performs no B2-C mutation.
10. SourceCoordinator still has no production hard-opt-out verifier. Static
    construction and call-graph inventory must prove no runtime injection was
    introduced. Production inventory of legacy unbound contact authority is a
    B2-D/B4 activation stop gate; it is not inferred from static code.
11. Raw mailboxes and verified user IDs do not appear in persisted authority,
    fixtures intended as evidence, evidence docs, logs, or Brain. Test inputs
    may use reserved example domains and assert their absence from stored data.
12. Every callback rebuilds derived evidence on SDK retry. Existing B1
    transaction readback remains exact. Executor apply-then-raise succeeds only
    for the complete expected v2 classification after-image; partial,
    malformed, unreadable, or mismatched state remains ambiguous.

## Exact provider-free API and schema delta

Private B1 helpers added to `email_automation/source_coordinator.py`:

```python
_contact_identity_canonical_json_bytes(value) -> bytes

_hard_optout_contact_identity_hashes(
    *, user_id, canonical_source_id, classification_input
) -> tuple[str, str]

_bound_hard_optout_evidence(
    *, user_id, canonical_source_id, classification_input, verified_evidence
) -> dict
```

No public SourceCoordinator signature changes.

`email_automation/row_authority.py` retains the existing public signatures:

```python
build_b1_authority_link(
    *, user_scope_hash, source_identity_document,
    source_classification_document, source_owner_document,
    source_ledger_document, work_key
)

validate_b1_authority_link(*, authority_link, user_scope_hash)
```

The validator discriminates exact link shapes by key set. V1 has the current
eleven fields. V2 has those eleven plus `exactIdentityHash` and
`canonicalMailboxIdentityHash`; v2 is contact-only and both new hashes are
required. The constant registry retains
`B1_AUTHORITY_LINK_HASH_DOMAIN = "sitesift.row.b1_authority_link.v1"` and adds
`B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN =
"sitesift.row.b1_authority_link.v2"`.

## File map

- Modify `email_automation/source_coordinator.py`: private mailbox derivation,
  exact v1/v2 deterministic-evidence validation, new-v2 persistence, and
  legacy-v1 retry preservation.
- Modify `email_automation/row_authority.py`: exact v1/v2 B1Link builder and
  validator discrimination plus v2-only contact-fanout origin gate; no
  store-method signature or B1 write.
- Modify `tests/test_source_coordinator.py`: complete pure/transaction/race/
  readback tests for identity-bound v2 evidence and v1 replay.
- Modify `tests/test_source_coordinator_integration.py`: deterministic source
  fixture includes the exact `canonicalSourceId` and `message.from` and proves
  the integrated v2 chain.
- Modify `tests/test_source_coordinator_inventory.py`: production verifier
  absence, no B2 import, no new authority writer, and no bypass.
- Modify `tests/test_row_authority_contracts.py`: retain v1 domain and register
  the exact v2 contact-link domain/containment.
- Modify `tests/test_row_authority_ownership.py`: independent v1/v2 vectors,
  builder/validator correlations, legacy compatibility, and store regressions.
- Modify the three design/roadmap documents named above only for the approved
  amendment/status link.
- Create
  `docs/superpowers/evidence/2026-08-04-b1-contact-identity-binding-amendment.md`
  after final code clearance.

No production runtime, provider, frontend, rules, workflow, dependency, or
Firestore fake file is expected to change.

## Task order

Task 0 freezes and publishes this plan/design milestone. Task 1 adds B1 v2
evidence using strict RED/GREEN. Task 2 adds discriminated v2 contact links
while preserving v1 bytes. Task 3 performs complete verification, two fresh
reviews, evidence publication, and exact-SHA GitHub proof. B2-C planning resumes
only after Task 3 is green.

## Implementation log

- Independently approved design/plan commit:
  `778e50208e42c91aab501340efdbeb8f88939202`.
- Exact plan-shape GitHub proof:
  `https://github.com/BaylorH/EmailAutomation/actions/runs/31001047514`
  (`offline-verification` job `92289728426`, successful at the exact commit).

Use this interpreter for every Python command:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python
```

## Mandatory plan-publication gate — complete before Task 1

- [x] **Step 1: Validate the draft and obtain two independent approvals**

Run:

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python - <<'PY'
from pathlib import Path

spec = Path(
    "docs/superpowers/specs/"
    "2026-08-04-b1-contact-identity-binding-amendment.md"
).read_text(encoding="utf-8")
plan = Path(
    "docs/superpowers/plans/"
    "2026-08-04-b1-contact-identity-binding-amendment.md"
).read_text(encoding="utf-8")
assert "**Plan deliverable:** both" in plan
assert "sitesift.row.b1_authority_link.v2" in spec
assert "legacy" in spec.lower()
assert "Production and Jill's return remain NO-GO" in plan
assert plan.count("- [ ] **Step") >= 18
print("ok")
PY
git diff --check
! rg -n 'TO[D]O|T[B]D|FIX[M]E|PLACEH[O]LDER|pending decisio[n]' \
  docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md \
  docs/superpowers/plans/2026-08-04-b1-contact-identity-binding-amendment.md
```

Reviewer A checks B1 trust, exact v1/v2 schemas, input/proof/hash chaining,
privacy, legacy replay, and production verifier containment. Reviewer B checks
B2 link domain discrimination, byte-compatible v1 claims/source links,
cross-user safety, TDD completeness, and B2-C eligibility. Any Critical or
Important finding resets that approval.

- [x] **Step 2: Mark the approved design and roadmap plan checkpoint**

Change the amendment status to `Approved`. Add these status items immediately
after B2-B in the roadmap:

```markdown
- [x] B1 contact-identity binding amendment is independently approved and its
  child plan is published.
- [ ] B1 contact-identity binding amendment code/evidence is green and
  published.
```

Add a short dependency subsection before B2-C that links this plan and states
that B2-C accepts only v2 verified-contact links.

- [x] **Step 3: Commit and inspect only the approved documentation milestone**

```bash
git add \
  docs/superpowers/specs/2026-08-02-shared-exact-source-coordinator-design.md \
  docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md \
  docs/superpowers/specs/2026-08-04-b1-contact-identity-binding-amendment.md \
  docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md \
  docs/superpowers/plans/2026-08-04-b1-contact-identity-binding-amendment.md
git diff --cached --stat
git diff --cached --check
git diff --cached
git commit -m "docs: bind B1 opt-outs to contact identity"
```

- [x] **Step 4: Push and prove exact-SHA plan CI**

```bash
git push origin codex/sitesift-production-clearance-20260804
B1_CONTACT_PLAN_SHA="$(git rev-parse HEAD)"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B1_CONTACT_PLAN_SHA"
gh run list --branch codex/sitesift-production-clearance-20260804 \
  --workflow production-clearance-ci.yml --commit "$B1_CONTACT_PLAN_SHA" \
  --limit 1 --json databaseId,headSha,status,conclusion,url
```

Select only a run whose `headSha` equals the exact plan SHA, wait using
`gh run watch RUN_ID --exit-status`, then recheck `headSha == plan SHA` and
`conclusion == success`. Record the SHA/run URL in the implementation log. Do
not open a PR, merge, deploy, or touch production.

### Task 1: Freeze identity-bound B1 deterministic evidence

**Files:**

- Modify: `email_automation/source_coordinator.py`
- Modify: `tests/test_source_coordinator.py`
- Modify: `tests/test_source_coordinator_integration.py`
- Modify: `tests/test_source_coordinator_inventory.py`

- [ ] **Step 1: Write the failing pure hash and exact-schema tests**

Add independent fixed expected vectors for a bare mailbox, a plus variant, a
Unicode/case/whitespace variant, and the same mailbox in two user scopes. Prove
exact/canonical equality only without a plus tag; controls, invalid UTF-8,
multiple/missing `@`, empty canonical local part, and 321-byte normalized input
fail. Include a fixed non-ASCII digest, Unicode `Cf` rejection, and a 513-byte
verified-user-ID rejection. Prove the new helper does not reuse B1's escaping
canonical helper and no runtime import of `row_authority` supplies the result.

- [ ] **Step 2: Write the failing verified-snapshot transaction tests**

Cover new v2 persistence, exact retry, two-worker equality, missing or
mismatched v2 `canonicalSourceId`, missing/non-string/malformed sender after
hard verification, verifier attempts
to return identity fields, input/evidence drift, apply-then-raise exact
after-image, partial/malformed/unreadable readback, no raw mailbox persistence,
and zero model calls. Retain tests showing missing `from` is legal when the hard
verifier returns `None` or local policy wins. Seed a self-consistent legacy v1
hard snapshot whose historical input has no `canonicalSourceId` or `from` and
prove exact retry neither invokes identity derivation nor upgrades it.

- [ ] **Step 3: Run the RED and record discriminating failures**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_source_coordinator \
  tests.test_source_coordinator_integration \
  tests.test_source_coordinator_inventory -v
```

Expected: only the newly added v2/parity assertions fail against the unchanged
implementation. Existing tests remain green. Record exact failing test names in
the implementation log before changing application code.

- [ ] **Step 4: Implement the minimum private derivation and v1/v2 evidence path**

Add the private helpers and exact evidence validators. Invoke identity
derivation only after successful non-local hard verification. New claimed
snapshots build v2; stored v1 retry rebuilds v1; stored v2 retry re-derives v2.
Do not add a collection, public parameter, raw persisted identity, or model/
provider seam.

- [ ] **Step 5: Run focused GREEN plus the complete B1 gate**

Use the blackholed environment in Task 3, then run the three focused modules
above and the complete B1 gate listed in Task 3. Expected: all pass with zero
skips/failures/errors and the static inventory still proves no production hard
verifier.

- [ ] **Step 6: Self-review, commit, push, and exact-SHA CI-prove Task 1**

```bash
git diff --check
git diff -- email_automation/source_coordinator.py \
  tests/test_source_coordinator.py \
  tests/test_source_coordinator_integration.py \
  tests/test_source_coordinator_inventory.py
git add email_automation/source_coordinator.py \
  tests/test_source_coordinator.py \
  tests/test_source_coordinator_integration.py \
  tests/test_source_coordinator_inventory.py
git commit -m "feat: bind verified opt-outs to contacts"
git push origin codex/sitesift-production-clearance-20260804
```

Resolve local/remote exact SHA, select only matching GitHub CI, wait for
success, and record the SHA/run URL before Task 2.

### Task 2: Add v2 verified-contact B1 links without changing v1 bytes

**Files:**

- Modify: `email_automation/row_authority.py`
- Modify: `tests/test_row_authority_contracts.py`
- Modify: `tests/test_row_authority_ownership.py`

- [ ] **Step 1: Write failing independent v1/v2 domain and schema tests**

Freeze the existing v1 digest before editing code. Add an independently
calculated v2 contact digest, domain-separation assertion, exact key sets, and
tests rejecting v1 material with v2 keys, v2 material under v1, one missing
identity hash, non-contact v2, extra keys, cross-scope validation, and defensive
copy drift.

- [ ] **Step 2: Write failing builder and composition tests**

Prove terminal/human and legacy-v1 contact fixtures still produce their exact
old links/hashes. Prove bound-v2 contact evidence emits v2 with hashes copied
only from stored classification, rejects caller/document substitution, and
retains the complete selection/owner/ledger/evidence chain. Prove v1 direct B1
claim/generation/settlement/source-link vectors remain unchanged and a v2
contact-fanout claim preserves the complete link. Add
`test_contact_fanout_origin_rejects_legacy_v1_contact_link_before_planning` and
prove it reaches neither request-ID derivation nor row-state planning.

- [ ] **Step 3: Run the RED and record discriminating failures**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts \
  tests.test_row_authority_ownership -v
```

Expected: only new v2 tests fail because the domain/shape is absent; frozen v1
vectors pass.

- [ ] **Step 4: Implement exact discriminated link construction/validation**

Retain the v1 constant and exact code path. Add the v2 domain, strict bound
evidence validation, v2 material builder, and key-set/domain discriminator.
Require the exact v2 shape in `_plan_contact_fanout_row_claim` before delegating
to the generic planner. Never infer v2 from caller data. Do not alter store
signatures, claim priority, transaction write counts, source collections, or
runtime imports.

- [ ] **Step 5: Run focused GREEN and complete B2 discovery**

```bash
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  tests.test_row_authority_contracts \
  tests.test_row_authority_ownership -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
```

Expected: all pass with zero skips/failures/errors; every frozen v1 vector is
byte-identical.

- [ ] **Step 6: Self-review, commit, push, and exact-SHA CI-prove Task 2**

```bash
git diff --check
git diff -- email_automation/row_authority.py \
  tests/test_row_authority_contracts.py \
  tests/test_row_authority_ownership.py
git add email_automation/row_authority.py \
  tests/test_row_authority_contracts.py \
  tests/test_row_authority_ownership.py
git commit -m "feat: carry verified contact identity in B1 links"
git push origin codex/sitesift-production-clearance-20260804
```

Resolve local/remote exact SHA, select only matching GitHub CI, wait for
success, and record the SHA/run URL before Task 3.

### Task 3: Full clearance, evidence, and publication

**Files:**

- Create:
  `docs/superpowers/evidence/2026-08-04-b1-contact-identity-binding-amendment.md`
- Modify: `docs/superpowers/plans/2026-08-04-stable-row-authority-b2.md`
- Modify: this plan only to mark completed checkboxes

- [ ] **Step 1: Run exact blackholed local verification**

```bash
export FIRESTORE_EMULATOR_HOST=127.0.0.1:9
export GOOGLE_CLOUD_PROJECT=sitesift-offline-ci
export OPENAI_API_KEY=
export SITESIFT_OUTBOUND_MODE=paused
export SITESIFT_SOURCE_COORDINATOR_MODE=disabled
export HTTP_PROXY=http://127.0.0.1:9
export HTTPS_PROXY=http://127.0.0.1:9
export ALL_PROXY=http://127.0.0.1:9
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export all_proxy=http://127.0.0.1:9
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  auth_service.test_auth_service_isolation \
  tests.test_jill_live_campaign_regressions \
  tests.test_full_campaign_e2e -v
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
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
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
  discover -s tests -p 'test_row_authority*.py' -v
SITESIFT_OUTBOUND_MODE=live \
  ../codex-release-a-medium-recovery-20260714/.venv/bin/python -m unittest \
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
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m compileall \
  -q email_automation scripts tests
../codex-release-a-medium-recovery-20260714/.venv/bin/python -m pip check
git diff --check
git status --short
```

Expected: release/auth, complete B1, complete B2, and retained M2 all pass with
zero skips/failures/errors; compile/pip/diff pass. The `live` M2 variable changes
only hermetic fake semantics while all provider proxies remain blackholed.

- [ ] **Step 2: Obtain two fresh full-diff approvals**

Reviewer A checks the exact baseline-to-code diff for B1 trust, schema/hash
versioning, immutability, no raw identity, retry/readback, and production
verifier absence. Reviewer B independently checks v1 byte compatibility, v2
contact correlation, B2-B regressions, cross-user safety, static containment,
and test discrimination. Any Critical or Important finding requires a new RED,
minimum fix, full gate rerun, and fresh approval.

- [ ] **Step 3: Freeze code candidate and prove exact GitHub SHA**

Compute `git diff --binary BASELINE..HEAD | shasum -a 256`, give that digest to
both reviewers, push the reviewed HEAD, prove remote branch equality, select
only the exact `headSha` CI run, wait with `--exit-status`, and recheck success.
No code changes are allowed after the final approvals without resetting them.

- [ ] **Step 4: Write and independently audit immutable evidence**

Record baseline, plan SHA, implementation commits, exact diff digest, test
counts/timings, RED/GREEN discrimination, v1/v2 vectors, verifier/runtime
inventory, reviewers, exact GitHub run/job URLs, production posture, and the
B2-C next gate. Do not record any raw mailbox, recipient, verified user ID, or
other PII. A reviewer must read the rendered evidence against command output.

- [ ] **Step 5: Mark final roadmap/plan status and publish evidence**

Mark only the amendment code/evidence roadmap item complete and the completed
checkboxes in this plan. Stage exactly the evidence, roadmap, and plan; inspect
the staged diff; commit `docs: record B1 contact identity evidence`; push; and
prove a second exact-SHA successful GitHub run. Local HEAD, remote branch, and
workflow `headSha` must match and the worktree must be clean.

- [ ] **Step 6: Stop at the B2-C plan boundary**

The amendment does not authorize B2-C code, production deployment, frontend
testing, campaign execution, Jill's return, or external communication. Resume
the separately reviewed B2-C child-plan gate next.

## Completion criteria

- [ ] New verified hard opt-outs persist identity-bound v2 evidence and no raw
  mailbox.
- [ ] Legacy v1 hard evidence and links remain immutable/readable but cannot
  authorize B2-C.
- [ ] V1 terminal/human link and downstream document vectors are byte-exact.
- [ ] V2 contact links are source-derived, user-scoped, domain-separated, and
  substitution-resistant.
- [ ] Full local and exact-SHA GitHub gates are green with two fresh approvals.
- [ ] Evidence and roadmap are published on the owned branch; production and
  Jill remain NO-GO pending B2-C/B2-D/B3/B4 and an authorized canary.
