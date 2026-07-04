# Safety Rails & Rollback Runbook

**Scope:** Outbound email automation (SiteSift / EmailAutomation). This is the operator
runbook for the live safety rails, how to flip each one, where failures surface, how to
roll forward (reopen users) and roll back (halt fast), and an honest list of what is
**not** yet enforced.

**Audience:** on-call operator during a staged production launch.

**Verified against worktree:** `codex/safety-rails-observability-20260704`
Full suite: **787 tests, OK (1 skipped)** · `git diff --check` clean · all modified
`.py` `py_compile` clean.

> **One-line panic button:** set `SITESIFT_OUTBOUND_MODE=paused` and redeploy/restart the
> worker. Every outbound path fails closed before it touches Microsoft Graph. Details in
> [§ Halt fast](#halt-fast-emergency-stop).

---

## The live rails

Five rails gate outbound mail. They are layered — each is independent, and several
overlap on purpose (defense in depth). All defaults are the SAFE / fail-closed value, so
an unset or corrupt env var never opens the floodgates.

| # | Rail | Control | Default | Fails… |
|---|------|---------|---------|--------|
| 1 | Outbound body validation | *(always on, no flag)* | on | closed (blocks send) |
| 2 | Daily send caps | `SITESIFT_DAILY_SEND_CAP`, `SITESIFT_GLOBAL_DAILY_SEND_CAP` | 500 / off | closed (retains queue) |
| 3 | Global outbound kill switch | `SITESIFT_OUTBOUND_MODE` | `live` | closed (→ `paused`) |
| 4 | Per-user allowlists | `SITESIFT_AUTO_REPLY_ALLOWLIST`, `SITESIFT_TOUR_ACTION_ALLOWLIST`, `SITESIFT_SCHEDULER_ALLOWED_USER_IDS` | Baylor test UID only | closed (blocks user) |
| 5 | Health cannot lie | `SITESIFT_SEND_HEALTH_ESCALATION`, `HEALTH_COUNT_ERROR_SEVERITY`, `DEAD_LETTER_ALERT_THRESHOLD` | on / `error` / 1 | closed (reports unhealthy) |

---

### Rail 1 — Outbound body validation

**What it does.** Every send path runs `validate_outbound_body()`
(`email_automation/outbound_safety.py:246`) before a message reaches Graph. It blocks:

- **Unresolved placeholders** (`{{first_name}}`, `[BROKER]`, etc.) — `find_unresolved_placeholders`.
- **Confidential-disclosure** language (client/tenant identity, credit details).
- **Fabricated approval / budget / financing** claims.
- **Unreviewed tour/LOI scheduling** language (unless the item is an explicitly reviewed
  tour invite, `allow_scheduling_language=True`).

**Where it is enforced (all six sinks):**
- `send_and_index_email` — `email_automation/email.py:2092`
- `send_outboxes` campaign drain — `email_automation/email.py:2935` (+ name-placeholder guard `:2926`)
- Dashboard thread reply — `email_automation/email.py:3262`
- Auto-reply — `email_automation/processing.py:2682`
- Follow-up — `email_automation/followup.py:615`
- Pending-response retry — `email_automation/pending_responses.py:203`

**How to operate.** Nothing to flip — it is unconditional. When a body is blocked the item
is moved to the **dead-letter queue** with the failure reason (`manual review required
before sending`) and surfaces via Rail 5's dead-letter alert. A blocked body is never
retried automatically; an operator must fix the content and re-queue.

---

### Rail 2 — Daily send caps (aggregate ceiling)

**What it does.** Bounds a fleet-wide blast that per-item retry caps cannot catch. Two
scopes, both counted per UTC day against a shared Firestore counter:

- **Per-user:** `SITESIFT_DAILY_SEND_CAP` (default **500**).
- **Global (all users):** `SITESIFT_GLOBAL_DAILY_SEND_CAP` (default **off / unlimited**).

Set either to a positive integer to enable; `0` or negative disables that scope.
Enforced in `send_outboxes` (`email_automation/email.py:2659`, checks `:2688`/`:2714`,
increments `:2770`).

**Fail-closed behavior.** If the counter cannot be **read** or the post-send increment
cannot be **written**, the drain **stops and retains the outbox** (`email_automation/email.py:2701`,
`:2787`) rather than sending blind. A transient store blip never opens the floodgates.

**How to operate.**
- Lower the cap during ramp: `SITESIFT_DAILY_SEND_CAP=25`, raise as confidence grows.
- Cap state is written to `systemHealth` via `_record_send_cap_health` (status `warning`
  when the ceiling is hit, `error` when the counter is unavailable) so a stalled queue is
  observable.
- **Known limitation — soft ceiling:** the check is per-recipient-*batch* (`current >=
  cap`), evaluated between recipients. A single recipient carrying a large multi-property
  batch can overshoot the cap by up to (batch size − 1) before the next check fires. Treat
  the cap as "approximately N," not a hard byte-level limit. See [§ Not yet enforced](#what-is-not-yet-enforced).

---

### Rail 3 — Global outbound kill switch

**What it does.** A single env var gates **every** outbound send at the entrypoint,
*before any Graph call is made* (even metadata reads). Modes:

| `SITESIFT_OUTBOUND_MODE` | Behavior |
|---|---|
| unset / `live` | Normal sending. |
| `dry_run` | Suppress all sends; log intent. |
| `paused` | Suppress all sends. |
| anything else | **Fail closed to `paused`** (`email_automation/email.py:99`) with a logged warning. |

Enforced at both send entrypoints:
- `send_and_index_email` — `email_automation/email.py:2076`
- `_send_outbox_as_reply` — `email_automation/email.py:1398`

Resolved once per send; any value but `live` returns a `suppressedByKillSwitch` result and
emits an `outbound.suppressed_by_kill_switch` log event.

**How to flip it (the panic button).**
1. Set `SITESIFT_OUTBOUND_MODE=paused` in the worker/job environment
   (Cloud Run service env var, or the deploy's env file).
2. Redeploy / restart the worker so the process picks up the new env.
3. Confirm the next run logs `🛑 ... suppressed_by_kill_switch` and sends nothing.

To resume: set back to `live` (or unset) and redeploy.

---

### Rail 4 — Per-user allowlists (staged rollout gate)

**What it does.** Independently of the kill switch, each automated action class only runs
for allowlisted users. This is the primary mechanism for **opening / closing users** during
a staged launch.

| Env var | Gates | Default (unset) |
|---|---|---|
| `SITESIFT_AUTO_REPLY_ALLOWLIST` | Automatic inbox replies (`processing.py:2642`) | Baylor test UID only |
| `SITESIFT_TOUR_ACTION_ALLOWLIST` | Tour scheduling actions (`processing.py:2658`) | Baylor test UID only |
| `SITESIFT_SCHEDULER_ALLOWED_USER_IDS` | Which users the scheduler processes at all (`scheduler_scope.py:51`) | Baylor dev UID |

**Format.** Comma/whitespace-separated Firebase UIDs. The literal `*` means **all users**
(only for full GA). Unset = the built-in default (Baylor test lane), which is the SAFE
value — a wiped env var does not accidentally open everyone.

**How to operate.** See [§ Reopening users](#reopening-users-roll-forward).

---

### Rail 5 — Health cannot lie (observability)

**What it does.** Closes the "green while broken" gap: previously a broken Graph send
returned `None`, so health stayed healthy while outreach was down.

Three sub-controls:

1. **Send-path escalation** — `SITESIFT_SEND_HEALTH_ESCALATION` (default **on**).
   Wraps each send driver (`main.py:165`); an uncaught driver exception becomes a
   graph-state `error` instead of silently vanishing, and `_overall_status`
   (`system_health.py:90`) escalates. Set to `0/false/no/off` only as a deliberate
   rollback to legacy behavior.
2. **Unreadable-queue severity** — `HEALTH_COUNT_ERROR_SEVERITY` (default **`error`**).
   A queue count that could not be read is treated as UNKNOWN, never as empty
   (`system_health.py:98`). Operators may downgrade to `warning`; there is deliberately
   **no** value that lets an unreadable count report healthy.
3. **Dead-letter alert threshold** — `DEAD_LETTER_ALERT_THRESHOLD` (default **1**,
   clamped to a minimum of 1). Any active dead-letter item raises an **error-severity**
   alert; a read failure forces the alert (`app.py:2192`, `:2207`).

**Where dead-letter alerts surface.** The `/api/firestore-inspect` endpoint
(`app.py:2244`) returns `result["alert"]` — an `error`-severity object listing
`activeDeadLetters`, `needsReconciliation`, threshold, and a human message. This is the
operator's dead-letter pager: poll that endpoint (or the dashboard inspect view that reads
it) and treat any `alert.severity == "error"` as needing attention. Overall system status
is written to `systemHealth/emailAutomation` per user (`system_health.py:140`); queue
backlogs show as `warning`, token/graph/send failures and unreadable queues show as
`error`.

---

## Rollback & recovery procedures

### Reopening users (roll forward)

"Reopening" a user = adding their UID to the relevant allowlist(s) so automation runs for
them.

1. **Confirm the kill switch is `live`** (`SITESIFT_OUTBOUND_MODE` unset or `live`).
2. **Add the UID** to the needed allowlist(s), comma-separated:
   - Inbox auto-replies → `SITESIFT_AUTO_REPLY_ALLOWLIST=UID1,UID2`
   - Tour actions → `SITESIFT_TOUR_ACTION_ALLOWLIST=UID1,UID2`
   - Scheduler must also include them → `SITESIFT_SCHEDULER_ALLOWED_USER_IDS=UID1,UID2`
   - Keep the existing Baylor test UID in the list unless you intend to drop it.
3. **Redeploy / restart** the worker so env changes take effect.
4. **Watch one cycle** with a low cap (e.g. `SITESIFT_DAILY_SEND_CAP=25`) before raising.
5. **Verify health** at `/api/firestore-inspect` (`alert` null) and
   `systemHealth/emailAutomation` (`status: healthy`) before widening further.

Go wider gradually. Full GA is `*` on the allowlists — do that last, deliberately, not as
a shortcut.

### Halt fast (emergency stop)

If sends look wrong (wrong recipients, bad content, runaway volume):

1. **`SITESIFT_OUTBOUND_MODE=paused`** → redeploy/restart. This is the hardest, broadest
   stop: it gates *every* send at the entrypoint regardless of allowlist or cap.
2. If you can only touch one narrower knob and the problem is auto-replies or tours,
   **empty the offending allowlist** (set it to a single known-safe UID or a bogus value)
   and redeploy — but the kill switch is faster and total; prefer it.
3. **Drop the cap to a floor** (`SITESIFT_DAILY_SEND_CAP=1`) as a belt-and-suspenders
   throttle while you investigate.
4. **Triage the dead-letter queue** via `/api/firestore-inspect`. Blocked/failed items
   sit in `deadLetterQueue` with a `failureReason`; items Graph already accepted but that
   failed to index sit with `status: needs_reconciliation` and `alreadySent: true` —
   **do not re-queue those**, they were sent; reconcile manually.

### Rolling the code change back

This branch is additive (rails default-on). To restore pre-rail behavior without reverting
code, disable per rail via env:
- `SITESIFT_SEND_HEALTH_ESCALATION=off` (legacy silent-send health)
- `SITESIFT_DAILY_SEND_CAP=0` and unset the global cap (no aggregate ceiling)
- `HEALTH_COUNT_ERROR_SEVERITY=warning` (softer unreadable-queue severity)

The kill switch and body validation have no "off" — that is intentional. A full code
revert (`git revert` of the branch merge) is the only way to remove them, and should not
be done during a live incident.

---

## Adversarial verdict (send entrypoint)

Reviewed `send_and_index_email` (`email.py:2049`), `_send_outbox_as_reply`
(`email.py:1382`), and the `send_outboxes` drain (`email.py:2589`) against the three
threat classes.

- **Placeholder reaching Graph — BLOCKED.** `validate_outbound_body` runs on every sink
  (six call sites above) plus a name-placeholder dead-letter at `email.py:2926`. No path
  found where an unresolved placeholder reaches Graph.
- **Wrong recipient — GUARDED.** Opt-out filter (`email.py:2107`), recipient-format
  validation (`:2127`), reply-all recipient filtering with a hard "no safe recipients"
  stop (`:1494`). No path found.
- **Duplicate send — GUARDED.** `_sent_retry_reconciliation_result` checks Sent Items
  before every retry (`:2957`, `:3272`); a Graph-accepted-but-unindexed send becomes a
  `needs_reconciliation` item instead of a retry (`:3024`, `:3280`); the Sent-Items guard
  fails **closed** to dead-letter when it cannot verify (`:2984`, `:3294`). No path found.
- **Silent failure — PARTIAL.** *Driver-level* exceptions escalate graph health
  (`main.py:165`), but *per-item* Graph send failures are caught inside `send_outboxes`
  and land in dead-letter / retry, surfacing only as a queue-count **warning**
  (`system_health.py:100`) — not an immediate graph `error`. They are still visible (and
  the dead-letter alert is `error`-severity), just via the queue path, not the send path.
- **Health green while broken — GUARDED** for queue-read outages (`COUNT_ERROR` → `error`,
  `system_health.py:98`), token, and graph errors.

**Verdict: SHIP-SAFE for a staged (allowlisted) launch.** No live path was found where a
placeholder, wrong recipient, or duplicate reaches Graph. The residual gaps are
observability/enforcement *sharpness* issues (listed below), not send-safety holes.

---

## What is NOT yet enforced

Be honest with the next operator. These are known and accepted for the staged launch, not
oversights:

1. **The daily cap is a soft ceiling, not a hard limit.** It is checked per-recipient-batch
   (`email.py:2688`), so a single recipient with a large multi-property batch can overshoot
   by up to (batch size − 1). There is no per-message atomic reservation. Do not rely on it
   for a precise byte-exact quota.

2. **Per-item send failures do not turn graph-health `error` directly.** They surface as a
   dead-letter/pending **warning** via queue counts, escalating to an `error`-severity
   **dead-letter alert** only once items land in the queue — not the instant a Graph POST
   fails. There is no live-streaming per-send failure metric.

3. **`operation="graph_send"` is a decorative label.** It is passed to
   `exponential_backoff_request` (`email.py:1545`, `:2298`) but the function
   (`utils.py:267`) never reads it — there is no dedicated per-send-operation health hook
   at the HTTP layer. Health is derived from driver exceptions + queue counts, not from
   this label.

4. **`_send_outbox_as_reply` does not self-validate its body.** Unlike `send_and_index_email`
   (which re-runs `validate_outbound_body` at `:2092`), the reply sink trusts its caller.
   Its sole production caller validates first (`email.py:3262`), so it is safe today — but
   the guard lives in the caller, not the sink. A future second caller could bypass it.

5. **Send-cap health is a side record, not part of overall status.** `_record_send_cap_health`
   writes its own field; `_overall_status` (`system_health.py:90`) only factors
   token/graph state and the four queue counts. A fail-closed cap-counter outage halts
   sending immediately but only turns *overall* health `warning` indirectly, once the
   retained outbox count grows > 0.

6. **No automated pager / webhook.** Dead-letter and health signals are exposed at
   `/api/firestore-inspect` and `systemHealth/emailAutomation` but nothing pushes them to
   Slack/PagerDuty/email — an operator (or an external poller) must watch them.

7. **Allowlists gate auto-reply, tour, and scheduler scope — not raw outbox sends.** The
   campaign outbox drain is bounded by the kill switch and caps, not by
   `SITESIFT_AUTO_REPLY_ALLOWLIST` (see the code comment at `email.py:66`). Closing a user
   out of *outbound campaign* mail during an incident means the kill switch or emptying
   their queue, not the auto-reply allowlist.

---

*Generated verifying the `codex/safety-rails-observability-20260704` worktree — suite
787/OK(1 skipped), diff-check clean, py_compile clean. Runbook doc only; no code changed.*
