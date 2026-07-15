# Contact Redirect Source Identity Implementation Plan

**Goal:** Make reviewed Contact Redirect actions send exactly once to the referred contact and leave both the original and replacement conversations in the correct lifecycle state.

**Architecture:** Preserve one canonical source-message identity contract across the Python worker and React dashboard. Reuse the worker's existing recipient-difference routing to send fresh indexed outreach, then apply an explicit source-thread disposition only after send success.

**Tech Stack:** Python, Firestore, Microsoft Graph, React, Jest.

## Task 1: Lock the identity contract with failing tests

**Files:**
- Modify: `tests/test_source_message_envelope.py`
- Modify: `src/components/InlineReplyComposer.test.jsx` in the dashboard repository

1. Assert `_source_message_identity_meta` emits `replyToMessageId` equal to the Graph message ID.
2. Assert a wrong-contact notification containing `sourceGraphMessageId` but not `replyToMessageId` queues a Contact Redirect.
3. Assert the queued outbox uses the canonical Graph anchor and does not request source-thread resume.
4. Retain a fail-closed test where every source Graph alias is absent.
5. Run focused tests and confirm they fail for the missing behavior.

## Task 2: Emit and resolve canonical source identity

**Files:**
- Modify: `email_automation/processing.py`
- Modify: `src/components/InlineReplyComposer.jsx` in the dashboard repository
- Modify: `src/utils/actionAudit.js` in the dashboard repository

1. Emit the legacy reply alias alongside the canonical source fields.
2. Resolve the source Graph anchor from all supported aliases in the dashboard.
3. Carry Graph and Internet Message identities into the audit and outbox.
4. Classify the action as `contact_redirect` with source `dashboard_contact_redirect` while keeping exact-script sending.
5. Run the identity tests until green.

## Task 3: Finalize the source thread after durable send

**Files:**
- Modify: `tests/test_action_audit_backend.py`
- Modify: `email_automation/email.py`

1. Add a failing test for `sourceThreadDisposition: contact_redirected`.
2. Implement guarded source-thread finalization after Graph send success.
3. Prove the original thread becomes stopped, receives redirect evidence, and is not resumed.
4. Prove ordinary reviewed replies still resume only eligible open threads.

## Task 4: Verify campaign safety

**Files:**
- Test: `tests/test_outbox_reply_recipient_routing.py`
- Test: `tests/test_action_audit_backend.py`
- Test: `tests/test_source_message_envelope.py`
- Test: `src/components/InlineReplyComposer.test.jsx` in the dashboard repository
- Test: `src/utils/actionAudit.test.js` in the dashboard repository

1. Run focused frontend and backend suites.
2. Run the repository-prescribed outbound-email safety suites.
3. Run frontend production build and lint/test gates available in the repository.
4. Review the diff for recipient routing, idempotency, lifecycle, and unrelated changes.

## Task 5: Publish and prove production behavior

1. Commit each repository's scoped changes and push both branches.
2. Open reviewable pull requests, resolve actionable feedback, and merge only with green checks.
3. Confirm production deployments complete.
4. Use the authenticated browser to run a contained one-row BP21 Contact Redirect proof.
5. Capture the recipient, single-send identity, source-thread terminal state, new active thread, notification cleanup, and zero-queue evidence.
6. Update the active experiment, backlog card, and evidence checkpoint with the result.
