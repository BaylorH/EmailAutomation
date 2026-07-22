# Deterministic Policy and Action Evaluator Design

**Status:** Approved by the standing production-readiness direction and Baylor's 2026-07-22 continuation
**Date:** 2026-07-22
**Deliverable:** Both code and verified findings
**Parent architecture:** `2026-07-22-broker-claim-pipeline-design.md`, stages 5-7

## Purpose

Convert validated, evidence-bound claims into deterministic campaign decisions and inspectable no-effect action plans. This slice proves what SiteSift *should* do before any legacy worker comparison or connection to Graph, Firebase, Sheets, follow-up scheduling, notifications, or outbound mail.

The evaluator answers four separate questions for each entity:

1. Is the property available?
2. Does it fit the effective campaign contract?
3. Are the required facts complete?
4. What is the conversation lifecycle state?

The planner then converts those answers into proposed actions with stable identities, source claims, explicit prior-state expectations, ordering, and approval ownership.

## Chosen Shape

Add a small pure evaluator and planner under `email_automation.claim_pipeline`.

- Do not extend `email_automation.processing`; it mixes interpretation and effects.
- Do not build a generic rules-engine framework; the Base V1 policy surface is small enough for named deterministic functions.
- Reuse the existing immutable `CampaignContract`, `DecisionSnapshot`, `ExecutionScope`, `PlannedAction`, and `ActionPlan` contracts.
- Keep one decision and one action plan per entity. Cross-entity results are returned together in deterministic entity order.
- Treat the existing boundary catalog as a stage oracle, then add executable policy fixtures with exact reason codes, required/forbidden action types, and prior state.

## Inputs

`PolicyEvaluationRequest` is immutable and contains:

- one `CampaignContract`;
- one `ExecutionScope` for the tracked row/thread;
- resolved entities;
- accepted claims only;
- a stable state-snapshot hash;
- current fact values by entity;
- current conversation and follow-up state by entity;
- authorized recipients for plan validation.

The request contains no service clients and no callable effect hooks. Construction fails on tenant, campaign, entity, or claim-scope mismatch.

## Outputs

`PolicyEvaluationResult` contains ordered `EntityPolicyResult` values. Each entity result has:

- one `DecisionSnapshot`;
- one `ApprovalClass` describing whether the decision can proceed automatically;
- one validated `ActionPlan`;
- deterministic reason codes;
- source claim IDs used by the decision;
- no effect receipt, because this stage cannot execute anything.

The whole result exposes a canonical digest so repeated evaluation can prove byte-stable semantics.

## Effective Claims

Claims are reduced per entity and predicate before policy runs.

1. A claim that is superseded by a valid later correction is inactive.
2. A corrected claim must point to the exact older claim and wins only for the same entity and predicate.
3. Fresh accepted claims are considered; rejected candidates never reach this layer.
4. Multiple active claims for one entity/predicate are allowed only when they agree after canonical value normalization.
5. Conflicting active claims produce a review decision. They do not pick the newest value silently.
6. Claims are sorted by predicate and claim ID before decision or action construction.

## Decision Precedence

Precedence is safety-oriented and independent of provider output order.

1. **Email opt-out:** freeze follow-ups immediately. A simultaneous call request remains human-owned. No outbound acknowledgement is planned.
2. **Target unavailable:** market is unavailable, fit is nonviable, completeness is not applicable, and conversation enters terminal intent.
3. **Hard contract mismatch:** market remains independently classified; fit becomes nonviable and conversation enters terminal intent.
4. **Ambiguity or conditional status:** create review or conditional state and block terminal mutation.
5. **Required facts complete:** conversation enters terminal intent for one closing acknowledgement.
6. **Return date:** conversation waits for the broker and cannot schedule earlier than the proven date.
7. **Otherwise:** conversation remains active with explicit missing fields.

Terminal intent is not terminal completion. Later effect work must prove the closeout was sent or explicitly waived before terminalizing.

## Base V1 Policy Rules

### Market State

- Explicit target availability `unavailable`, `leased`, or `off_market` becomes `unavailable`.
- Explicit `available` becomes `available`.
- `asking_status=accepting_backups` becomes `conditional`.
- Remediation alone may establish `available` only when it clearly concerns an existing target that can be remediated; it never overrides an unavailable claim.
- Missing or conflicting market evidence becomes `unknown` or review, never unavailable by inference.

### Fit State

- Market unavailable becomes `nonviable` for that entity only.
- Occupancy after a hard `occupancy_by` date becomes `nonviable`; on or before it is viable for that rule.
- A term below a hard `minimum_term_months` becomes `nonviable`.
- Definite funded remediation completed before the hard occupancy date becomes `conditional` rather than unavailable.
- Possible, approval-dependent, costly, or undated remediation becomes `review`.
- A hard-requirement key the evaluator does not understand becomes review, not an allow.
- Missing hard requirements never get invented from soft preferences.

### Completeness

- A nonviable or opted-out entity is `not_applicable` unless review is required.
- Every configured required field must have one non-conflicting active accepted claim for the entity.
- All required fields present becomes `complete`.
- Missing fields becomes `incomplete` and lists exact predicate names.
- Conflicts or policy ambiguity becomes `blocked`.

### Conversation State

- Opt-out, target unavailable, hard non-fit, or complete required facts becomes `terminal_intent`.
- A return date becomes `waiting_broker` when no higher-precedence terminal state exists.
- Human-owned ambiguity becomes `review`.
- Otherwise the state is `active`.
- The evaluator cannot emit `terminal_pending_ack` or `terminal`; those require effect evidence outside this slice.

### Entity Isolation

- Decisions are per entity.
- An alternate property always gets its own decision and human-required proposal.
- Alternate facts cannot produce automatic `FACT_UPDATE` actions for the target row.
- Split suites remain separate. One unavailable suite cannot terminalize an available sibling or the building.
- Contact claims can freeze contact follow-ups or create contact review actions but cannot mutate property facts.

## Action Planning

Actions are derived from policy invariants rather than legacy event order.

1. `FACT_UPDATE` for each unambiguous supported property fact, with exact claim value and current fact as expected prior state.
2. `FOLLOWUP_FREEZE` for every terminal-intent decision, before any status or draft proposal.
3. `STATUS_TRANSITION` to `terminal_intent`, `waiting_broker`, or `review` when conversation state changes.
4. `ALTERNATE_PROPERTY_PROPOSAL` for alternate identities, always human-required.
5. `RECIPIENT_CHANGE`, `TOUR_REQUEST`, and `CALL_REQUEST` always human-required.
6. `REVIEW_ITEM` for conflicts, unsupported hard requirements, tentative remediation, conditional market status, or any decision that cannot safely proceed.
7. No `OUTBOUND_DRAFT` in this slice. Closeout language belongs to the later drafting/shadow gate.

Action sequence is deterministic:

1. fact updates;
2. follow-up freeze;
3. status transition;
4. human-owned proposals and requests;
5. review item.

Every action is validated by `validate_action_plan`. Validator support must be widened only where a deterministic decision legitimately supports an action, such as terminal non-fit supporting follow-up freeze. It must not weaken recipient, target, tenant, campaign, row, or approval checks.

## Reason Codes

Reason codes are a closed vocabulary and sorted before identity creation. Initial codes include:

- `broker_confirmed_available`
- `broker_confirmed_unavailable`
- `accepting_backup_offers`
- `hard_occupancy_after_deadline`
- `hard_term_below_minimum`
- `definite_remediation_before_deadline`
- `tentative_remediation_requires_review`
- `required_facts_complete`
- `required_facts_missing`
- `contact_opted_out`
- `broker_return_date`
- `redirect_requires_approval`
- `alternate_property_requires_approval`
- `call_requires_approval`
- `conflicting_active_claims`
- `unsupported_hard_requirement`

Each review item carries a concise summary plus structured details containing the relevant reason codes, entity ID, and source claim IDs. Nothing is merely labeled "needs attention."

## Executable Policy Lattice

Create strict policy fixtures that cover both positive and opposed cases:

- available target with missing facts;
- explicit unavailable target;
- tour-only unavailable wording represented without a property-unavailable claim;
- hard occupancy miss versus soft occupancy preference;
- hard term miss versus no minimum term;
- definite versus tentative remediation;
- conditional backup offers;
- split-suite mixed availability;
- alternate property isolation;
- correction supersession;
- complete required facts;
- OOO return date;
- referral/redirect approval;
- opt-out plus call request;
- conflicting active claims;
- unknown hard requirement;
- claim and entity input order permutations.

Every case declares exact decisions, reason codes, missing fields, required action signatures, forbidden action signatures, and the no-side-effect policy. The loader rejects unknown keys and incomplete or contradictory expectations.

## Verification

The slice passes only when:

- every behavior was introduced by a failing test observed before implementation;
- all policy fixtures pass in normal and reversed input order;
- repeated evaluation produces identical decision, action, and result digests;
- opposed cases prove no wrong-row update, no automatic new recipient, no follow-up after terminal intent, and no silent ambiguity;
- package import isolation still excludes Firebase, Graph, Sheets, requests, OpenAI, processing, follow-up, and messaging;
- focused tests, compilation, diff integrity, and the full backend suite pass;
- no credentials, provider calls, production data, deployment, or external side effects are used.

## Deferred Work

- Legacy shadow comparison and discrepancy grading.
- Persistence of claims, decisions, plans, and review items.
- Closeout drafting and tone evaluation.
- Effect execution, retries, idempotent recovery, and sent confirmation.
- Admin review UI and user-facing explanations.
- Browser campaign ladder and production rollout.

