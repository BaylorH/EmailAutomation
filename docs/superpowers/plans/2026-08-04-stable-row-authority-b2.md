# Stable Row Authority B2 Program Roadmap

**Goal:** Deliver the provider-free B2 logical row authority as five small,
reviewed, exact-SHA GitHub milestones without changing production behavior.

**Deliverable:** both (provider-free code and production-clearance findings)

**Approved design:**
`docs/superpowers/specs/2026-08-04-stable-row-authority-b2-design.md`

**Roadmap baseline:** `bbd739abb3c272443e35fcd356c6a820871e027e`

**Production posture:** NO-GO. B2 has no runtime mode and may not be imported by
B1 or legacy effect paths. No deploy, `main` merge, provider/client call,
campaign, frontend change, Firestore rule change, or environment enablement is
part of this roadmap.

## Why this is a roadmap

The approved design covers several dependent subsystems. An earlier omnibus
draft described all of them accurately but grouped too many RED/GREEN cycles
into single tasks. Execution is therefore split into child implementation plans
that contain complete compilable tests and code for one reviewable slice. A
child plan must be approved, committed, and pushed before its code starts.

## Fixed corrections from roadmap review

These clarify the approved design and apply to every child plan:

1. Domain hashing uses the byte contract
   `domain.encode("utf-8") + b"\0" + canonical_json_bytes(material)`. The
   domain is not a field inside the canonical JSON object. Logical payload
   fields remain top-level beside `schemaVersion` and `userScopeHash`; there is
   no generic nested `payload` wrapper. Independent expected digests are frozen
   before implementation.
2. Duplicate valid row-marker matches return the full bounded canonical match
   tuple. They do not disappear behind an exception. Identity/location code
   hashes that complete tuple into a null-coordinate `ambiguous` revision.
3. A valid deleted row identity remains bindable by a late/recreated thread so
   the root can discover its durable settlement. Deletion blocks new row
   claims/effects, not immutable historical association.
4. The 400-write test boundary lives in a B2-only fake subclass/wrapper in
   `tests/row_authority_fakes.py`. Retained B1 fake infrastructure is unchanged.
5. The existing global B2-literal prohibition is replaced—not supplemented—by
   a path-aware allowlist before the first B2 production module is created.
6. Every publication gate resolves local HEAD, proves the branch ref equals
   that SHA, selects a workflow run whose `headSha` equals it, waits with
   `--exit-status`, and rechecks successful conclusion before continuing.

## Child implementation plans

### B2-A0 — Canonical contracts and isolated test harness

Plan:
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a0-contracts.md`

Creates the B2-only bounded transaction fake, path-aware containment, canonical
JSON/domain hashing, strict UUIDv4 row IDs, verified-user scope hashes, mailbox
normalization/hashes, base errors, bounds, and complete primitive tests.

Publication checkpoint: `B2-A0`.

### B2-A1 — DeveloperMetadata identity and location revisions

Plan path:
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-a1-identity-location.md`

Creates pure DeveloperMetadata dictionaries and parser, marker-aware Sheet
model, exact document schemas, identity/revision/head initialization, location
CAS, apply-then-raise readback, deletion, ambiguity, and coordinate reuse.

Publication checkpoint: `B2-A`.

### B2-B — Bindings, claims, generations, settlements, and B1 links

Plan path:
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-b-ownership.md`

Creates immutable thread/reverse bindings, deleted-row late-root discovery,
stable contact associations/evidence, discriminated authority origins,
priority/all-or-none claims, leases/fences, operator decline, settlements, and
read-only B1 post-settlement links.

Publication checkpoint: `B2-B`.

### B2-C — Contact-wide opt-out convergence and release

Plan path:
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-c-contact-compliance.md`

Creates exact/canonical alias authority, fail-closed suppression, verified
opt-out contact settlements/heads, stable binding fan-out, bounded discovery
and apply, authenticated release, restore/no-op/superseded evidence, late
binding recertification, and race fencing.

Publication checkpoint: `B2-C`.

### B2-D — Offline migration, health, retention, reset, and evidence

Plan path:
`docs/superpowers/plans/2026-08-04-stable-row-authority-b2-d-clearance.md`

Creates the sanitized offline migration report, allocation/review/link
contracts, bounded exact health audit, automatic-cleanup exclusion, explicit
reset inventory, complete static containment, full regressions, independent
reviews, and immutable B2 evidence.

Publication checkpoints: `B2-D code` and `B2 final evidence`.

## Dependency order

```text
B2-A0 contracts/harness
  -> B2-A1 identity/location
  -> B2-B row ownership
  -> B2-C contact compliance
  -> B2-D clearance/evidence
  -> B3 atomic pre-send/provider-effect authority
  -> B4 frontend/rules/migration execution/live proof
  -> production clearance decision
```

No child may start until the previous child is locally green, independently
approved, committed, pushed to the owned release branch, read back at the exact
SHA, and green in Production Clearance CI.

## Exact publication protocol

The user explicitly authorized keeping the owned GitHub branch current at
milestones. Publication does not authorize a PR, `main` merge, deployment,
release, external message, or production runtime action.

At each named checkpoint run:

```bash
git push origin codex/sitesift-production-clearance-20260804
B2_CHECKPOINT_SHA="$(git rev-parse HEAD)"
B2_REMOTE_SHA="$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)"
test "$B2_CHECKPOINT_SHA" = "$B2_REMOTE_SHA"

B2_RUN_ID=""
for attempt in {1..30}; do
  B2_RUN_ID="$(gh run list \
    --branch codex/sitesift-production-clearance-20260804 \
    --workflow production-clearance-ci.yml \
    --commit "$B2_CHECKPOINT_SHA" \
    --limit 1 \
    --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$B2_CHECKPOINT_SHA"'"))[0].databaseId // empty')"
  test -n "$B2_RUN_ID" && break
  sleep 2
done
test -n "$B2_RUN_ID"
gh run watch "$B2_RUN_ID" --exit-status
test "$(gh run view "$B2_RUN_ID" --json headSha --jq .headSha)" = \
  "$B2_CHECKPOINT_SHA"
test "$(gh run view "$B2_RUN_ID" --json conclusion --jq .conclusion)" = \
  success
gh run view "$B2_RUN_ID" --json url,jobs \
  --jq '{runUrl: .url, jobs: [.jobs[] | {name, url, startedAt, completedAt, conclusion}]}'
gh run view "$B2_RUN_ID" --log | \
  rg 'Ran [0-9]+ tests in|OK$|No broken requirements|(^| )ok$'
test "$(git rev-parse HEAD)" = "$B2_CHECKPOINT_SHA"
test "$(git ls-remote origin \
  refs/heads/codex/sitesift-production-clearance-20260804 | cut -f1)" = \
  "$B2_CHECKPOINT_SHA"
test -z "$(git status --porcelain)"
```

Record the printed exact SHA, run/job URL, test counts, and durations; never
select a run for a different SHA.

## GitHub and production governance

- Work only on `codex/sitesift-production-clearance-20260804`.
- `main` is currently unprotected and is therefore a release-governance risk.
- Never direct-push or merge `main` during B2.
- Every code push must include automatic discovery of all existing
  `test_row_authority*.py` modules once B2-A0 adds that CI step.
- B2 logical settlements do not prove a Sheet, Graph, reply, notification, or
  send-permit effect occurred.
- A user-launched canary is not considered until B3-B4 are green and a separate
  production decision is recorded.

## Current status

- [x] M0 exact-candidate CI baseline is frozen and green.
- [x] B1 exact-source authority is frozen and green.
- [x] B2 design is independently approved, pushed, and green at
  `bbd739abb3c272443e35fcd356c6a820871e027e`.
- [x] B2-A0 child plan is independently approved and published at its plan milestone.
- [x] B2-A0 code is green and published.
- [x] B2-A1 child plan is independently approved and published.
- B2-A1 local code candidate
  `4c35f941b762975c589ead4a117e98ae79470b5b` is independently approved;
  exact-SHA publication is pending.
- [ ] B2-A1 is green and published.
- [ ] B2-B is green and published.
- [ ] B2-C is green and published.
- [ ] B2-D code/evidence is green and published.
- [ ] B3-B4 production clearance gates are complete.
