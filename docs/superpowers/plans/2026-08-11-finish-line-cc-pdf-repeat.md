# Finish-Line CC, PDF, Repeat, and Chronology Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to
> execute this plan task-by-task with strict RED/GREEN evidence and separate
> specification and code-quality reviews.

**Goal:** Close the smallest deterministic gaps before the next monitored
return-user canary: reject any automatic request that re-asks a known field,
order mixed Firestore/Graph history by message direction, and quarantine every
row-level asset from an ambiguous mixed-property PDF. Then live-prove CC,
ambiguous-PDF, and long-turn behavior on existing controlled campaign rows.

**Deliverable:** both code and verified findings.

**Architecture:** Keep the existing reply-all path unchanged. Add one pure
column-contract helper that identifies configured Ask fields inside request
clauses and require the result to be a nonempty subset of authoritative missing
fields. Add one shared direction/timestamp resolver used by both Firestore and
Graph history; bind Graph-only direction to the authenticated mailbox identity.
Reuse the existing attachment property verdict so an ambiguity escalation can
route only an exact `target` PDF to current-row asset writers. Preserve original
message/thread attachment evidence for review. Ship these guards as one bounded
backend revision, then exercise one campaign row at a time with follow-ups off.

**Tech stack:** Python 3, `unittest`, Firestore/Graph test fakes, Google Sheets
readback, `fitz`/PyMuPDF for a native-text PDF fixture, Poppler for visual
rendering, Cloud Run/Cloud Tasks, and the existing release/readiness tooling.

**Safety:** No live provider contact during Tasks 1-6. No PII or mailbox address
may enter source, fixtures, docs, Brain, logs, or readiness evidence. Live tasks
require current-turn authorization naming every exact self-owned recipient.
Global creation/automation remain Closed, follow-ups remain off, and live tests
stop on any audience, row, send-count, lifecycle, queue, or residue mismatch.

---

## Task 1: Hard-reject mixed known-and-missing field requests

**Files:**

- Modify: `email_automation/column_config.py`
- Modify: `email_automation/processing.py`
- Modify: `tests/test_column_mode_contract.py`

- [ ] Add RED cases proving an LLM draft is rejected when one request clause
  asks for a known configured Ask field plus a missing configured Ask field.
- [ ] Add positive controls for an acknowledgement of a known field followed by
  a request for only the missing field.
- [ ] Add request-clause controls for question sentences, semicolon-separated
  clauses, and bullet lists.
- [ ] Add a custom `ask_required` field alias case and prove a custom known-field
  re-ask is rejected.
- [ ] Preserve the existing Note/Skip/formula rejection cases.
- [ ] Run the focused class against pre-fix code and capture the expected
  assertion failures:

  ```bash
  python3 -m unittest -v \
    tests.test_column_mode_contract.BrokerReplyColumnModeValidationTests
  ```

- [ ] Add a pure helper in `column_config.py` that returns the configured Ask
  field headers requested in explicit request clauses. Reuse the existing
  request-intent regex, word-boundary matcher, canonical configured header,
  canonical aliases, and custom Ask header/paraphrase terms.
- [ ] Fail closed on malformed `column_config`.
- [ ] Change `_response_mentions_missing_fields()` to require:
  `requested_fields` is nonempty and `requested_fields <= missing_fields`, after
  the existing nonrequestable-field guard.
- [ ] Keep the existing exact-missing deterministic fallback unchanged.
- [ ] Run GREEN, then the neighboring completion and column-contract suites:

  ```bash
  python3 -m unittest -v \
    tests.test_column_mode_contract \
    tests.test_processing_completion_guards
  ```

- [ ] Self-review for false request detection and commit as:
  `fix: reject known field reasks`.

## Task 2: Make mixed-source conversation chronology direction-aware

**Files:**

- Modify: `email_automation/messaging.py`
- Modify: `email_automation/ai_processing.py`
- Modify: `email_automation/processing.py`
- Modify: `tests/test_messaging_conversation_payload.py`

- [ ] Add a RED unit case where an inbound message has `sentDateTime=10:00`
  and `receivedDateTime=10:05`, while a manual outbound was sent at `10:03`.
  Assert mailbox chronology is outbound then inbound.
- [ ] Add an unsorted twelve-message RED case and assert `limit=10` returns the
  exact chronological last ten.
- [ ] Add Graph-only RED cases where both timestamps exist: sender equal to the
  authenticated mailbox is outbound; a different sender is inbound.
- [ ] Add a fail-closed Graph-only case for missing mailbox identity so an
  ambiguous message is never promoted to outbound merely because it has a sent
  timestamp.
- [ ] Capture the expected failures:

  ```bash
  python3 -m unittest -v tests.test_messaging_conversation_payload
  ```

- [ ] Add a pure direction-aware timestamp selector: durable indexed direction
  first; outbound uses sent time; inbound uses received time; missing preferred
  values fall back to the alternate time then `createdAt`.
- [ ] Add a pure Graph direction resolver: compare normalized `from` to the
  authenticated mailbox identity when both dates exist; retain the safe
  one-date rules and default ambiguous unknowns to inbound.
- [ ] Thread optional `authenticated_mailbox_email` from
  `process_inbox_message()` through `propose_sheet_updates()` into
  `build_conversation_payload()`. Do not add another `/me` request.
- [ ] Use the shared timestamp selector for Firestore sorting, merged sorting,
  and payload timestamps so all three views agree.
- [ ] Run GREEN and the message-order/dedupe/replay neighbors:

  ```bash
  python3 -m unittest -v \
    tests.test_messaging_conversation_payload \
    tests.test_message_history_dedupe \
    tests.test_operator_message_replay
  ```

- [ ] Self-review identity fallback and commit as:
  `fix: order conversation history by direction`.

## Task 3: Quarantine mixed-property PDF assets

**Files:**

- Modify: `email_automation/processing.py`
- Modify: `tests/test_jill_live_campaign_regressions.py`
- Add: `tests/test_mixed_pdf_asset_quarantine.py`

- [ ] Add a pure RED test: one attachment that names the exact target and a
  competing street identity plus a `needs_user_input` event with reason
  `multi_property_attachment` must yield `current=[]` and no event assets.
- [ ] Add a target-only positive control and competing/addressless negatives.
- [ ] Generate a native-text three-page in-memory PDF with fictional addresses:
  target availability/no target figures on page 1; conflicting complete Suite A
  and Suite B figures plus a portfolio total on pages 2-3.
- [ ] Pass the actual bytes through `process_pdf_for_ai()` and assert local text
  extraction includes all three pages and classifies the source as `mixed`.
- [ ] Add a pipeline RED using the real extracted manifest. Assert:
  no scalar `apply_proposal_to_sheet`, no flyer/floorplan/property-image writes,
  no AI_META or Sheet change-log writes, no new row, and no send; exactly one
  paused `needs_user_input:multi_property_attachment` action remains.
- [ ] Capture the expected RED failures:

  ```bash
  python3 -m unittest -v \
    tests.test_mixed_pdf_asset_quarantine \
    tests.test_jill_live_campaign_regressions.JillLiveCampaignRegressionTests.test_competing_multi_property_brochure_escalates_instead_of_writing_current_row
  ```

- [ ] Import/reuse `_attachment_property_verdict` in `processing.py`.
- [ ] In the ambiguity/no-new-property branch, send only verdict `target` to
  current-row asset routing; exclude `mixed`, `competing`, and `addressless`.
  Do not delete or detach message/thread attachment provenance.
- [ ] Run GREEN and neighboring PDF/property-image/link suites:

  ```bash
  python3 -m unittest -v \
    tests.test_mixed_pdf_asset_quarantine \
    tests.test_jill_live_campaign_regressions \
    tests.test_pdf_link_changelog \
    tests.test_property_image_resolver \
    tests.test_broker_language_broker_attachment_or_link_only
  ```

- [ ] Materialize the synthetic PDF once in a temporary directory, render every
  page with `pdftoppm -png`, inspect the page images, and delete only the temp
  directory afterward. Do not commit a real address or mailbox.
- [ ] Self-review zero-effect assertions and commit as:
  `fix: quarantine ambiguous pdf assets`.

## Task 4: Review each code task before integration

**Files:** all Task 1-3 diffs and tests.

- [ ] For each task, dispatch a fresh specification reviewer after its commit.
- [ ] Fix every P0/P1/spec gap with a new RED where behavioral.
- [ ] Re-run the specification review until PASS.
- [ ] Dispatch a fresh code-quality reviewer only after spec PASS.
- [ ] Fix and re-review every P0/P1 quality issue.
- [ ] After all three tasks, dispatch one final cross-task reviewer covering
  request detection, identity/direction, PDF provenance, and fail-closed scope.

## Task 5: Run the release-sized local verification matrix

**Files:** repository-wide verification only; no production writes.

- [ ] Run all focused suites from Tasks 1-3 fresh.
- [ ] Run the exact existing 133-test CC/reply-all set documented in the design
  finding; do not claim CC live proof from deterministic tests.
- [ ] Run correction/current-value, lifecycle, scheduler, inbox authority,
  source-envelope, reply safety/indexing, sent-mail guard, and release-safety
  suites selected by the system audit packet.
- [ ] Run:

  ```bash
  python3 -m py_compile \
    email_automation/column_config.py \
    email_automation/messaging.py \
    email_automation/ai_processing.py \
    email_automation/processing.py
  git diff --check
  git status --short
  ```

- [ ] Record exact command counts and failures. A partial run cannot support a
  completion or deploy claim.

## Task 6: Stage a bounded deploy and live runbook

**Files:**

- Add: `docs/superpowers/runbooks/2026-08-11-finish-line-live-canaries.md`

- [ ] Record the immutable pre-deploy commit and currently healthy production
  revision as rollback identity, without user IDs or mailboxes.
- [ ] Require counter reset, one available row per case, blank target facts,
  intact same-row Gross formula, follow-ups off, queues/residue zero, and exact
  current-turn recipient authorization.
- [ ] Define independent source/Sheet and control-plane watchers for each case.
- [ ] Define per-turn cardinalities, stop gates, containment, and rollback.
- [ ] Deploy only after Tasks 1-5 pass; route one revision at 100% only after
  readiness and health checks. Do not open broad automation or add users.

## Task 7: Live-prove copied-party reply-all on one existing row

- [ ] Get current-turn authorization naming all three exact self-owned
  mailboxes. No inferred address is acceptable.
- [ ] Send one full-facts reply in the existing controlled thread with one safe
  copied party and Bcc empty.
- [ ] Require exact automatic reply audience: canonical broker mailbox in To,
  safe copied party in Cc, no product self, plus alias, Bcc, duplicate, or
  unknown recipient.
- [ ] Require one inbound, one automatic send/index, exact row facts and Gross,
  one completion, other rows unchanged, queues/residue/errors zero.

## Task 8: Live-prove mixed-PDF escalation on one existing row

- [ ] Get current-turn authorization for the exact self-owned sender/recipient.
- [ ] Render the target identity only at runtime into the verified synthetic PDF.
- [ ] Send once in the existing thread; require no automatic reply, scalar write,
  row-level link/image/AI_META/change log, new row, or terminal state.
- [ ] Require exactly one review action and paused active lifecycle; preserve the
  message-level attachment for review; other rows/formulas unchanged.

## Task 9: Live-prove long-turn correction, pause/resume, and truncation

- [ ] Get current-turn authorization for the exact self-owned sender/recipient.
- [ ] Use one untouched row and the approved thirteen-message sequence from the
  design: SF/adversarial repeat; Rent+call pause; Dashboard continuation; SF
  correction; Rent correction; reconfirmation while withholding OpEx; final
  OpEx/reconfirmation and one close.
- [ ] At every automatic turn, inspect the new body and require requested fields
  to equal the authoritative missing set—never merely overlap it.
- [ ] Require the call turn to pause with zero automatic send and the monitored
  continuation to resume safely.
- [ ] Before final proposal, prove more than ten pre-close messages exist; after
  close, prove final corrected facts/Gross, exactly one terminal close, no
  correction loss, wrong-row effect, duplicate, or residue.
- [ ] Invoke one normal worker cycle after settlement and require zero send/index
  delta. Do not label this uncertain-provider retry proof.

## Task 10: Update bounded readiness evidence

**Files:** readiness registry, sanitized evidence note, generated views, and
readiness tests in the existing readiness worktree.

- [ ] Add only sanitized, release-bound, scope-bound live evidence.
- [ ] Close copied-party, PDF ambiguity, hard-repeat, or long-turn blockers only
  when their exact live acceptance criteria passed.
- [ ] Keep natural-language polish separate unless it caused functional harm.
- [ ] Keep autonomous follow-ups HOLD unless independently live-proven.
- [ ] Regenerate both views, run bare and fixed-time `--check`, run the readiness
  and release-safety suites, compile, diff-check, review, and commit.

## Completion boundary

Supervised use remains one monitored row at a time throughout this plan. The
smallest broader return-user recommendation may expand only to the exact
capabilities whose live blockers are closed. It must not silently enable
autonomous follow-ups, broad accounts, unverified PDF classes, or unspecified
recipient topologies.
