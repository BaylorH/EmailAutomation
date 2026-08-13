# CE-Q1 Conversation and Extraction Qualification Gate Design

**Status:** Approved by the user on 2026-08-13; implementation planning and building authorized in this task

**Deliverable:** both

**Production-source ancestor:** `6caa8ec14cc525299cfb8ed13bdd219f35c4322b`

**Implementation base:** `b400ee5ad55ac75203da6a53730c4a134cad79e5`

## Goal

Build a trustworthy, effect-free qualification gate for the SiteSift behaviors
that have caused the most user harm: repeated questions, lost corrections,
wrong-property or wrong-suite facts, numeric and unit errors, unsupported
terminal decisions, mishandled alternate properties or broker questions, and
robotic conversation copy.

The gate must pressure those behaviors without sending mail, calling a model or
mailbox provider, touching production, opening either global campaign switch,
or treating an ordinary user as a test harness.

## Decision

Build CE-Q1 as two closed, sequential qualifications rather than one broad E2E
test. The first qualification contains three evidence levels that may not be
collapsed into one another:

1. **CE-Q1A — deterministic replay and state qualification.** Run sanitized
   user-bug fixtures through the real production parsing, guard, binding,
   lifecycle, and write-planning code using frozen provider proposals. `L1`
   proves semantic behavior, `L2` proves exact state-unit behavior through
   strict in-memory adapters, and `L3` proves Firestore persistence and replay
   behavior in a newly controlled local emulator boundary. External network
   and production-client construction are fatal. CE-Q1A is the work authorized
   by this design.
2. **CE-Q1B — pinned-model shadow, separately approved later.** After CE-Q1A
   passes, a new authorization may allow the pinned model to generate proposals
   over the same sanitized corpus while every persistence and communication
   effect remains disabled. CE-Q1B can report only the observed behavior and
   current natural-language quality of its declared attempts; it is not a
   statistical proof of general model repeatability.

CE-Q1A may prove that deterministic safety boundaries, state transitions, and
the evaluation instrument work. It cannot prove that a live model will produce
the right proposal, that the deployed service currently behaves correctly, or
that a recipient sees natural copy.

The implementation base is the reviewed `b400ee5` descendant of production
source `6caa8ec`. It retains the production behavior lineage and adds the #84
lazy-provider boundary needed to prove that test collection and import do not
construct Firestore, Firebase, OpenAI, MSAL, or other provider clients. This
design does not merge or deploy that branch.

The first implementation produces the qualification instrument and an honest
baseline finding. It does not silently repair product failures in the same
change. A reproduced product failure gets its own TDD change and independent
review, followed by a complete clean CE-Q1 rerun; a failing scenario is never
removed, waived, or rewritten to match current behavior.

The `b400ee5` baseline is expected to be red or unverified in several places:
there is no durable explicit-decline memory, no product-observable per-fact
provenance, no deterministic suite identity, no shared pure finalizer for all
five voice contexts, and missing-field voice is intentionally one fixed
template. A truthful `FAIL`/`UNVERIFIED` report is success for the initial
instrument build; it identifies the next bounded product TDD work. It is not an
invitation to weaken the gate until the candidate turns green.

## Why this approach

### Selected: layered replay, then stateful qualification, then a later model shadow

This separates three questions that otherwise create false confidence:

- Did the model propose the right meaning?
- Did deterministic code reject unsafe meaning and bind accepted facts exactly?
- Did state and lifecycle effects occur exactly once in the right place?

Each layer receives a closed input and produces a separately scored output.
Failures retain their layer identity instead of being averaged into one quality
number.

### Rejected: jump directly to a self-owned live mailbox canary

A small live case would repeat already-proven happy paths while leaving corpus
breadth unknown. It would also combine model, provider, mailbox, state, and
delivery effects, making a failure harder to localize. No exact self-owned
recipient has been named for this gate, and no live effect is authorized.

### Rejected: model-only prompt evaluation

Model-only evaluation can score extraction and prose, but it cannot prove
wrong-row prevention, terminal ordering, idempotency, pause/resume, or absence
of outbox effects. Prior live-model sweeps also demonstrated that a permissive
substring scorer can substantially misgrade results. A model proposal is not a
committed state result.

### Rejected: one weighted quality score

Natural prose cannot compensate for a wrong value, repeated question, wrong
property, unsupported terminal decision, or duplicate action. Safety and
grounding are binary hard gates. Voice is a separate review surface evaluated
only after those gates pass.

## Evidence boundary

Historical live evidence remains useful as fixture provenance, not as a pass on
the new candidate:

| Identity | Evidence label | Permitted claim |
| --- | --- | --- |
| `c6dbe4a` / `62a7d59` | `HISTORICAL_BOUNDED_LIVE` | A narrow thirteen-message correction/pause/resume sequence, mixed-property PDF abstention, settled replay, and earlier exact-value canaries worked in those bounded runs |
| `6caa8ec` | `RECORDED_PRODUCTION_SOURCE` | Brain FDR-041 records this source as deployed with authenticated-health-only proof; CE-Q1 does not independently contact production to re-prove it |
| `b400ee5` | `PROPOSED_SOURCE_ONLY_BASE` | Reviewed lazy-provider/source work is a suitable offline qualification base; it is not merged, deployed, or live-certified |

`ai_processing.py`, `column_config.py`, and `followup.py` retain the relevant
historical source ancestry, but `processing.py`, `messaging.py`, and
`pending_responses.py` changed after the historical live runs. Historical
observations may seed newly fictionalized fixtures and narrowly support the
unchanged extraction observations. They cannot mark the current or proposed
full pipeline passed by ancestry alone.

CE-Q1 records every result against the exact source commit and the hashes of the
owner modules it exercised. A relevant owner-module change invalidates that
result.

## Scope

### Included

- Sanitized synthetic fixtures derived from the established user-bug families
  `EXT-01` through `EXT-06`, `IN-09`, and `IN-10`.
- Known-fact memory, explicit decline, corrections, stale or quoted history,
  histories longer than ten messages, and exact missing-field questions.
- Property and suite binding, including same-address multiple suites and mixed
  documents.
- Rent versus operating-expense semantics, monthly versus annual basis,
  corrections, ranges, and digit decoys.
- Target-bound evidence for terminal decisions.
- Alternate-property proposals, broker questions, pause/resume, terminal
  ordering, and idempotent replay.
- Draft-only voice contexts: launch, missing-field request, correction-close,
  follow-up, and monitored continuation.
- Local state, write-plan, audit, review-action, and formula outcomes.
- Exact proof that collection and execution create no external effect.

### Excluded

- OpenAI or any other model-provider call in CE-Q1A.
- Microsoft Graph, mailbox, Drive, Google Sheets, production Firestore, Cloud
  Tasks, Cloud Run, or any non-loopback network call.
- Outbox creation, draft creation, mail send, recipient testing, campaign
  creation, initial dispatch, automatic follow-ups, or `/process-user` calls.
- Global or per-user campaign-access changes.
- Ordinary-user observation, multi-user operation, broad rollout, or formal
  Release A.
- UI hardening, Campaign Readiness integration, Phase 2 reply-review controls,
  and unrelated baseline test debt.
- A claim that replayed or historical drafts establish current-model voice.

## Artifacts

CE-Q1 implementation adds a small, versioned qualification package:

1. `docs/release-safety/ceq1-execution-manifest.json` — the hand-maintained
   scenario registry and input/response/owner-module hashes. It contains no
   expected state, expected verdict, sabotage mapping, or oracle values.
2. `tests/fixtures/ceq1/inputs/` — synthetic messages, documents, frozen
   initial state, and runtime configuration.
3. `tests/fixtures/ceq1/responses/` — hash-addressed frozen provider responses,
   stored separately from inputs and oracles.
4. `tests/fixtures/ceq1/oracles/` — sealed expected facts, effects, first/replay
   state, and `coverage-contract.json`, readable only by the scoring process
   after execution has ended.
   Committed fixtures use newly fictionalized `.invalid` identities and
   fictional properties only. Raw FDR, Gmail, provider, or production payload
   text may not be copied into them.
5. `tests/ceq1/harness.py` — a pure execution coordinator that receives only
   input and frozen-response bundles and calls explicit production seams. It
   cannot open the oracle bundle and owns no credentials, SDK clients, global
   mutable provider state, or network transport.
6. `tests/ceq1/scorer.py` — a separate scoring process that receives the sealed
   execution result plus oracle after the SUT process has exited. It cannot
   import or invoke product code.
7. `tests/test_ceq1_manifest.py` — schema, closure, sanitization, coverage, and
   oracle-sabotage tests.
8. `tests/test_ceq1_semantic_replay.py` — L1 proposal, binding, extraction,
   event, draft-obligation, and hard-safety tests.
9. `tests/test_ceq1_stateful_replay.py` — `L2` state transition, exact-write,
   formula, review, lifecycle, and idempotency tests through strict in-memory
   adapters.
10. `tests/test_ceq1_emulator_replay.py` — `L3` loopback Firestore persistence,
   namespace, transaction, cleanup, and replay tests.
11. `scripts/run_ceq1.py` — the outer fail-closed supervisor and report writer.
12. `docs/release-safety/evidence/ceq1/` — generated, sanitized JSON and Markdown
   reports. Reports contain scenario IDs, source hashes, status, and redacted
   diffs, never raw customer data, credentials, provider payloads, or recipient
   addresses.

No CE-Q1 source may be imported by the production worker. A static dependency
test enforces one-way imports: qualification code may call production seams;
production modules may not import qualification modules or fixtures.

The execution and scoring bundles are capability-separated by filesystem
mounts, not by convention. The SUT container does not receive the repository
tree or full execution manifest. It receives an allowlist-generated read-only
projection of only the exact product modules and execution harness, one minimal
descriptor `{scenarioId, layer, inputHash, responseHash}`, and read-only input
and response mounts. It receives no oracle, coverage-contract, docs, or other
test-fixture path. After it exits, the supervisor seals and hashes the result;
only then does a fresh scorer process receive the result and read-only oracle.
Mutation controls are generated from the closed result schema in a third process
that receives neither product code nor expected values.

The existing `tests/test_harness.py`, provider-backed `quality_benchmark.py`,
manual/live scripts, and `tests/conversations/generated/index.json` are not gate
inputs. The first copies expected updates into observed output, the second calls
a provider and uses shallow substring scoring, the manual scripts are effectful,
and the generated index contains nonportable absolute paths whose referenced
fixtures are absent. Existing in-memory doubles may be extracted and hardened,
but `tests/test_full_campaign_e2e.py` itself is not qualification evidence: it
has inherited failures and intentionally exercises fake outbox, send, and
follow-up effects that CE-Q1 forbids.

## Implementation architecture

The runner reuses production seams deliberately rather than inventing a second
conversation pipeline:

- The existing credential-absent collection tripwire is extracted into an
  execution-long guard and extended to every constructor and effect boundary
  named below.
- Raw fictionalized message history first passes through the actual
  `build_conversation_payload(..., limit=10)` projection with strict
  Firestore/Graph adapters. Only that exact emitted payload may enter
  `propose_sheet_updates()` for a history-qualified case.
- `propose_sheet_updates()` receives that runtime-projected conversation and a
  strict queue-backed replacement for
  `ai_processing.client.responses.create`. The replacement releases only the
  frozen response whose prompt/config hash matches the manifest and rejects any
  extra call.
- Native-text PDF cases enter through the real local attachment parser and
  page/property segmentation path. The runner does not inject oracle page text
  after the parser boundary.
- The actual proposal post-processors, property/attachment guards, event
  filters, and `apply_proposal_to_sheet()` write planning remain in path.
- The actual authoritative post-write missing-field recomputation,
  `_select_automatic_response_body`, and context-specific finalization seam
  remain in path; the runner never grades a convenient intermediate
  `proposal.response_email` when production would replace it.
- L2 uses closed in-memory Firestore/Sheet/Graph adapters with exact method
  allowlists and operation receipts. L3 replaces only the Firestore state
  adapter with the validated task-owned emulator client.

Helper-level calls are labeled `SEAM_UNIT` only. Every pipeline-required
scenario enters through the real `process_inbox_message()` entrypoint with
strict adapters for Graph reads, Firestore state, Sheets, attachment/URL
content, and the frozen model response. This preserves production authority
filtering, header/body handling, history projection, proposal invocation,
write/event ordering, lifecycle decisions, and response suppression. The
qualification coordinator supplies dependencies and records effects; it does
not reimplement or reorder orchestration.

The pipeline-required set is exact: `CEQ-LONG-01`, `CEQ-MEM-01`,
`CEQ-TERM-01`, `CEQ-TERM-02`, `CEQ-SUITE-01`, `CEQ-PDF-01`, `CEQ-ALT-01`,
`CEQ-IN-09`, `CEQ-IN-10`, `CEQ-WRONG-01`, `CEQ-OOO-01`, and
`CEQ-AUDIENCE-01`. A future behavior-preserving production-owned pure
orchestration seam may replace direct entrypoint execution only after
characterization tests prove byte-for-byte/effect-for-effect equivalence and
runtime itself calls that seam. Until then, a scenario that bypasses the
entrypoint cannot satisfy a pipeline claim.

Reply-capable pipeline scenarios call the entrypoint with
`allow_outbound_reply=True` and the real paused outbound policy so the runtime
must make and record its own reply/suppression decision. `False` is permitted
only for a separately labeled extraction-unit diagnostic. Audience and final
rendered-draft claims still require the shared pure finalization seams below;
paused suppression alone cannot certify metadata that runtime has not yet
materialized.

At `b400ee5`, a paused send is not effect-free at the state layer: after
`send_reply_in_thread` returns `suppressed_by_kill_switch`, the failure handler
falls through to `queue_pending_response`. The baseline exact-pipeline scenarios
must run this behavior through a strict in-memory pending-response adapter,
record it as an expected source-only defect, and return `FAIL`; they may not
write a real/emulator pending-send document or call the result zero-delta. A
separate TDD product fix must add a production-owned kill-switch terminal
outcome before pending-response projection. Only a reviewed successor with that
fix may expect exactly one suppressed chokepoint, zero pending projection, zero
transport, and a hard-pass reply-capable replay.

For each execution, the runner snapshots and compares the complete target row,
formulas, provenance, every sibling row, thread and conversation state, review
actions, terminal actions, pending-response state, audit state, outbox/send and
follow-up namespaces, and provider-call ledger. A scoped snapshot that omits an
effect surface cannot issue a verdict.

Directly passing a manifest's complete history to `propose_sheet_updates()` is
allowed only for a proposal-unit diagnostic labeled `BYPASSED_HISTORY`; it
cannot satisfy `CEQ-LONG-01`, `CEQ-MEM-01`, EXT-01, or any production-runtime
claim. Those cases seed the strict history adapters, exercise deduplication,
direction-aware ordering, quote stripping, and the production ten-message
window, and then grade only the emitted payload plus durable row/thread memory.
If an accepted correction or decline falls outside the window and is not
durably represented, the case must fail rather than receiving richer fixture
context.

Current runtime paths do not expose a common pure final rendered draft: the
extraction-only path returns before response selection, and the paused-send path
returns before signature/footer rendering. CE-Q1 may not reconstruct that output
inside its scorer. Before a voice case can be eligible, a separately reviewed,
behavior-preserving seam extraction must make these production-owned functions
authoritative:

- `finalize_launch_draft`;
- `finalize_missing_field_draft`;
- `finalize_correction_close_draft`;
- `finalize_followup_draft`; and
- `finalize_monitored_continuation_draft`.

Each pure function returns a closed `FinalDraft` record containing subject,
plain body, HTML body, `To`, `CC`, reply mode, and signature identity. The
corresponding runtime path must call the same function immediately before its
effect boundary, with byte-for-byte characterization tests against `b400ee5`.
Any output drift stops the refactor. Until a context has this shared production
seam, its `VOICE-*` expected verdict is `UNVERIFIED` and it cannot enter blinded
review.

## Scenario contract

Every execution-manifest entry is closed and contains only stable `id`, user-bug
family, purpose, sanitized provenance label, input/response bundle hashes, and
owner-module hashes. The sealed scorer-side coverage contract owns layer,
response-class, voice eligibility, oracle hash, promotion class, expected
verdict, sabotage mapping, and explicit non-claims.

The read-only input bundle contains exact chronological messages with direction
and stable IDs; initial target and sibling rows; thread state; configured field
modes; attachments; and a synthetic clock. The separate response bundle
contains one or more frozen provider proposals, including unsafe shapes that a
deterministic guard must refuse or repair.

The sealed oracle contains:

- expected accepted facts with canonical field, value, unit or basis, source
  message ID, source span, target property and suite, freshness rule, and any
  allowed normalization or range transform;
- expected events, review actions, lifecycle transitions, final draft
  obligations, and per-layer verdict;
- forbidden facts, events, questions, recipients, commitments, and effects;
- exact complete state after the first run and replay; and
- applicable sabotage IDs and expected failure reasons.

The validator rejects extra keys, missing oracle fields, unrecognized families,
nonfictional identities, absolute paths, raw production IDs, timestamps outside
the synthetic clock, and any case without both a positive expectation and a
near-miss or sabotage assertion. Filesystem capability tests prove that oracle
data is not mounted or otherwise reachable by the SUT or frozen replay client;
a source-level assertion alone is insufficient.

## Minimum scenario deck

The initial deck must cover each row below. Existing regression inputs may be
adapted only after they pass the closed fixture validator; a test name or broad
suite is not itself coverage.

| Family | Required pressure cases | Hard outcome |
| --- | --- | --- |
| `EXT-01` | fact already supplied; fact explicitly declined; correction after message ten; acknowledgement versus question | Never ask for a known or declined fact; ask only the current missing set |
| `EXT-02` | supported terminal statement; stale quoted terminal; wrong-property terminal; ambiguous statement | Terminalize only with a fresh target-bound citation; otherwise retain state and review |
| `EXT-03` | same address with two suites; mixed-property PDF; mixed-suite PDF; exact target-only attachment | Bind every value and terminal signal to one entity or abstain with one review action |
| `EXT-04` | rent 14 / OpEx 4; monthly versus annual; corrected numeric; range and digit decoy | Preserve field, value, and basis exactly; never mine rent as OpEx or vice versa |
| `EXT-05` | complete non-viable transition; injected write failure; target comment column beyond `Z` | Ordered operations are receipted; any partial outcome is detected, blocks false completion, and remains visibly retryable |
| `EXT-06` | target rejected with viable alternate; alternate mentioned but unavailable; two alternates | Produce one target-bound actionable proposal only when actually offered and in scope |
| `IN-09` | direct broker question; confidential identity question; question plus partial specs | Answer only from allowed context or create one actionable review while retaining safe facts |
| `IN-10` | unrelated mail; quoted CRE text in unrelated mail; tracked reply near-miss | Produce no automated response or campaign-state mutation for untracked/non-CRE mail |
| autoresponse | out-of-office with dates; generic auto-acknowledgement; quoted prior CRE text | Produce no extraction, terminal decision, or automatic reply; apply the exact local reschedule/pause policy at most once |
| audience | copied-party reply-all; display-name ambiguity; wrong-tenant signature decoy | Final draft metadata preserves the exact intended recipient/CC and tenant identity without sending |
| chronology | 13+ messages; delayed inbound; monitored continuation; pause/resume; settled replay | Latest corrections win, pause holds, resume is explicit, close occurs once, replay is zero-delta |
| voice | launch; missing-field; correction-close; follow-up; continuation | Only drafts that pass every hard oracle enter blinded voice review |

Each family must include a deliberately sabotaged oracle or implementation
substitution that fails for the intended reason. A test that cannot detect its
seeded historical failure is not qualification evidence.

The first manifest must contain these stable IDs; aliases or broad suite names
do not count as execution:

| Scenario ID | Required pressure |
| --- | --- |
| `CEQ-LONG-01` | Thirteen-message correction, pause, monitored continuation, close, and same-state replay |
| `CEQ-MEM-01` | An explicitly declined required fact is durably remembered and never requested by automatic reply or follow-up |
| `CEQ-TERM-01` | Fresh target-bound terminal evidence drives receipted move/comment/highlight/audit orchestration, including a column beyond `Z`; every injected partial outcome is detected/retryable and later follow-up generation is zero |
| `CEQ-TERM-02` | Addressless, stale, quoted, or otherwise ungrounded model terminal output causes zero terminal effect |
| `CEQ-SUITE-01` | One street address, Suites 100 and 200, conflicting scalar and terminal facts |
| `CEQ-PDF-01` | Native three-page mixed-property PDF quarantine and exactly one review action |
| `CEQ-OPEX-01` | `$14 NNN` rent basis plus `$4 OpEx`, with a hostile frozen proposal |
| `CEQ-OPEX-02` | OpEx absent or pending while the frozen proposal invents a value; the value must be removed |
| `CEQ-ALT-01` | Target non-viable plus alternate property; exactly one durable actionable proposal across replay |
| `CEQ-IN-09` | Partial safe specs plus a fictional broker/franchise question; safe facts survive and exactly one escalation occurs |
| `CEQ-IN-10` | Plausible but untracked non-CRE mail; model, Sheet, action, outbox, follow-up, and reply counts remain zero |
| `CEQ-WRONG-01` | One broker owns multiple target rows and the subject drifts; only the durable target may change |
| `CEQ-OOO-01` | Fictional out-of-office and auto-acknowledgement variants produce no extraction/terminal/reply and exactly the allowed local lifecycle effect |
| `CEQ-AUDIENCE-01` | Copied-party reply-all context retains exact fictional To/CC and tenant signature metadata with zero delivery effect |
| `VOICE-LAUNCH` | Final selected launch body |
| `VOICE-MISSING` | Final selected missing-field body across repeated turns |
| `VOICE-CORRECTION-CLOSE` | Final selected correction-and-close body |
| `VOICE-FOLLOWUP` | Final selected offline follow-up body, without authorizing follow-up delivery |
| `VOICE-CONTINUATION` | Final selected monitored-continuation body |

Current code has no durable accepted/declined-fact ledger. `CEQ-MEM-01` therefore
requires either a durable answered/declined state with exact provenance or a
separately designed one-review/no-reask terminal policy; a blank, withheld, or
pending value is not an acceptable substitute for an explicit decline.

Current entity binding also has no deterministic suite identity. Until such a
binding is separately designed and proved, `CEQ-SUITE-01` must expect exact
abstention, zero scalar or terminal writes, and one review action. CE-Q1 may not
declare arbitrary multi-suite extraction qualified from address-only matching.

### Coverage closure

The sealed scorer bundle has `coverage-contract.json` whose closed records are
`{variantId, scenarioId, layers, sabotageId, promotionClass,
expectedVerdict, nonClaims}`. `promotionClass` is exactly `required` or
`diagnostic`. The validator requires exact set equality with the variant keys
below, verifies that every referenced scenario and sabotage case executed in
every named layer, and emits the count and sorted contract hash. One scenario
per family is not sufficient. The SUT never receives this file.

| Family | Mandatory `variantId` values |
| --- | --- |
| `EXT-01` | `known-filled`, `explicit-decline`, `correction-after-window`, `acknowledgement-not-question` |
| `EXT-02` | `fresh-target-terminal`, `stale-quoted-terminal`, `wrong-property-terminal`, `addressless-terminal`, `ambiguous-terminal` |
| `EXT-03` | `same-address-two-suites`, `mixed-property-pdf`, `mixed-suite-pdf`, `exact-target-attachment` |
| `EXT-04` | `rent14-opex4`, `monthly-annual`, `latest-correction`, `numeric-range`, `digit-decoy`, `unsupported-opex` |
| `EXT-05` | `ordered-success`, `move-failure`, `comment-failure`, `highlight-failure`, `audit-write-failure`, `terminal-state-failure`, `column-beyond-z`, `retry-after-partial-attempt` |
| `EXT-06` | `viable-alternate`, `alternate-unavailable`, `two-alternates`, `same-event-replay` |
| `IN-09` | `direct-broker-question`, `confidential-identity-question`, `question-plus-partial-specs` |
| `IN-10` | `unrelated-mail`, `quoted-cre-nearmiss`, `tracked-reply-nearmiss` |
| chronology | `thirteen-message-window`, `delayed-inbound-order`, `pause-hold`, `monitored-resume`, `settled-replay` |
| autoresponse | `dated-ooo`, `generic-auto-ack`, `quoted-cre-ooo` |
| audience | `copied-party-reply-all`, `display-name-ambiguity`, `wrong-tenant-signature-decoy` |
| PDF layout | `native-text-three-page`, `image-only-explicitly-unverified` |
| voice | `launch`, `missing-field`, `correction-close`, `followup`, `continuation` |

Metamorphic wording variants are additional records, not substitutes for these
keys. Missing, skipped, filtered, duplicated, or unexpectedly passing variants
make the manifest incomplete and prevent a gate verdict.

Every hard-safety and state variant is `required`. The
`image-only-explicitly-unverified` variant is `diagnostic` until an effect-free
OCR/parser path exists. Each `VOICE-*` variant is `diagnostic` until its shared
production finalization seam exists, then becomes required for the separate
voice verdict through a reviewed coverage-contract change. Diagnostic variants
must still execute and match their expected `UNVERIFIED`/non-claim; they do not
silently count as hard passes.

## Oracle hierarchy

CE-Q1 scores outcomes in this order:

1. **Binding and evidence:** subject entity, field, value, unit, freshness, and
   cited source are exact.
2. **Forbidden effects:** no cross-entity write, unsupported terminal state,
   unsafe question, or communication effect occurred.
3. **State:** target row, sibling rows, formulas, lifecycle, review surface, and
   audit state match exactly.
4. **Idempotency:** the same input and stable identity produce zero additional
   action on replay.
5. **Draft obligations:** the final production-selected and rendered body,
   including deterministic replacement copy and footer, acknowledges supplied
   facts, asks only for missing fields, makes no invented commitment, and
   contains no unsafe or placeholder text.
6. **Voice:** eligible drafts are reviewed for natural cadence and professional
   continuity only after steps 1–5 pass.

No weighted average exists. One failure in steps 1–5 fails the scenario and the
gate.

Before product cases can produce a verdict, calibration mutations must prove
that the instrument turns red for every one of these defects: extra or wrong-row
write, wrong field/value/unit/basis, quoted-only support, invented fact, known
or declined fact re-ask, uncited terminal, duplicate action, forbidden event or
send, hidden provider construction, hidden network attempt, altered output
cardinality, and changed client/guard identity. A surviving mutant yields
`INSTRUMENT_FAILURE`, never `PASS_OFFLINE`.

## CE-Q1A L1 semantic replay

The L1 runner feeds each frozen proposal through the real deterministic
post-model seams used by the worker. It must test both a safe proposal and the
historical unsafe proposal shape where applicable.

The frozen replay client is queue-based and closed: it fails on an extra or
missing call, prompt-hash drift, response-order drift, or access to the fixture
oracle. Supplying an inline conversation may avoid the production Graph and
Firestore fetches, but `dry_run=True` is not an effect boundary because the
current proposal path still calls OpenAI before suppressing later logging.

The runner compares structured records, never raw JSON substrings. Event type,
field name, value, unit, evidence pointer, action cardinality, and draft
obligations are individually typed and compared. Reserved grammar tokens such
as `response_email`, `no_updates`, and event prefixes may not fall through to
substring matching.

The following are hard failures:

- a known or declined fact is requested;
- a correction loses to stale, quoted, forwarded, or attachment evidence;
- an accepted value lacks exact target and evidence binding;
- rent, OpEx, area, count, or height changes field or basis;
- any value or terminal signal crosses property or suite boundaries;
- ambiguity produces a guessed value, sendable draft, terminal state, or more
  than one review action;
- a terminal decision lacks a fresh target-bound citation;
- an alternate property or broker question disappears or becomes an unsafe
  automatic action;
- unrelated mail produces an automatic response; or
- a draft contains a placeholder, invented fact, repeated question, unsafe
  disclosure, audience or signature drift, or unsupported commitment.

`tests/test_harness.py` and any simulator that copies `expected_updates` into
the observed result are explicitly forbidden as CE-Q1 evidence.

Each supported field has an independent evidence oracle defining source span,
target property and suite, freshness, unit or basis, allowed conversion, and
range behavior. Proposal `reason` prose is not a citation. The oracle applies
to every numeric and free-text field, including rent, operating expenses,
area, ceiling height, and power; absence of fresh support requires exact
abstention.

At `b400ee5`, proposal updates and applied records do not expose a typed source
message/span provenance field. The independent oracle may prove that a value is
supportable, but it may not infer and credit provenance on the product's behalf.
Every affected baseline case therefore reports product provenance as
`UNVERIFIED` and fails the hard provenance predicate. A separately designed TDD
product change must add a closed, durable per-fact evidence reference that is
observable in both the accepted proposal and applied record before a successor
can pass. Free-form `reason`, grader-selected spans, or matching the expected
value to one of several identical source strings do not count.

`CEQ-PDF-01` uses a newly generated fictional three-page native-text PDF and
exercises the actual local parser. Image-only/OCR layouts may be added only if
their parser can run inside the same effect boundary; otherwise the report
labels them `UNVERIFIED` and does not generalize the native-PDF result to them.

## CE-Q1A L2 state-unit replay

The L2 runner starts from a fresh in-memory state namespace per case, applies
the L1-qualified proposal through the real state and write-planning boundaries,
and reads back the complete scoped state. It does not replace a transition with
a direct fixture assignment. Reusable fakes may be extracted from the current
full-campaign and reply-review tests only after send/outbox behavior is removed
and every method is closed to expected calls.

For every case it proves:

- target row and configured formulas are exact;
- all sibling rows and unrelated threads are byte-stable;
- applied facts retain source-message and entity provenance;
- review and terminal action cardinality is exact;
- pause prevents continuation; resume is an explicit monitored transition;
- terminal close happens at most once;
- every injected failure leaves an exact visible partial/retryable receipt,
  blocks a false completed state, and preserves enough evidence to resume or
  reconcile;
- a second identical run is zero-delta; and
- provider, Graph, mailbox, external Sheets, tasks, outbox, send, and non-loopback
  network counters remain zero.

L2 is labeled `STATE_UNIT`, never persistence evidence.

Because Google Sheets and Firestore do not share a transaction, CE-Q1 makes no
cross-store atomicity claim. L2 proves ordering, failure detection, and retry
semantics against the closed adapter contract. Actual Google Sheets persistence
and cross-provider recovery remain `UNVERIFIED` until a separately authorized,
test-owned external-provider tier; they cannot be inferred from fake Sheets.

## CE-Q1A L3 emulator persistence replay

There is no inherited Firestore-emulator runner that satisfies this boundary.
CE-Q1 therefore builds a new task-owned local runner rather than rebranding a
dead-port check or provider-backed E2E server. It binds a unique synthetic
`.invalid` fixture namespace, an allowlisted demo project, and validated
loopback Firestore/Auth emulator endpoints before constructing a client. It
uses strict in-memory adapters for services without local emulators, including
Sheets and Graph.

L3 reruns the mandatory stateful scenarios and proves:

- two independent baseline reads and a complete namespace inventory occur
  before the first mutation;
- fixture campaign-access and automation switches begin false, remain false,
  and no product path attempts to open them;
- real Firestore transactions, timestamps, retry identities, and readbacks
  satisfy the L2 oracle;
- no document exists outside the task-owned namespace before, during, or after
  cleanup;
- the exact same source identity replay is zero-delta;
- interruption and injected transaction failure retain an exact visible
  retryable record and never falsely report the terminal workflow complete;
- the emulator process, ports, namespace, and temporary files are task-owned
  and cleanup is positively verified; and
- every external constructor, endpoint, outbox, pending-send, follow-up-send,
  and communication counter remains zero.

The “during” claim comes from a continuous path-enforcement ledger, not only
before/after inventory. Every Firestore/Firebase client factory used by product
modules is identity-pinned to a namespace-enforcing wrapper around the exact
emulator client. The wrapper canonicalizes and records every collection,
document, query, batch, transaction, create, update, delete, and read path and
rejects an out-of-namespace path before transport. Batch/transaction child
references remain wrapped. The emulator request/audit stream is independently
reconciled against that ledger at shutdown. Mutation tests deliberately attempt
an out-of-namespace transient create/delete and must prove it was blocked and
recorded; any SDK operation not attributable to the wrapper or any ledger/audit
disagreement is `INSTRUMENT_FAILURE`.

Until L3 passes, CE-Q1A may report L1/L2 diagnostics but cannot issue
`PASS_OFFLINE`.

L3 proves real Firestore persistence plus fake-Sheets adapter orchestration. It
does not upgrade the separately unverified Google Sheets or distributed
cross-store behavior.

## Voice review

CE-Q1A validates the voice instrument using frozen drafts and produces a
blinded review packet. It does not generate new model copy.

The current missing-field runtime deliberately discards model prose and selects
one fixed deterministic template. CE-Q1 must therefore score the final
runtime-selected body, not `proposal.response_email`, and must preserve the
fixed-template result as evidence of the current voice limitation. Exact field
selection can pass while repeated natural phrasing remains refuted or
unverified; those are different claims.

The five contexts are scored separately on a five-point scale for natural
flow, professional tone, context continuity, concision, and absence of obvious
AI tells. Two independent blinded reviewers grade every eligible draft. At
least one reviewer must be a human operator before voice can be promoted beyond
`partial`. Both reviewers must score every dimension at least `4/5`; a third
blinded reviewer resolves any difference greater than one point. Any draft
below `3/5`, any hard semantic fault, or any safety disagreement fails the
voice packet rather than being averaged.

The binary exclusions remain authoritative: no repeated question, invented
fact, invented promise, forbidden disclosure, placeholder, wrong audience, or
request for an accept-only/note/skip/formula field. A voice rewrite must rerun
the complete semantic and stateful deck.

Current-model naturalness remains open after CE-Q1A. Only separately approved
CE-Q1B may populate the same packet with freshly generated pinned-model drafts.
CE-Q1B must use a predeclared fixed attempt count: five attempts for every
P0/safety family and three for quality-only cases. Every attempt counts. There
is no retry-until-green, best-of-N selection, or deletion of invalid JSON or
model refusals. A provider outage becomes `UNVERIFIED` after at most three
bounded instrument retries; malformed output or refusal is a product-reliability
failure. If only a moving model alias is available, the verdict is explicitly
time-bounded and invalidates on alias, model, SDK, prompt, or configuration
drift.

Any future CE-Q1B authorization must bind the exact immutable model revision or
explicitly name the moving alias; endpoint and test account; request/prompt and
configuration hashes; data-retention posture; OS and API egress allowlists;
attempt, token, cost, and wall-time ceilings; and the exact permitted claim.
Three or five observations remain an observed-attempt report unless a separate
statistical sampling design is reviewed.

## No-effect execution boundary

The canonical host supervisor starts SUT, emulator, and scorer children inside
an OS-enforced sandbox; Python monkeypatches and counters are defense in depth,
not the containment proof. The preferred boundary is a pinned local container
with no external network interface, loopback only inside its namespace,
read-only repository/input/response mounts, no oracle mount, task-owned tmpfs
for writes, dropped capabilities, no host or container-engine socket, and no
inherited descriptors except closed stdio pipes. An equivalent OS sandbox must
prove the same properties. If neither is available, L3 is `BLOCKED`.

The supervisor writes a durable task identity and child/container identity
before releasing startup, observes the complete process tree independently,
and handles `INT`/`TERM` with bounded TERM→KILL cleanup followed by process,
port, mount, and temp-state verification. An outer-supervisor `SIGKILL` cannot
be called clean: it leaves the durable ownership receipt, and the next run
refuses to start until the exact task-owned orphan is reconciled. Unknown or
ambiguous descendants are `INSTRUMENT_FAILURE`, never assumed absent.

Inside that sandbox, the canonical runner starts with real credentials absent
and a closed synthetic configuration. Because `app_config.py` requires values
unless `E2E_TEST_MODE=true`, CE-Q1 sets that existing mode and its exact mock
sentinels deliberately, overrides production-looking default project/bucket
values with declared demo values, and proves that any attempt to use a sentinel
at a client boundary fails. The receipt says `REAL_CREDENTIALS_ABSENT` and lists
the synthetic configuration hashes; it does not misleadingly claim that all
configuration strings are empty.

The child explicitly sets `SITESIFT_OUTBOUND_MODE=paused` because the absent
default is live. Before importing product code it installs temporal guards that
fail on:

- Firestore, Firebase, OpenAI, MSAL, Graph, Sheets, Tasks, or HTTP client
  construction outside an explicitly injected local adapter;
- DNS resolution, socket construction or connect, and HTTP requests to any
  non-loopback destination;
- subprocess invocation of `gcloud`, mailbox helpers, scheduler or manual-live
  scripts, and any `firebase` command except the exact audited L3 launcher;
- unbound use of `_sheets_client` or `build_conversation_payload`; any use of
  Drive, Tasks, follow-up-send, claim, or retry entrypoints; any pending-send
  entrypoint outside the exact baseline exception below; and
  direct harness use of a mail-delivery entrypoint outside the exact bound
  pipeline exception below;
- reads of production credential files or ambient Cloud SDK overrides;
- writes outside the case's temporary directory or explicitly bound local
  emulator namespace; and
- imports of scripts classified as manual/effectful entrypoints.

The child receives a closed environment allowlist, a task-owned temporary home
and cache, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and
`PYTHONDONTWRITEBYTECODE=1`. It does not load `.env`, shell profiles, Cloud SDK
configuration, keychains, or ambient credential helpers. The receipt records
permitted environment variable names and hashes of non-secret values, never
secret values.

All fixture bundles are scanned before mounting or execution. Child stdout,
stderr, crash dumps, and generated results are captured into a quarantined
task-owned mount and are never echoed to the terminal or report before the
closed PII scanner passes. Scan failure retains only the quarantined artifact
identifier and fails the tier.

Guards remain installed from import through interpreter exit and verify their
own identity at exit so test code cannot replace them. The final report records
constructor attempts and network/effect attempts, not merely successful calls.
Any attempted forbidden effect is a gate failure even if a mock prevented the
effect.

The sole mail-entrypoint exception is the real in-process
`send_reply_in_thread` call reached naturally from a pipeline-required
`process_inbox_message(..., allow_outbound_reply=True)` execution. It is not
patched out or called directly by the harness. `SITESIFT_OUTBOUND_MODE=paused`
must make it return at its production kill-switch boundary before body
formatting, signature attachment, token, HTTP, or Graph transport work. The
receipt requires exactly one expected chokepoint invocation, exact
`suppressed_by_kill_switch` outcome, zero transport/client construction, and
zero Graph/send attempt. Any other mail entrypoint, direct invocation,
different outcome, or downstream access is a hard effect failure.

The sole baseline pending-send exception is the `queue_pending_response` call
naturally reached after that exact paused outcome on `b400ee5`. It is
identity-pinned to a strict in-memory adapter that records arguments and raises
on any storage/client access; exactly one call earns only the expected baseline
`FAIL` described above. Direct harness calls, multiple calls, any different
pending operation, or any Firestore/emulator write remain hard effect failures.
This exception is removed from the expected contract after the kill-switch
product fix.

The L3 launcher is the sole subprocess exception. It runs inside the same
OS-isolated network and filesystem boundary and must use a preinstalled,
hash-pinned emulator binary or JAR with update checks and downloads disabled,
an allowlisted demo project, explicit loopback hosts and task-owned ports, and a
captured process-group identity. Any different argument, binary hash, spawned
descendant, endpoint, or download attempt is forbidden. The outer supervisor,
not the emulator or Python child, certifies its process and cleanup state.

The corpus, captured stdout/stderr, reports, and committed artifacts pass a
closed privacy check with two distinct claims:

1. a mechanical scanner rejects name/address patterns, mailbox identities,
   message or thread identifiers, sheet/document/Graph IDs, credential forms,
   production project/bucket names, seeded forbidden tokens, and any identity
   outside the declared `.invalid` allowlist; and
2. fixture-generation provenance proves that prose and ordinary numeric values
   were newly authored from a synthetic template without access to raw FDR,
   Gmail, provider, or customer payloads. An independent reviewer checks that
   provenance and the resulting fixture diff.

The scanner does not claim it can identify an arbitrary copied customer phrase
or number from content alone. Unknown origin, a missing provenance receipt, or
a mechanical hit quarantines the artifact and stops the tier. A fixture
previously described as “sanitized” is not admitted without both checks.

## Execution and failure order

1. Verify exact clean commit, ancestry from `6caa8ec`, manifest hash, fixture
   hash, owner-module hashes, Python runtime, and dependency-lock hash.
2. Run the real-credential-absent, synthetic-config-bound,
   constructor-blocked collection contract.
3. Validate manifest closure, sanitization, family coverage, and source hashes.
4. Run oracle sabotage tests and require the expected RED reasons.
5. Run all mandatory L1 semantic replays with no skip, xfail, filtering, or
   missing scenario ID.
6. Start a fresh L2 namespace and run all state-unit replays.
7. Start a fresh task-owned L3 emulator and run the mandatory persistence deck.
8. Rerun every case in reversed order and require identical structured digests.
9. Run each case three times and report any variance; deterministic replay must
   have one digest.
10. Generate the blinded voice packet from only hard-pass final rendered
    bodies.
11. Produce the sanitized report and independently review the exact commit and
    frozen evidence.

The first non-pass stops promotion. Remaining cases may continue only in an
explicit diagnostic mode whose output is labeled incomplete and cannot produce
a gate verdict.

## Gate verdicts

The only CE-Q1A verdicts are:

- `PASS_OFFLINE` — every promotion-required L1, L2, and L3 variant, sabotage
  control, order reversal, repeat digest, no-effect guard, and state readback
  passed; every diagnostic variant executed and matched its declared non-claim.
- `FAIL` — at least one required outcome was wrong.
- `INSTRUMENT_FAILURE` — runner, dependency, emulator, scorer, guard, or
  evidence collection was incomplete or untrustworthy.
- `UNVERIFIED` — bounded instrument repair or an authorized model tier ended
  without authoritative product evidence.
- `BLOCKED` — a required local dependency or independently reviewable input was
  unavailable before execution.

There is no partial pass. Voice is reported separately as `review_ready`,
`partial`, `pass_frozen_drafts`, or `fail`; it never changes a hard `FAIL` to a
pass.

Verdict precedence is deterministic:

1. a missing local prerequisite before execution is `BLOCKED`;
2. any incomplete, contaminated, escaped, or untrustworthy instrument after
   execution begins is `INSTRUMENT_FAILURE`, which suppresses product verdicts;
3. any wrong promotion-required product outcome is `FAIL`;
4. any absent/unknown promotion-required evidence is `UNVERIFIED`; otherwise
5. the result is `PASS_OFFLINE`.

A declared diagnostic `UNVERIFIED` does not change `PASS_OFFLINE`, but its
non-claim remains binding. A separate `nextGateEligibility` record lists the
diagnostics required by each future gate. For example, voice-finalizer
diagnostics must be resolved before CE-Q1B voice review, while image-only OCR
may remain outside a text-only next gate.

The promotion predicate is closed:

```text
CE_Q1A_GREEN =
  calibrated_instrument
  AND mandatory_manifest_complete
  AND oracle_capability_separation_proved
  AND runtime_history_projection_exact
  AND all_promotion_required_L1_L2_L3_hard_invariants_pass
  AND all_diagnostic_variants_match_declared_nonclaims
  AND product_provenance_predicates_pass
  AND replay_zero_effect
  AND os_sandbox_and_cleanup_proved
  AND forbidden_external_constructor_attempts == 0
  AND allowed_local_emulator_constructor_counts_are_exact
  AND forbidden_network_attempts == 0
  AND pii_findings == 0
  AND exact_head == b400ee5_or_independently_reviewed_successor
```

Any missing or false term is classified by the precedence above, never a
partial go.

## Reporting and readiness projection

The report must include:

- exact source, ancestor, dependency, manifest, fixture, and owner-module
  hashes;
- per-scenario family, layer, `VERIFIED`/`REFUTED`/`UNVERIFIED` result,
  structured semantic diff, attempt ledger, before/after full-state hashes, and
  non-claim;
- field-level accuracy and abstention counts by family rather than one average;
- repeat-question, cross-entity, uncited-terminal, duplicate-action, and
  forbidden-effect counts, each required to be zero;
- repeat and reverse-order digests;
- complete constructor/network/effect attempt counters;
- voice scores and reviewer provenance without reviewer PII; and
- explicit separation of historical live evidence, CE-Q1A offline evidence,
  and any later CE-Q1B model evidence.

Latency, token, cost, and phrase-frequency observations are diagnostic only.
They never offset or alter a hard-safety verdict.

The readiness registry may add CE-Q1A as `deterministic_test` evidence only. It
must not change an item to `proven_live`, enable a rollout gate, close backlog
`#24`, or claim current-model voice. Existing live evidence is not rewritten.

## Stop conditions

Stop immediately and retain the sanitized failure packet if:

- any external client is constructed or network/provider/mailbox call is
  attempted;
- a credential, production project, real recipient, customer value, or raw
  production identifier appears in a fixture or report;
- a source or fixture hash changes after the run starts;
- an oracle cannot distinguish the historical failure from the expected fix;
- scorer output disagrees with direct structured-record inspection;
- any known-field repeat, wrong entity, wrong value/basis, uncited terminal,
  duplicate action, undetected/unretryable partial transition, or replay delta
  occurs;
- a voice-only change affects a deterministic outcome;
- test ordering changes the digest; or
- the runner cannot prove complete cleanup and zero forbidden effects.

An instrument defect receives at most three bounded repair attempts and then
the tier is `UNVERIFIED`. No hidden retry, case removal, xfail, score waiver, or
fixture relaxation is allowed. After any runner, oracle, product, or fixture
change, freeze a new exact commit and rerun the complete affected tier from a
clean state; an in-place partial rerun cannot promote the result.

## Promotion boundary

`PASS_OFFLINE` authorizes only a decision about the next test:

1. independent exact-head specification, security, and empirical review;
2. a separate CE-Q1B plan and explicit authorization for any pinned-model call;
3. after CE-Q1B, a separately authorized exact self-owned L4 canary with one
   existing row/property and follow-ups disabled; and
4. only after those gates, passive ordinary-user backlog `#24` observation.

CE-Q1A does not authorize merge, deployment, `/process-user`, provider use,
mailbox use, outbox creation, sending, switch changes, campaign launch,
automatic follow-ups, unattended operation, or broader admission.

The two production campaign switches remain false by non-contact: CE-Q1 does
not read or mutate production merely to re-prove them. Offline follow-up drafts
are voice samples and cannot close backlog `#82`; alternate-property events are
graded without creating or launching a row and cannot close `#83`; UI,
manual-override, dashboard-truth, and campaign-launch defects remain outside
this gate. No L4 or ordinary-user action is eligible without a newly named exact
self-owned recipient and separate current-turn authority.

No CE-Q1 result carries forward after a relevant change to `ai_processing.py`,
`column_config.py`, response selection or lifecycle handling in
`processing.py`, message/history ordering, attachment parsing, sheet
application, `followup.py`, the model/prompt/configuration, the fixture or fake
service contract, the oracle, or the grader. The report names the exact affected
tier and requires its full rerun; loose descendant ancestry is insufficient.

## Rollback and cleanup

CE-Q1A has no production rollback because it has no production effect. On local
failure:

- stop the runner;
- preserve the sanitized fixture ID, structured diff, and guard counters;
- terminate only task-owned local emulator processes;
- verify the task-owned ports and temporary namespace are released;
- retain no credentials, raw provider payloads, or customer data; and
- leave both global campaign switches and all production resources untouched.

## Acceptance criteria for implementation planning

The implementation plan may be written only after this specification is
reviewed and the following are unambiguous:

1. `b400ee5` is the exact implementation base and `6caa8ec` is the production
   ancestor, not a newly certified runtime.
2. CE-Q1A makes zero model/provider/mailbox/live calls.
3. Every mandatory pressure variant has an executed closed scenario, layer,
   sabotage control, expected verdict, and count/hash receipt.
4. Structured exact oracles replace substring and expected-output-copying
   scorers, and execution cannot mount/read the sealed oracle.
5. L1, L2 `STATE_UNIT`, and L3 emulator-persistence evidence are distinct; only
   L3 may make a persistence claim.
6. History-qualified cases consume only the actual production ten-message
   projection plus durable state, never the fixture's complete history.
7. Product provenance is observable in the product record or graded
   `UNVERIFIED`/`FAIL`; scorer inference never earns provenance credit.
8. Cross-store Google Sheets/Firestore atomicity and external Sheets persistence
   remain explicit non-claims in CE-Q1A.
9. Safety and grounding are hard binary gates; voice cannot compensate.
10. Voice on frozen drafts and current-model voice are reported separately, and
   no context is eligible until its production-owned pure finalization seam is
   shared by runtime and qualification.
11. An outer OS-enforced supervisor, capability-separated mounts, complete
    process-tree cleanup, and quarantined-output scan back the no-effect claim.
12. A complete effect-attempt receipt and owner-module invalidation rule exist.
13. `PASS_OFFLINE` changes no production or user-admission state.
14. CE-Q1B, L4, ordinary-user observation, launch, and follow-ups each retain a
    separate approval gate.

## Refutation conditions

This design is refuted if the production seams cannot be invoked without
constructing a provider client, if a closed synthetic fixture cannot represent
the necessary evidence and entity bindings, if stateful execution cannot be
isolated from outbox/send paths, or if the oracle cannot distinguish the known
historical failures from their safe outcomes. In that event, stop and redesign
the seam or runner; do not weaken the effect boundary or substitute a broader
live test.
