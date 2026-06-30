# SiteSift Backend Agent and Review Rules

## Plain-English Mission

This repository is the production email worker for SiteSiftAI. A merge to `main`
can cause Microsoft Graph emails to be sent by the scheduled worker. Treat every
outbound email, scheduler, inbox processor, follow-up, and recovery change as a
production safety decision.

## Current Recovery Context

After the first multi-user beta incident, production is intentionally narrowed:

- normal users should only use the core campaign automation lane,
- Tour Scheduling is not part of normal production emailing,
- Results and map work must not send email unless explicitly promoted later,
- Jill/live customer data is read-only unless an exact repair is approved,
- Baylor/BP21 accounts are the approved live-proof lane.

## Required Review Posture

When reviewing or editing this repo, prioritize these risks before style:

1. Wrong recipient or dropped CC/reply-all.
2. Duplicate send after Graph partially succeeds.
3. Unresolved placeholders such as `[NAME]` reaching an email.
4. Jill/MOHR signature or identity leaking to another user.
5. Tour/LOI scheduling copy sent from the core campaign lane.
6. Follow-up or auto-reply sent after a broker reply or manual user response.
7. Failed sends hidden from the dashboard/operator.
8. Firestore/Sheet state moved or deleted without durable audit evidence.
9. A debug or scheduler route processing more users than intended.
10. Tests that only assert implementation details and miss real broker language.

## Production Lanes

| Lane | Sends email? | Production rule |
|---|---:|---|
| Core campaign outreach | Yes | Allowed, but must pass recipient, placeholder, signature, dedupe, audit, and reply-all guards. |
| Inbox processing and extraction | Sometimes | May draft/respond only under the core campaign contract; must not schedule tours. |
| Dashboard manual replies | Yes after user action | Must remain cancelable/audited until Graph send succeeds. |
| Follow-ups | Yes autonomously | Must check inbound replies, manual Sent Items, terminal state, opt-out, and retry guards first. |
| Results/PDF/Map | No | Read-only report generation; no email side effects. |
| Tour Scheduling | Not for normal users | Future/controlled lane only; not core outreach. |
| Recovery/dead-letter | Only after explicit operator action | Must expose failures before retrying and protect against duplicate sends. |

## CodeRabbit Must Flag

- Direct Microsoft Graph send/reply calls outside the reviewed chokepoints.
- Scheduler changes that widen user scope or change processing order.
- Prompt/classifier changes without event fixtures and dashboard/sheet handling.
- UI-only entitlement checks for backend-capable behavior.
- New Firestore writers for the same state without transaction/audit reasoning.
- Recovery code that deletes evidence or retries sends silently.

## Tests Expected Before Production

For any outbound-email change, run or add targeted tests around:

- `tests/test_outbox_safety.py`
- `tests/test_action_audit_backend.py`
- `tests/test_outbox_reply_recipient_routing.py`
- `tests/test_followup_terminal_state.py`
- `tests/test_pending_responses.py`
- `tests/test_processing_completion_guards.py`
- `tests/test_processing_reply_safety.py`
- `tests/test_jill_june_regressions.py`
- `tests/test_scheduler_scope.py`
- `tests/test_scheduler_lease.py`

For scheduler changes, also prove the workflow command, scheduler scope, lease,
and per-user health readback.

## Golden Rule

If you cannot explain exactly which users, recipients, Firestore collections,
Google Sheet rows, and Graph threads a change can touch, do not treat it as
production-ready.
