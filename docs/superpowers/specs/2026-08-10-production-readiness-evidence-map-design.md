# Production readiness evidence map design

Status: approved for planning on 2026-08-10

## Outcome

SiteSift will maintain one small, versioned evidence overlay that answers two
different questions without conflating them:

1. **Can a user safely return at this level today?**
2. **Which product behaviors are proven live, partially proven, or still open?**

The map is an operational aid, not a new release obstacle. A quality item
blocks only the capability gates explicitly named on that item. Historical
cleanup, copy polish, and broader edge-case work cannot silently reverse a
narrower user return decision.

## Existing sources remain authoritative for their own concerns

- `feature-registry.json` owns feature identity, lane, module ownership, send
  risk, dependencies, and stop conditions.
- `feature-gradebook.json` owns scenario and variation taxonomy.
- `production-v1-fixture-map.json` owns deterministic fixture coverage.
- `system-audit-matrix.json` owns cross-repository review surfaces and
  readbacks.
- The new evidence overlay owns observed proof, known quality items, and current
  rollout decisions. It must reference the existing IDs instead of copying their
  definitions.

## Artifacts

### Hand-maintained source

Add `docs/release-safety/readiness-registry.json` with four top-level
collections:

- `releaseIdentity`
- `rolloutGates`
- `evidence`
- `qualityItems`

The file is the only new hand-maintained source. It contains no mailbox
addresses, names, message bodies, property addresses, tokens, document IDs, or
absolute local paths.

### Deterministic generated views

Add a sanitized durable proof summary at
`docs/release-safety/evidence/2026-08-11-controlled-reopen.md`. Add
`scripts/generate_readiness_views.py` and generate:

- `docs/release-safety/current-user-readiness.md`
- `docs/release-safety/full-quality-coverage.md`

The renderer supports `--check`. It sorts by stable IDs, produces identical
output for identical input, and never reads live systems. Generated files are
committed so a human can read the current answer without running tooling.

## Rollout decision model

The following capability gates are independent and ordered:

1. `login_view`
2. `supervised_campaign_use`
3. `autonomous_campaign_use`

Each gate has:

- `status`: `go`, `ready_for_canary`, or `hold`
- `scope`: the accounts, campaign shape, and operational limits covered
- `allows` and `forbids`: explicit capabilities at this gate
- `evidenceIds`: exact proof supporting the decision
- `blockerIds`: only quality items that truly prevent this level
- `guardrails`: limitations that permit safe use without claiming full quality
- `rollback`: the effective containment action
- `asOf`: the last authoritative decision time
- `nextAction`: the smallest promotion action, or null
- `invalidatedBy`: deploy, policy, allowlist, queue, send-cap, or evidence
  changes that require recalculation

The generated readiness view leads with the highest currently safe level and
the smallest action needed to reach the next level. It separately lists
post-return improvements.

## Evidence model

Each evidence item has:

- stable `id`
- `featureIds` from `feature-registry.json`
- `scenarioIds` from the gradebook
- `proofLevel`: `live_production`, `production_readback`,
  `deterministic_test`, `source_review`, or `historical`
- `result`: `pass`, `partial`, or `fail`
- `observedAt`
- immutable release references where applicable
- sanitized artifact references or hashes
- the exact claim proved
- required source-of-truth readbacks
- limitations and explicit non-claims
- freshness and invalidation rules
- optional `supersedes` links

A live campaign closes only the scenarios it actually exercised. For example,
a ten-row launch with simple text extraction, corrected values, unavailable
events, formulas, and no observed repeated asks does not prove complex CC
topology, dense multi-suite PDFs, long conversations, or every voice shape.

## Quality item model

Each quality item has:

- stable `id`
- affected `featureIds` and `scenarioIds`
- `state`: `proven_live`, `source_only`, `partial`, `open`, or
  `ready_for_live`
- `severity`
- `blocksGates`
- `guardrail`
- supporting `evidenceIds` or sanitized `legacyRefs`
- `nextProof`
- `owner`

Initial quality items must include the user-reported repeat/flyer problem,
reply-all/CC preservation, PDF extraction ambiguity, natural voice, and
multi-turn behavior. User reports prove the historical failure shape; they do
not prove current production behavior or root cause.

## Current migration boundary

Seed all sixteen `production_v1_core` features. Do not mark them all live from
one campaign.

The first migration records these distinct current decisions:

- login/view: `go`
- supervised campaign use: `ready_for_canary`; Baylor must deliberately enable
  and launch the exact one-row campaign with follow-ups off
- autonomous campaign use: `hold`

The first returning-user canary can promote supervised campaign use. It cannot
by itself clear autonomous follow-ups, ambiguous multi-property documents, or
other capabilities excluded by its scope. Those capabilities need their own
named proof before they are included in a wider gate.

The recent live scale proof may support only its observed launch, recipient
binding, simple/correction extraction, same-row formulas, unavailable
terminalization, automatic close, dashboard action, queue, counter, and
residue scenarios. CC, ambiguous PDF/multi-suite extraction, voice variety,
long multi-turn conversations, autoresponders, and the hard no-repeat
rejection path remain partial or open unless separately evidenced.

The existing command board, evidence ledger, and sanitized user-bug registry
are migration inputs. After the generated views reconcile with them, they are
marked historical rather than maintained as competing current-status sources.

## Freshness and invalidation

- Live behavioral evidence remains valid only for the immutable release lineage
  and owner modules it names. A relevant production change invalidates it.
- Control-plane readbacks expire when the relevant control, allowlist, queue,
  counter window, or deployment changes.
- Automated evidence is valid only at its recorded commit and test command.
- Historical user reports do not expire, but never count as proof of current
  behavior.
- A failing or regressed item overrides an older pass for the same release and
  scenario.

The renderer displays stale evidence as stale; it never silently upgrades it to
pass.

## Validation and failure behavior

Add `tests/test_readiness_registry.py` with checks that:

1. every referenced feature and scenario exists;
2. all sixteen core features appear in the generated coverage map;
3. rollout statuses use the fixed vocabulary and include evidence or blockers;
4. a quality item blocks only its declared gates;
5. live claims include immutable release references, readbacks, limitations,
   and freshness rules;
6. stale or regressed evidence cannot render as current pass;
7. generated Markdown is deterministic and `--check` is clean;
8. evidence cannot broaden from one scenario to an entire feature;
9. the committed source and generated views contain no obvious PII, secrets,
   absolute paths, or raw message content;
10. CC, PDF, voice, multi-turn, and repeat-ask items remain linked until exact
    closure evidence exists;
11. mapped deterministic fixtures are never rendered as live proof;
12. every `ready_for_canary` gate names its scope, forbidden actions, next
    action, and rollback.

Invalid input fails generation and CI with the offending stable ID. It never
changes runtime configuration, sends email, or mutates production.

## Non-goals

- No dashboard UI, database, or runtime worker change.
- No replacement for the feature registry, gradebook, fixture map, or audit
  matrix.
- No requirement that every quality gap close before a narrower safe rollout.
- No live-system queries from the renderer or tests.
- No PII or raw production payloads in version control.

## Implementation order

1. Add schema validation tests and an empty renderer contract.
2. Add the readiness registry, sanitized proof summary, and deterministic
   renderer.
3. Seed the three rollout gates and all sixteen core features using only
   bounded current claims.
4. Link the five named quality/edge-case families.
5. Generate both views and reconcile them against the current durable recovery
   record.
6. Mark the old command board as historical after parity review.

This work may proceed alongside the monitored user canary, but it cannot delay
that canary or broaden its permissions.
