# SiteSift Disabled Staging Evidence and Admin View Design

**Date:** 2026-07-24
**Status:** Design-only artifact complete; implementation not authorized
**Decision:** Define a future, isolated, default-off staging evidence boundary and
a separate read-only Admin evidence view while the effect adapter remains
disabled.
**Deliverable:** Finding
**Implementation state:** Not authorized

## 1. Decision, Scope, and Non-Authorization

This artifact specifies how a future system may persist and display sanitized
evidence produced from the disabled claim pipeline. It preserves the existing
pure claim, policy, action, and effect-plan evaluation boundary. It does not
connect that boundary to a runtime worker or grant any authority to perform an
effect.

The approved scope is limited to this design:

- Define a future immutable evidence envelope and append-only storage contract.
- Define a future, separate staging persistence adapter that is disabled by
  default.
- Define a future read-only Admin route, API, and client boundary for sanitized
  evidence.
- Define fail-closed behavior, security constraints, observability, verification,
  and separate future approval gates.

This design explicitly does not authorize:

- Implementation of any schema, serializer, writer, store, API, client, route,
  page, feature flag, project, credential, or deployment.
- Enabling the effect adapter or treating `would_apply` as an applied effect.
- Production access or use of production Firebase project
  `email-automation-cache`.
- Creation or use of a staging cloud project. No isolated staging project or
  store is asserted to exist.
- Microsoft Graph, Google Sheets, Gmail, OpenAI, Firebase, mailbox, provider, or
  other live calls.
- Queue, draft, send, retry, notification, follow-up, scheduler, worker,
  campaign-control, or deployment wiring.
- Jill data, live campaign data, customer data, real recipients, or production
  credentials.

The effect adapter remains disabled. Every future phase in this design also
keeps it disabled.

## 2. Current Approved Baseline

The design is anchored to the approved disabled-effect-adapter evidence:

| Baseline item | Pinned value or fact |
|---|---|
| Code revision | `5a09a67729fb3054298a92cebf40937056c48647` |
| Evidence commit | `df8425269c1ce3ab9bc4611705706d78c39dff02` (`df84252`) |
| Source hash | `b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634` |
| Fixture schema | `claim-pipeline-effect-adapter-fixtures-v1` |
| Fixture SHA-256 | `c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229` |
| Canonical report SHA-256 | `33103b700cebe55133d3d97a6dba8743a3961cd49040e88e8807c8d5bbc9c7b2` |
| Result digest | `450124af49e8c7827ee14ca99cdc13056865103a771a7028b20fb9b1ada63d7e` |
| Full backend tests | 2,251 passed |
| Focused claim tests | 436 passed |
| Isolation tests | 19 passed |
| Canonical cases | 54 exact-oracle case runs passed |
| Evaluator | `evaluate_effect_plan(request) -> DryRunCommitReceipt` |
| Dry-run statuses | `would_apply`, `blocked`, `skipped` |
| Adapter behavior | Evaluates only; never executes or persists an effect |

`email_automation/claim_pipeline/effect_adapter.py` exposes the pure
`evaluate_effect_plan` function. Its closed reasons are
`eligible_automatic_action`, `eligible_human_approved_action`,
`approval_required`, `approval_scope_mismatch`, `unsupported_action_type`,
`stale_snapshot`, `stale_contract`, `prior_state_mismatch`,
`idempotency_key_already_committed`, `dependency_blocked`,
`terminal_outbound_suppressed`, and `plan_contract_violation`.

The evidence commit proves that the adapter performed no provider, Graph,
Firebase, Sheets, mailbox, queue, notification, follow-up, draft, send,
deployment, Jill, live-data, or production-configuration operation. It unlocks
design only. Persistence and staging integration remain unapproved.

The current Admin `origin/main` state is also a design constraint:

- `App.js` protects `/operations` with `requireUsageAdmin`.
- `src/firebase.js` defaults to production `email-automation-cache` unless
  emulator mode is explicitly enabled.
- `/operations` imports read and mutation clients and contains campaign access
  toggles.

Therefore the evidence view must not be added to `/operations`, must not reuse
its client, and must not depend on the production-default Firebase module.

## 3. Safety Invariants

The following invariants are mandatory and are stronger than convenience or
availability:

1. `evaluate_effect_plan` remains pure, deterministic, and unaware of evidence
   serialization, persistence, HTTP, Admin, Firebase, or environment identity.
2. A future pure projector may accept an already validated `ActionPlan`, its
   bounded claims, and an already-produced `DryRunCommitReceipt`, but may emit
   only a sanitized `EvidenceProjection`. It cannot call the evaluator, worker,
   or any effect surface.
3. The future serializer accepts only the receipt, sanitized projection,
   bounded provenance, externally supplied timestamps, and an independently
   produced verifier attestation. The future persistence adapter accepts only
   the resulting validated evidence payload. Neither accepts a callback,
   client, credential, or effect command.
4. The persistence adapter is default-off. Missing configuration selects the
   disabled no-op implementation, not a cloud store.
5. `adapter_mode` is exactly `disabled` in every accepted envelope. Any other
   value is invalid and verification-failing.
6. No component falls back to production, another cloud project, a shared
   store, or local storage after a configured store failure.
7. Evidence is append-only and immutable. Reads never write, repair, migrate,
   acknowledge, or normalize records.
8. An unknown taxonomy, status, reason, field, or environment identity fails
   closed and remains visible as an ephemeral sanitized warning.
9. The Admin view has no effect or mutation controls and receives no write
   capability.
10. Removing or disabling every future evidence component leaves claim
    evaluation and the production worker unchanged.
11. A `DryRunCommitReceipt` proves classification only. No caller-supplied
    counter, boolean, or hash may by itself prove that zero effects occurred.

## 4. Boundary Architecture

The current and future boundaries are deliberately separated:

```text
CURRENT
Pure claim/policy/action evaluator
  -> evaluate_effect_plan(...)
  -> immutable DryRunCommitReceipt
  -> STOP

FUTURE, EACH ARROW SEPARATELY APPROVED
Already validated ActionPlan + bounded claims + DryRunCommitReceipt
  -> pure sanitized evidence projector
  -> immutable EvidenceProjection
DryRunCommitReceipt + EvidenceProjection + verifier attestation
  -> pure canonical evidence serializer
  -> immutable EvidencePayload
  -> default-off staging evidence writer
  -> writer-bound destination attestation and immutable EvidenceEnvelope
  -> explicitly authorized isolated staging store
  -> separate read-only evidence read model and GET API
  -> dedicated admin-only evidence route and read-only client
```

There is no arrow from the evidence system to a worker, effect executor,
campaign control, queue, draft, send, notification, or production service.

### 4.1 Ownership Boundaries

| Boundary | Owner | Permitted responsibility | Forbidden responsibility |
|---|---|---|---|
| Claim/policy/action evaluator | Backend claim pipeline | Pure validation and deterministic planning | I/O, timestamps, persistence, environment lookup |
| Disabled effect evaluator | Backend claim pipeline | Return `DryRunCommitReceipt` classifications | Apply, persist, queue, retry, or dispatch |
| Evidence projector | Future backend evidence package | Produce bounded opaque claim/action references from already validated inputs | Raw evidence output, I/O, evaluator invocation |
| Evidence serializer | Future backend evidence package | Validate projection and attestation, canonicalize, hash | I/O, credential loading, evaluator invocation |
| Trusted writer factory | Future staging infrastructure boundary | Select disabled writer or construct one exact allowlisted staging writer | Caller-supplied identity, ADC/default credentials, production fallback |
| Evidence writer port | Future backend evidence package | Bind destination attestation and append one validated payload atomically | Accept plans or effects; update existing evidence |
| Store/read model | Future isolated staging evidence service | Immutable storage and bounded projection | Runtime state, customer state, UI repair |
| Read API | Future Admin backend boundary | Server-authorized, sanitized GET responses | Writes, effect calls, generic proxying |
| Admin evidence view | Future Admin application | Render read-only summaries and warnings | Mutation controls, write-on-read, operations-client reuse |

The evaluator and serializer are separate because persistence must never become
an implementation detail of `evaluate_effect_plan`. The writer port and reader
port are also separate so the Admin path cannot acquire write authority through
a shared repository object.

### 4.2 Future Interface Sketches

These sketches define ownership and type constraints only. They are not build
instructions and do not authorize a store or effect integration.

```python
def project_disabled_evidence(
    plan: ActionPlan,
    claims: tuple[Claim, ...],
    receipt: DryRunCommitReceipt,
) -> EvidenceProjection:
    """Pure: emit bounded opaque claim/action references only."""


def serialize_disabled_evidence(
    receipt: DryRunCommitReceipt,
    projection: EvidenceProjection,
    provenance: EvidenceProvenance,
    timestamps: EvidenceTimestamps,
    verification: EvidenceVerificationAttestation,
) -> EvidencePayload:
    """Pure: validate, sanitize, canonicalize, and hash."""


class StagingEvidenceWriter(Protocol):
    def append(self, payload: EvidencePayload) -> EvidenceAppendResult:
        """Bind destination identity and append one immutable envelope."""


class DisabledStagingEvidenceWriter:
    def append(self, payload: EvidencePayload) -> EvidenceAppendResult:
        """Return a disabled no-op result and perform no I/O."""


def create_staging_evidence_writer() -> StagingEvidenceWriter:
    """Trusted bootstrap factory; no request or caller identity input."""


class StagingEvidenceReader(Protocol):
    def list_runs(self, query: RunListQuery) -> RunPage: ...
    def get_run(self, run_id: str) -> RunSummary | None: ...
    def list_rows(self, run_id: str, query: RowListQuery) -> RowPage: ...
```

`EvidenceAppendResult` reports disabled no-op, appended, same-hash duplicate,
conflict, invalid, or unavailable. Storage outcomes are not evidence
dispositions and cannot change action meaning.

The reader interface exposes no generic query, collection handle, transaction,
write method, delete method, or raw-record method.

The trusted factory, not a caller, owns environment validation and credential
selection. An enabled writer has its destination identity fixed at construction
and accepts no project, store, namespace, credential, configuration, or
identity argument. Its allowlist and trust anchor are pinned by the separately
approved build/deployment identity, not request data or a default environment
fallback.

## 5. Immutable Evidence Envelope

### 5.1 Canonical Shape

An accepted envelope has this conceptual shape:

```json
{
  "schema_version": "sitesift-disabled-evidence-v1",
  "run_id": "run_<derived-stable-bounded-id>",
  "taxonomy_version": "sitesift-evidence-disposition-v1",
  "code_revision": "5a09a67729fb3054298a92cebf40937056c48647",
  "evidence_commit": "df8425269c1ce3ab9bc4611705706d78c39dff02",
  "report_sha256": "33103b700cebe55133d3d97a6dba8743a3961cd49040e88e8807c8d5bbc9c7b2",
  "result_digest": "450124af49e8c7827ee14ca99cdc13056865103a771a7028b20fb9b1ada63d7e",
  "fixture_schema": "claim-pipeline-effect-adapter-fixtures-v1",
  "source_marker": "fixture",
  "fixture_ref": "fixture_<opaque-id>",
  "adapter_mode": "disabled",
  "environment_marker": "local_fixture",
  "timestamps": {
    "evaluation_started_at": "<RFC3339 UTC>",
    "evaluation_completed_at": "<RFC3339 UTC>",
    "captured_at": "<RFC3339 UTC>"
  },
  "content_hashes": {
    "source_sha256": "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634",
    "fixture_sha256": "c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229",
    "projection_sha256": "<sha256>",
    "receipt_payload_sha256": "<sha256>",
    "payload_sha256": "<sha256>",
    "envelope_sha256": "<sha256>"
  },
  "zero_effect_attestation": {
    "attestation_schema": "sitesift-zero-effect-attestation-v1",
    "verified_source_sha256": "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634",
    "verified_report_sha256": "33103b700cebe55133d3d97a6dba8743a3961cd49040e88e8807c8d5bbc9c7b2",
    "verified_result_digest": "450124af49e8c7827ee14ca99cdc13056865103a771a7028b20fb9b1ada63d7e",
    "test_manifest_sha256": "<sha256>",
    "isolation_tests_passed": 19,
    "verifier_id": "<approved-opaque-id>",
    "verifier_version": "<approved-version>",
    "verification_run_id": "<opaque-id>",
    "signature": "<integrity-protected-attestation>"
  },
  "destination_attestation": {
    "environment": "local_fixture",
    "project_or_store": "<writer-generated-opaque-id>",
    "namespace": "<writer-generated-opaque-id>",
    "deployment_identity_sha256": "<writer-generated-sha256>"
  },
  "summary": {
    "claim_count": 0,
    "action_count": 0,
    "warning_count": 0
  },
  "rows": []
}
```

The example preserves the pinned baseline values. Future evidence must pin its
own approved code and evidence revisions rather than copying these values
without re-verification.

### 5.2 Field Rules

| Field | Rule |
|---|---|
| `schema_version` | Required exact supported value; missing or unknown values fail closed |
| `run_id` | Required and derived from schema version, receipt identity, projection hash, fixture hash, code revision, and result digest; caller selection is forbidden |
| `taxonomy_version` | Required exact supported version; no best-effort decoding |
| `code_revision` | Full lowercase commit hash; required provenance |
| `evidence_commit` | Full lowercase commit hash for the approved evidence record |
| `report_sha256` | Exact lowercase SHA-256 of the canonical report |
| `result_digest` | Exact lowercase SHA-256 result digest |
| `fixture_schema` | Required exact approved fixture schema |
| `source_marker` | Exactly `fixture` or `local_fixture`; no live value exists |
| `fixture_ref` | Opaque bounded identifier, never a filename containing customer data |
| `adapter_mode` | Exactly `disabled` |
| `environment_marker` | Writer-generated `local_fixture` or separately approved `isolated_staging`; never production |
| `timestamps` | Required RFC3339 UTC values with start less than or equal to completion less than or equal to capture |
| `content_hashes` | Required lowercase SHA-256 values over canonical byte definitions |
| `zero_effect_attestation` | Required independently produced integrity-protected attestation; caller assertions are forbidden |
| `destination_attestation` | Required writer-generated destination and deployment identity bound into the stored envelope hash |
| `summary` | Derived from rows; supplied values must exactly match recomputation |
| `rows` | Bounded sanitized claim/action evidence only |

`run_id` uses this exact identity recipe:

```text
"run_" + hex_sha256(
  utf8("sitesift-disabled-evidence-run-v1\0")
  + canonical_json({
      "schema_version": schema_version,
      "receipt_id": dry_run_commit_receipt_id,
      "projection_sha256": projection_sha256,
      "fixture_sha256": fixture_sha256,
      "code_revision": code_revision,
      "result_digest": result_digest
    })
)
```

The domain separator includes one NUL byte. Timestamps and destination identity
are deliberately excluded from logical run identity but remain hashed into the
payload/envelope. A retry must reuse the original timestamps and bytes; a
changed capture for the same logical run is a conflict, not a second run.

Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace, and
strict finite JSON values. `receipt_payload_sha256` hashes the sanitized ordered
receipt payload. The pure serializer computes `payload_sha256` before any
destination fields exist. The trusted writer binds its destination attestation
and then computes `envelope_sha256` over the complete canonical envelope except
the `envelope_sha256` field itself. Timestamps are included in both applicable
hashes.

An evidence run is acceptable only when all provenance fields and hashes are
present and verify. A hash is not a substitute for source authorization. The
serializer validates the attestation structure and signature; the writer and
reader validate it against the exact trust anchor approved for that gate. The
API derives `zero_effect_verification` from that validation and never trusts a
stored boolean. Without a valid attestation, the run is unverified and excluded
from normal reads.

The current baseline contains independently reviewable source, report, result,
and test evidence, but no staging signing trust anchor is approved. Gate 1 may
use a fixture trust anchor only for tests. A real isolated staging trust anchor
requires the later identity/security approval gate.

### 5.3 Row Model

Rows are immutable and ordered by `(row_kind, sequence, row_id)`. The pure
projector must prove that every action row matches one receipt action ID and
that every claim reference is present in the already validated plan. A row may
contain only:

- Opaque `row_id`, `claim_ref`, `action_ref`, and dependency references.
- `row_kind` equal to `claim` or `action`.
- Bounded action type, exact `policy_status`, exact closed `policy_reason`,
  `execution_status`, and evidence disposition.
- Sequence, aggregate counts, and content hashes.
- A sanitized source category such as `fixture`.

Rows must not contain customer content, raw evidence, raw claims, recipients,
email addresses, tenant/customer identifiers, message subjects, message bodies,
attachments, Sheet values, Graph IDs, campaign names, property addresses,
exception messages, exception stacks, tokens, or credentials.

The accepted envelope is bounded to 400 total rows, 256 KiB of canonical JSON,
128 characters for opaque identifiers, 64 characters for enum-like values, and
no free-form display text. Over-limit input is invalid; it is not truncated.

The projector hashes claim and action references with a versioned domain
separator. It never emits claim values or raw evidence. The serializer rejects
missing, extra, duplicate, or foreign projected rows.

## 6. Closed Evidence Disposition Taxonomy

The only allowed values for `disposition` are exactly:

| Disposition | Meaning |
|---|---|
| `proposed` | A sanitized, provenance-valid claim reference exists; no action or effect is implied |
| `blocked_by_policy` | A known evaluator rule blocked or skipped the action |
| `blocked_by_disabled_adapter` | The evaluator returned known `would_apply` eligibility, but the disabled adapter prevents an effect |
| `invalid_input` | A verifier/read-model warning for a known invalid schema, provenance, hash, bound, identity, or status/reason pair |
| `unknown_taxonomy` | A verifier/read-model warning for an unrecognized taxonomy version, status, reason, or disposition |

The set has exactly five members. No catch-all success state, applied state, or
UI-only alias may be added without a new taxonomy version and a separate
approval.

The deterministic mapping is:

| Source evidence | Evidence disposition |
|---|---|
| Provenance-valid sanitized claim candidate | `proposed` |
| `would_apply` plus a known eligible reason | `blocked_by_disabled_adapter` |
| `blocked` or `skipped` plus a known closed reason | `blocked_by_policy` |
| Known validation failure before a valid receipt exists | `invalid_input` |
| Unknown version, status, reason, or disposition | `unknown_taxonomy` |

Valid action rows also preserve `policy_status` exactly as `would_apply`,
`blocked`, or `skipped` and preserve the exact closed `policy_reason`. A
claim row records `execution_status=not_applicable_claim`; a `blocked` or
`skipped` action records `execution_status=not_attempted_policy_gate`; and a
`would_apply` action records
`execution_status=not_attempted_adapter_disabled`. These are the only three
execution-status values. The Admin label for `would_apply` is "Policy eligible;
execution not attempted because the adapter is disabled." This separates policy
eligibility from execution state. `would_apply` is never displayed as applied.

`invalid_input` and `unknown_taxonomy` are never accepted as persisted action
rows. They are reserved for ephemeral verifier results and read-model warning
rows. A known status and known reason in an invalid combination is
`invalid_input`, fails verification, and is excluded from normal reads.

Unknown values are never silently mapped to `blocked_by_policy`,
`invalid_input`, or any familiar display label. They cause verification failure,
normal-read exclusion, and an explicit Admin warning generated from bounded
header metadata without a write. A UI version that does not understand the
declared taxonomy must refuse to render the run as valid.

## 7. Storage Model and Duplicate Handling

### 7.1 Logical Records

The future store contains two append-only record families:

| Record | Key | Contents |
|---|---|---|
| Evidence run | `run_id` | Immutable envelope header, summary, hashes, verification and destination attestations |
| Evidence row | `(run_id, row_id)` | Immutable bounded claim/action projection |

An accepted run and all of its rows become visible in one atomic
create-if-absent transaction. The bounded row limit exists in part so the
complete write can fit one transaction. A store that cannot atomically commit
the complete bounded run is not eligible for approval.

No update, merge, upsert, delete, repair, backfill-on-read, mutable status
field, or application-managed quarantine record is permitted. Any future schema
migration writes a new versioned record; it does not modify the source
evidence. Pre-writer validation failures are visible only in the verifier result
and passive platform telemetry. A reader that encounters a malformed or unknown
stored record emits a sanitized warning in its response without writing.

### 7.2 Idempotency

The serializer derives `run_id`; the trusted writer recomputes it before any
store access. Duplicate handling uses derived `run_id` and `envelope_sha256`
together:

1. No existing `run_id`: atomically create the run and all rows.
2. Existing `run_id` with the same `envelope_sha256`: return a same-hash
   idempotent result without writing or changing timestamps.
3. Existing `run_id` with a different `envelope_sha256`: preserve the original,
   reject the candidate, return a conflict result, emit only bounded passive
   telemetry, and fail verification. Never replace the original or create a
   second logical run.

Storage retry logic may repeat only the identical create request and identical
hash. It cannot regenerate timestamps, run IDs, or content between attempts.

## 8. Staging and Production Separation

Production Firebase project `email-automation-cache`, its buckets, databases,
credentials, service accounts, and aliases are forbidden. There is no fallback
to that project under any name or configuration condition.

No persistence is currently authorized. If Gate 2 is separately approved, its
only eligible store is an explicit temporary fixture-only local path:

- `source_marker` is `fixture` or `local_fixture`.
- Any local persistence is explicit test input under a temporary isolated path.
- No Jill record, live campaign, production export, production snapshot, or
  copied customer record is allowed.
- No production credential may be loaded, mounted, referenced, or accepted.

Future cloud staging requires the trusted writer factory to validate an exact
approved target before credential loading or client construction. The disabled
writer is selected before any environment lookup or credential loading.
Enabled-writer checks must require:

- An explicitly approved environment class equal to isolated staging.
- An exact approved project/store identifier supplied by that later approval.
- No ADC, default credential, production-capable principal, or caller-supplied
  credential.
- A dedicated workload identity principal with permission only to the approved
  staging resource and no production IAM.
- Explicit inequality with `email-automation-cache` and all known production
  aliases.
- A store namespace reserved only for disabled evidence.
- `adapter_mode` exactly `disabled` and an allowed fixture source marker.

After token acquisition, the writer verifies token issuer, audience, principal,
and resource before the first store operation. Every accepted run atomically
records a writer-generated destination project/store/namespace reference and
deployment-identity digest. Missing, ambiguous, emulator-like, defaulted,
production, or mismatched identity selects failure or the disabled no-op
writer. It never selects a writable cloud adapter. Failure of an authorized
staging store does not fall back to local storage, production, or another
project.

This design does not name a staging project because none is approved or assumed
to exist.

## 9. Read-Only Admin Evidence View

### 9.1 Dedicated Surface

The future view uses a dedicated admin-only route, conceptually:

```text
/admin/evidence/disabled-runs
```

The route is separate from `/operations`. It uses a dedicated read-only client
module and dedicated read-only server endpoints. It must not import the current
mixed-mutation `/operations` client, campaign access toggles, or the
production-default `src/firebase.js`.

The route is registered only in a separately approved isolated-staging Admin
build whose exact project and API identity pass the same no-fallback checks. It
is absent or hard-disabled in production and in any build with missing,
defaulted, or mismatched identity.

Server-side admin authorization is authoritative. A route guard equivalent to
the current `requireUsageAdmin` protection provides defense in depth in the
browser, but a passed client guard never substitutes for server authorization.

### 9.2 Required View Content

The view presents:

- Run summary and bounded aggregate counts.
- Sanitized claim and action rows.
- Evaluator status and closed policy reason.
- Evidence disposition, including `blocked_by_disabled_adapter`.
- The zero-effect and disabled-adapter proof.
- Evaluation and capture timestamps.
- Full code revision and evidence commit.
- Taxonomy version, report SHA-256, result digest, and content hash status.
- Prominent unknown-taxonomy, invalid-record, conflict, and unavailable-store
  warnings.

Unknown or invalid records are shown only as ephemeral sanitized warning
summaries.
Their raw candidate content is never returned to the browser.

### 9.3 Forbidden Controls and Behaviors

The view contains no approve, retry, send, draft, stop, queue, notify, mutate,
deploy, campaign access, repair, edit, delete, or acknowledge control. It also
contains no generic action menu or link into an effect workflow.

There is no write-on-read:

- Opening, refreshing, filtering, sorting, paginating, or expanding a run does
  not write a view count, acknowledgement, normalization, cache document, or
  repair.
- The browser client exposes only typed list/get methods.
- The server reader uses read-only credentials or a read-only service role.
- Invalid records remain unavailable. The UI cannot repair them.

### 9.4 API Sketch

The read contract is GET-only:

```text
GET /api/admin/disabled-evidence/runs?limit=<n>&cursor=<opaque>
GET /api/admin/disabled-evidence/runs/{run_id}
GET /api/admin/disabled-evidence/runs/{run_id}/rows?limit=<n>&cursor=<opaque>
```

Any POST, PUT, PATCH, DELETE, effect verb, generic collection query, or
unbounded export is absent or returns method not allowed.

Conceptual list response:

```json
{
  "items": [
    {
      "run_id": "run_<opaque>",
      "adapter_mode": "disabled",
      "taxonomy_version": "sitesift-evidence-disposition-v1",
      "captured_at": "<RFC3339 UTC>",
      "code_revision": "<full-hash>",
      "evidence_commit": "<full-hash>",
      "summary": {
        "claim_count": 0,
        "action_count": 0,
        "warning_count": 0
      },
      "zero_effect_verification": "verified",
      "hashes_verified": true
    }
  ],
  "warnings": [
    {
      "record_ref": "run_<opaque>",
      "disposition": "unknown_taxonomy",
      "failure_code": "unsupported_taxonomy"
    }
  ],
  "next_cursor": null
}
```

Conceptual detail responses add only the whitelisted provenance, hash,
zero-effect, and row fields defined in this document. They never return the
stored raw record. `warnings` are ephemeral response projections from bounded
header metadata and are never persisted by the reader.

Run lists default to 25 and allow at most 50 items. Row lists default to 50 and
allow at most 100 items. Cursors are opaque, signed or integrity-protected, and
bound to the filter and sort order. Invalid limits or cursors fail rather than
expanding access. Stable ordering is by capture timestamp and `run_id`.

### 9.5 Security and Privacy

The server must verify an authenticated admin principal on every request and
return unauthenticated or forbidden responses before store access. Authorization
is not inferred from route visibility, email domain, client claims alone, or a
query parameter.

Responses are generated from a strict field allowlist with schema validation
and output length checks. The following are forbidden in storage projections,
logs, API responses, browser state, and UI text:

- Customer content or customer identifiers.
- Real recipients, sender addresses, or email addresses.
- Raw claim evidence, raw fixture evidence, message subject, or message body.
- Property address, attachment content, Sheet cell content, or Graph message
  data.
- Exception text, stack traces, tokens, credentials, or environment secrets.

The API applies bounded pagination, request-size limits, response-size limits,
timeouts, and rate limits. Error responses use closed error codes and request
IDs, not raw exceptions.

## 10. Observability and Audit

Observability records safety outcomes without evidence payloads:

- Append attempts: disabled no-op, accepted, same-hash duplicate, conflict,
  invalid input, identity rejection, unavailable store, and atomic failure.
- Read attempts: authorized, unauthenticated, forbidden, not found, invalid
  record, unknown taxonomy, and store unavailable.
- Verification: schema result, hash result, zero-effect result, taxonomy result,
  and environment-identity result.

Metrics use bounded labels only. Run IDs, user IDs, hashes, and project IDs are
not metric labels. Structured logs may contain a request ID, opaque actor
reference, opaque run reference, closed outcome code, environment class,
timestamp, and component version. They contain no evidence rows or customer
data.

Admin reads perform zero application writes and require read-only credentials.
They may rely on passive infrastructure access logs and metrics that are
outside the evidence application's data plane and correctness path. Reader code
does not emit a durable audit event, quarantine record, view count,
acknowledgement, or cache write.

The disabled writer performs no I/O, including application audit I/O. An
enabled writer may emit bounded platform telemetry after returning a storage
outcome, but telemetry failure cannot rewrite the outcome or claim that an
already committed envelope is absent. Same-hash duplicates perform no
application data write.

## 11. No-Op and Rollback Behavior

The default writer is a deterministic no-op and performs no I/O. A no-op result
must not be reported as persisted evidence.

Disabling or removing the future writer:

- Stops new staging evidence recording.
- Does not alter `evaluate_effect_plan`, claim evaluation, policy evaluation,
  action planning, workers, queues, campaigns, or effects.
- Does not delete or mutate previously stored evidence.

Disabling or removing the future API or Admin view:

- Removes visibility only.
- Does not change evaluator behavior, writer behavior, stored evidence, or
  runtime processing.
- Requires no evidence migration or runtime rollback.

Stored evidence remains append-only and immutable. Invalid records are made
unavailable and represented only by ephemeral sanitized read warnings. They are
never repaired, rewritten, accepted, deleted, or quarantined by the UI.

## 12. Exact Failure Modes

| Failure mode | Required fail-closed behavior | Visible evidence |
|---|---|---|
| Missing schema version, code revision, evidence commit, report hash, result digest, fixture schema, source marker, or timestamps | Reject before writer selection | Ephemeral `invalid_input` result and failed verification |
| Malformed field, timestamp, enum, bound, JSON value, or hash | Reject; do not truncate or coerce | `invalid_input` warning |
| `adapter_mode` is absent or not exactly `disabled` | Reject and raise a safety alert | `invalid_input` warning; zero accepted writes |
| Unsupported schema/taxonomy version, status, reason, or disposition | Reject normal storage/read; never map | Ephemeral `unknown_taxonomy` warning and failed verification |
| Known status and known reason in an invalid pair | Reject normal storage/read | Ephemeral `invalid_input` warning and failed verification |
| Duplicate `run_id`, same envelope hash | Return idempotent same-hash result; no write | Existing immutable run only |
| Duplicate `run_id`, different envelope hash | Preserve original and reject candidate; no application write | Conflict result and failed verification |
| Hash recomputation mismatch | Reject; no application write | Integrity warning |
| Production or ambiguous project/store identity | Reject before credential or store use | Environment-identity safety alert |
| Production credential, audience, alias, or resource detected | Reject before store access | Credential-identity safety alert; no credential detail |
| Jill, live campaign, customer, recipient, or non-fixture source detected | Reject before serialization or storage | Sanitized source-policy warning |
| Persistence configuration absent | Use disabled no-op writer | Explicit not-persisted result |
| Authorized staging store unavailable | Fail the append; no fallback | Store-unavailable outcome |
| Transaction fails before commit | Abort complete write | Atomic-failure outcome; no visible run |
| Failure after uncertain commit response | Read by `run_id` and exact hash only; never regenerate | Accepted, same-hash duplicate, or unavailable |
| Partial run or missing row observed | Exclude run from normal reads; perform no repair or write | Integrity warning; failed health check |
| Row or envelope exceeds a bound | Reject whole run; do not truncate | `invalid_input` warning |
| Unauthorized Admin request | Return unauthenticated or forbidden before store read | Passive bounded access-denial telemetry |
| Unknown or malformed record encountered during read | Do not repair, write, or return raw content | Ephemeral invalid/unknown warning |
| Read store unavailable | Return unavailable; do not use another store | Read-unavailable warning |
| UI/client attempts a mutation method | No client method; server returns method not allowed | Security event |
| Route guard and server authorization disagree | Server denial wins | Forbidden response and passive bounded telemetry |

No failure mode promotes evidence, enables an effect, changes a campaign, or
causes a retry outside the evidence append itself.

## 13. Test and Verification Matrix

| Area | Exact verification | Required result |
|---|---|---|
| Baseline pinning | Assert pinned revisions, source/report/result/fixture hashes, fixture schema, and evidence counts | Exact match |
| Evaluator isolation | Re-run static/import isolation and verify no I/O or persistence call | 19 isolation tests remain passing |
| Existing regressions | Run focused claim and full backend suites when implementation is later proposed | 436 focused and 2,251 full baseline tests do not regress |
| Pure projection | Same validated plan, claims, and receipt produce identical bounded opaque rows | Exact byte and hash equality; no raw claim data |
| Pure serializer | Same receipt, projection, provenance, timestamps, and attestation serialize identically | Exact byte and hash equality |
| Required provenance | Remove each required provenance field one at a time | Every case rejected |
| Adapter mode | Test missing, enabled, dry-run alias, mixed case, and `disabled` | Only exact `disabled` accepted |
| Taxonomy closure | Enumerate accepted dispositions | Exactly the five declared values |
| Unknown taxonomy | Inject unknown version, status, reason, and disposition | Visible warning, failed verification, no normal record |
| Status mapping | Cover every closed status/reason pair | Exact deterministic disposition |
| Invalid status mapping | Cross every known status with every known reason | Every invalid pair returns `invalid_input` and no normal record |
| Privacy | Inject recipient, body, raw evidence, address, customer ID, and stack fields | Whole record rejected; no leaked value |
| Bounds | Test 400 and 401 rows, exact and overlong strings, 256 KiB boundary | Boundary accepted; excess rejected without truncation |
| Hash integrity | Change every hashed section independently | Mismatch rejected |
| Timestamp rules | Test UTC, ordering, missing timezone, invalid and reversed times | Only valid ordered RFC3339 UTC accepted |
| Zero-effect attestation | Forge counters, boolean, source hash, test digest, signature, trust identity, and run ID | Every forged or untrusted attestation rejected |
| Derived run identity | Change caller run ID or replay logical evidence with changed timestamps/content | Writer recomputes ID; original preserved; conflict fails |
| Disabled writer | Invoke with valid envelope and no approved store identity | No I/O and explicit not-persisted result |
| Trusted writer factory | Try caller identity, ADC, default credentials, production-capable principal, and target override | No enabled writer constructed |
| Production rejection | Supply `email-automation-cache`, aliases, bucket, and production credential audience | Rejected before client creation or network access |
| No fallback | Fault configured staging store and observe clients/stores used | Only the approved target attempted |
| Atomicity | Fault every evidence write position | Either complete immutable run or no visible run |
| Same-hash duplicate | Append identical run twice | One stored run; second call writes nothing |
| Different-hash duplicate | Reuse `run_id` with changed content | Original preserved; conflict returned; verification fails; no application write |
| Store unavailable | Force connection, timeout, and authorization failures | Closed unavailable result; no fallback |
| Read authorization | Test unauthenticated, non-admin, stale admin, and admin principals | Only current server-authorized admin may read |
| Route guard | Bypass or remove browser guard in a test | Server still denies unauthorized access |
| GET-only API | Attempt POST, PUT, PATCH, DELETE, generic query, and export | No mutation or unbounded endpoint exists |
| No write-on-read | Snapshot all writable stores before and after list/detail/pagination | Zero writes |
| Pagination | Test defaults, maxima, invalid cursor, cursor tampering, and stable ordering | Exact bounded behavior |
| Read sanitization | Seed every forbidden field in a test record | Record unavailable or sanitized warning; field never returned |
| UI content | Render normal, blocked, disabled, invalid, unknown, empty, and unavailable states | Required fields, policy/execution separation, and warnings visible |
| UI controls | Query rendered UI and imported client surface | No forbidden control or mutation import |
| Operations isolation | Dependency scan from evidence route/client | No `/operations` client, campaign toggle, or `src/firebase.js` import |
| Rollback/no-op | Disable/remove writer, API, and view in isolation tests | Evaluator and worker behavior unchanged |
| ASCII/design scope | Scan this artifact and repository diff | ASCII only; exactly one changed file |

Verification is three-state for each check: passed, failed, or unavailable. An
unavailable required check blocks the corresponding gate; it is never counted
as passed.

## 14. Phased Future Plan and Approval Gates

Every gate requires a separate explicit approval before work starts and an
explicit stop after its evidence is reviewed. Approval does not cascade. No gate
authorizes effects.

| Gate | Future scope | Evidence required before stopping | Explicitly not authorized |
|---|---|---|---|
| 1. Schema, projector, serializer, and attestation tests | Immutable types, closed taxonomy, pure bounded projector, canonical serializer, fixture trust anchor, hash/privacy/bound tests | Exact projector/serializer and adversarial attestation/schema report | Any persistence, network, Admin, or effect work |
| 2. Local fixture-only persistence | Explicit temporary local store, atomic append, duplicate/invalid-input tests, default no-op writer | Fault-injection and zero-network evidence | Cloud project, production data, live data, or effects |
| 3. Isolated staging identity and security approval | Review an exact new project/store, namespace, workload identity, trust anchor, retention, and access policy | Written target, IAM, trust, and cleanup approval; no resource created | Provisioning, production Firebase, deployment, Admin, or effects |
| 4. Isolated resource provisioning | Create only the approved empty staging resources and least-privilege identities | Identity attestation, no-production-IAM proof, empty-store proof, and cleanup drill | Evidence ingestion, Admin access, production, or effects |
| 5. Fixture-only staging writer and ingestion | Deploy the trusted writer boundary and ingest only approved sanitized fixtures | Atomicity, idempotency, destination attestation, privacy, rollback, and zero-effect evidence | Live data, read API/UI, production, or effects |
| 6. Read API | Read-only server adapter and GET contracts against the approved isolated staging store | Auth, privacy, pagination, no-write-on-read, and no-fallback evidence | Admin UI, mutations, production deployment, or effects |
| 7. Admin UI | Dedicated guarded staging route and separate read-only client | UI state matrix, dependency isolation, accessibility, and no-control proof | `/operations` reuse, campaign controls, deployment, or effects |
| 8. Staging deployment | Deploy only the approved read-only API/UI to the approved isolated staging environment | Environment identity, rollback, smoke, authorization, and zero-effect evidence | Production deployment, worker wiring, live campaigns, or effects |

At the end of each gate, work stops. The reviewer either approves the next gate,
requests design changes, or refutes the approach. Even completion of Gate 8
leaves the effect adapter disabled and does not authorize any effect path.

## 15. Acceptance and Refutation Criteria

### 15.1 This Design Artifact Is Accepted Only If

- It is the only changed file and contains design only.
- It pins all baseline revisions, hashes, digests, test counts, and evaluator
  facts exactly.
- It preserves evaluator purity and defines persistence as a separate,
  default-off future boundary.
- It defines the immutable envelope, independently verifiable zero-effect
  attestation, exact five-value evidence disposition taxonomy, derived run
  identity, idempotent duplicate rules, and fail-closed storage behavior.
- It forbids production Firebase, fallback, production credentials, Jill/live
  data, and every effect surface.
- It defines a separate read-only Admin route, endpoint, and client with both
  server authorization and route-guard defense in depth.
- It includes exact failure modes, verification coverage, no-op rollback, and
  eight separately approved future gates.
- It ends without authorizing implementation or claiming a staging project
  exists.

### 15.2 The Design Is Refuted If

Any of the following is sufficient to reject this design or a future gate:

- The evaluator imports, calls, or knows about persistence.
- An envelope can record an adapter mode other than exact `disabled`.
- An unknown state is hidden, coerced, or mapped to a known state.
- A duplicate run can overwrite evidence or a partial run can become readable.
- Store failure can fall back to production, another project, or local storage.
- `email-automation-cache`, production credentials, Jill data, live campaign
  data, customer content, recipients, raw evidence, bodies, or stacks can enter
  the evidence path.
- Admin authorization exists only in the browser.
- A read can write, repair, acknowledge, retry, or mutate evidence.
- The evidence page imports the `/operations` mixed-mutation client,
  `src/firebase.js`, or campaign access controls.
- The UI exposes an effect or mutation control.
- Disabling the evidence writer, API, or view changes evaluator or worker
  behavior.
- A future phase starts without its own approval or is treated as effect
  authorization.

## 16. Explicitly Excluded Files and Surfaces

The sole authorized repository content change for this task is adding:

```text
docs/sitesift-disabled-staging-evidence-admin-view-design.md
```

The design grants no authority to change:

- `email_automation/claim_pipeline/effect_adapter.py` or any claim pipeline
  evaluator, contract, fixture, runner, or test.
- Any backend worker, inbox processor, follow-up, queue, pending response,
  notification, outbox, draft, send, scheduler, recovery, or deployment file.
- Any Firebase, Graph, Sheets, Gmail, OpenAI, provider, credential, or
  production configuration.
- Admin `App.js`, `src/firebase.js`, `/operations`, its read/mutation clients,
  campaign access toggles, or any existing Admin route.
- Production project `email-automation-cache` and every Jill or live-campaign
  surface.
- Any merge, tag, release, deployment, or repository content change other than
  the single documentation artifact above.

## 17. Next Approval Decision and Stop

**Next approval decision:** Approve or reject Gate 1 only: immutable schema,
pure sanitized projector, canonical serializer, and verifier-attestation tests.
That decision would still require a separate implementation task and would
authorize no persistence or effects.

**STOP:** No schema, serializer, persistence, staging integration, API, Admin
view, project, credential, deployment, or effect work may begin from this
artifact without the next explicit approval.
