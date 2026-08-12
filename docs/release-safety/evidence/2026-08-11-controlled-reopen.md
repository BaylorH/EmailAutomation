# Controlled reopen evidence — 2026-08-11

This is a sanitized aggregate record. It contains no contact details, communication text, document identifiers, or local locations.

## Release identity

- Backend commit: `62a7d59e434881e0a230395523b3e6df86dec1f6`
- Production revision: `process-user-00097-yus`
- Final control readback: `2026-08-12T04:05:30Z`

## Earlier bounded observations

- One controlled ten-row launch produced ten unique initial sends and ten unique indexes. The scoped queue drained, counters reconciled, and zero scoped residue remained.
- Five simple or correction extraction rows closed with formulas written to the same rows. No repeated ask was observed in those rows.
- Two unavailable rows reached terminal state with follow-ups stopped.

These observations remain useful history but were recorded against the preceding release. The current decisions below rely on the final control readback, current-release PDF and long-turn proofs, and the narrowly reviewed carry-forward described below.

## Returning-user canary

- Row 8 received one full-spec reply and closed without another ask. The Sheet read back Total SF 21,600, Rent/SF/Yr 17.25, Ops Ex/SF 4.10, and same-row formula `G8*(H8+I8)/12` with result 38,430.00.
- Row 9 first received partial facts. It remained active, and the automatic response requested only operating expenses rather than repeating accepted availability, Total SF, or Rent/SF/Yr facts.
- Row 9 then received operating expenses plus a Total SF correction. The correction won: the Sheet read back Total SF 47,900, Rent/SF/Yr 15.35, Ops Ex/SF 3.85, and same-row formula `G9*(H9+I9)/12` with result 76,640.00. The row closed without another ask.
- The dashboard showed both target rows completed at the end. Every other campaign row remained unchanged.
- Automatic-send and message-index counters reconciled at 20/20. Action, pending, reconciliation, duplicate, error, dead-letter, claim, task, and scoped-residue readbacks were zero.
- Campaign controls read back Closed/Closed. Queued, blocked, pending, dead-letter, stopping, and stop-failed counts were all zero.
- Production revision `process-user-00092-som` reported 100 percent health with zero application errors and zero 5xx responses at `2026-08-11T17:28:58Z`.

The replies were functionally safe but do not clear natural voice. One close contained jammed punctuation and stock follow-up phrasing; the final close used a spaced hyphen and familiar stock phrasing. The partial response itself was clean and requested only the missing field.

## Current control readback

- Login and view remained available on the finish-line release.
- The current production revision was healthy and held sole 100 percent serving traffic. Finish-line product send and index counters reconciled at 7/7.
- The queue drained with zero scoped residue, zero application errors, and zero 5xx responses.
- Controls read back Closed/Closed, the exact client remained paused outside deliberate admission, and no send-capable residue remained in the audited workspace scope.

## Finish-line certification

### Copied-party reply-all

- One monitored copied-party response produced one inbound index, one automatic send and index, and one terminal close.
- The automatic response used the canonical To, retained exactly one safe copied Cc, left Bcc empty, and contained no product self, alias, duplicate, or unknown audience.
- The Sheet read back Total SF 52,400, Rent/SF/Yr 14.80, Ops Ex/SF 3.95, the same-row live formula preserved, and Gross 81,875.00.
- Follow-ups stopped at completion. Send and index counts reconciled, every other row remained unchanged, and scoped residue was zero.

This closes the bounded copied-party live gap for the exercised same-thread automatic-response shape. It does not claim arbitrary participant graphs, blocked-party combinations, cross-tenant use, or autonomous follow-ups.

The copied-party live proof ran on the immediately preceding release. The intervening fix was attachment-only: it coalesced ambiguous attachment review actions and did not touch reply-all routing, recipient filtering, outbound send, fact extraction, same-row Sheet update, or terminal lifecycle handling. A narrow source review therefore carries only this exact copied-party result forward to the current release; this was not a second live send.

### Ambiguous mixed-property PDF

- The first attempt produced a duplicate review action and triggered the required hard stop. It was rejected as certification evidence.
- Fix commit `62a7d59` coalesced the ambiguous-attachment review action. A clean retry on the deployed candidate produced one inbound index, exactly one proposal audit, and exactly one review action with reason `multi_property_attachment`.
- The active lifecycle paused for review. There were zero automatic sends or indexes, zero terminal closes, and zero counter delta.
- Facts, assets, target-row values, formula, every other row, and the Sheet remained unchanged. Duplicate, reconciliation, failure, and queue residue were zero after drain.

This closes the bounded mixed-property ambiguity gap for the exercised synthetic shape. It proves fail-closed review routing, not broad PDF extraction quality.

### Thirteen-message correction and ordering flow

- The settled conversation contained exactly 13 messages: one baseline outbound, six controlled inbounds, one Dashboard continuation, and five automatic responses. Product send and index counters increased by exactly six.
- A call request produced one pause and one call action with no automatic send. The monitored Dashboard continuation sent and indexed once, resolved the call action, resumed the thread, and requested only operating expenses.
- Later corrections replaced the earlier Total SF and Rent values without ordering loss. Every product response requested only the exact missing field and never repeated a known field.
- The Sheet read back final Total SF 40,800, Rent/SF/Yr 15.10, Ops Ex/SF 3.75, the same-row live formula preserved, and Gross 64,090.00 before one terminal close.
- All product outbounds used the canonical audience with Cc and Bcc empty. Every other row remained unchanged and scoped residue was zero.
- One ordinary worker cycle after settlement produced zero send, index, counter, fact, asset, lifecycle, action, or Sheet delta. This is idempotent replay evidence, not uncertain-provider recovery evidence.

This closes the bounded hard-repeat-ask and long-multiturn-ordering gaps for the exercised correction, call-pause, Dashboard-resume, and terminal-close spine. Natural voice remains an open, nonblocking quality item.

## Current capability boundary

- Login and view: GO.
- Supervised campaign use: GO only for one deliberately admitted, continuously monitored, one-row, one-property existing campaign for one user at a time, with follow-ups off. This does not authorize a new campaign launch.
- Controls stay Closed/Closed and the exact client stays paused until deliberate admission.
- Autonomous campaign use: HOLD. Autonomous follow-ups are the sole remaining named readiness blocker.

## Explicit non-claims

This record does not clear autonomous follow-ups, natural voice variety, broad campaign creation, simultaneous or multirow campaigns, cross-tenant use, uncertain-send recovery, unattended recovery, arbitrary attachment layouts, arbitrary copied-party graphs, or broad unattended use.
