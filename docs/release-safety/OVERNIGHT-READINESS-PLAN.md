# Overnight production-readiness run — plan + live scorecard (2026-07-03 → 04)

**Goal:** by morning, an evidence-based verdict on bringing users back to production — every event, every dashboard interaction, and the messy ways brokers actually write, pressure-tested against the REAL logic, bugs found AND fixed, rubric gaps closed, scheduler advanced. Push to branches as progress lands.

**Why this run is different (not another "some progress" check-in):** it is goal-directed and convergent. Each surface runs **find → adversarially verify → fix (TDD) → prove (permanent test) → loop-until-dry**, and the run stops a surface only when N consecutive rounds find nothing new. Coverage is measured against the 10 readiness gates below, not "tests added."

## The readiness bar (scorecard — the actual verdict)
| # | Gate | State at start | Target |
|---|------|----------------|--------|
| 1 | Rubric green on Base-V1 (no borrowed greens) | 69/40/10 base; borrowed greens purged | 0 required needs_fixture on Base-V1 |
| 2 | 3 lanes (independent + integrated + full-campaign) real+passing | indep mostly; cross 2/8; campaign 0/9 | all 3 lanes real per base feature |
| 3 | Scheduler migrated to Firebase, lease intact | WS-B scaffold only | tested Cloud Run Job, dev-proven |
| 4 | Send-path invariants hold adversarially | strong (placeholder/recipient/tour/signature) | no phrasing breaks a guard |
| 5 | Golden campaign passes end-to-end, no code change mid-pass | not run (emulator blocked) | green on emulator/in-memory |
| 6 | Safety rails live (allowlist, caps, kill switch, dead-letter alert) | partial | all present + tested |
| 7 | Next-version isolated (Results/Tour/PDF/Map out of Base build) | WS-C done (flag + tree-shake) | ✅ pending review |
| 8 | Staging env (emulator or project) | emulator blocked (no JRE) | in-memory Firestore harness stood up |
| 9 | Observability (health can't lie; silent-drops emit evidence) | partial | verified |
| 10 | Rollback + runbook current | — | written |

## Live progress ledger (2026-07-04 overnight)
- **Surface A COMPLETE (gate 4 deterministic half).** All 20 broker-language events green; 54 pinned bugs fixed across 3 passes (pass 1 ~26 @ `e4ceb01`, passes 2+3 the remaining 28 @ `72db6c8`). Full suite 661 tests OK (0 fails, 1 pre-existing skip), 205/205 broker-language, 0 regressions. Recovery note: pass-2 ran to completion in background; a mid-flight `git stash` incident had trapped the `followup.py` + `sent_mail_guard.py` fixes in `stash@{0}` — detected, restored, union state verified fresh. Key hardening: fail-closed Sent Items continuation guard (conversationId-scoped + paginated), paused-thread follow-up block, plus-alias operator drop, company-name greeting guard, rejected-tour-time never confirmed, extraction failures raise `RetryableProcessingError` (no silent broker-payload loss), deterministic wrong-property flyer guard. PR **#15** (draft), CodeRabbit full review returned 17 findings (see fix pass below). Commits `72db6c8`, `109f866`.
- **Surface A′ SWEPT — real-AI classification, LIVE OpenAI gpt-5.2, ZERO sends (gate 4 model half).** Built a dry_run harness driving `ai_processing.propose_sheet_updates` with `conversation=` + a pre-import Firestore mock (zero-recorded-calls assertion held; only egress was `api.openai.com`). **271 designed cases, 303 live calls, 46 confirmed misreads (30 HIGH / 12 MED / 4 LOW)**, each reproduced on rerun. Findings + 19-item fix plan grouped by real source file: `docs/release-safety/surface-aprime-real-ai-findings.md`. Top HIGHs: unavailable-patterns terminalizing a live row off another building's lease / quoted history / tour-slot phrasing; new_property fired off quoted/withdrawn/other-property mentions; TI-credit written as rent; cross-property rent written into the target row; wrong-person greeting auto-sent; mixed-basis opex ($/MO vs /YR). **Fix pass COMPLETE** (`17c307d`): **43 fixes landed** (FIX-01..18 + CodeRabbit), 50+ new deterministic/prompt-mechanical regression tests in `tests/test_aprime_*.py`, full suite 737 OK, 0 regressions. Also fixed the CodeRabbit-caught Surface-A regression (wrong-property guard was dropping valid same-property flyers) and outbound_safety false-positives. Honest deferrals: M26 (CC'd third-party optout in free text) + M36 (PDF-sourced new_property) covered by prompt rules only — deterministic keying risked false-stripping genuine sender opt-outs. PR #15 CodeRabbit re-review requested.
- **Surface C COMPLETE — full dashboard/callable surface hardening (gates 1,4,6).** Backend PR **#16** (`cca350d`): every mutating dashboard/debug/admin/device-flow route now verified-token + caller-scoped + input-bounded (closed cross-tenant inbox/Firestore disclosure, cross-tenant destructive cleanup, MSAL identity confusion, session-uid cache-wipe, recon leakage). Full suite **741 OK**. Frontend PR **#6** (email-admin-ui, `52c3237`): 14 onCall + 4 onRequest hardened, **115/115** — closed anonymous permanent `deleteSheet`, anonymous arbitrary-sheet-write `acceptNewProperty`, MSAL mailbox-binding forgery, sheet IDOR, audit-trail spoofing, OpenAI-cost-abuse input caps. Both CodeRabbit full-review triggered.
- **Surface G COMPLETE (dev-proven) — WS-B Cloud Run scheduler (gate 3).** PR **#17** (`d419c4c`), 451-test suite green. 7 parity items landed vs legacy `.github/workflows/email.yml` (kept intact). Scheduler-safety hardening: **fail-closed scope** on Cloud Run Job/Service (`K_SERVICE`) AND any unrecognized runtime — the legacy all-user default now requires a positive GitHub Actions signal (a bare `docker run` or Cloud Run Service can no longer silently process every live user); **globally-unique lease owner** (`CLOUD_RUN_EXECUTION:TASK_INDEX`, not `hostname:pid`→`<host>:1` collision) closing a concurrent-double-run vector; `timeoutSeconds 2400 < lease TTL 2700`; `.dockerignore` secret-leak guard; hard startup env gate before lease acquisition; SIGTERM→atexit bridge proven with a falsification control; cutover+rollback runbook in `deploy/README.md`. **Both scheduler-safety findings came from the run's own adversarial verify pass and were closed with regression tests.** Docker build + in-image secret scan deferred (no Docker CLI on host — command documented). **One Unverified live gate:** real Cloud Run SIGTERM grace timing + atexit upload against Firebase Storage needs a GCP run (forbidden here). **No-send incident logged (not a violation):** a red-phase test ran `python main.py` once — Firestore lease acquired+released cleanly, zero sends possible (dummy Azure secret), health docs self-heal on next cron.

## Input corpus — how brokers actually talk
Seeds: `feature-gradebook.eventVariantCatalog` (20 events × `sampleTriggers` × `nearMisses` × `stopIf`) + `combinationStressDecks` (8) + the real bp21 conversation threads (League City Golden Replay etc.). Each surface agent GENERATES many realistic phrasing variations per seed: terse / verbose / typo'd / partial / multi-intent / ambiguous / quoted-history / regional / attachment-only / conflicting-with-old-quote. Near-misses are the false-positive controls.

## What is testable locally vs. needs secrets
- **Local (the bulk):** every deterministic guard + handler + state transition + extraction application + reply-safety + `/api` route + conversation-panel contract. Driven via the proven Flask harness + `email_automation.*` with faked Firestore/Sheets/Graph. NO live sends. (venv: `scratchpad/bevenv`.)
- **Needs secrets (flagged, not blocking):** the LLM event-classification/extraction quality (OpenAI), real Graph/Sheets. Real-AI-in-the-loop pressure test runs only if an OpenAI key is provided; otherwise the LLM boundary is mocked with varied realistic proposals and the DOWNSTREAM handling is what gets hammered.

## Surfaces (worked in priority order, loop-until-dry each)
- **A — Guard & reply-safety robustness across broker-language variation + near-misses** (send-path invariants, gate 4). Placeholder / tour-leak / opt-out / wrong-contact / non-viable-vs-unavailable / reply-all-CC filtering / signature identity, each across many phrasings; near-miss = must-NOT-fire control. *(launching first)*
- **B — Property-extraction robustness** across spec formats ("$0.82 NNN" vs "82 cents triple net" vs partial vs attachment-only vs conflicting old quote) → `apply_proposal_to_sheet` (gates 1,4).
- **C — Dashboard interaction surface** — extend the token-auth + validation hardening to ALL mutating `/api` routes + the 7 `httpsCallable` functions + conversation-panel actions; fuzz each (gates 1,4,6).
- **D — State-permutation matrix** — every feature × every state (`statePermutations`) closes the base needs_fixture + stress gaps (gates 1,2).
- **E — Combination stress decks** — the 8 hard multi-feature scenarios → real integration tests (gate 2 integrated lane).
- **F — Full-campaign lifecycle** — in-memory Firestore double (no JRE) → chained upload→…→completion e2e (gates 2,5,8).
- **G — Scheduler migration** — finish WS-B Cloud Run Job using the legacy `.github/workflows/email.yml` (worked ~95%) as reference; lease + SIGTERM + dev-scope tested (gate 3).
- **H — Safety rails + observability + runbook** — recipient allowlist, daily caps, kill switch, dead-letter alerting, health-can't-lie check, rollback runbook (gates 6,9,10).

## Loop protocol (per cycle, overnight)
1. Read this scorecard; pick the highest-value surface with open gaps.
2. Run a find→verify→fix→prove workflow (fan-out; adversarial verify; loop-until-dry).
3. Integrate: real fixes committed (TDD), permanent tests added, rubric cells closed with honest provesBehavior.
4. **Push** the branch (authorized). Update this scorecard with evidence + bug counts.
5. Launch the next cycle (workflow completion re-invokes automatically; long fallback wake as backstop).
6. Stop when all 10 gates are green with evidence, or morning — then write the go/no-go verdict.

## Branches (push authorized)
- `codex/prod-v1-rubric-integrity-20260702` (rubric + lanes)
- `codex/backend-contract-hardening-20260703` (/api hardening + fuzz — extend here for surface C)
- `codex/frontend-api-token-auth-20260703` (frontend token)
- new per-surface branches as needed; keep each coherent.

## Hard rules (unchanged)
TDD; adversarially verify every case; NEVER fake a green — if a cell can't be honestly proven, document the reason/bug. No live email except bp21harrison@gmail.com ↔ baylor.freelance@outlook.com. No deploy / no reopening users without Baylor. The final real Jill-style end-to-end campaign is the LAST checkpoint, after the bar is green.
