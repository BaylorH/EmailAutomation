# SiteSift Production Automation Certification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private production-resident certification system that substitutes approved browser/mailbox inputs, executes the same deployed SiteSift business logic, captures forbidden external effects, verifies real isolated state, replays, cleans up, and stamps one business capability at a time.

**Architecture:** Separate transport acquisition from canonical business inputs and isolate final delivery behind request-scoped runtime dependencies. A strict scenario registry and private revision-bound route run in a fixture-only Cloud Run twin that uses the exact immutable image digest staged for production. A CLI reconciles required and forbidden effects, replay, cleanup, candidate/twin identity, and strict config parity into a sanitized stamp. The infinite backlog remains intact, but a one-capability ranked frontier controls execution.

**Tech Stack:** Python 3, Flask/Cloud Run, Firestore, Google Sheets/Drive APIs, Microsoft Graph-compatible message envelopes, OpenAI Responses API, unittest, gcloud.

---

## Fixed program rules

- Start from clean branch `feat/native-image-attachment-ingestion-20260816`; production evidence anchor is `1a20ba44a46e0aeed7620a6408856c0aacf6c7d9`.
- Never use the dirty main checkout or merge unrelated branches.
- One active capability, one behavior per commit, RED before GREEN.
- The backlog is retained and re-ranked; it is never treated as an ordered work list.
- Do not work on Results, Maps, Tour Scheduling, broad UI redesign, general cleanup, worker migration, or native-image enablement unless the active stamp proves it is the direct blocker.
- Do not build duplicate matching, extraction, rendering, Sheet, or lifecycle logic inside certification code.
- Do not use a global mutable test mode.
- Do not call a local run, mock, health check, or source review a production stamp.
- No real mailbox delivery, public Drive permission, or external-human communication belongs to an agent-run certification.
- An agent must never submit fixture prompts/files to OpenAI. Model-dependent scenarios stop as `INSTRUMENT_BLOCKED:user_runtime_launch_required` after preparing one exact command; Baylor launches that product-runtime action, and the agent may then inspect sanitized evidence read-only.
- Public-link publication is captured and remains `NOT_TESTED`; extraction/private fixture-Drive behavior may be stamped separately.
- The agent never pushes to a public Git remote, changes production traffic, deploys Hosting/shared customer Functions, or performs any mutation with later customer/external-effect potential. It commits/reviews locally, prints one exact Baylor command, and resumes after read-only parity/provider proof. Agent-safe mutations are limited to IAM-private fixture/twin infrastructure that cannot reach mailboxes, real AI, public Drive permissions, production schedulers/queues, or customer/public surfaces.
- Every build turn ends with a committed checkpoint or a durable resume handoff.

## File structure

**Create:**

- `email_automation/automation_runtime.py` — immutable request-scoped sources, clocks, counters, and delivery dependency bundle.
- `email_automation/message_transport.py` — canonical inbound envelope, Graph/fixture source boundary, outbound delivery protocol and receipt.
- `email_automation/certification/__init__.py` — package marker only.
- `email_automation/certification/models.py` — strict scenario/run/effect/readback/cleanup/verdict types.
- `email_automation/certification/canonical_json.py` — one bounded deterministic fixture-config parser/canonicalizer shared by the deployed route and CLI.
- `email_automation/certification/input_handoff.py` — sealed one-use canonical-input envelope stored in the certification database, never accepted in the backend request body.
- `email_automation/certification/scenario_registry.json` — canonical in-image closed registry loaded by route, runner, tests, and ranker.
- `email_automation/certification/image_manifest.py` — shared canonical deployable-source manifest generator included in the image.
- `email_automation/certification/capture.py` — final-message capture transport.
- `email_automation/certification/effect_transports.py` — model-inference deny/user-launch gate and Drive-publication capture boundary.
- `email_automation/certification/scenarios.py` — closed approved scenario registry.
- `email_automation/certification/fixtures.py` — isolated fixture preflight, seed, readback, and cleanup.
- `email_automation/certification/runner.py` — seed → execute → readback → replay → cleanup state machine.
- `email_automation/certification/evidence.py` — sanitized revision-bound evidence and stamp writer.
- `scripts/certify_production.py` — authenticated deployed-run CLI and direct readback verifier.
- `scripts/verify_image_source_manifest.py` — compare the reviewed deployable-source manifest with bytes baked into the exact tested image.
- `scripts/deploy_certification_twin.sh` — deploy the reviewed immutable candidate image to the private fixture-only certification service.
- `deploy/cloudrun-certification-service.yaml` — exact certification-twin service identity and fixture-only configuration contract.
- `scripts/rank_certification_frontier.py` — deterministic next-capability selection and stamp invalidation after a deployed diff.
- `docs/release-safety/production-certification/frontier.json` — tracked static capability dependencies/ranking policy only; never dynamic production state.
- `docs/release-safety/production-certification/identity.schema.json` — tracked schema for private/local identity exports.
- `docs/release-safety/production-certification/stamps/README.md` — sanitized stamp schema and private-retention contract; no dynamic stamp is committed to the product branch.
- `tests/test_automation_runtime.py` — request isolation and default-production behavior.
- `tests/test_message_transport.py` — Graph/fixture input parity and outbound receipt contract.
- `tests/test_production_certification.py` — route, runner, hostile scope, replay, cleanup, and first slice.
- `tests/test_certification_canonical_json.py` — fixed canonical-byte/digest vectors, bounds, and duplicate-key rejection under the supported runtimes.
- `tests/test_certification_frontier.py` — deterministic priority, dependency, invalidation, and resume behavior.
- `tests/test_external_effect_inventory.py` — complete Graph/OpenAI/public-Drive and raw-log bypass inventory.
- `tests/test_certification_image_manifest.py` — exact image/checkout source parity and mismatch controls.

**Approved planning input (already checked in with this plan):**

- `docs/superpowers/plans/2026-08-17-production-automation-certification-scenarios.json` — finite capability scenario IDs, production seams, exact existing test-module inventory, source anchors, and frontend artifact readback contract. Builders may add a scenario only through a reviewed plan/frontier successor; they may not replace it with repository-wide test discovery.

**Modify:**

- `service.py` — add private prepare/run/status/review/abort/recover/cleanup routes with closed schemas and bidirectional service fences.
- `email_automation/processing.py` — use one inbound hydration source and pass request runtime through the selected message path.
- `email_automation/file_handling.py` — forward an approved preloaded attachment snapshot without duplicating processing.
- `email_automation/utils.py` and `email_automation/service_providers.py` — route only the final public Drive permission mutation through `DrivePublicationTransport`; preserve ordinary defaults.
- `email_automation/ai_processing.py`, `email_automation/column_config.py`, and `scheduler_runner.py` — route every Responses/Chat/File provider effect and isolated usage accounting through the request runtime; preserve ordinary defaults.
- `email_automation/email.py` — route final delivery and scoped counters through request runtime while preserving ordinary defaults.
- `email_automation/followup.py` — use the same outbound delivery boundary.
- `email_automation/pending_responses.py` — use the same outbound delivery boundary.
- `main.py` — ordinary runtime construction only; certification must not call `refresh_and_process_user()`.
- `deploy/cloudrun-service.yaml` and `scripts/deploy_process_user.sh` — keep ordinary process-user staging private, untagged, and at 0% until proof; do not introduce a mutable test-mode environment switch.
- `scripts/phase1_rollout.py` — require a successful exact-image certification-twin stamp and strict candidate/twin configuration comparison in the under-lock pre-promotion authorization slice.
- `tests/test_process_user_tagless_staging_contract.py` and `tests/test_process_user_phase1_rollout_contract.py` — prove ordinary tagless staging remains unchanged and promotion cannot proceed without the exact-image twin evidence or when any unallowlisted config differs.

## Locked interfaces

Later tasks must use these names and responsibilities unless a RED demonstrates that the signature cannot preserve current behavior. If a signature changes, update this section and every later task in the same planning commit before implementation continues.

`email_automation/message_transport.py`:

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Protocol


class DeliveryKind(str, Enum):
    NEW = "new"
    REPLY = "reply"
    REPLY_ALL = "reply_all"


@dataclass(frozen=True)
class HydratedInboundMessage:
    summary: Mapping[str, Any]
    full_text: str
    text_for_ai: str
    source_envelope: Mapping[str, Any]
    internet_headers: tuple[Mapping[str, str], ...]
    attachment_snapshot: tuple[Mapping[str, Any], ...]


class InboundMessageSource(Protocol):
    def hydrate(self, summary: Mapping[str, Any]) -> HydratedInboundMessage: ...


@dataclass(frozen=True)
class CanonicalConversationState:
    reply_target: HydratedInboundMessage
    prior_messages: tuple[HydratedInboundMessage, ...]
    sent_receipts: tuple[Mapping[str, str], ...]


class ConversationStateSource(Protocol):
    def load(self, conversation_key: str) -> CanonicalConversationState: ...


@dataclass(frozen=True)
class OutboundDraft:
    kind: DeliveryKind
    subject: str
    body: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    reply_to_message_id: Optional[str] = None
    attachments: tuple[Mapping[str, Any], ...] = ()
    idempotency_key: str = ""


@dataclass(frozen=True)
class DeliveryReceipt:
    status: str
    provider_message_id: str
    internet_message_id: str
    conversation_id: str


class OutboundDraftTransport(Protocol):
    def deliver(self, draft: OutboundDraft) -> DeliveryReceipt: ...
```

`email_automation/automation_runtime.py`:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Protocol

from .message_transport import (
    ConversationStateSource,
    InboundMessageSource,
    OutboundDraftTransport,
)


@dataclass(frozen=True)
class CounterReservation:
    scope: str
    key: str
    amount: int
    limit: int


@dataclass(frozen=True)
class CounterReservationToken:
    reservation_id: str
    reservations: tuple[CounterReservation, ...]


class CounterStore(Protocol):
    def reserve_many(
        self,
        reservations: tuple[CounterReservation, ...],
        idempotency_key: str,
    ) -> Optional[CounterReservationToken]: ...
    def release_many(self, token: CounterReservationToken) -> None: ...


class EffectScope(Protocol):
    def assert_firestore_path(self, path: str) -> None: ...
    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None: ...
    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None: ...
    def assert_drive_parent(self, parent_id: str) -> None: ...
    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None: ...


class AIProviderTransport(Protocol):
    def create_response(self, request: Mapping[str, Any]) -> Any: ...
    def create_chat_completion(self, request: Mapping[str, Any]) -> Any: ...
    def upload_file(self, file_obj: Any, purpose: str) -> Any: ...


class DrivePublicationTransport(Protocol):
    def publish(
        self,
        file_id: str,
        permission: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class AutomationRuntime:
    inbound: InboundMessageSource
    conversations: ConversationStateSource
    outbound: OutboundDraftTransport
    counters: CounterStore
    now: Callable[[], datetime]
    firestore: Any
    sheets: Any
    drive: Any
    effect_scope: EffectScope
    ai_provider: AIProviderTransport
    drive_publication: DrivePublicationTransport
    certification_run_id: Optional[str] = None
    certification_scope: Optional[str] = None
```

`email_automation/certification/models.py`:

```python
from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class CertificationVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSTRUMENT_BLOCKED = "INSTRUMENT_BLOCKED"
    NOT_TESTED = "NOT_TESTED"


@dataclass(frozen=True)
class CertificationRequest:
    scenario_id: str
    run_id: str
    expected_revision: str


@dataclass(frozen=True)
class RunAuthorization:
    scenario_id: str
    run_id: str
    source_revision: str
    image_digest: str
    certification_service: str
    certification_revision: str
    production_candidate_revision: str
    caller_identity_digest: str
    fixture_config_secret_version: str
    fixture_config_digest: str
    scenario_registry_digest: str
    launch_class: str
    input_producer_kind: str
    canonical_input_digest: str
    input_producer_artifact_digest: str
    authorization_expires_at: str
    authorization_digest: str


@dataclass(frozen=True)
class AuthorizedRunIdentity(RunAuthorization):
    pass


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: str
    capability_id: str
    fixture_key: str
    oracle_projection_key: str
    expected_verdict: CertificationVerdict
    capability_stamp: bool
    input_producer_kind: str
    launch_class: str
    required_effects: Mapping[str, int]
    forbidden_effects: Mapping[str, int]
    requires_human_review: bool
    model_repeat_count: int
    naturalness_rubric_version: str


@dataclass(frozen=True)
class SealedCanonicalInput:
    scenario_id: str
    run_id: str
    canonical_payload_bytes: bytes
    input_producer_kind: str
    canonical_input_digest: str
    input_producer_artifact_digest: str


@dataclass(frozen=True)
class CertificationResult:
    run_id: str
    scenario_id: str
    source_revision: str
    image_digest: str
    certification_revision: str
    production_candidate_revision: str
    fixture_config_secret_version: str
    fixture_config_digest: str
    scenario_registry_digest: str
    launch_class: str
    input_producer_kind: str
    canonical_input_digest: str
    input_producer_artifact_digest: str
    authorized_identity_digest: str
    evidence_digest: str
    verdict: CertificationVerdict
    prompt_digest: str
    requested_model: str
    resolved_model: str
    model_fingerprint: str
    review_set_digest: str
    review_rubric_version: str
    review_results: tuple[Mapping[str, str], ...]
    failure_phase: str
    failure_code: str
    replay_delta: Mapping[str, int]
    residue: Mapping[str, int]
```

`RunAuthorization.authorization_digest` is lowercase SHA-256 over SiteSift-canonical-JSON-v1 bytes of every other `RunAuthorization` field, with field names exactly matching the dataclass and `authorization_digest` omitted. `authorization_expires_at` is UTC RFC3339 `YYYY-MM-DDTHH:MM:SSZ` with no fractional seconds or offset aliases. The runtime recomputes the digest from exact stored scalar types before every prepare/claim/recovery transition; it never trusts the stored digest. Fixed vectors and one-field mutation tests cover every field, expiry encoding, missing/extra fields, and circular/self-including digest attempts.

Certification uses scoped client wrappers inside a separate private `process-user-certification` Cloud Run service. That service runs the exact immutable candidate image but uses a dedicated certification service account with access only to the certification Firestore project/database and the exact shared fixture Sheet/Drive folder; it has no mailbox/send/queue/production-data authority. The wrappers call `EffectScope` before every write/batch/transaction operation, including document references returned from queries, and before every Sheet value/grid/batch request, Drive parent operation, or Drive permission publication. Ordinary `process-user` runtime uses the current production identity and clients. A post-run readback is corroboration; process isolation, least privilege, route fences, and scoped clients together prevent an out-of-fixture write.

`email_automation/certification/evidence.py` owns a permanent sanitized run ledger outside fixture cleanup:

```python
from datetime import datetime


class CertificationRunLedger(Protocol):
    def assert_prepared(self, request: CertificationRequest) -> None: ...
    def assert_claimed(
        self,
        request: CertificationRequest,
        authorized_identity: AuthorizedRunIdentity,
    ) -> None: ...
    def mark_running(self, run_id: str, phase: str) -> None: ...
    def mark_awaiting_review(
        self,
        run_id: str,
        review_metadata: Mapping[str, object],
        expires_in_seconds: int,
    ) -> None: ...
    def validate_generation(self, run_id: str, generation: int) -> None: ...
    def register_inflight(self, run_id: str, generation: int, operation_id: str) -> None: ...
    def clear_inflight(self, run_id: str, generation: int, operation_id: str) -> None: ...
    def mark_inflight_ambiguous(
        self,
        run_id: str,
        generation: int,
        operation_id: str,
        safe_provider_code: str,
    ) -> None: ...
    def revoke_generation_and_begin_quiescence(
        self,
        run_id: str,
        expected_generation: int,
    ) -> int: ...
    def assert_generation_quiescent(
        self,
        run_id: str,
        revoked_generation: int,
    ) -> None: ...
    def record_terminal(self, result: CertificationResult) -> bool: ...
    def list_stale_nonterminal(self, older_than: datetime) -> tuple[str, ...]: ...
    def read_nonterminal(
        self,
        run_id: str,
    ) -> tuple[CertificationRequest, AuthorizedRunIdentity, str]: ...
    def append_cleanup_result(
        self,
        run_id: str,
        evidence_digest: str,
        residue: Mapping[str, int],
    ) -> bool: ...
```

`prepare_authorized_run()` owns all data-plane writes; the operator owns none. It first creates one permanent sanitized `PREPARING` record for a never-used run ID. For a frontend scenario it invokes the private adapter with exact `{scenarioId,runId,sourceRevision,nonceDigest,expiresAt}`; the random nonce is server-created, bound to `PREPARING`, and never accepted from the operator. A final certification-database transaction requires the same `PREPARING` record and nonce, validates the returned payload and independently read adapter identity, creates one-use authorization plus sealed input, and moves the ledger to `PREPARED`. Concurrent/replayed prepare, response replay, expired nonce, or adapter mismatch is rejected. Failure after any pre-claim boundary leaves either one recoverable `PREPARING` record with no ephemeral input/auth or one complete `PREPARED` triple. `abort_prepared_run()` transactionally deletes ephemeral records and terminalizes `PREPARING/PREPARED` only after proven no `/run` request or expiry; an ambiguous HTTP call must query status first.

`begin_authorized_run()` uses one transaction to read/recompute/validate authorization and sealed input, require `PREPARED`, delete both one-use records, and move the permanent sanitized ledger to `CLAIMED` with the complete canonical `AuthorizedRunIdentity` object and its digest. There is no consumed-without-claim window. It returns the immutable identity/input plus a run-level cleanup handle allocated before `CertificationFixture.open()`. The handle knows only run paths resolved from secret-owned concrete resource IDs through registry logical aliases and is invoked unconditionally even when fixture construction fails. `mark_running` is monotonic; terminal recording is idempotent. Recovery reconstructs and revalidates the complete identity, reads state, quiesces, cleans, and terminalizes without invoking business execution. Missing/mutated fields fail closed. A claimed run is never reusable.

CLAIMED/RUNNING carries a generation-scoped execution lease. Every certification Firestore/Sheet/Drive/capture/provider operation validates the current generation, atomically registers an operation ID, and revalidates immediately before contact. A conclusive provider response clears the registration. A client timeout, connection loss, or cancellation does not: it atomically marks the operation `AMBIGUOUS`, moves the run and its unique per-run resource partition to `QUARANTINED`, and blocks cleanup, reuse, terminal `PASS`, and stamps. Every wrapper enforces a 60-second maximum client deadline, but that deadline is never treated as proof of server-side cancellation. Recovery rejects records newer than 720 seconds (the exact 540-second service timeout plus 180-second margin), any unexpired lease, or a changed generation. It rereads provider-server `updatedAt`, state, and lease inside one transaction, then CAS-revokes/increments the generation and enters `QUIESCING`. A conclusive path waits at least 75 seconds—the 60-second maximum in-flight call plus a 15-second margin—and requires zero registered operations for the revoked generation before cleanup/readback. An ambiguous registration may clear only after provider-specific terminal operation/idempotency evidence proves completion or nonexecution; a point-in-time absence read never proves nonexecution. If the provider has no authoritative reconciliation mechanism, the sanitized result stays `INSTRUMENT_BLOCKED:ambiguous_provider_effect`, the exact resources remain quarantined, and the frontier cannot reuse them or stamp the capability. A stale worker cannot register another effect. The deadline, quiescence margin, threshold, generation, operation state, and safe reconciliation evidence enter configuration and evidence.

Cleanup receives bounded retries. If business execution terminalizes `FAIL` or `INSTRUMENT_BLOCKED` with ordinary residue, `cleanup --run-id` is allowed for that terminal record: it can only invoke the idempotent run-level cleanup/readback handle, never business execution, never alter the original verdict, and only append a sanitized cleanup evidence digest. It must reject a quarantined ambiguous-provider operation until the separate provider-specific reconciliation transition supplies conclusive terminal evidence and clears the registration. The frontier cannot resume the fixture until both that transition (when applicable) and a direct zero-residue readback succeed.

`email_automation/certification/runner.py`:

```python
def run_certification(
    request: CertificationRequest,
    authorized_identity: AuthorizedRunIdentity,
    sealed_input: SealedCanonicalInput,
    cleanup_handle: CertificationRunCleanup,
) -> CertificationResult:
    validate_authorized_identity_matches_request(request, authorized_identity)
    validate_sealed_input_matches_identity(
        request,
        authorized_identity,
        sealed_input,
    )
    scenario = get_scenario(request.scenario_id)
    run_ledger = get_certification_run_ledger()
    run_ledger.assert_claimed(request, authorized_identity)
    fixture = None
    verdict = CertificationVerdict.FAIL
    failure_phase = "fixture_open"
    failure_code = "unclassified_failure"
    residue = {"unknown": 1}
    review_pending = False
    try:
        fixture = CertificationFixture.open(
            scenario,
            request,
            authorized_identity,
            sealed_input,
        )
        run_ledger.mark_running(request.run_id, "fixture_opened")
        failure_phase = "preflight"
        fixture.preflight_clean()
        failure_phase = "seed"
        fixture.seed()
        run_ledger.mark_running(request.run_id, "seeded")
        failure_phase = "execute"
        first = fixture.execute()
        run_ledger.mark_running(request.run_id, "executed")
        failure_phase = "reconcile"
        fixture.assert_effects(first)
        failure_phase = "replay"
        replay = fixture.execute()
        fixture.assert_zero_replay_delta(first, replay)
        if scenario.requires_human_review:
            review_projection = fixture.build_bounded_review_projection()
            review_metadata = cleanup_handle.store_ephemeral_review_projection(
                review_projection,
                expires_in_seconds=86400,
            )
            run_ledger.mark_awaiting_review(
                request.run_id,
                review_metadata,
                expires_in_seconds=86400,
            )
            review_pending = True
    except CertificationInstrumentError as exc:
        verdict = CertificationVerdict.INSTRUMENT_BLOCKED
        failure_code = allowlisted_failure_code(exc)
    except Exception as exc:
        verdict = CertificationVerdict.FAIL
        failure_code = allowlisted_failure_code(exc)
    else:
        verdict = CertificationVerdict.PASS
        failure_phase = ""
        failure_code = ""
    finally:
        if not review_pending:
            cleanup_code, residue_code, residue = cleanup_handle.cleanup_and_readback_with_retry(
                fixture
            )
    if review_pending:
        raise CertificationReviewPending(request.run_id)
    if cleanup_code or residue_code or any(residue.values()):
        verdict = CertificationVerdict.FAIL
        failure_phase = "cleanup" if cleanup_code else "residue_readback"
        failure_code = cleanup_code or residue_code or "residue_nonzero"
    result = build_sanitized_result(
        request,
        authorized_identity,
        scenario,
        fixture,
        verdict,
        residue,
        failure_phase=failure_phase,
        failure_code=failure_code,
    )
    if not record_terminal_with_bounded_retry(run_ledger, result):
        raise CertificationTerminalizationPending(request.run_id)
    return result
```

`build_sanitized_result` uses the shared `SiteSift canonical JSON v1` helper to compute `authorized_identity_digest` and `evidence_digest`; the preimage includes every `AuthorizedRunIdentity` field, declared required/forbidden effects, observations, replay, cleanup, residue, and bounded `failure_phase`/allowlisted `failure_code`. It refuses a missing, blank, mutated, or request-mismatched identity field. Raw exception text is discarded and is never serialized, logged, or durable; only the bounded safe phase/code survives. Tests fault each boundary and require the expected code without leaking the sentinel. `recover --run-id` handles stale nonterminal records by readback and cleanup only; `cleanup --run-id` handles terminal residue only; neither may call `fixture.execute()`.

Exact phases are `prepare`, `claim`, `fixture_open`, `preflight`, `seed`, `execute`, `reconcile`, `replay`, `awaiting_review`, `review`, `quiescing`, `provider_reconciliation`, `cleanup`, `residue_readback`, `terminalize`, and `recovery` (empty only on PASS). Exact failure codes are `instrument_unavailable`, `user_runtime_launch_required`, `user_review_required`, `authorization_invalid`, `identity_mismatch`, `scope_denied`, `preflight_dirty`, `execution_failed`, `effect_mismatch`, `replay_delta`, `review_failed`, `human_review_expired`, `quiescence_timeout`, `ambiguous_provider_effect`, `cleanup_failed`, `residue_read_failed`, `residue_nonzero`, `terminalization_pending`, `stale_generation`, and `unclassified_failure` (empty only on PASS). Unknown exceptions map to `unclassified_failure`; neither type nor message is retained.

`AWAITING_REVIEW` is nonterminal and the sole allowed cleanup deferral. An ephemeral fixture-scoped, generation-bound, cleanup-owned artifact stores the ordered bounded `{ordinal,kind,bodyDigest,subject,body}` list for at most 24 hours. The permanent sanitized ledger stores only ordered digests/count, canonical review-set digest, rubric version, expiry, and released lease—never text. Zero-capture scenarios need no review. Baylor-manual `/review` submits exact ordered `{ordinal,bodyDigest,verdict,reasonCode}`. CAS rejects omissions/extras/duplicates/reordering/wrong digest/rubric/expiry/concurrency; one failure fails the scenario. Only after every message is reviewed does cleanup/readback run and terminalization occur. Expiry revokes, quiesces, cleans, and terminalizes `INSTRUMENT_BLOCKED:human_review_expired`; raw text is removed. Every other execution path cleans before terminalization. Tests prove raw subject/body absent from ledger/evidence/logs. Agent mode cannot call review-input/review or capture output.

`email_automation/certification/canonical_json.py` defines `SiteSift canonical JSON v1`. It accepts at most 65,536 UTF-8 bytes containing one duplicate-free exact JSON object; recursively it permits only exact string keys and `null`, booleans, signed 64-bit integers, strings, lists, and objects, with depth at most 16, width at most 64, and at most 4,096 total values. It rejects floats (including exponent notation), duplicate keys at any depth, byte-order marks, invalid UTF-8, and oversized inputs before allocating an unbounded copy. Canonical bytes are produced with UTF-8, lexicographically sorted keys, no insignificant whitespace, lowercase JSON literals, minimal base-10 integers, and JSON escaping equivalent to Python `json.dumps(..., ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))`. Both route and CLI import this one helper; neither reimplements the algorithm.

## Phase A — Freeze the mission and characterize shared boundaries

### Task 0: Build the deterministic ranked frontier

**Files:**

- Create: `docs/release-safety/production-certification/frontier.json`
- Create: `docs/release-safety/production-certification/identity.schema.json`
- Create: `docs/release-safety/production-certification/stamps/README.md`
- Create: `scripts/rank_certification_frontier.py`
- Create: `tests/test_certification_frontier.py`

- [ ] **Step 1: Write the ranking RED**

Load the approved planning manifest and pin ordered capabilities, bootstrap, explicit refutation scenarios, all 91 complete scenario definitions, unique finite IDs, execution/model-repeat/human-review contract, exact source anchors, prebuild `frontendCertificationSourceAnchor=null`, and test-module paths. Null blocks spreadsheet admission until a reviewed coordinated adapter successor. At this first task validate the manifest by itself: reject wildcards, missing per-scenario fields/cardinalities, duplicate/unclassified scenarios, an unknown capability reference, or a fixture value that is not a repository-safe logical alias. Do not require the not-yet-created in-image registry here. Task 1 adds canonicalization and the runtime registry, then extends this same ranker test to require exact manifest↔runtime parity before either task is considered jointly GREEN. A safety failure outranks core completion; dependencies outrank dependents; unrelated backlog stays inactive; changed paths/identity invalidate matching stamps; unknown source/image/config/registry/prompt/requested-or-resolved-model/fingerprint/dependency/cross-repo changes fail closed. Select one active capability and at most one independent instrument/authority blocker.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v tests.test_certification_frontier
```

Expected: failure because the frontier and deterministic ranker do not exist.

- [ ] **Step 3: Implement the narrow ranker**

The script accepts `--frontier`, private sanitized `--stamps`, `--previous-identity`, `--current-identity`, `--changed-paths`, and `--json`. Identity contains full Git SHA, immutable image/config/registry/prompt/model/dependency/fixture/cross-repo identities. It never reads the general backlog to choose work. It emits active capability, invalidations, blockers, and reason codes. Unknown changes fail closed. It never mutates tracked product files; optional output must target an explicit non-repository path, while durable current state lives in the private certification ledger and sanitized Brain checkpoint. This prevents recording a stamp from changing the source SHA it certifies.

- [ ] **Step 4: Run GREEN and hostile cases**

```bash
python3 -B -m unittest -v tests.test_certification_frontier
python3 -B -m py_compile scripts/rank_certification_frontier.py
```

Expected: deterministic results under input reordering; unrelated backlog examples never change the selected capability; a safety blocker does.

- [ ] **Step 5: Commit**

```bash
git add \
  docs/release-safety/production-certification/frontier.json \
  docs/release-safety/production-certification/identity.schema.json \
  docs/release-safety/production-certification/stamps/README.md \
  scripts/rank_certification_frontier.py \
  tests/test_certification_frontier.py
git commit -m "feat: rank the production certification frontier"
```

### Task 1: Add the ranked-frontier contract and scenario manifest schema

**Files:**

- Create: `email_automation/certification/models.py`
- Create: `email_automation/certification/scenarios.py`
- Create: `email_automation/certification/scenario_registry.json`
- Create: `email_automation/certification/canonical_json.py`
- Test: `tests/test_production_certification.py`
- Test: `tests/test_certification_canonical_json.py`
- Modify: `tests/test_certification_frontier.py`

- [ ] **Step 1: Write the RED for closed scenario selection**

Define tests that accept only an exact plain request:

```python
{"scenarioId": "campaign-one-property", "runId": "cert-20260817-0001", "expectedRevision": "full-git-sha"}
```

Reject missing/extra keys, non-string values, whitespace-padded IDs, unknown scenarios, reused run IDs, arbitrary UID/client/sheet/recipient/body fields, and a mismatched revision. Load the exact `bootstrapScenario.scenarioId` from the approved manifest, require it to be finite and unique across all capability scenario IDs, and require `capabilityStamp=false`; it is an infrastructure proof, not an out-of-order capability pass. Require exactly 91 capability definitions and one-to-one equality between every capability `scenarioIds` member and one full definition containing logical fixture/oracle aliases, exact effect cardinalities, verdict/stamp, producer/launch class, repeat count, and review contract. Reject a default/inherited field, a generic self-asserted success effect, or any concrete resource identifier in the manifest/runtime bytes.

Add canonical JSON REDs in this task because scenario-registry loading depends on it: fixed bytes/digests, duplicate keys, Unicode ordering, integers, width/depth/size, and Python 3.12 parity vectors. Add a sealed-input RED that mutates the original nested payload after sealing and proves stored canonical bytes, the input digest, execution input, and evidence digest remain unchanged.

- [ ] **Step 2: Run the focused RED**

Run:

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.ProductionCertificationModelTests \
  tests.test_certification_canonical_json
```

Expected: failures because the certification package and strict request model do not exist.

- [ ] **Step 3: Implement immutable types and the first closed scenario**

Implement SiteSift canonical JSON v1 first, then frozen dataclasses or equally strict immutable types. Copy the finite approved bootstrap/refutation and all 91 complete capability scenario objects into canonical in-image `scenario_registry.json`; `scenarios.py`, route, runner, tests, and ranker load those exact bytes through that shared helper. `scenarioRegistryDigest` is lowercase SHA-256 of those canonical bytes; there is no hand-retyped Python registry, and manifest↔runtime IDs/fields must have exact parity. Required effects must come from observed store/capture projections keyed by each registry oracle alias; a scenario cannot emit its own pass token. Model scenarios carry exact launch class and `modelRepeatCount=3`; deterministic scenarios use repeat count 0. All three fresh model runs must pass—never average. Bind prompt digest, requested model, provider-returned `response.model`, and any stable fingerprint to each run/stamp. An unresolved mutable alias or changed/absent resolved identity invalidates/fails closed. Bootstrap and explicit impossible-oracle refutation both have `capabilityStamp=false`. The registry owns only logical fixture/oracle/capture aliases; concrete Sheet, Drive, client, recipient, thread, event, and state identities come only from the bound numeric fixture-config secret and source/image scans prove none leaked. Seal input by storing canonical immutable bytes plus their digest; parsing for execution happens from a fresh bounded decode of those bytes, never a caller-owned object.

- [ ] **Step 4: Run GREEN and neighboring import checks**

Run the focused test and:

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.ProductionCertificationModelTests \
  tests.test_certification_canonical_json \
  tests.test_certification_frontier
python3 -B -m py_compile \
  email_automation/certification/canonical_json.py \
  email_automation/certification/models.py \
  email_automation/certification/scenarios.py \
  tests/test_certification_canonical_json.py \
  tests/test_certification_frontier.py \
  tests/test_production_certification.py
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add \
  email_automation/certification/__init__.py \
  email_automation/certification/canonical_json.py \
  email_automation/certification/models.py \
  email_automation/certification/scenarios.py \
  email_automation/certification/scenario_registry.json \
  tests/test_certification_canonical_json.py \
  tests/test_certification_frontier.py \
  tests/test_production_certification.py
git commit -m "test: define closed production certification scenarios"
```

### Task 2: Characterize the current inbound and outbound boundaries

**Files:**

- Create: `tests/test_message_transport.py`
- Inspect: `email_automation/processing.py:6397`
- Inspect: `email_automation/processing.py:9058`
- Inspect: `email_automation/email.py:3240`
- Inspect: `email_automation/processing.py:5626`
- Inspect: `email_automation/followup.py:2887`

- [ ] **Step 1: Write transcript-characterization tests**

Pin the current normalized body, headers, envelope, attachments, selected To/CC/BCC, subject, body, signature, and durable state writes for one initial message and one inbound reply. Assert that every known sending lane reaches the identified final delivery calls.

- [ ] **Step 2: Run the characterization suite**

```bash
python3 -B -m unittest -v \
  tests.test_source_message_envelope \
  tests.test_graph_send_inventory \
  tests.test_message_transport
```

Expected: existing controls pass; new tests fail only where the shared boundary is missing.

- [ ] **Step 3: Commit tests only**

```bash
git add tests/test_message_transport.py
git commit -m "test: pin automation transport boundaries"
```

## Phase B — Extract shared inputs without changing production behavior

### Task 3: Isolate canonical inbound hydration

**Files:**

- Create: `email_automation/message_transport.py`
- Modify: `email_automation/processing.py:6397`
- Modify: `email_automation/processing.py:9058`
- Test: `tests/test_message_transport.py`
- Test: `tests/test_source_message_envelope.py`

- [ ] **Step 1: Add RED parity cases**

For body text, HTML, quoted content, sender/reply-to/To/CC, headers, attachment flag, message IDs, timestamps, reply targets, prior thread messages, and Sent Items reconciliation receipts, require Graph-backed and supplied fixture sources to produce equal immutable canonical envelopes/conversation state. Every certification lane must complete with the entire Graph request layer patched to raise; no read, draft/createReplyAll, send, or Sent Items call is allowed.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_message_transport.CanonicalInboundMessageTests
```

Expected: failure because hydration remains embedded in `process_inbox_message()` and `_save_message_to_thread()`.

- [ ] **Step 3: Implement one source protocol**

Move acquisition only. Do not move matching, authority, AI, Sheet, event, or reply policy. Ordinary production constructs Graph-backed inbound/conversation sources; certification selects registry logical aliases and resolves their concrete fixture sources only from the bound numeric secret. Both call the same canonicalization functions. Automatic replies and follow-ups derive reply metadata and synthetic sent receipts from `CanonicalConversationState` plus `DeliveryReceipt`, never a hidden Graph lookup.

- [ ] **Step 4: Run GREEN and inbound neighbors**

```bash
python3 -B -m unittest -v \
  tests.test_message_transport \
  tests.test_source_message_envelope \
  tests.test_inbound_authority_m1 \
  tests.test_processing_reply_identity
```

Expected: all pass and ordinary production call signatures remain backward-compatible.

- [ ] **Step 5: Commit**

```bash
git add \
  email_automation/message_transport.py \
  email_automation/processing.py \
  tests/test_message_transport.py \
  tests/test_source_message_envelope.py
git commit -m "refactor: isolate inbound message hydration"
```

### Task 4: Forward supplied attachment snapshots through the shared pipeline

**Files:**

- Modify: `email_automation/file_handling.py:1714`
- Modify: `email_automation/file_handling.py:2630`
- Modify: `email_automation/processing.py`
- Test: `tests/test_image_attachment_ingestion.py`
- Test: `tests/test_scanned_pdf_extraction.py`

- [ ] **Step 1: Write RED cases**

Require approved raw attachment snapshots to follow the same ordering, PDF/native classification, address binding, caps, normalization, privacy projection, and fail-closed behavior as Graph-acquired snapshots, with zero Graph call.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_image_attachment_ingestion \
  tests.test_scanned_pdf_extraction
```

Expected: new supplied-snapshot integration assertions fail on the missing forwarding seam.

- [ ] **Step 3: Implement minimal forwarding**

Forward the immutable snapshot; do not duplicate validators, parsers, address classifiers, manifest builders, or model request construction.

- [ ] **Step 4: Run GREEN**

Run the same command. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add \
  email_automation/file_handling.py \
  email_automation/processing.py \
  tests/test_image_attachment_ingestion.py \
  tests/test_scanned_pdf_extraction.py
git commit -m "refactor: accept certified attachment snapshots"
```

## Phase C — Capture final outbound behavior through the ordinary state machine

### Task 5: Add request-scoped runtime defaults

**Files:**

- Create: `email_automation/automation_runtime.py`
- Test: `tests/test_automation_runtime.py`

- [ ] **Step 1: Write RED isolation and counter-atomicity tests**

Prove two concurrent runtime objects cannot share a capture, clock, counter, inbound/conversation source, AI transport, Drive-publication transport, run ID, or fixture scope. Prove omitted runtime dependencies construct ordinary production behavior. Prove arbitrary runtime construction is unavailable from public request data. Certification AI deny/capture must raise `user_runtime_launch_required` before any provider request; certification Drive publication must validate exact file/body, record would-publish, and never call `permissions.create`. For counters, require one atomic user+global reservation or no reservation, multi-message amounts, concurrent limit enforcement, same-key retry returning the original token without a second increment, and exact idempotent refund by that token only after a proven no-send. Ambiguous delivery retains the reservation and binds it to reconciliation; later confirmed-send keeps it, while later proven no-send releases it once. Releasing the same token twice is a zero-delta success; a partial user/global reservation, cross-token release, ambiguous refund, or over-refund is a failing RED.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v tests.test_automation_runtime
```

- [ ] **Step 3: Implement the immutable runtime bundle**

Keep it small: inbound/conversation sources, outbound delivery, AI inference, Drive publication, clock, counter store, run identity, scoped clients, and certification scope. `EffectScope` implements the locked Firestore path, A1 Sheet target, typed Sheet request/body, Drive parent, and exact permission file/body checks; it contains no business rules.

- [ ] **Step 4: Run GREEN and compile**

```bash
python3 -B -m unittest -v tests.test_automation_runtime
python3 -B -m py_compile email_automation/automation_runtime.py
```

- [ ] **Step 5: Commit**

```bash
git add email_automation/automation_runtime.py tests/test_automation_runtime.py
git commit -m "feat: add request scoped automation runtime"
```

### Task 5A: Route the first vertical slice through scoped data clients

**Files:**

- Modify: `email_automation/clients.py`
- Modify: `email_automation/email.py`
- Modify: `email_automation/messaging.py`
- Modify: `email_automation/campaign_safety.py`
- Modify: `email_automation/processing.py`
- Modify: `email_automation/followup.py`
- Modify: `email_automation/sheets.py`
- Modify: `email_automation/notifications.py`
- Test: `tests/test_automation_runtime.py`
- Test: `tests/test_outbox_safety.py`
- Test: `tests/test_production_certification.py`

- [ ] **Step 1: Write a global-client-denial RED for one-property outreach**

Seed the closed one-property fixture, pass an explicit certification runtime to `send_outboxes()`, and make every ambient production Firestore/Sheets/Drive client constructor and imported global raise on access. Require the scoped runtime to complete the ordinary campaign/outbox, campaign-authority and opt-out reads, action-audit/counter writes, thread/message/index writes, follow-up scheduling, and row-highlight write. Require zero Drive call. Include conditional notification deletion so the shared finalizer cannot fall back to `_fs`. Outbound extraction is deliberately Task 6: in this task only, patch the existing direct Graph boundary with the established deterministic fake and assert its expected calls; do not claim capture transport or make a network request.

- [ ] **Step 2: Write wrapper-escape REDs**

The Firestore fence must survive collection/document chains, queries, streamed snapshots and each snapshot's `.reference`, transactions, batches, merge/set/update/delete/create, and retry callbacks. The Sheets fence must cover values get/update/batchUpdate plus spreadsheet numeric-grid `batchUpdate` requests such as row highlighting; `EffectScope` must expose a typed `assert_sheet_request(spreadsheet_id, body)` rather than validating only A1 ranges. The Drive wrapper is deny-all in the first slice. Mutate each wrapper to return one raw provider object and prove a targeted test fails before provider invocation.

- [ ] **Step 3: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_automation_runtime.ScopedClientContractTests \
  tests.test_production_certification.FirstSliceClientIsolationTests \
  tests.test_outbox_safety
```

- [ ] **Step 4: Thread one explicit runtime/client provider through the reached call graph**

Add optional runtime/client parameters with current production defaults. Do not use a mutable global or `ContextVar`. The first-slice call graph is exactly:

- `email.py`: `send_outboxes`, `_send_single_outbox_item`, `send_and_index_email`, `_claim_outbox_item`, `_update_action_audit`, `_finalize_successful_outbox_item`, `_move_to_dead_letter`, `_record_outbox_reconciliation`, `_record_send_cap_health`, `_read_client_automation_decision`, `_campaign_sheet_header_and_row`, `_has_existing_thread_for_property`, and `get_contact_email_count`;
- `clients.py`: `_get_sheet_id_or_fail`;
- `campaign_safety.py`: `get_client_automation_decision`;
- `processing.py`: `is_contact_opted_out`;
- `messaging.py`: `save_thread_root`, `save_message`, `_delete_synthetic_outbound_duplicates`, `index_message_id`, `lookup_thread_by_message_id`, and `index_conversation_id`;
- `followup.py`: `schedule_followup_for_thread`, including the request clock;
- `sheets.py`: `highlight_row`; and
- `notifications.py`: `delete_notification_and_decrement_counters`.

Every function reached by the first slice must use the supplied clients/counters; functions outside this slice remain unchanged until their own pack. Ordinary calls with no runtime keep current behavior. Returned document references remain wrapped. Certification never reads or writes production `sendCounters`; it uses the runtime's atomic isolated `CounterStore`.

- [ ] **Step 5: Run GREEN, default-production parity, and commit**

```bash
python3 -B -m unittest -v \
  tests.test_automation_runtime \
  tests.test_production_certification.FirstSliceClientIsolationTests \
  tests.test_outbox_safety \
  tests.test_graph_send_inventory \
  tests.test_followup_terminal_state \
  tests.test_notifications
git add \
  email_automation/clients.py \
  email_automation/email.py \
  email_automation/messaging.py \
  email_automation/campaign_safety.py \
  email_automation/processing.py \
  email_automation/followup.py \
  email_automation/sheets.py \
  email_automation/notifications.py \
  tests/test_automation_runtime.py \
  tests/test_outbox_safety.py \
  tests/test_production_certification.py
git commit -m "refactor: scope certification data clients"
```

### Task 6: Isolate initial-outreach delivery

**Files:**

- Modify: `email_automation/message_transport.py`
- Modify: `email_automation/email.py:3240`
- Modify: `email_automation/email.py:3839`
- Modify: `email_automation/email.py:4788`
- Create: `email_automation/certification/capture.py`
- Test: `tests/test_message_transport.py`
- Test: `tests/test_outbox_safety.py`

- [ ] **Step 1: Write RED receipt and state-parity tests**

Capture must receive the final safe envelope only after recipient, placeholder, signature, cancellation, duplicate, and policy checks. A successful synthetic receipt must drive the same thread, message, index, follow-up, audit, claim, and terminalization writes as a successful Graph receipt. A failed/ambiguous receipt must preserve the same retry/reconciliation behavior.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_message_transport.OutboundDeliveryTests \
  tests.test_outbox_safety
```

- [ ] **Step 3: Inject the delivery protocol at the last common boundary**

Do not move rendering or safety logic into the transport. Preserve existing default signatures by constructing ordinary production runtime when none is supplied.

- [ ] **Step 4: Run GREEN and all send neighbors**

```bash
python3 -B -m unittest -v \
  tests.test_message_transport \
  tests.test_outbox_safety \
  tests.test_graph_send_inventory \
  tests.test_sent_mail_guard_recipient_continuation \
  tests.test_followup_terminal_state
```

- [ ] **Step 5: Commit**

```bash
git add \
  email_automation/message_transport.py \
  email_automation/email.py \
  email_automation/certification/capture.py \
  tests/test_message_transport.py \
  tests/test_outbox_safety.py
git commit -m "refactor: isolate initial outreach delivery"
```

### Task 7A: Route automatic same-thread replies through delivery

**Files:**

- Modify: `email_automation/processing.py:5626`
- Test: `tests/test_processing_reply_safety.py`
- Test: `tests/test_graph_send_inventory.py`

- [ ] Write a RED that patches the entire Graph request layer to raise, exercises `new`, `reply`, and `reply_all`, and proves canonical conversation state supplies the exact reply target/history/Sent receipt, final safe To/CC/BCC, and one capture only when policy permits.
- [ ] Run `python3 -B -m unittest -v tests.test_processing_reply_safety tests.test_graph_send_inventory` and record the intentional failure.
- [ ] Pass `AutomationRuntime.outbound` through the existing automatic-reply path without moving recipient, cancellation, policy, or audit logic.
- [ ] Rerun both modules and the source-envelope suite.
- [ ] Commit only processing/tests: `git commit -m "refactor: route automatic replies through delivery"`.

### Task 7B: Route pending-response retries through delivery

**Files:**

- Modify: `email_automation/pending_responses.py`
- Test: `tests/test_pending_responses.py`

- [ ] Write a RED proving success, provider ambiguity, retry, cancellation, and terminalization use the shared receipt while direct Graph is unreachable.
- [ ] Run `python3 -B -m unittest -v tests.test_pending_responses` and record the intentional failure.
- [ ] Inject only the delivery dependency; preserve pending-response retry and evidence semantics.
- [ ] Rerun the module and commit: `git commit -m "refactor: route pending replies through delivery"`.

### Task 7C: Route dashboard/manual replies through delivery

**Files:**

- Modify: `email_automation/email.py:2239`
- Test: `tests/test_outbox_reply_recipient_routing.py`
- Test: `tests/test_process_outbox_item_scope.py`

- [ ] Write a RED proving exact reply target, current authority/cancellation reread, safe reply-all audience, audit ownership, and zero direct Graph call under certification runtime.
- [ ] Run `python3 -B -m unittest -v tests.test_outbox_reply_recipient_routing tests.test_process_outbox_item_scope` and record the intentional failure.
- [ ] Reuse `OutboundDraftTransport`; do not bypass or duplicate the manual-item authority state machine.
- [ ] Rerun both modules and commit: `git commit -m "refactor: route manual replies through delivery"`.

### Task 7D: Route follow-ups through delivery

**Files:**

- Modify: `email_automation/followup.py:2887`
- Test: `tests/test_followup_terminal_state.py`
- Test: `tests/test_broker_language_followup_due.py`

- [ ] Write a RED with every Graph request denied, proving due-time selection, exact remaining fields, fixture-sourced Sent Items/manual continuation, stop/opt-out/terminal suppression, and final capture require zero mailbox read or delivery.
- [ ] Run `python3 -B -m unittest -v tests.test_followup_terminal_state tests.test_broker_language_followup_due` and record the intentional failure.
- [ ] Inject only the delivery and request clock dependencies; preserve follow-up policy and counters.
- [ ] Rerun both modules and commit: `git commit -m "refactor: route followups through delivery"`.

### Task 7E: Prove there is no recovery or alternate-send bypass

**Files:**

- Modify only if RED proves a bypass: `email_automation/dead_letter_recovery.py`, `email_automation/operator_replay.py`, or the exact discovered sender.
- Test: `tests/test_graph_send_inventory.py`
- Test: `tests/test_dead_letter_recovery.py`
- Test: `tests/test_resend_failed_responses.py`

- [ ] Expand the static/dynamic inventory RED so every mailbox-read/send-capable callable either routes through `InboundMessageSource`/`ConversationStateSource`/`OutboundDraftTransport` or is proven unreachable/manual-only. Patch every Graph request to raise in all 7A–E certification cases.
- [ ] Run `python3 -B -m unittest -v tests.test_graph_send_inventory tests.test_dead_letter_recovery tests.test_resend_failed_responses`.
- [ ] If a bypass exists, change only that lane and add its focused state-parity test. Then rerun the exact three modules above plus `tests.test_message_transport` and py-compile every changed source before committing. If no bypass exists, commit the test-only proof after the same GREEN command.
- [ ] Commit: `git commit -m "test: close alternate automation send paths"`.

### Task 7F: Route every AI and public-Drive effect through request scope

**Files:**

- Create: `email_automation/certification/effect_transports.py`
- Create: `tests/test_external_effect_inventory.py`
- Modify: `email_automation/automation_runtime.py`
- Modify: `email_automation/ai_processing.py`
- Modify: `email_automation/column_config.py`
- Modify: `email_automation/file_handling.py`
- Modify: `email_automation/service_providers.py`
- Modify: `email_automation/utils.py`
- Modify: `scheduler_runner.py`
- Test: exact focused neighbors named by the static inventory.

- [ ] Write a RED inventory that enumerates every `client.responses.create`, `chat.completions.create`, `client.files.create`, `openai_client()`/ambient client import, Drive `permissions().create`, and wrapper alias in deployable source. Under a certification runtime, replace the raw OpenAI client, all network transports, and raw Drive permission provider with raising sentinels. Exercise every reachable call family and prove no byte/provider request escapes. Mutate one routed call back to the raw provider and require the inventory test to fail. Any future direct call fails the static test.
- [ ] Require ordinary production defaults to preserve exact current behavior. Agent-safe certification constructs a provider that returns `user_runtime_launch_required` before response/chat/file-upload. Only an authorized `launch_class=user_runtime` prepared record may construct the real provider. Thread request-scoped Firestore/clock through OpenAI usage events and rollups; patch ambient `_fs` to raise and require exact isolated certification-database accounting/readback.
- [ ] Add `DrivePublicationTransport` at all five current call sites across four files: two in `file_handling.py`, one each in `service_providers.py`, `utils.py`, and `scheduler_runner.py`. Pin that baseline set, then require zero direct sites outside the provider adapter. Certification validates exact fixture file ID plus duplicate-free `{role:"reader",type:"anyone"}`, records would-publish, returns a synthetic fixture-private receipt, calls no permission provider, and proves zero public permissions before/after. Ordinary defaults call the provider unchanged.
- [ ] Run `python3 -B -m unittest -v tests.test_external_effect_inventory` and record the expected direct-call failures before creating `email_automation/certification/effect_transports.py`.
- [ ] Make the smallest shared-boundary change, then rerun that exact test plus every focused OpenAI/PDF/property-image/scheduler neighbor discovered by the inventory. Py-compile `email_automation/certification/effect_transports.py` and every changed source.
- [ ] Stage the exact inventory, new transport module, and only the production files changed by its RED; verify the staged path list, then commit `git commit -m "refactor: scope external provider effects"`. `effect_transports.py` may not remain an unowned later artifact.

### Task 7G: Remove fixture values from durable application logs

- [ ] Write canary REDs around every certification-reachable print/logger call in AI, PDF/image, message, and runner paths. Include raw body, filename, file ID, pixels/base64, provider token, adapter payload, model proposal, and exception sentinels. Capture stdout/stderr/logging locally and require only stable codes, counts, phases, and safe digests.
- [ ] Refactor only failing shared log statements; ordinary operational meaning must remain available through safe codes. Rerun exact affected neighbors and `tests.test_external_effect_inventory`, then commit `fix: sanitize certification reachable logs`.
- [ ] The deployed run verifier queries Cloud Logging for the exact twin revision/run window and rejects any fixture canary/body/file ID/token or raw exception before cleanup/stamp. Sanitized evidence alone never substitutes for this readback.

## Phase D — Build the sealed certification runner

### Task 8: Implement fixture lifecycle and evidence projections

**Files:**

- Create: `email_automation/certification/fixtures.py`
- Create: `email_automation/certification/evidence.py`
- Use unchanged: `email_automation/certification/canonical_json.py`
- Create: `email_automation/certification/input_handoff.py`
- Modify: `email_automation/certification/models.py`
- Test: `tests/test_production_certification.py`
- Test: `tests/test_certification_canonical_json.py`

- [ ] **Step 1: Write hostile-scope and cleanup REDs**

Cover non-fixture IDs, unknown recipients, wrong Sheet/Drive ancestry, dirty prestate, nonempty queue/lease, partial seed, partial cleanup, reused run ID, authorization/input reuse, out-of-scope Firestore transaction/batch/write, Sheet range/grid body, Drive parent/permission body, raw body/pixel/token leakage, and intentionally wrong expected effects. Under certification, patch every Graph request, OpenAI request, and Drive `permissions.create` provider call to raise. Deterministic scenarios must still run; model-dependent scenarios must return `INSTRUMENT_BLOCKED:user_runtime_launch_required` before provider contact; Drive publication must produce only a validated would-publish capture plus zero-public-permission readback.

Add exact REDs proving no claim/execute/terminal `PASS` when any authorized-run field is missing, blank, request-mismatched, or mutated. Changing only secret version/digest, scenario-registry digest, producer kind, canonical-input digest, producer-artifact digest, launch class, verified caller digest, or expiry changes both identity and evidence digests. Allow only `backend_registry_v1` and `frontend_functions_adapter_v1`. Compute `scenarioRegistryDigest` from the in-image canonical registry. Compute producer-artifact digests from the locked canonical preimages. Python recomputes `canonicalInputDigest` from payload canonical bytes; frontend self-report is comparison-only. Add fixed Python/Node Unicode, key-order, signed-integer, and mutation vectors.

Prove preparation is failure-atomic: fault creation of `PREPARING`, adapter invocation, adapter response validation, the final `PREPARED` transaction, each authorization/input write, and the call-before-HTTP boundary. The only accepted states are no record, recoverable `PREPARING` with no ephemerals, or a complete `PREPARED` triple. Prove abort is allowed only after proven no-run/expiry and atomically deletes ephemerals plus terminalizes; an ambiguous `/run` call must status-check, and `CLAIMED` is recovery-only. Fault every claim read/write and prove authorization/input deletion and durable `CLAIMED` transition are atomic. Test the exact `RunAuthorization` digest vectors and recomputation. Mutate the outer payload and nested lists/dicts immediately after sealing; execution and evidence must use only the immutable canonical byte string retained by `SealedCanonicalInput`, and the original digest must still match those bytes.

Allocate run cleanup before fixture open; fault before/inside open and require zero authorization/input/raw-payload/fixture residue. Add bounded cleanup retries plus terminal cleanup-only recovery that never executes business logic or changes the original verdict. Add fixed canonical-byte and SHA-256 vectors, nested duplicate-key/float/UTF-8/bounds cases, and prove current Python plus production Python 3.12 emit identical bytes. Patch real clients to assert forbidden operations are never attempted; later readback alone is insufficient.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.CertificationFixtureTests \
  tests.test_certification_canonical_json
```

- [ ] **Step 3: Implement preflight, seed, narrow readbacks, and exact cleanup**

Resolve allowlisted exact concrete resource identities only from the bound numeric fixture-config secret after selecting repository-safe logical aliases from the in-image registry. Construct request-scoped clients under the certification runtime identity; the operator has no data-plane role. Each wrapper fences path/range/grid/parent/permission before provider contact. `/prepare` owns the PREPARING→PREPARED lifecycle; `/run` owns the atomic PREPARED→CLAIMED transition and unconditional cleanup handle. Raw canonical input bytes are ephemeral residue and only their digest survives. The permanent ledger transactionally rejects run-ID reuse. Evidence includes all identity fields, safe launch class, safe failure phase/code, counts, hashes, and bounded generic summaries—not addresses, bodies, IDs, tokens, fixture values, exception text, or customer data.

- [ ] **Step 4: Run GREEN, compile, and commit**

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.CertificationFixtureTests \
  tests.test_certification_canonical_json
python3 -B -m py_compile \
  email_automation/certification/input_handoff.py \
  email_automation/certification/fixtures.py \
  email_automation/certification/evidence.py \
  email_automation/certification/models.py
git add \
  email_automation/certification/fixtures.py \
  email_automation/certification/evidence.py \
  email_automation/certification/input_handoff.py \
  email_automation/certification/models.py \
  tests/test_production_certification.py \
  tests/test_certification_canonical_json.py
git commit -m "feat: add isolated certification fixture lifecycle"
```

### Task 9: Implement seed → execute → readback → replay → cleanup

**Files:**

- Create: `email_automation/certification/runner.py`
- Modify: `email_automation/certification/scenarios.py`
- Modify: `email_automation/certification/evidence.py`
- Modify: `email_automation/certification/models.py`
- Use unchanged: `email_automation/certification/input_handoff.py`
- Test: `tests/test_production_certification.py`

- [ ] **Step 1: Write state-machine and terminal-ledger REDs**

No `PASS` without exact authorized identity, preflight, execution, required/forbidden reconciliation, replay, cleanup, and zero residue. Any exception before/inside fixture construction, after seed, during cleanup/readback, or terminal-ledger write retains sanitized durable state. Prove `PREPARING → PREPARED → CLAIMED → RUNNING(phase) → QUIESCING|QUARANTINED → terminal` is monotonic, terminal recording is idempotent, bounded retries recover transient writes, and recovery never calls `execute()`. Add missing/mutated-field REDs for every authorization/identity field, including registry digest, launch class, verified caller digest, source/image env readbacks, and expiry. Each failure boundary must produce an allowlisted `failure_phase`/`failure_code` and no raw sentinel. Duplicate run IDs never execute. Terminal cleanup-only repair may append zero-residue evidence but cannot change the original verdict or capability stamp. Add a race RED that blocks a Sheet/Drive provider call after generation validation, causes a client timeout, then lets the provider commit late. Recovery must retain the ambiguous registration, quarantine the per-run resources, perform no cleanup/reuse/stamp, and reject an absence-only readback. A second branch supplies authoritative provider-specific terminal evidence; only then may the registration clear, the 75-second quiescence gate complete, and cleanup/readback begin. A new stale-worker registration is always a failure.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.CertificationRunnerTests
```

- [ ] **Step 3: Implement the first vertical slice**

`campaign-one-property` seeds one synthetic property/client/outbox, invokes `send_outboxes()` with certification runtime, captures exactly one final message, verifies thread/index/follow-up/audit state, replays with zero extra effect, and cleans every fixture artifact.

- [ ] **Step 4: Run GREEN, neighbors, and commit**

```bash
python3 -B -m unittest -v \
  tests.test_production_certification \
  tests.test_automation_runtime \
  tests.test_message_transport \
  tests.test_outbox_safety
git add \
  email_automation/certification/runner.py \
  email_automation/certification/scenarios.py \
  email_automation/certification/evidence.py \
  email_automation/certification/models.py \
  tests/test_production_certification.py
git commit -m "feat: run isolated production automation certification"
```

### Task 10: Expose private revision-bound certification routes

**Files:**

- Modify: `service.py`
- Create: `deploy/cloudrun-certification-service.yaml`
- Use unchanged: `email_automation/certification/canonical_json.py`
- Modify: `email_automation/certification/input_handoff.py`
- Test: `tests/test_process_user_service.py`
- Test: `tests/test_production_certification.py`
- Test: `tests/test_certification_canonical_json.py`

- [ ] **Step 1: Write route REDs**

Require Google OIDC verification from exact operator plus exact `K_SERVICE=process-user-certification`: signature, issuer, audience, exact `email`, `email_verified=true`, and exact numeric `sub` from an independent service-account describe. RED wrong email/sub/audience/issuer/unverified/missing-email cases. Add closed `POST /certification/prepare`, `/run`, `/status`, `/review-input`, `/review`, `/abort`, `/recover`, and `/cleanup` operations. Prepare/run bodies remain exact `{scenarioId,runId,expectedRevision}`; status/review-input/abort/recover/cleanup accept exact run ID plus expected revision. Review accepts exact `{runId,expectedRevision,reviewSetDigest,rubricVersion,reviews:[{ordinal,bodyDigest,verdict,reasonCode}]}` with one-use state and allowlisted values; it requires exact ordered cardinality. No route accepts fixture identity or arbitrary input. `SITESIFT_SOURCE_REVISION` and `SITESIFT_IMAGE_DIGEST` must be present, canonical, equal on candidate+twin, and independently match Git plus Cloud Run Admin API; forged/missing values reject.

Prepare creates PREPARING then PREPARED as specified; run consumes authorization/input while claiming; status resolves ambiguous calls. `review-input` is available only in `AWAITING_REVIEW` and returns one bounded ordered array containing every transient `{ordinal,kind,bodyDigest,subject,body}` projection for the run, with addresses/IDs removed; it never returns only a first message and does not paginate. Review must submit exactly one result for every array element, bind the ordered set digest and rubric, run cleanup/readback, and terminalize once. Raw review text is transient fixture state, never logs/evidence. Abort accepts only PREPARING/PREPARED proven not run; recover never executes; cleanup accepts only terminal failure/residue. Reject every other caller/service, missing/reused/expired/mismatched record, malformed schema, unknown scenario/producer, wrong revision/image/fixture/registry/input/producer digest, secret alias, concurrent run, or caller-supplied payload/resource. Safe durable responses contain only state, run/revision/image and safe digests, verdict, failure phase/code, review digest/rubric/safe reason, and evidence digest.

Add bidirectional route fences before parsing bodies or constructing provider clients: every ordinary mutating route including `/process-user` and `/process-outbox` is inert on `process-user-certification`; every `/certification/*` route is inert on ordinary `process-user`; health is the only shared route. RED all directions and assert zero provider call.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_process_user_service \
  tests.test_production_certification.CertificationRouteTests
```

- [ ] **Step 3: Add the closed prepare/run/status/review/abort/recover/cleanup routes**

Do not call `refresh_and_process_user()`. Implement the locked lifecycle and schemas exactly. Construct identities only from trusted records, verified OIDC, in-image registry bytes, direct service/source/image env plus independent readbacks, and immutable fixture-secret readback. Inspect PREPARED/CLAIMED/terminal evidence to prove every field entered identity/evidence preimages. Construct certification runtime with zero-Graph sources, outbound capture, agent-safe AI deny/user-launch gate, and Drive-publication capture. The manifest contains no mailbox/send/queue/production authority. Ordinary defaults remain behaviorally equivalent behind the service fence.

- [ ] **Step 4: Run GREEN and commit**

```bash
python3 -B -m unittest -v \
  tests.test_process_user_service \
  tests.test_production_certification.CertificationRouteTests
python3 -B -m py_compile \
  service.py \
  tests/test_process_user_service.py \
  tests/test_production_certification.py
git add \
  service.py \
  deploy/cloudrun-certification-service.yaml \
  email_automation/certification/input_handoff.py \
  tests/test_process_user_service.py \
  tests/test_production_certification.py
git commit -m "feat: expose private production certification route"
```

### Task 11: Add the revision-bound certification CLI

**Files:**

- Create: `scripts/certify_production.py`
- Use unchanged: `email_automation/certification/canonical_json.py`
- Use unchanged: `email_automation/certification/input_handoff.py`
- Test: `tests/test_production_certification.py`
- Test: `tests/test_certification_canonical_json.py`

- [ ] **Step 1: Write CLI and recovery REDs**

Refuse dirty/unpushed source, wrong local/upstream/remote SHA, candidate/twin image mismatch, wrong serving source/image/config, unallowlisted config, mutable fixture secret, independently read fixture digest mismatch, registry/input/producer drift, nonprivate or excessive IAM, raw evidence, missing replay, cleanup failure, stale authorization, or a verdict without direct readbacks. OIDC REDs cover signature, issuer, audience, email, `email_verified`, numeric `sub`, and missing `--include-email`. Preparation REDs cover every PREPARING/PREPARED fault, failure before HTTP, ambiguous HTTP, abort, status, recovery, cleanup-only repair, and replay. `recover` never prepares or executes. `cleanup` never executes or changes the original verdict.

Agent mode must refuse any scenario marked `requiresUserRuntimeLaunch=true` before `/run`, write `INSTRUMENT_BLOCKED:user_runtime_launch_required`, and print one exact sanitized manual command. It must also refuse `/review-input`, any raw captured-message output, public Git push, production traffic change, shared Functions/Hosting deployment, public Drive permission, or model-provider call. The manual review command displays bounded synthetic text locally to Baylor and submits only safe verdict/reason/body-hash; agent mode resumes from sanitized `/status` and never captures that command's stdout. Tests patch forbidden commands/APIs to raise. After Baylor performs a manual action, the agent uses only read-only parity/status/evidence commands to continue.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_production_certification.CertificationCliTests
```

- [ ] **Step 3: Implement authenticated invocation and independent readbacks**

The CLI uses `gcloud auth print-identity-token --include-email --impersonate-service-account=sitesift-certification-operator@email-automation-cache.iam.gserviceaccount.com --audiences=<exact-private-url>`. It independently reads the operator service account numeric `uniqueId` and validates token signature/issuer/audience/email/verified/sub through the service. The exact local release principal is resolved from `scripts/process_user_gcloud_preflight.sh::PROCESS_USER_APPROVED_ACCOUNT` and `scripts/phase1_rollout.py::ACCOUNT`; they must agree. Compare that value to provider IAM without printing/persisting it; only its canonical member digest enters evidence. Its Token Creator grant is resource-level on the operator only.

The CLI independently reads/digests one immutable numeric fixture-secret version through the operator's exact secret-only access, but performs no Firestore/Sheet/Drive write and never invokes the frontend adapter. It calls `/prepare`; the twin loads the in-image registry, computes authoritative canonical input/producer digests, and invokes the adapter itself for spreadsheet admission. The CLI status-checks PREPARED, calls `/run`, then rereads secret, records, revisions, source/image, strict config/IAM, fixture projections, evidence, queue/lease, replay, cleanup, and zero public permissions. A failure before a proven `/run` call invokes `/abort`; an ambiguous call invokes `/status`; CLAIMED/RUNNING invokes recovery only. It emits sanitized evidence and exact user-launch commands only.

- [ ] **Step 4: Run GREEN, compile, syntax, and commit**

```bash
python3 -B -m unittest -v tests.test_production_certification
python3 -B -m unittest -v tests.test_certification_canonical_json
python3 -B -m py_compile scripts/certify_production.py
git add scripts/certify_production.py tests/test_production_certification.py
git commit -m "feat: certify deployed SiteSift capabilities"
```

## Phase E — Verify, deploy safely, and prove the certification bootstrap

### Task 12: Add the exact-image certification-twin deployment contract

**Files:**

- Create: `scripts/deploy_certification_twin.sh`
- Modify: `deploy/cloudrun-certification-service.yaml`
- Modify: `scripts/deploy_process_user.sh`
- Modify: `scripts/phase1_rollout.py`
- Modify: `tests/test_process_user_tagless_staging_contract.py`
- Modify: `tests/test_process_user_phase1_rollout_contract.py`
- Modify: `tests/test_process_user_production_deploy_contract.py`
- Test: `tests/test_production_certification.py`

**Locked candidate/twin normalized-difference matrix:**

| Class | Exact fields | Rule |
|---|---|---|
| Must equal | repository-at-digest image; `SITESIFT_SOURCE_REVISION`; `SITESIFT_IMAGE_DIGEST`; container command/args/port; CPU/memory; timeout/concurrency/scaling; `ENFORCE_OPENAI_BUDGET`; `USAGE_MONTHLY_BUDGET_USD`; send caps; native/outbound gates; `OPENAI_API_KEY` secret reference; every model/prompt/feature env or unclassified spec field | direct values must also match Git/Admin API; then deep exact equality |
| Must be absent from twin | `FIREBASE_BUCKET`; `AZURE_API_APP_ID`; `AZURE_API_CLIENT_SECRET`; `FIREBASE_API_KEY`; `GOOGLE_OAUTH_CLIENT_ID`; `GOOGLE_OAUTH_CLIENT_SECRET`; `GOOGLE_REFRESH_TOKEN`; `PROCESS_USER_AUTH`; `SITESIFT_AUTO_REPLY_ALLOWLIST`; `SITESIFT_TOUR_ACTION_ALLOWLIST`; any Graph/mailbox credential; any Cloud Tasks target/queue binding | presence is a hard failure |
| Must exist only on twin | `FIRESTORE_DATABASE=sitesift-certification`; exact Secret Manager reference `CERTIFICATION_FIXTURE_CONFIG=sitesift-certification-fixture-config:<positive-decimal-version>`; certification route/audience and capture/deny mode | missing, alias/nondecimal/disabled/destroyed version, extra-keyed, wrong-type, or wrong reference is a hard failure |
| Must differ exactly | service/revision name; service account (`sitesift-certification-runtime@email-automation-cache.iam.gserviceaccount.com`); platform-injected `K_SERVICE`; traffic target (twin is never production traffic) | normalize to named sentinels only after validating exact expected values |
| IAM must differ | twin runtime: exact certification DB/fixture Sheet/private folder, AI-secret plus fixture-config-secret access, invoke only `certifyCampaignInput`, no production/mailbox/send/queue/public-permission role; operator: invoke twin and read only exact fixture-config secret, no DB/Sheet/Drive/AI/queue/send role; input SA `sitesift-certification-input@...`: exact fixture-config secret + fixture Sheet read only, no DB/write/Drive/AI/production role; existing release principal resolved from agreeing controller constants: resource-level Token Creator on operator only; candidate unchanged | any other invoker, direct user token, project-wide/broad role, unknown secret, operator DB write, input-SA write/production access, or twin production authority is a hard failure |

The comparator first captures exact plain maps, converts environment arrays to duplicate-free exact-string name maps, and validates every required equality, omission, addition, identity, IAM grant, denied production probe, fixture-secret version matching `^[1-9][0-9]*$`, and independently read lowercase SHA-256 from the shared `SiteSift canonical JSON v1` helper. Candidate-only values must also match the separately frozen reviewed candidate baseline. It rejects `latest`, aliases, disabled/destroyed versions, a version/digest mismatch, duplicate or non-plain keys, and any unknown asymmetric field. It then canonicalizes the two maps: for every validated candidate-only path it replaces the candidate value and inserts the same named sentinel on the twin; for every validated twin-only path it does the inverse; and for each exact identity difference it replaces both values with one paired sentinel. No asymmetric key is merely deleted, and no unclassified key is normalized. After deterministic reserialization, the final deep comparison therefore sees only approved paired sentinels plus fields that must be byte-for-byte equal. Update this matrix and its hostile tests in the same reviewed planning successor before allowing another difference.

- [ ] **Step 1: Write RED contract cases**

Ordinary stage remains untagged at 0%. The twin deployment accepts only the already built canonical repository-at-digest candidate image and exact source/image env bindings. Its runtime, operator, input-adapter SA, and resolved release-principal IAM must equal the locked matrix. The operator has no database writes; `/prepare` under the twin runtime owns authorization/input/ledger writes. The input Function has fixture-read-only authority. Fixture IDs live only in the immutable numeric secret, never request/repository/alias. Independently read version/digest through the operator and bind them with the in-image registry digest to identity/authorization/evidence/invalidation.

RED exact IAM/config cases include wrong/direct caller; wrong email/sub/aud/iss/unverified OIDC; broad Token Creator/Secret/DB role; operator DB write; input-SA DB/write/production access; runtime production/mailbox/send/queue/public-permission access; alias/nondecimal/disabled/destroyed/wrong secret version; digest disagreement; forged source/image env; unknown asymmetric config; unpaired allowed config; and one exact valid matrix. Missing revision, mutable image/secret alias, fixture/registry digest drift, candidate traffic, queue/tasks/switch/lock drift, or any unknown difference blocks proof and promotion.

- [ ] **Step 2: Run RED**

```bash
python3 -B -m unittest -v \
  tests.test_process_user_tagless_staging_contract \
  tests.test_process_user_phase1_rollout_contract \
  tests.test_process_user_production_deploy_contract \
  tests.test_production_certification.CertificationTwinDeployTests
```

Expected: new exact-image twin cases fail while all existing tagless-stage and rollout tests remain green.

- [ ] **Step 3: Implement the minimal exact-image twin lifecycle**

Do not change ordinary stage order. Implement and test support for one immutable digest to be offered to both surfaces, but do not perform the ordinary-service stage in this task. Baylor alone runs that 0% `process-user` staging command in Task 14; the agent then verifies it read-only and may deploy the same digest to the IAM-private fixture-only twin. Add the twin-stamp identity/config checks to the existing under-lock authorization slice before any production traffic change. The twin is never a production traffic target.

- [ ] **Step 4: Run GREEN and mutation controls**

Run the same four modules. Temporarily mutate each of image equality, certification service-account exactness, IAM denial, canonical config allowlist, candidate-traffic check, private URL identity, stamp binding, and pre-promotion revalidation to prove a targeted test fails; restore production bytes after each mutation.

- [ ] **Step 5: Commit**

```bash
git add \
  scripts/deploy_certification_twin.sh \
  deploy/cloudrun-certification-service.yaml \
  scripts/deploy_process_user.sh \
  scripts/phase1_rollout.py \
  tests/test_process_user_tagless_staging_contract.py \
  tests/test_process_user_phase1_rollout_contract.py \
  tests/test_process_user_production_deploy_contract.py \
  tests/test_production_certification.py
git commit -m "feat: deploy an exact image certification twin"
```

### Task 13: Whole-source verification and independent review

- [ ] **Write the image-source RED.** Create `tests/test_certification_image_manifest.py` before production edits. Require missing manifest, omitted/added/changed source, host/image mismatch, `.dockerignore` drift, unsorted paths, self-inclusion, and a tested-vs-staged digest mismatch to fail. Run `python3 -B -m unittest -v tests.test_certification_image_manifest` and record the expected missing-module/manifest failures.
- [ ] **Implement the narrow image-source gate.** Create `email_automation/certification/image_manifest.py` and `scripts/verify_image_source_manifest.py`; modify only `Dockerfile` to write `/app/.sitesift-source-manifest.json` after `COPY`. The manifest is canonical sorted `{path,size,sha256}` for every regular deployable `/app` file except the manifest itself and interpreter caches. The verifier computes the same set from the reviewed checkout under exact `.dockerignore` semantics and fails on added, omitted, or changed image bytes.
- [ ] **Run GREEN and commit this baby step.** Run `python3 -B -m unittest -v tests.test_certification_image_manifest`, py-compile both Python files, run `git diff --check`, then explicitly stage only `Dockerfile`, `email_automation/certification/image_manifest.py`, `scripts/verify_image_source_manifest.py`, and `tests/test_certification_image_manifest.py`. Commit `feat: bind certification tests to image source bytes` before continuing the whole-source matrix.
- [ ] Run focused certification tests.
- [ ] Run every changed subsystem's existing neighbor suites.
- [ ] Run the established release-control suites.
- [ ] Run both supported `py_compile` interpreters where dependencies permit.
- [ ] Run `tests.test_certification_canonical_json` under the current interpreter and the exact Python 3.12 production image; fixed vectors and digests must be byte-identical.
- [ ] After all local commits/reviews, stop before public Git push and print the exact branch push command for Baylor. Do not run it. Resume only after read-only local/upstream/remote equality proves the manual push completed.
- [ ] Invoke the one approved private artifact build exactly once from that pushed SHA, resolve one canonical `repository@sha256` digest, and pull that exact digest. Record its source revision and in-image source manifest. Never rebuild for staging.
- [ ] Assert Python 3.12 and run certification/runtime/message/service plus every changed business-neighbor suite against baked `/app` source. Mount only exact tests/test-data read-only under `/app/tests` (and another explicitly listed non-source test-data path if a RED proves it necessary); never mount the checkout source or run from `/workspace`:

```bash
docker run --rm --entrypoint python \
  "$TESTED_IMAGE_DIGEST" \
  -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
docker run --rm \
  --env E2E_TEST_MODE=true \
  --env PYTHONPATH=/app \
  --entrypoint python \
  --volume "$PWD/tests:/app/tests:ro" \
  --workdir /app \
  "$TESTED_IMAGE_DIGEST" \
  -B -m unittest -v \
  tests.test_production_certification \
  tests.test_certification_canonical_json \
  tests.test_automation_runtime \
  tests.test_message_transport \
  tests.test_process_user_service
```

  Add each changed production neighbor to this exact-digest gate. Run the image-manifest verifier before tests. A local pass cannot replace it. RED a mismatch between `TESTED_IMAGE_DIGEST` and the digest later offered to staging/twin; staging must reject before deploy.
- [ ] Run `git diff --check`, exact path scope, secret/PII scans, and clean-status proof.
- [ ] Mutation-test the paired-sentinel comparator and fixture binding: leave one validated candidate-only or twin-only field unpaired, normalize one unknown field, accept `latest`, skip the second version/digest read, or change the safe digest while retaining the version; each mutation must fail a focused test.
- [ ] Obtain independent spec and quality reviews. Any P0/P1 blocks; any valid P2 receives TDD before deployment.
- [ ] Commit only review-driven tests/fixes as new successors; never amend historical RED/GREEN commits.

### Task 14: Stage the exact immutable candidate

- [ ] Re-prove manual public-push parity and require the one already built/tested `TESTED_IMAGE_DIGEST`; a different or newly built digest fails before deploy.
- [ ] The agent must not deploy the ordinary `process-user` candidate, even at 0%. Print one exact Baylor-run existing staging-contract command using `TESTED_IMAGE_DIGEST`, `SITESIFT_SOURCE_REVISION`, and `SITESIFT_IMAGE_DIGEST`; stop before executing it. Resume only with read-only Cloud Run/provider reads proving Baylor staged that same digest untagged at 0% without a rebuild.
- [ ] Independently verify identity, digest, configuration parity, private IAM, readiness, queue/switches/lock, and ordinary native-image gate state.
- [ ] Deploy the exact same repository-at-digest image to private `process-user-certification` under its fixture-only service account; do not rebuild.
- [ ] Prove private IAM and exact OIDC claims; runtime has fixture-only access, operator has twin invoke + exact fixture-secret read but no DB write, input SA has exact fixture read only, and the resolved local release principal has resource-level Token Creator only. Bind registry/secret/source/image identities and locked paired-sentinel config comparison.
- [ ] Use `/prepare` to create one scenario/twin/candidate/image-bound PREPARED run; do not mutate either serving revision configuration.
- [ ] Do not enable real mailbox delivery or browser actions.

### Task 15: Run the non-capability production bootstrap proof

- [ ] Require `campaign-one-property` to match the manifest bootstrap, retain `capabilityStamp=false`, create a fresh run ID through `/prepare`, and invoke it on the exact twin/candidate digest.
- [ ] Require one correct captured outreach message and exact state writes.
- [ ] Require zero Graph delivery, zero non-fixture writes, and zero global-counter effect.
- [ ] Replay the same stimulus and require zero additional effect.
- [ ] Clean up and require zero residue.
- [ ] Create a different run ID and fresh PREPARED records for manifest `campaign-one-property-impossible-oracle`; require `FAIL` and `capabilityStamp=false`. No request may supply an oracle.
- [ ] Independently review the sanitized stamp.
- [ ] Record only certification-instrument readiness; do not mark or advance any capability from this bootstrap result.
- [ ] Under the rollout lock, directly reread candidate/twin image equality, candidate and rollback identities, strict configuration comparison, production and certification IAM, queue/task, switches, health, traffic/tag topology, and both lock snapshots before promotion.
- [ ] Prepare a third fresh run ID/input/authorization exclusively for post-promotion smoke; never reuse either consumed bootstrap run. Put its `/run`, direct production source/image/config/IAM/traffic/health readbacks, and failure handling inside the fenced rollout controller.
- [ ] The agent does not change production traffic. It prints the exact controller command for Baylor. The user-launched controller promotes only if prechecks pass; on any post-promotion smoke/readback failure it immediately restores the captured prior revision, verifies rollback identity/image/config/traffic/health, records `FAIL`, and retains the lock/manual-recovery state when rollback proof is incomplete. The agent resumes with read-only verification after the command completes.

## Phase F — Capability packs, strictly one at a time

For every pack, repeat: finite registry scenarios → RED → minimal shared-code change → local neighbors → commit → independent review → manual public-push handoff → read-only parity → one immutable private build → Baylor-manual exact-image 0% ordinary candidate plus agent-safe private twin → fresh PREPARED run per scenario → required/forbidden readbacks → replay → cleanup → stamp. Deterministic agent-safe scenarios continue automatically. A model scenario prepares exactly three fresh run IDs and prints one user-runtime command that executes all three; all must independently pass with identical prompt digest and resolved provider model/fingerprint, never averaging. The agent resumes from sanitized evidence. Human-naturalness rows enter `AWAITING_REVIEW`: the Baylor-manual CLI returns the exact bounded ordered set of every synthetic subject/body, displays it only locally, binds all body digests plus rubric version, accepts exactly one safe verdict/reason per item, then cleanup proceeds; until then the row is `INSTRUMENT_BLOCKED:user_review_required`.

When deployable identity changed, use fresh run IDs for pre- and post-promotion proof. The agent never pushes or changes production traffic; Baylor runs the exact push/controller commands. The fenced controller must roll back and verify the prior revision on failed post-promotion smoke/readback. The agent then proves parity and production state read-only. When identity did not change, directly prove the stamp binds to current production; do not promote. Invalidate affected predecessor stamps, rerank, and continue. A 0%-candidate-only stamp never satisfies current-production completion.

### Immutable cross-repository source rule

The `spreadsheet-admission` pack also uses `/Users/baylorharrison/Documents/GitHub.nosync/email-admin-ui`. Never touch its dirty default checkout. Create a clean worktree at baseline SHA `2ad02ee2b9bfad9d331d50dbfe341742159404b2` and use Node 20. Create exactly `functions/certificationInput.js`, `functions/certificationInput.test.js`, `scripts/read-production-certification-identity.mjs`, and `scripts/read-production-certification-identity.test.mjs`; modify only `functions/index.js`, `functions/package.json`/lock if required, and the exact Firebase function config. RED tests cover closed schema, wrong caller, arbitrary workbook/UID/resource fields, nonce/expiry/source drift, response replay, payload mutation, Unicode/key-order/integer vectors, denied production reads/writes, provider-identity mismatch, and canary leakage through every `console.*`/framework error path. GREEN runs exact Node tests, lint/compile, and fake-provider identity reads before local commit/review. Post-run verification queries the exact adapter Function/Cloud Run revision logs and requires no fixture payload/canary/token/raw error.

`certifyCampaignInput` runs under `sitesift-certification-input@email-automation-cache.iam.gserviceaccount.com`, private invoker only from the certification runtime SA. It reads only the exact fixture-config secret and fixture Sheet, has no database/write/Drive/AI/mail/send/queue/production authority, and accepts exact `{scenarioId,runId,sourceRevision,nonceDigest,expiresAt}`. It is stateless: the short-lived server nonce is bound to backend `PREPARING`; backend CAS accepts the validated response once, so adapter response replay is harmless and rejected without giving the Function impossible database authority. It constructs the campaign command with existing mapping/safety helpers and returns bounded payload plus comparison-only digest/source identity. Python recomputes the authoritative canonical input digest.

After review, make a coordinated backend successor that replaces manifest `frontendCertificationSourceAnchor:null` with the exact frontend successor SHA. The agent does not push either public repo. It prints exact push commands and resumes after read-only parity. There is no existing Functions release workflow: Baylor manually runs one reviewed command pinned to Firebase Tools `14.27.0`, Node 20, project/region/service account/private invoker, and `--only functions:certifyCampaignInput`. It must not deploy Hosting or another Function. Provide exact rollback/removal command and pre/post source/package/revision/image/IAM reads. Missing/mismatched adapter identity is `INSTRUMENT_BLOCKED`.

Artifact expectations are split. Hosting and every pre-existing Function remain frozen to independently captured pre-change live identities. The live Hosting release/version full file map must remain the baseline: read provider `files[].path` and `files[].hash`, validate the official hash encoding, normalize to the same lowercase hexadecimal SHA-256 representation used by `.firebase/hosting.YnVpbGQ.cache`, sort canonical `{path,hash}` records, and require digest `36e84117a21d2bcaad1db504ec71c35d9daadda6a88fa377b924c94cfc74b6d3`; fixed vectors cover provider encoding and malformed shapes. Both public manifests remain byte-identical. Existing Gen2 source/package/revision/image identities and the Gen1 `api` archive manifest must equal the frozen baseline and must not be redeployed. Only `certifyCampaignInput` must match the reviewed successor package/source/revision/image from independent provider reads; self-report and `functions:list` never establish identity. Any unavailable identity is `INSTRUMENT_BLOCKED`.

### Executable pack map

Each pack starts by freezing the finite scenarios in the named test modules. Inspect only the listed seams plus a directly called helper proven by the RED. If a required production seam or live artifact falls outside the row, add the evidence to the backlog, fail closed, and update this plan/frontier before editing it.

| Order | Capability | Production seams | Required focused/neighbor tests | Production stamp focus |
|---:|---|---|---|---|
| 1 | `spreadsheet-admission` | UI: `src/components/AddClientModal.jsx`, `src/components/ColumnMappingStep.jsx`, `src/components/StartProjectModal.jsx`, `src/utils/campaignAskFields.js`, `functions/initialScriptSafety.js`, exact campaign-command writers in `functions/index.js`; backend: `email_automation/column_config.py`, `email_automation/sheets.py` | exact `spreadsheet-admission.existingTestModules` plus its required new REDs in the approved scenario manifest | canonical campaign command, aliases/custom columns, formulas, invalid/duplicate/blank rows, multi-tab handling; no DOM claim |
| 2 | `authoritative-field-contract` | `email_automation/column_config.py`, `email_automation/ai_processing.py`, `email_automation/processing.py`, UI `src/utils/campaignAskFields.js` | exact `authoritative-field-contract.existingTestModules` plus its required new REDs in the approved scenario manifest | exact known/missing/optional/skipped set and order; no known/formula/declined re-ask |
| 3 | `initial-outreach-quality` | `email_automation/email.py`, `email_automation/outbound_safety.py`, whitelabel/signature helpers called by `send_outboxes()` | exact `initial-outreach-quality.existingTestModules` plus its required new REDs in the approved scenario manifest | correct recipient/property/questions/signature; natural wording; no placeholder/BCC/duplicate |
| 4 | `thread-property-binding` | `email_automation/processing.py`, `email_automation/messaging.py`, canonical source envelope in `email_automation/message_transport.py` | exact `thread-property-binding.existingTestModules` plus its certification scenarios in the approved manifest | exact/negative/changed-subject/same-broker/multi-property binding; ambiguity produces review only |
| 5 | `text-extraction-sheet-integrity` | `email_automation/ai_processing.py`, `email_automation/processing.py`, `email_automation/sheets.py`, `email_automation/sheet_operations.py` | exact `text-extraction-sheet-integrity.existingTestModules` plus its certification scenarios in the approved manifest | partial/correction/conflict/vague/range/decline; correct row after reorder; formulas/human values/siblings preserved |
| 6 | `property-decision` | `email_automation/processing.py`, `email_automation/messaging.py`, terminal-state notification/audit helpers | exact `property-decision.existingTestModules` plus its certification scenarios in the approved manifest | exactly viable, non-viable with reason, or review; coherent Sheet/thread/audit/clocks |
| 7 | `natural-reply-closure` | `email_automation/ai_processing.py`, `email_automation/processing.py`, `email_automation/email_operations.py` and final rendering helpers | exact `natural-reply-closure.existingTestModules` plus its required naturalness REDs in the approved manifest | concise human English, only remaining questions, no confidential answer/promise; every finite output gets explicit human-naturalness verdict |
| 8 | `pdf-and-link-understanding` | `email_automation/file_handling.py`, `email_automation/property_images.py`, `email_automation/processing.py`, `email_automation/ai_processing.py` | exact `pdf-and-link-understanding.existingTestModules` plus its certification scenarios in the approved manifest | native text, scan, link, mixed/wrong/multi-suite/private cases; asset-type-specific stamp and quarantine |
| 9 | `operator-actions` | `email_automation/email.py`, `email_automation/operator_replay.py`, reply-review action handlers, `service.py` | exact `operator-actions.existingTestModules` plus its certification scenarios in the approved manifest | approve/edit/stop/resume as canonical parameters; correct thread and exactly-once effect; no browser claim |
| 10 | `followup-and-stop-controls` | `email_automation/followup.py`, shared outbound transport, stop/cancel/opt-out authority helpers | exact `followup-and-stop-controls.existingTestModules` plus its required fixed-clock REDs in the approved manifest | fixed clock before/at/after due; no post-reply/manual/terminal/opt-out send |
| 11 | `retry-reorder-recovery` | `email_automation/scheduler_lease.py`, `email_automation/sent_mail_guard.py`, `email_automation/processing.py`, `email_automation/dead_letter_recovery.py`, `email_automation/pending_responses.py` | exact `retry-reorder-recovery.existingTestModules` plus its certification scenarios in the approved manifest | fault every durable boundary; duplicate/reordered events and lease collision converge once or fail visibly |
| 12 | `whole-scrub` | certification scenarios/runner plus only previously stamped shared seams | exact `whole-scrub.existingTestModules` plus the planned production scenario module in the approved manifest | integrated multi-property campaign, expected decision per property, minimal communication, zero-effect replay and zero residue |

Native images remain a separate `NOT_TESTED` pack until authoritative complete-address binding is approved and enabled. Optional Graph/browser shells come last and can never substitute for a core stamp.

## Frontier and checkpoint protocol

After every stamp or blocked run:

1. Append new defects or ideas to the full backlog with evidence and business impact.
2. Score them by safety, core completion, correctness, visibility/reliability, language, cost, then adjacency.
3. Keep exactly one active capability and at most one instrumentation blocker.
4. Do not delete or bury lower-priority work.
5. Do not interrupt the active stamp unless a new item has a recorded higher safety or direct user-value score.
6. Write a Brain checkpoint and, if the session must stop, a resume handoff naming the exact next RED or production readback.

The executor does not pause merely because a phase or stamp completed. It reranks and continues. It stops only at the already-declared Baylor-manual boundaries (public Git push, customer/shared deployment or production traffic, real model invocation, raw naturalness review), a genuinely new external-contact/destructive authority, unavailable ownership fact, or material product choice. At a declared boundary it leaves a sanitized exact command and resumable state; it never simulates completion.

When context or usage capacity forces a stop, commit all verified progress, leave unsafe partial production changes disabled, and write a resume handoff that includes:

- current local/upstream/deployed revisions;
- active capability and rank reason;
- completed RED/GREEN/stamp receipts;
- any invalidated predecessor stamps;
- exact clean/dirty paths;
- exact next command; and
- the same terminal condition: continue autonomously to the whole-product stamp.

## Completion criteria

The program is complete only when the current production revision and its exact-image certification twin are bound to green stamps for every enabled core capability and the whole-scrub pack, with zero wrong-row/recipient effects, zero unsupported facts, formula preservation, natural finite messages, exactly-once replay/retry behavior, actionable failures, and zero certification residue. Disabled or intentionally deferred lanes remain explicitly `NOT_TESTED`; they are never inferred from neighboring proof.
