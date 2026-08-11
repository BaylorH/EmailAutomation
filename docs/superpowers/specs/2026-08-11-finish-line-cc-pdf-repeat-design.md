# Finish-Line CC, PDF, and Repeat-Ask Hardening Design

**Status:** Approved direction; implementation-ready

**Deliverable:** both

## Goal

Close the smallest remaining blockers between supervised campaign use and a
broader autonomous canary without widening follow-ups or user scope. The work
must prove copied-party reply-all safety, fail closed on one mixed-property PDF,
and hard-reject automatic copy that re-asks an already-known field.

## Decision

Use a patch-first, live-proof-second sequence:

1. Keep the existing reply-all implementation unchanged because the deployed
   path and its targeted zero-network suite already cover safe recipient
   hydration, self/alias removal, opt-outs, dedupe, and send/index lineage.
2. Tighten missing-field response validation so every explicitly requested Ask
   field is still missing after the current Sheet write.
3. Make conversation ordering direction-aware so delayed inbound delivery and
   manual Sent-items continuations cannot reorder the ten-message model window.
4. Quarantine all row-level assets when one attachment is classified as a
   mixed-property ambiguity, while preserving the attachment at message/thread
   level for manual review.
5. Deploy the three bounded backend fixes together, reprove the release identity,
   then run live CC, mixed-PDF, and long-turn cases in that order.

This beats live-first testing because the source audit found two deterministic
gaps that could produce a broker-facing repeat or a wrong-row asset write. It
beats a prompt rewrite because both failures need hard post-model guards. A
larger extraction/reply architecture rewrite is rejected because it would delay
returning users and broaden the regression surface.

## Scope

### Included

- Core inbox automatic replies only.
- Configured Ask fields, including custom field labels.
- Mixed-property and mixed-suite PDF ambiguity.
- Existing controlled campaigns and self-owned test mailboxes only.
- One monitored row at a time, follow-ups off.
- Source, Sheet, Dashboard, Graph-audit, queue, counter, and residue readbacks.

### Excluded

- Autonomous follow-ups.
- Arbitrary Microsoft proxy-alias equivalence.
- OCR-only/image-only PDF confidence improvements.
- New campaign-generation behavior.
- Tour scheduling, Results, or map lanes.
- General natural-language style rewriting; observed punctuation and stock-copy
  issues remain a nonblocking quality track.

## Design

### 1. Requested-field subset guard

The deployed `_response_mentions_missing_fields()` accepts an LLM reply when it
mentions any missing field. It therefore accepts a draft that asks for one
missing field and one already-known field.

Add a deterministic extractor for configured Ask fields mentioned inside
request clauses. The response is eligible only when:

- at least one configured Ask field is explicitly requested;
- every requested field is in the authoritative post-write `missing_fields`;
- no Note, Skip, or formula field is requested.

Acknowledging a known value is not a request. Question/bullet language is a
request. When validation fails, retain the existing deterministic fallback,
which lists exactly the current missing fields. Do not raise model temperature
or add conversation memory in this change.

### 2. Direction-aware conversation chronology

Before PDF work, make message chronology deterministic. The deployed helpers
sort every message by `sentDateTime or receivedDateTime`, regardless of
direction. A delayed inbound can therefore appear before an intervening manual
outbound, and a Graph-only Sent-item can be misclassified when both timestamps
exist.

Add one shared direction-aware timestamp helper. Outbound messages use their
sent timestamp; inbound messages use their received timestamp. Resolve
direction from durable indexed direction first, then authenticated-mailbox
sender/folder provenance for Graph-only messages. Tests must cover a delayed
inbound versus intervening outbound and an unsorted twelve-message history whose
last ten are selected in exact mailbox chronology.

### 3. Mixed-PDF asset quarantine

The scalar guard already removes competing attachment facts, injects
`needs_user_input:multi_property_attachment`, nulls the automatic response, and
prevents terminal/tour effects. The remaining gap is asset routing:
`_partition_property_attachments()` currently retains a whole attachment when
it mentions the target, even if the same attachment also names a competitor.

During `multi_property_attachment` escalation, route an attachment to the
current row only when `_attachment_property_verdict(...)` is exactly `target`.
Exclude `mixed`, `competing`, and `addressless` attachments from current-row
flyer, floorplan, property-image, AI_META, and row-level change-log writes.
Preserve the original message attachment and its message/thread provenance for
manual review.

### 4. Synthetic PDF fixture

Generate a native-text, three-page PDF:

- Page 1: dynamic exact target heading; availability only; explicitly says the
  target's suite-specific figures are not confirmed.
- Page 2: fictional competing property, Suite A, with complete tempting facts.
- Page 3: second fictional competing property, Suite B, with different complete
  facts and a portfolio total.

The PDF must extract through the normal local text path. A deterministic test
must render it to PNG for visual QA and extract it back before it is used by the
pipeline test. No real address, recipient, or mailbox may be committed.

### 5. Live acceptance order

#### CC/reply-all

On an untouched existing row, a controlled broker replies with full facts and
one controlled copied participant. The automatic reply must preserve the safe
Cc, strip the product mailbox and original plus alias, send/index exactly once,
write only the target row, calculate Gross, and terminalize once.

#### Mixed PDF

On another untouched row, a controlled broker attaches the mixed fixture and
states that the schedule does not identify which option belongs to the target.
Expected outcome: no reply, no scalar or row-level asset write, no new row, one
`needs_user_input:multi_property_attachment` action, and paused active state.

#### Long-turn repeat/correction sequence

On a third untouched row, run thirteen total messages so the final proposal is
built from the last ten of twelve pre-close messages:

1. Broker gives Total SF and adversarially asks to be asked for it again;
   automation asks only for Rent and OpEx.
2. Broker gives Rent and requests a call; the call event deterministically
   pauses automation with no automatic reply.
3. A monitored Dashboard continuation answers the call request and asks only
   for OpEx, returning the thread to active.
4. Broker corrects Total SF but withholds OpEx; automation asks only for OpEx.
5. Broker corrects Rent but withholds OpEx; automation asks only for OpEx.
6. Broker reconfirms both corrected values but still withholds OpEx; automation
   asks only for OpEx. The history is now beyond ten messages.
7. Broker supplies OpEx and reconfirms the final values; automation closes once.

Every automatic request may ask only the authoritative missing set; corrections
must win. A final worker rerun must be zero-send/idempotent.

## Safety Gates

Stop immediately on any of the following:

- unknown, dropped, duplicated, or self recipient;
- a known-field re-ask;
- any scalar or asset write from the mixed PDF;
- wrong-row or other-row mutation;
- new row creation during the mixed-PDF case;
- send during a paused or ambiguous state;
- premature terminal state or correction loss;
- duplicate/unindexed send, actionable failure, dead letter, claim, task, or
  queue residue;
- release identity or allowlist drift.

The live cases require current-turn explicit authorization for every exact
self-owned recipient. Addresses are runtime-only and never recorded here or in
Brain.

## Rollback

- Keep global creation and automation controls closed during preparation.
- Keep follow-ups disabled throughout.
- Before live proof, preserve the current production revision as the rollback
  target.
- If deterministic verification fails, do not deploy.
- If live verification fails, close/pause the exact test campaign, stop further
  sends, preserve the scoped evidence, and roll traffic back to the prior
  revision when the failure is code-related.

## Acceptance Criteria

1. Focused missing-field tests prove mixed known+missing asks are rejected and
   acknowledgements of known facts remain allowed.
2. Direction-aware chronology tests prove delayed inbound/manual-outbound order
   and exact last-ten truncation.
3. Pure partition and pipeline tests prove a mixed attachment produces zero
   current-row asset effects and one paused review action.
4. The native PDF fixture renders cleanly and round-trips through the normal
   extraction path.
5. All required outbound, extraction, lifecycle, scheduler, and release-safety
   regression suites pass.
6. CC live proof has the exact safe audience and one send/index.
7. PDF live proof has zero send and zero row-level writes.
8. Long-turn live proof crosses ten messages without a known-field repeat,
   loses no correction, closes once, and remains idempotent on rerun.
9. Readiness evidence promotes only the blockers actually closed; autonomous
   use stays HOLD until every named blocker has current live proof.

## Refutation Conditions

The design is refuted if request-clause parsing cannot distinguish a known-field
acknowledgement from an ask without suppressing valid replies, if mixed assets
cannot be quarantined without losing message-level review provenance, or if the
live provider cannot preserve the exact safe copied-party topology despite the
passing deterministic path.
