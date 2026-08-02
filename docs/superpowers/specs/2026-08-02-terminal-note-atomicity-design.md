# Terminal Note Atomicity Design

**Status:** Approved for autonomous execution under the existing SiteSift production-readiness mission.

**Deliverable:** code

## Goal

Make a `property_unavailable` / requirements-mismatch transition recoverable and fail-closed: the system may not move or finalize a row, mark the event handled, or send its terminal acknowledgement unless the reason is durably present in the campaign Sheet.

## Reproduced Root Cause

`process_inbox_message` currently moves the row and finalizes Firestore thread state before attempting the Notes/Comments write. That write is wrapped in a nested catch that logs and continues. A missing notes column or Sheets failure therefore produces the exact customer-visible split state: a correct terminal reply and stopped/non-viable row with no durable reason.

## Commit Protocol

Google Sheets and Firestore cannot share one transaction, so this transition is a small idempotent saga:

1. Stage every Firestore thread root for the source row as non-sendable. This existing step remains first.
2. Build one stable terminal note using the inbound message timestamp, not wall-clock retry time, and merge it with the source row's existing Notes/Comments value.
3. Ensure the divider, then commit the destination-row copy, merged terminal note, and source-row deletion in one ordered Google Sheets `batchUpdate`. Missing column, read failure, or batch failure is fatal and retryable; the move and note succeed or fail together.
5. Reconcile the staged thread roots to the final row and stopped/non-viable state. Hidden `False`/zero results must not count as success.
6. Create the idempotent operator notification and mark the event handled only after note, row move, and state finalization succeed.
7. Continue to the response path only after the preceding evidence exists.

On retry after the Sheet batch committed but Firestore finalization did not, the row is already below the divider. An idempotent note helper validates or repairs the exact stable note before state/event finalization. The existing pending-terminal fields keep all staged roots ineligible for follow-up throughout any partial failure.

## Stable Note Identity

The human-readable note remains the current format produced by `_build_property_unavailable_comment`, including truthful requirements-mismatch language and alternate-property context. Its date comes from `receivedDateTime` (falling back to `sentDateTime`, then UTC now only for legacy messages without either timestamp), so a retry on another day does not create a second note.

The move helper uses the zero-based notes column in an `updateCells` request between its existing copy and delete operations, so columns beyond `Z` and punctuation in tab titles do not require A1 conversion on the normal path. The already-below repair path uses safe A1 construction. The Sheets retry wrapper retries explicit 429s but does not blindly replay ambiguous 5xx mutations; a later processing retry reconciles an ambiguous result by reading the committed row first.

## Required Invariants

- A terminal-note batch failure commits neither the row move nor the note and occurs before Firestore terminal finalization, event handling, or outbound reply.
- A missing Notes/Comments column fails closed and records a retryable processing failure.
- Reprocessing an already-written note does not duplicate it.
- An already-below row cannot be finalized or marked handled without the durable note.
- Requirements mismatch never gets described as property unavailability.
- All row-linked roots remain follow-up-ineligible after the initial stage, including on failure.
- No customer campaign, production flag, mailbox send, or external contact is part of this fix or its tests.

## Test Boundary

Extend the existing full `process_inbox_message` harness in `tests/test_compound_nonviable_processing.py`; do not prove only a detached helper. The harness must expose the note cell, move/finalization calls, handled events, reply calls, and staged Firestore roots.

Required red-green cases:

1. Atomic Sheet batch rejection: retryable error; neither note nor move commits; no Firestore finalization, handled event, or reply; all roots staged non-sendable.
2. Missing Notes/Comments column: same fail-closed outcome.
3. Successful Sheet helper contract: one batch contains insert, copy, note update, and delete; existing notes are preserved and the terminal note is appended once.
4. Already-below retry/legacy row: note is ensured idempotently before finalization; a note failure prevents handled state.
5. Requirements-mismatch full path: truthful note text is persisted.

## Non-Goals

- redesigning all campaign state transitions;
- changing model prompts or OpenAI behavior;
- deploying to production;
- running provider calls or sending mail;
- solving unrelated row-shift, completion, or notification behavior unless a required regression exposes it as part of this exact invariant.
