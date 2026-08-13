# Policy-blocked reply review design

Status: approved for implementation on 2026-08-12

## Cross-repository release and rollback invariant

Backend RC status: non-deployable on its own.

The first prerequisite is Firestore rules that make processingFailures owner-readable and server-write-only and exclude that collection from every generic or owner-write catchall. The second prerequisite is the projection-only UI guard and passive review card. Operators must deploy and certify both prerequisites before the backend producer.

Only after both prerequisite surfaces are certified may operators stage the backend producer as the deterministic `process-user-stage-<12-character-HEAD>` revision. Staging must use an immutable digest, `--no-traffic`, and no tag. Before building, the script must reject LATEST or implicit canonical `spec.traffic`, ambiguous routing or tags, a deterministic-candidate collision anywhere in the full service revision inventory, and an unreadable baseline positive revision. Readback must prove that the Ready candidate is untagged at 0 percent and matches the baseline revision configuration apart from immutable image and revision identity, while the prior sole 100 percent revision retains the stable `release-a` and every auxiliary pinned tag mapping with canonical `spec.traffic` unchanged. Staging does not pause or resume a queue, mutate traffic, certify an HTTP route, promote, or roll back.

In short: deploy the backend producer with no traffic before promotion.

Both global campaign switches remain false throughout this milestone. Operators must not call `POST /process-user` or perform a provider or mailbox canary.

Promotion uses the versioned `GET /health/identity/v1` route while preserving
the exact legacy `GET /health` and `/healthz` body. The controller must re-prove
the deployed rules, Hosting version/assets, both false switches, exact private
Cloud Run service and project-level invoker bindings on the parentless project,
with no broad principal, plus the exact named operator credential with every
Cloud SDK token/file/impersonation/account/project override absent, prior
revision/digest and routing, candidate immutable
digest/config/readiness, exact queue configuration, and zero listed tasks before
mutation. It pauses the queue, requires three empty snapshots over at least ten
seconds, assigns one HEAD-derived temporary tag, rejects unauthenticated access
and redirects, proves both health contracts on the exact candidate, removes the
tag, then reasserts PAUSED plus empty tasks immediately before promotion. It
revalidates candidate identity/digest/config and both health contracts after
promotion, reasserts the closed prerequisites and PAUSED/empty queue immediately
before resume, then performs a final closed-prerequisite readback. Any observed
task is sticky and forbids resume. Failure restores the old 100-percent target
and `release-a` only while PAUSED, and resumes only after exact old topology,
digest, health, IAM, switches, and zero tasks are proven; otherwise the state is
manual recovery and the controller must never claim that an unproved queue is
paused.

For rollback, roll back or disable the backend producer first. Operators must retain the hardened UI and server-write-only rules while any reply-review projection or projection-recovery row may remain. Operators must never restore client write access to processingFailures without a separately reviewed migration or removal of every affected row.

## Outcome

When an otherwise valid automatic inbox reply is stopped by the existing
`blocked_auto_reply_policy` allowlist guard, the first failed attempt will
create one durable manual-review record and one visible notification. It will
not enter `pendingResponses`, query Sent Items, create an outbox item, call a
provider, or resume the thread.

The current UI will show the preserved draft in the exact paused conversation
as a read-only review notice. It will not offer Save, Send, Dismiss, Mark
handled, or any other mutation until a separately designed authenticated API
exists.

This is an existing-row supervised-response improvement. It does not change
the production capability gates: login/view remains GO, the bounded existing-
row response lane remains GO, and launch, creation, follow-ups, broad access,
and unattended automation remain HOLD.

## Why this is the next bounded milestone

The allowlist guard itself is correct: it prevents an unapproved automatic
send before any Graph effect. The defect is what happens next. The generic
failure path currently stores the draft in `pendingResponses`, and the pending
worker treats the policy decision like a transport failure. It may perform
Sent Items lookups and retry the same local policy block until a random dead-
letter record is created after five attempts.

That behavior adds no user value and obscures the actual decision. A policy
block is deterministic operator work, not a transient delivery error.

## Scope and repository lineages

### Backend

Implementation starts from the clean finish-line integration lineage at
`e54241529f128933203d61789cff1f9fcf7211b4`. Before feature work, the additive
SiteSift #77 backend contract commits through
`406b0e843391a653051b193d06b328727e6351c3` are replayed in order and verified
for patch equivalence. This preserves the already completed offline capability
and recovery-payload work on one cumulative branch.

### UI

Implementation starts from the clean, pushed SiteSift #77 UI lineage at
`a9ed9b52fa620e1655590f3ac05962a985b82553` in a separate isolated worktree.

The canonical checkouts remain untouched because they contain unrelated dirty
state.

## Backend contract

### Stable identity

Each review is identified per source reply intent:

```text
reviewId = sha256("blocked-auto-reply:v1\n" + threadId + "\n" + sourceMessageId)
```

An independent `intentHash` is the SHA-256 of canonical JSON containing only
the immutable identity and draft fields:

```json
{
  "clientId": "...",
  "conversationId": "...",
  "recipient": "...",
  "responseBody": "...",
  "sourceMessageId": "...",
  "subject": null,
  "terminalDisposition": null,
  "threadId": "..."
}
```

The review ID deduplicates ordinary worker replay. The intent hash prevents a
different draft or identity packet from being silently written under the same
source-message identity. An exact replay is a no-op; an identity or intent
conflict fails closed.

`subject` is deliberately nullable. The automatic-reply policy guard runs
before `send_reply_in_thread` resolves the Graph subject, and the guard must not
be moved or weakened to populate a display field. Recipient and response body
are required; subject and conversation identity are preserved when available.

### Review record

The authoritative projection is written at
`users/{uid}/deadLetterQueue/{reviewId}` with this closed schema:

```json
{
  "recordType": "reply_review",
  "schemaVersion": 1,
  "reviewId": "...",
  "failureCode": "blocked_auto_reply_policy",
  "status": "needs_review",
  "recoveryStatus": "needs_review",
  "manualActionRequired": true,
  "automaticRetryAllowed": false,
  "alreadySent": false,
  "source": "autoResponse",
  "clientId": "...",
  "threadId": "...",
  "sourceMessageId": "...",
  "conversationId": null,
  "recipient": "...",
  "subject": null,
  "responseBody": "...",
  "terminalDisposition": null,
  "draftVersion": 1,
  "intentHash": "...",
  "notificationId": "...",
  "createdAt": "SERVER_TIMESTAMP",
  "updatedAt": "SERVER_TIMESTAMP"
}
```

The record deliberately omits `retryable: false`. Existing health code treats
that legacy field as resolved/terminal and would hide unresolved operator
work. `automaticRetryAllowed: false` is the explicit policy field.

No new generic dead-letter behavior is inferred from this record. A later
authenticated action service will own its lifecycle.

### Notification projection

The same transaction creates a deterministic `action_needed` notification on
the exact client:

```json
{
  "kind": "action_needed",
  "priority": "important",
  "threadId": "...",
  "meta": {
    "reason": "reply_review_required",
    "failureCode": "blocked_auto_reply_policy",
    "reviewActionMode": "projection_only",
    "reviewId": "...",
    "sourceMessageId": "...",
    "suggestedEmail": {
      "to": ["..."],
      "subject": null,
      "body": "..."
    }
  }
}
```

The notification ID is derived from a versioned dedupe key containing the
review ID. The client unread count and `notifCounts.action_needed` increment
only when the notification is first created.

### Atomic state transition

One Firestore transaction reads the client, thread, review, and notification
before writing. It requires the exact client and thread documents to exist.
For a new review it atomically:

1. writes the review record;
2. writes the notification;
3. increments client notification rollups once; and
4. pauses automated continuation on the exact thread by setting
   `status=action_needed`, `statusReason=blocked_auto_reply_policy`,
   `followUpStatus=stopped`, disabling the follow-up config, and clearing any
   pending follow-up time plus `processingBy`, `processingAt`, and
   `processingLeaseUntil`.

The transaction does not clear `followUpSendAttempt.leaseUntil`. That field is
the separate provider-reconciliation marker for an uncertain send, not the
worker processing lease, and preserving it avoids erasing delivery-uncertainty
evidence.

There is no intermediate state where a review exists without its notification
or where a notification is visible while automatic follow-up remains eligible.

Missing identities, missing client/thread documents, an unexpected preexisting
notification, or an intent conflict fail closed. Projection failure never
falls through to `pendingResponses`. It raises a retryable processing error so
the inbound item remains visible without provider or outbox effects.

### Projection failure recovery

The inner projection branch performs no failure write. It raises one typed
error carrying a closed, bounded intent to the outer inbox boundary. That
boundary writes exactly one `processingFailures` row under a
fixed-length domain-separated SHA-256 document ID derived from length-prefixed
UTF-8 thread and canonical processed-key identities. Raw identities never
appear in the document ID; they remain only in the closed recovery envelope. The canonical
processed key is the RFC message ID when present and otherwise the Graph
message ID. The recovery packet binds that canonical key, Graph message ID,
RFC message ID, client, thread, recipient, body, nullable subject/conversation,
and validated terminal disposition.

An ordinary inbox scan reads that exact failure document before batching. A
valid pending packet, an unreadable lookup, or a projection-shaped invalid
packet blocks ordinary pipeline replay, so Sheet/event work cannot be repeated.
The dedicated processing-failure worker honors campaign terminal suppression,
validates the packet's exact keys, version, identity, types, and bounds, then
calls only the idempotent review projection. It does not fetch Graph, rerun the
inbox pipeline, write an outbox/pending response, or repeat Sheet/event work.
On success it writes deduplicated processed markers for the canonical, Graph,
and RFC identities before deleting the failure row. A projection, marker, or
delete failure leaves the bounded recovery visible and retryable.

Campaign suppression never replaces a reply-review recovery status. Temporary
suppression preserves the pending status and retryability for a later direct
projection attempt. Terminal suppression sets `retryable=false` while retaining
the pending or manual status and recording only separate
`automationSuppressed*` metadata.

If the original draft intent itself is invalid or exceeds a closed bound, the
outer boundary writes a non-retryable identity-only manual-recovery envelope.
It contains only the stable recovery kind/version/failure code plus exact
client, thread, canonical, Graph, and RFC message IDs; it never persists or
logs the invalid recipient, body, subject, or terminal data. Ordinary scans,
the retry worker, and stale-failure reconciliation all leave this packet inert
and visible for manual remediation instead of replaying prior side effects.

This recovery packet becomes authoritative only after the server-write-only
Firestore prerequisite above is deployed and certified. Backend code alone
must never be deployed while owner clients can rewrite `processingFailures`.

### Processing branch

`_queue_response_retry_or_reconciliation` handles
`blocked_auto_reply_policy` before the generic pending-response branch. A
successful projection returns `review_required`.

`_handle_auto_response_send_failure` treats `review_required` as handled for
the current message so the same processing pass cannot prepare and attempt a
second automatic reply. It does not treat the draft as delivered. Deferred
closing-reply completion explicitly remains unresolved for this outcome, so a
policy-blocked closing draft cannot complete the client.

The existing branches for campaign terminal state, recipient opt-out, sent-
but-unindexed reconciliation, unsafe bodies, kill-switch suppression, and true
transport failures retain their current meanings.

### Legacy pending-response conversion

The worker recognizes only the exact historic policy-block signature before
campaign gating, Sent Items checks, attempt increments, or sending:

```text
Automatic inbox replies are disabled for this user; manual review required before auto-reply
```

It transactionally converts that pending document into the same deterministic
review, records the source pending ID, and deletes the pending document. An
exact replay is idempotent. Conversion failure leaves the pending document
unchanged, records a local operation error, and performs no provider access.

Legacy provenance requires the exact historic `lastError`; a caller-supplied
`failureCode` alone is insufficient. The pending document ID must equal its
stored thread ID and `attempts` must be a positive non-boolean integer, matching
the closed shape created by the old first-attempt queue path.

This milestone does not rewrite arbitrary exhausted dead-letter records or
infer policy provenance from broad phrases such as "manual review".

## UI contract

### Classification

Add an explicit predicate for a projection-only reply review:

```js
reason === 'reply_review_required' &&
meta.reviewActionMode === 'projection_only'
```

Such a notification is never sendable and is excluded from the existing
composer/mutation selector even though older `reply_review_required`
notifications retain their current compatibility behavior.

It is still a current dashboard attention item. The client action count and
action stack include it as a safe navigation target labeled `Review Draft`.
Selecting it only expands and scrolls to the read-only card; it does not imply
send authority. This separates discoverability from mutability instead of
hiding unresolved review work because it is intentionally non-sendable.

### Exact-thread placement

Projection-only reviews match only `notification.threadId` or
`notification.meta.threadId` against the exact underlying document IDs in the
grouped conversation. They never use row anchor, address, subject, email, or
substring fallback.

The expanded matching conversation renders one
`PolicyBlockedReplyReviewNotice`. The card displays:

- `Manual review required`;
- `Saved draft`;
- the preserved recipient and body;
- the preserved subject, or `Subject unavailable` when the early policy guard
  correctly stopped processing before subject resolution; and
- `Secure review actions are not enabled in this build.`

It has no buttons, editable fields, links that mutate state, Firestore writes,
Functions calls, outbox calls, retry behavior, or optimistic removal. Missing
required identity, recipient, or body fields fails closed to a compact
unavailable notice and still exposes no action. A missing subject alone is not
an invalid projection.

Existing sendable action notifications can continue to render their composer
independently. A projection-only review can never cause `InlineReplyComposer`
to mount.

## Security boundary and deferred action phase

The repository currently has no proven deployable end-user API that can safely
own review mutations:

- `/api/dismiss-notification` is unauthenticated and trusts caller-supplied
  user and object identifiers;
- the Cloud Run worker route is an internal processing surface, not Firebase
  user auth;
- direct frontend Firestore writes cannot be declared secure without a
  reviewed rules contract; and
- current generic reply outbox items do not reserve a policy-review decision
  before provider send.

Therefore this milestone exposes no review mutation.

A separate future phase may add authenticated exact-ID endpoints for get,
draft save, send reservation, and dismiss. That phase must derive the UID from
a verified Firebase token, reject cross-tenant identities, use optimistic draft
versions and one-use decision reservations, create at most one server-bound
outbox item, never call Graph inline, and keep send/dismiss failures visible.
It is not part of this implementation or rollout.

## Failure and observability rules

- A policy block creates no provider request, Sent Items read, outbox item, or
  pending-response item.
- An exact worker replay creates no duplicate record, notification, or counter
  increment.
- A same-ID/different-intent replay creates no writes and raises a stable
  conflict.
- A projection write failure does not fall through to a transport retry.
- A legacy conversion happens before all provider and attempt-count effects.
- Logs use stable failure codes and shortened opaque IDs; they do not log draft
  bodies or recipient addresses.
- Historical implementation boundary: the original source milestone performed
  no deployment, live mailbox action, gate change, or external communication.
  The later 2026-08-12 Option A approval supersedes only its no-deployment
  clause for the closed rules -> passive UI -> tagless backend Phase 1 rollout.
  It does not authorize a mailbox/provider canary, either global switch, Phase
  2 actions, or any external communication.

## Verification

Backend tests must prove:

1. deterministic identity and canonical hashing;
2. atomic review, notification, client counter, and thread-pause writes;
3. exact replay idempotence and intent-conflict rejection;
4. zero pending/provider/outbox effects for a new policy block;
5. projection failure remains retryable without generic queue fallback;
6. one processing pass cannot attempt a second automatic response;
7. blocked closing drafts do not complete clients;
8. exact legacy pending conversion before any Sent Items read or send;
9. conversion failure leaves the source document and attempt count unchanged;
10. unrelated pending-response and send-outcome branches remain green.

UI tests must prove:

1. projection-only review notifications are not sendable;
2. the projection remains a counted/clickable dashboard attention item whose
   click only navigates to the card;
3. older compatible review notifications are not silently reclassified;
4. exact thread ID matches and fuzzy row/address/subject matches do not;
5. the read-only card displays the preserved draft and boundary copy;
6. no Send, Save, Dismiss, Mark handled, or editable control exists;
7. `InlineReplyComposer` does not mount for the projection;
8. a missing identity fails closed; and
9. existing sendable action/composer behavior remains green.

## Refutation conditions

Implementation stops or is redesigned if any test or review shows that:

- creating or viewing a review can queue/send an email;
- the projection depends on fuzzy thread identity;
- a policy block can still enter the retry worker;
- duplicates increment counters or overwrite a different intent;
- unresolved review work is hidden from health/UI state;
- projection creation can leave follow-up automation eligible; or
- the UI needs the unauthenticated dismissal route to provide the promised
  value.

## Alternatives rejected

- **Keep using `pendingResponses`:** wrong retry semantics and unnecessary
  provider reads.
- **Use the outbox as draft storage:** an outbox item means queued-for-send and
  may be consumed by the scheduler.
- **Use the dormant NotificationsSidebar:** it duplicates classification and
  mutation logic and is not the established conversation path.
- **Expose send/dismiss now:** no trustworthy deployed user-auth boundary has
  been proven.
- **Silently drop the draft:** removes the retry loop but gives the user no
  recoverable work.

## Implementation order

1. Reconcile the additive #77 backend lineage onto the finish-line integration
   base.
2. Establish backend RED tests for the deterministic projection.
3. Implement the transaction and processing branch under TDD.
4. Establish and implement the exact legacy pending conversion.
5. Establish UI RED tests for non-sendable exact-thread projection and the
   read-only card.
6. Run focused and broad offline verification, independent specification and
   quality/security reviews, then push both branches without a PR or deploy.
