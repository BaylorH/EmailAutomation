# Contact Redirect Source Identity Design

## Problem

The worker records the inbound Microsoft Graph message that caused a wrong-contact action under canonical `source...` metadata. The dashboard only accepts the older `replyToMessageId` alias before it will queue a reviewed action. A valid Contact Redirect can therefore appear complete in the UI but fail locally with “missing the original email.”

The existing dashboard also treats every reviewed action as a normal reply and asks the worker to resume the source thread after send. That is wrong for a redirect: the original contact has already said they are not responsible for the property.

## User-visible contract

1. A Contact Redirect remains a reviewed action. The user can inspect and edit the exact recipient and message before sending.
2. The sent email is new outreach to the referred contact. It is not a Microsoft Graph reply to the wrong contact and does not forward the private source conversation.
3. The new outreach keeps the same campaign, property row, and property subject internally so future replies populate the correct row and remain visible in the same campaign workflow.
4. One click creates one atomic action-audit/outbox pair. Retries cannot create a second logical action.
5. The wrong-contact notification remains visible until Microsoft confirms the send.
6. After confirmed send, the source thread is marked `stopped` with reason `contact_redirected`; it is never resumed. The newly indexed outreach thread is the active conversation with the referred contact.
7. If the source Graph message cannot be resolved from any supported identity field, sending fails closed and the action remains available for review.

## Identity contract

The worker emits the source Graph message ID under all supported aliases while old records remain in production:

- `replyToMessageId`
- `sourceMessageId`
- `sourceGraphMessageId`
- `sourceMessage.graphMessageId`

The dashboard resolves the Graph anchor in that order and carries the canonical identities into both the action audit and outbox. Internet Message IDs are preserved for evidence and correlation, but are never substituted for a Graph message ID.

The worker's existing recipient-routing guard remains authoritative. It verifies that the source Graph message belongs to the selected user's source thread and campaign. If the reviewed recipient differs from the original sender, the worker uses its existing fresh-send-and-index path instead of Graph reply.

## Post-send lifecycle

The dashboard sets `sourceThreadDisposition: contact_redirected` for Contact Redirect actions and does not set `resumeThreadOnSend`.

After a durable send, the worker validates that the source thread still exists, belongs to the same campaign, and is open. It then records:

- `status: stopped`
- `followUpStatus: stopped`
- `statusReason: contact_redirected`
- the reviewed destination email and contact name
- the outbox and action-audit IDs that caused the transition
- the send timestamp

A missing, mismatched, or already-terminal source thread is not modified. Send finalization continues so a successful email is never retried merely because cleanup could not be applied.

## Scope

This change does not automatically forward messages, bypass review, rewrite spreadsheet contact columns, or expose the original conversation to the referred contact. Those are separate product decisions. It repairs the production send contract and lifecycle for the existing reviewed Contact Redirect action.

## Verification

- Backend unit test proves every new source envelope exposes `replyToMessageId`.
- Frontend component test proves an existing notification with only `sourceGraphMessageId` queues successfully with canonical identity and redirect lifecycle metadata.
- Frontend test proves a notification missing every Graph anchor still fails closed.
- Backend finalization test proves successful redirect stops the source thread and does not resume it.
- Existing outbox recipient-routing and dashboard action-audit suites remain green.
- A contained BP21 browser campaign proves the reviewed redirect is sent once to the alias, the original notification clears after send, the original thread is terminal, and queues return to zero.
