# Workflow-Intent Action Closure Design

## Goal

Turn accepted tour and information-request claims into explicit, human-owned
work without sending email, mutating campaign facts, or weakening terminal
suppression.

## Decision

Use the existing `tour_request` action and add a parallel
`information_request` action. Both actions require human approval and carry
only bounded notes. They are work records, not permission to draft, send, or
change a recipient.

Rejected alternatives:

- Reusing only a generic review item would hide what the broker requested and
  recreate the dashboard ambiguity Jill reported.
- Automatically responding or sending materials would require recipient,
  content, attachment, and authorization policy that this no-effect planner
  intentionally does not own.
- Treating the claims as informational only would preserve the two known gaps
  and allow real broker requests to disappear from the action plan.

## Decision Precedence

For a nonterminal message containing a tour or information request:

- approval is `human_required`;
- conversation state is `review`;
- the specific request action is emitted;
- the existing generic review item remains as the queue entry;
- no automatic outbound action is emitted.

For a message that also contains opt-out, unavailable, or another terminal
condition:

- terminal intent and follow-up freeze retain precedence;
- the specific request actions remain visible for an operator;
- the conversation is not reopened;
- no outbound action is emitted.

This preserves every accepted broker intent without allowing a lower-priority
request to override a stop condition.

## Contracts

Add `ActionType.INFORMATION_REQUEST` with:

- approval class: `human_required`;
- source support: `ClaimPredicate.INFORMATION_REQUEST`;
- allowed payload: `notes` only;
- no required prior-state snapshot because the action itself does not mutate
  campaign state.

The existing `ActionType.TOUR_REQUEST` keeps its current validation contract.
Policy emits both request actions from their matching effective claims. Reason
codes are `tour_requires_approval` and
`information_request_requires_approval`.

## Repetition And Conflicts

The policy consumes the effective claim set produced by the existing claim
reducer. Repeated identical requests collapse to one predicate-level action;
conflicting active claims continue through the existing review path. No new
deduplication or conflict model is introduced in this change.

## Proof

Tests must prove:

1. Each action is rejected when automatic or supported by the wrong claim.
2. A pure tour request and a pure information request enter review and produce
   their typed human-required action.
3. Repeated information requests produce one typed request action.
4. Mixed referral, call, tour, information, return-date, remediation, and
   opt-out evidence preserves terminal suppression while exposing all four
   human-owned actions.
5. Unavailable or opt-out conditions never produce an automatic outbound
   action and are never reopened by a request.
6. The recorded provider-to-policy shadow has no workflow-intent gap codes and
   all exact oracles pass.
7. Focused tests, isolation checks, and the full backend suite pass without
   importing or executing effect adapters.

## Scope Boundary

This change ends at deterministic action planning. It does not add execution,
persistence, dashboard rendering, automatic redirect, automatic attachment
delivery, provider calls, deployment, or production access. Those remain
separate proof gates.
