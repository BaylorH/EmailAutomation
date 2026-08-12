# Policy-Blocked Reply Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing automatic-reply policy guard into one deterministic first-attempt manual-review projection and show its saved draft read-only in the exact UI conversation, with zero provider, outbox, retry, deploy, or gate-widening effects.

**Architecture:** A new backend domain module owns stable review identity and one Firestore transaction spanning the review record, notification, client rollups, thread pause, and optional legacy pending deletion. Processing branches on the exact policy outcome before the generic retry queue. The UI adds an explicit projection-only classifier and a passive exact-thread card that cannot mount the existing reply composer.

**Tech Stack:** Python 3.12, Firestore transactions, `pytest`, React 19, Jest/Testing Library, CSS, Git worktrees, Git/GitHub branch push.

**Design:** `docs/superpowers/specs/2026-08-12-policy-blocked-reply-review-design.md`

---

## Cross-repository release and rollback invariant

Backend RC status: non-deployable on its own.

The first prerequisite is Firestore rules that make processingFailures owner-readable and server-write-only and exclude that collection from every generic or owner-write catchall. The second prerequisite is the projection-only UI guard and passive review card. Operators must deploy and certify both prerequisites before the backend producer.

Only after both prerequisite surfaces are certified may operators stage the backend producer as the deterministic `process-user-stage-<12-character-HEAD>` revision. Staging must use an immutable digest, `--no-traffic`, and no tag. Readback must prove that the Ready candidate is untagged at 0 percent while the prior sole 100 percent revision retains the stable `release-a` mapping. Staging does not pause or resume a queue, mutate traffic, certify an HTTP route, promote, or roll back.

Both global campaign switches remain false throughout this milestone. Operators must not call `POST /process-user` or perform a provider or mailbox canary.

For rollback, roll back or disable the backend producer first. Operators must retain the hardened UI and server-write-only rules while any reply-review projection or projection-recovery row may remain. Operators must never restore client write access to processingFailures without a separately reviewed migration or removal of every affected row.

Campaign suppression never replaces a reply-review recovery status. Temporary suppression preserves pending retryability for direct recovery after automation resumes; terminal suppression sets `retryable=false` while retaining the pending or manual status and writing only separate `automationSuppressed*` metadata.

Every reply-review recovery row uses one fixed-length domain-separated SHA-256 document ID derived from length-prefixed UTF-8 thread and canonical processed-key identities. Raw thread, Graph, and RFC identities remain only inside the closed recovery envelope; generic processing-failure document IDs are unchanged.

## File structure

### Backend repository

- Create `email_automation/reply_reviews.py`: stable IDs, closed payloads, exact legacy classifier, and atomic create/convert transaction.
- Create `tests/test_reply_reviews.py`: domain and transaction contract tests.
- Modify `email_automation/processing.py`: policy outcome branch, handled-without-delivery semantics, and projection failure propagation.
- Modify `email_automation/pending_responses.py`: exact legacy conversion before provider/attempt effects.
- Modify `tests/test_processing_reply_indexing.py`: new policy branch and no-fallback tests.
- Modify `tests/test_pending_responses.py`: legacy conversion ordering and failure tests.
- Modify `tests/test_processing_completion_guards.py`: closing draft cannot complete the client.
- Modify `tests/test_compound_nonviable_processing.py`: route-level retry-boundary and single-response regressions.
- Create/modify only the #77 contract files replayed in Task 0 before feature work.

### UI repository

- Modify `src/utils/actionNotifications.js`: explicit projection-only classification and exact-thread matching helpers.
- Modify `src/utils/actionNotifications.test.js`: sendability and compatibility regressions.
- Create `src/components/PolicyBlockedReplyReviewNotice.jsx`: passive saved-draft card.
- Create `src/components/PolicyBlockedReplyReviewNotice.test.jsx`: display and no-control contract.
- Create `src/styles/PolicyBlockedReplyReviewNotice.css`: scoped card styles.
- Modify `src/components/ClientRow.jsx`: label projection navigation as `Review Draft`.
- Modify `src/components/ClientRow.test.jsx`: safe navigation-label regression.
- Modify `src/components/ConversationsPanel.jsx`: exact projection selection, scroll target, and card placement independent of the composer.
- Modify `src/components/ConversationsPanel.test.jsx`: route-level exact-match and no-composer regressions.

### Task 0: Reconcile the cumulative backend base

**Files:**
- Replay only the eleven additive SiteSift #77 commits and their seven contract paths.

- [ ] **Step 1: Verify the isolated backend worktree and immutable bases**

Run:

```bash
git status --short
pre_replay=$(git rev-parse HEAD)
test -n "$pre_replay"
git rev-parse e54241529f128933203d61789cff1f9fcf7211b4
git rev-parse 406b0e843391a653051b193d06b328727e6351c3
```

Expected: clean status. The plan/spec commit is a descendant of the integration
base; save that exact clean commit as `pre_replay`. Never substitute local
`main`, which is a divergent checkout. The immutable original #77 parent is
`9e63704d3584966944814a93594d2be1e4b2fcb0`.

- [ ] **Step 2: Replay the exact additive commits in order**

```bash
git cherry-pick \
  6c81286 c6079e2 57e264d 60d0655 8a52ffc 69550bb \
  4d9cb51 df92371 211a0c1 d791b84 406b0e8
```

Expected: no conflict and only these paths appear across the replay range:

```text
contracts/campaign-capabilities-v2.json
email_automation/campaign_capabilities.py
email_automation/recovery_payload.py
tests/run_sitesift77_offline.py
tests/test_campaign_capabilities.py
tests/test_recovery_payload.py
tests/test_sitesift77_offline_runner.py
```

- [ ] **Step 3: Verify patch equivalence and focused contracts**

Run:

```bash
git range-diff --no-color \
  9e63704d3584966944814a93594d2be1e4b2fcb0..406b0e8 \
  "$pre_replay"..HEAD
git diff --exit-code 406b0e8 HEAD -- \
  contracts/campaign-capabilities-v2.json \
  email_automation/campaign_capabilities.py \
  email_automation/recovery_payload.py \
  tests/run_sitesift77_offline.py \
  tests/test_campaign_capabilities.py \
  tests/test_recovery_payload.py \
  tests/test_sitesift77_offline_runner.py
.venv/bin/python -m pytest -q \
  tests/test_campaign_capabilities.py \
  tests/test_recovery_payload.py \
  tests/test_sitesift77_offline_runner.py
git diff --name-status "$pre_replay"..HEAD
git diff --check "$pre_replay"..HEAD
```

Expected range-diff: eleven `=` rows. Expected name-status: exactly the seven
added paths listed above. If `.venv` does not exist, create it with pinned
Python 3.12 and install the repository requirements before running tests. Do
not commit the environment.

Expected: all replayed patches compare equivalently, focused tests pass, and
the diff check is clean. Stop on any semantic drift.

### Task 1: Deterministic review transaction

**Files:**
- Create: `email_automation/reply_reviews.py`
- Create: `tests/test_reply_reviews.py`

- [ ] **Step 1: Write the complete transaction RED**

Build Firestore fakes that enforce reads-before-writes and record transaction
operations. Add tests for:

```python
review_id = build_policy_blocked_reply_review_id(
    thread_id="thread-1",
    source_message_id="message-1",
)
assert review_id == hashlib.sha256(
    b"blocked-auto-reply:v1\nthread-1\nmessage-1"
).hexdigest()

created = create_policy_blocked_reply_review(
    user_id="uid-1",
    client_id="client-1",
    thread_id="thread-1",
    source_message_id="message-1",
    recipient="contact@example.test",
    response_body="Hi,\n\nThanks.",
    subject=None,  # Policy guard may run before subject resolution.
    conversation_id="conversation-1",
)
assert created.status == "created"
```

Assert the exact closed review and notification shapes, one counter increment,
the thread pause patch (including a cleared
`followUpConfig.processingLeaseUntil` while preserving the independent
`followUpSendAttempt` reconciliation marker), no `retryable: false`, and zero
`pendingResponses` or `outbox` writes. Add exact replay, different-intent
conflict, missing client, missing thread, preexisting-notification conflict,
and transaction exception cases. Recipient and response body are required;
nullable subject is a valid positive case and remains null in both projections.

- [ ] **Step 2: Run the focused test and confirm RED**

```bash
.venv/bin/python -m pytest -q tests/test_reply_reviews.py
```

Expected: import failure because `email_automation.reply_reviews` is absent.

- [ ] **Step 3: Implement stable identities and closed payload builders**

Use canonical JSON and frozen result/error types:

```python
POLICY_BLOCK_FAILURE_CODE = "blocked_auto_reply_policy"
PROJECTION_ONLY_MODE = "projection_only"

@dataclass(frozen=True)
class ReplyReviewProjection:
    review_id: str
    notification_id: str
    status: str  # "created" or "existing"

class ReplyReviewProjectionError(RuntimeError):
    pass

class ReplyReviewConflict(ReplyReviewProjectionError):
    pass
```

Reject blank IDs, recipients, or bodies before accessing Firestore. Bound
string fields to the existing outbound body/metadata constraints. Do not log
body or recipient content.

- [ ] **Step 4: Implement the atomic create transaction**

All transaction reads precede writes. For a new record, write review,
notification, client counters, and thread pause. For an exact existing record,
return `existing` without any write. A conflict raises a stable error and
performs no write.

Use deterministic document IDs rather than `.add()`. The helper may share the
existing counter calculation, but it must not call `write_notification`
outside the transaction.

- [ ] **Step 5: Run focused GREEN and commit**

```bash
.venv/bin/python -m pytest -q tests/test_reply_reviews.py
.venv/bin/python -m py_compile email_automation/reply_reviews.py tests/test_reply_reviews.py
git diff --check
git add email_automation/reply_reviews.py tests/test_reply_reviews.py
git commit -m "feat: project policy-blocked reply reviews"
```

Expected: every transaction test passes and the worktree is clean after commit.

### Task 2: Branch before the generic retry queue

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `tests/test_processing_reply_indexing.py`
- Modify: `tests/test_processing_completion_guards.py`
- Modify: `tests/test_compound_nonviable_processing.py`

- [ ] **Step 1: Write focused processing RED tests**

Add tests that set:

```python
processing._set_reply_send_outcome(
    error="Automatic inbox replies are disabled for this user; manual review required before auto-reply",
    outcome="blocked_auto_reply_policy",
)
```

Then assert:

- `create_policy_blocked_reply_review` is called once with exact identifiers and
  draft fields;
- the result is `review_required`;
- `queue_pending_response` and `record_sent_unindexed_response` are not called;
- `_handle_auto_response_send_failure` returns true only to stop another local
  reply attempt, not as evidence of delivery;
- a projection exception becomes `RetryableProcessingError` and never falls
  through to the queue; and
- deferred closing completion is skipped for the policy-review outcome.

Add a route-level regression where two response scenarios are possible in one
message and prove only the first policy projection occurs.

- [ ] **Step 2: Run named tests and confirm RED**

```bash
.venv/bin/python -m pytest -q \
  tests/test_processing_reply_indexing.py \
  tests/test_processing_completion_guards.py \
  tests/test_compound_nonviable_processing.py \
  -k 'policy_block or reply_review'
```

Expected: failures show the current `queued_retry` behavior and closing
completion bug.

- [ ] **Step 3: Implement the exact outcome branch**

Place it after campaign terminal/recipient/sent-unindexed decisions and before
`queue_pending_response`:

```python
if send_outcome.outcome == "blocked_auto_reply_policy":
    create_policy_blocked_reply_review(...)
    return "review_required"
```

Map `ReplyReviewProjectionError` to `RetryableProcessingError`. Add
`review_required` to the local handled outcomes, and explicitly treat
`blocked_auto_reply_policy` as unresolved in deferred completion.

- [ ] **Step 4: Run focused and adjacent GREEN**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reply_reviews.py \
  tests/test_processing_reply_indexing.py \
  tests/test_processing_completion_guards.py \
  tests/test_compound_nonviable_processing.py \
  tests/test_processing_reply_safety.py
```

Expected: new and existing send-outcome cases pass.

- [ ] **Step 5: Commit**

```bash
git add email_automation/processing.py \
  tests/test_processing_reply_indexing.py \
  tests/test_processing_completion_guards.py \
  tests/test_compound_nonviable_processing.py
git diff --cached --check
git commit -m "fix: stop retrying policy-blocked replies"
```

### Task 3: Convert exact legacy pending rows before provider access

**Files:**
- Modify: `email_automation/reply_reviews.py`
- Modify: `email_automation/pending_responses.py`
- Modify: `tests/test_reply_reviews.py`
- Modify: `tests/test_pending_responses.py`

- [ ] **Step 1: Write legacy conversion RED tests**

Test the exact historic failure string and prove conversion occurs before:

- `get_client_automation_decision`;
- `find_matching_sent_message_for_retry`;
- `find_sent_conversation_continuation_for_retry`;
- `send_reply_in_thread`; and
- any attempt update.

The conversion transaction must re-read and delete the exact pending document,
write `sourcePendingResponseId`, and create the same review projection. Add
negative controls for near-match/manual-review strings, exact replay, source
document disappearance, intent conflict, and transaction failure. Failure
leaves the source row and `attempts` unchanged.

- [ ] **Step 2: Run focused RED**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reply_reviews.py \
  tests/test_pending_responses.py \
  -k 'policy_block or reply_review'
```

Expected: the current worker reaches the provider/retry path.

- [ ] **Step 3: Implement exact classification and conversion**

Expose a pure classifier:

```python
def is_legacy_policy_blocked_pending_response(data: Mapping[str, Any]) -> bool:
    return data.get("lastError") == LEGACY_POLICY_BLOCK_ERROR
```

Call conversion immediately after reading the pending row fields and before
campaign/provider logic. On projection failure append a local error operation
state and continue without modifying the pending document. Require the source
document ID to equal its stored thread ID and `attempts` to be a positive
non-boolean integer; `failureCode` alone is not legacy provenance.

- [ ] **Step 4: Run full pending/processing GREEN and commit**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reply_reviews.py \
  tests/test_pending_responses.py \
  tests/test_processing_reply_indexing.py \
  tests/test_processing_completion_guards.py
git diff --check
git add email_automation/reply_reviews.py email_automation/pending_responses.py \
  tests/test_reply_reviews.py tests/test_pending_responses.py
git commit -m "fix: migrate blocked reply retries to review"
```

### Task 4: Create the isolated UI branch and establish RED

**Files:**
- Create a UI worktree from exact `a9ed9b52fa620e1655590f3ac05962a985b82553`.
- Modify/create only the UI files listed above.

- [ ] **Step 1: Create and verify the isolated UI worktree**

```bash
git worktree add \
  /Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/policy-blocked-reply-review-20260812 \
  -b codex/policy-blocked-reply-review-ui-20260812 \
  a9ed9b52fa620e1655590f3ac05962a985b82553
git -C /Users/baylorharrison/.config/superpowers/worktrees/email-admin-ui/policy-blocked-reply-review-20260812 \
  status --short
```

Expected: clean worktree at the exact SiteSift #77 head.

- [ ] **Step 2: Write classifier and exact-match RED tests**

In `actionNotifications.test.js`, create a notification with:

```js
const review = {
  id: 'notification-1',
  kind: 'action_needed',
  threadId: 'thread-1',
  rowAnchor: 'Shared Address',
  meta: {
    reason: 'reply_review_required',
    reviewActionMode: 'projection_only',
    reviewId: 'review-1',
    sourceMessageId: 'message-1',
    suggestedEmail: {
      to: ['contact@example.test'],
      subject: 'Re: Example',
      body: 'Hi,\n\nThanks.'
    }
  }
};
```

Assert it is projection-only, not sendable, matches `thread-1`, and does not
match another thread with the same row anchor/subject. Preserve a control that
an older review notification without `projection_only` retains compatibility.
Also assert it remains a current/renderable dashboard attention item when the
exact thread exists, so it is counted and can navigate to the review card.

- [ ] **Step 3: Write card and panel RED tests**

Assert the card shows the saved draft and boundary copy, has no textbox/button/
link, and handles missing required identity, recipient, or body without offering
action. A null subject renders `Subject unavailable` without invalidating the
otherwise complete projection. At panel level,
assert an expanded exact thread renders the card and not
`InlineReplyComposer`; a fuzzy-only thread renders neither card nor composer.
At row level, assert the safe navigation button says `Review Draft` and only
invokes the existing conversation navigation callback.

- [ ] **Step 4: Run focused RED**

```bash
CI=true npm test -- --runInBand \
  src/utils/actionNotifications.test.js \
  src/components/PolicyBlockedReplyReviewNotice.test.jsx \
  src/components/ClientRow.test.jsx \
  src/components/ConversationsPanel.test.jsx
```

Expected: missing exports/component and current sendable classification cause
the selected tests to fail.

### Task 5: Implement the passive exact-thread UI

**Files:**
- Modify: `src/utils/actionNotifications.js`
- Modify: `src/utils/actionNotifications.test.js`
- Create: `src/components/PolicyBlockedReplyReviewNotice.jsx`
- Create: `src/components/PolicyBlockedReplyReviewNotice.test.jsx`
- Create: `src/styles/PolicyBlockedReplyReviewNotice.css`
- Modify: `src/components/ClientRow.jsx`
- Modify: `src/components/ClientRow.test.jsx`
- Modify: `src/components/ConversationsPanel.jsx`
- Modify: `src/components/ConversationsPanel.test.jsx`

- [ ] **Step 1: Implement closed classifiers**

```js
export function isProjectionOnlyReplyReviewNotification(notification) {
  return notification?.kind === 'action_needed' &&
    notification?.meta?.reason === 'reply_review_required' &&
    notification?.meta?.reviewActionMode === 'projection_only';
}

export function projectionOnlyReplyReviewMatchesThread(notification, thread) {
  if (!isProjectionOnlyReplyReviewNotification(notification) || !thread) return false;
  const exactIds = new Set([thread.id, ...(thread.threadIds || [])].filter(Boolean));
  const notificationThreadId = notification.threadId || notification.meta?.threadId;
  return Boolean(notificationThreadId && exactIds.has(notificationThreadId));
}
```

Check the projection predicate before the legacy
`reason === 'reply_review_required'` sendable branch.

Keep projection-only reviews inside the current dashboard attention/rendering
selector when and only when their exact thread exists. Do not reclassify them
as sendable. This lets existing action counts and client-row navigation surface
the unresolved review without mounting a mutation UI.

- [ ] **Step 2: Implement the passive card**

The component accepts only `notification`. It extracts the closed projection
fields, renders plain text, and has no callbacks or mutation imports. Use a
`<section aria-label="Policy-blocked reply review">`, a `Saved draft` status,
and a fail-closed unavailable state for missing review/thread/source IDs.

- [ ] **Step 3: Wire exact selection independently from the composer**

In `ConversationsPanel`, derive:

```js
const projectionReview = findProjectionOnlyReplyReviewForThread(
  thread,
  allClientNotifs
);
```

Render its card inside the expanded conversation and register its exact
notification ID in `actionInputRefs`. Keep `matchingNotification` restricted
to sendable actions. Update `notificationMatchesThread` so projection-only
items cannot use fuzzy row fallback. Label the client-row navigation target
`Review Draft`.

- [ ] **Step 4: Run focused GREEN and adjacent regressions**

```bash
CI=true npm test -- --runInBand \
  src/utils/actionNotifications.test.js \
  src/components/PolicyBlockedReplyReviewNotice.test.jsx \
  src/components/ClientRow.test.jsx \
  src/components/ConversationsPanel.test.jsx \
  src/components/InlineReplyComposer.test.jsx \
  src/contexts/NotificationsContext.test.js \
  src/contexts/NotificationsContext.test.jsx
```

Expected: all selected suites pass and no snapshot/update broadens mutation
behavior.

- [ ] **Step 5: Commit the UI slice**

```bash
git diff --check
git add src/utils/actionNotifications.js src/utils/actionNotifications.test.js \
  src/components/PolicyBlockedReplyReviewNotice.jsx \
  src/components/PolicyBlockedReplyReviewNotice.test.jsx \
  src/styles/PolicyBlockedReplyReviewNotice.css \
  src/components/ClientRow.jsx src/components/ClientRow.test.jsx \
  src/components/ConversationsPanel.jsx \
  src/components/ConversationsPanel.test.jsx
git commit -m "feat: show policy-blocked reply reviews"
```

### Task 6: Broad offline verification and independent reviews

**Files:**
- No production edits unless a concrete review finding is fixed under a fresh
  RED→GREEN cycle.

- [ ] **Step 1: Run backend verification**

```bash
.venv/bin/python -m pytest -q \
  tests/test_reply_reviews.py \
  tests/test_processing_reply_indexing.py \
  tests/test_processing_completion_guards.py \
  tests/test_processing_reply_safety.py \
  tests/test_pending_responses.py \
  tests/test_dead_letter_visibility.py \
  tests/test_system_health.py
.venv/bin/python -m pytest -q
.venv/bin/python -m py_compile \
  email_automation/reply_reviews.py \
  email_automation/processing.py \
  email_automation/pending_responses.py
git diff --check e542415..HEAD
git status --short
```

If the complete suite has an environment-only blocker, record the exact
failure and still require every affected focused/adjacent suite to be green.

- [ ] **Step 2: Run UI verification**

```bash
CI=true npm test -- --runInBand
npm run build
npm run test:sitesift77:harness
npm run test:sitesift77:functions
git diff --check a9ed9b5..HEAD
git status --short
```

All commands are offline/local. Do not start the Firestore emulator route again
unless a changed file invalidates its accepted evidence; this slice does not
touch the #77 supervisor, profiles, proof runner, policies, or Functions
contracts.

- [ ] **Step 3: Request two independent reviews per changed repository**

One reviewer checks specification/data-flow compliance. A separate reviewer
checks code quality, concurrency, security, and test strength. Require explicit
APPROVE with no P0/P1/P2 on exact SHAs. Any concrete finding gets a strict
tests-first follow-up commit and both reviews rerun.

Review prompts must explicitly inspect:

- zero provider/outbox/pending effects on policy block;
- Firestore read-before-write and exact replay/conflict behavior;
- client counter and thread-pause atomicity;
- legacy conversion ordering;
- no hidden completion/resume/follow-up path;
- projection-only not sendable;
- exact thread binding with no fuzzy fallback; and
- absence of UI mutation routes/callbacks/retries.

- [ ] **Step 4: Record final receipts**

Capture exact branch heads, commits, test counts, diff scope, clean status, and
review verdicts. Do not create a deployment claim or alter readiness evidence;
this code has not been deployed or live-certified.

### Task 7: Push durable branches without deploying

- [ ] **Step 1: Fetch and verify fast-forward safety**

For each repository, fetch the exact remote branch namespace, verify the local
branch contains its expected base, and confirm no force push is needed.

- [ ] **Step 2: Push only the two milestone branches**

```bash
git push -u origin codex/policy-blocked-reply-review-20260812
git push -u origin codex/policy-blocked-reply-review-ui-20260812
```

Expected: normal non-force pushes. Read back both remote SHAs and compare to
local. Do not open a PR, merge, deploy, call a provider, or mutate production
gates.

- [ ] **Step 3: Handoff**

Report the visible value, exact non-effects, branch/SHA receipts, test/review
evidence, and the next bounded milestone: design and deploy a Firebase-authenticated
exact-ID action API before enabling save/send/dismiss controls.
