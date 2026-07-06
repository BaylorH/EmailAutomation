# ⟢ MORNING VERDICT (2026-07-04) — CONDITIONAL GO

**Bottom line: NO-GO for an unattended, all-users reopen tonight. GO for a staged, allowlisted reopen once Baylor runs the one remaining live checkpoint and the three named pre-reopen items close.** The core mandate — proving Core Campaign Automation *cannot* send placeholders, wrong recipients, accidental tour language, stale retries, bad auto-replies, hidden failures, bad reply-all, bad row anchors, or unsafe scheduler behavior — is met **with evidence** on the deterministic + real-AI + integration + full-campaign lanes. What is *not* yet proven is anything that can only be proven against live production (the BP21 golden campaign, real Graph/GCP timing), plus one auth-service defect and one observability follow-on. Those are the gate to a real reopen, and they are Baylor's call.

**Users back on? NO tonight (unattended). YES for a staged allowlisted pilot after: (1) the live BP21↔BP21 golden campaign passes; (2) the auth-service MSAL shared-cache identity-mixing fix lands; (3) the swallowed-per-item send-failure observability follow-on lands.** Full reopen only after the pilot is clean.

## 10-gate scorecard — final
| # | Gate | Verdict | Evidence |
|---|------|---------|----------|
| 1 | Rubric green on Base-V1 (no borrowed greens) | 🟢 **GREEN (base) / caveat** | `featureFixtureMatrix` 102 covered / **7 needs_fixture (all structural N/A, honest gapReason) / 10 needs_live_proof**; 0 genuine coverage debt on the base independent lane. Borrowed greens purged; enforcement forbids duplicate provesBehavior (12/12 green). **Caveat:** `featureStressMatrix` = 101 needs_fixture (the stress-class × feature dimension — `rate_limit_429`, `concurrent_runner`, `malformed_data`, `retry_storm`…) is the largest remaining coverage gap, deliberately NOT faked overnight. |
| 2 | 3 lanes real+passing (independent + integrated + full-campaign) | 🟢 **GREEN** | Independent: 102 covered. Integrated: 8 combination stress decks as real chained tests + `crossFeatureMatrix` 3 covered. Full-campaign: **9/9 lanes** driven end-to-end (Surface F, adversarially confirmed genuine). |
| 3 | Scheduler migrated to Firebase, lease intact | 🟡 **GREEN (dev-proven) / 1 live gate** | WS-B Cloud Run Job: fail-closed scope (Job+Service+unknown-runtime), globally-unique lease owner, `timeout 2400 < TTL 2700`, startup env gate, SIGTERM→exit 143 (interrupted run no longer masked as success). **Live gate:** real GCP SIGTERM grace timing + atexit upload needs a deploy (forbidden here). PR #17, 451 tests. |
| 4 | Send-path invariants hold adversarially | 🟢 **STRONG GREEN** | Surface A (20/20 broker-language events), A′ (46 live-OpenAI misreads fixed), E (identity-leak false-negative fixed), D (signature-HTML placeholder leak fixed), B (placeholder-value sheet writes blocked). No phrasing tried broke a guard. |
| 5 | Golden campaign passes e2e, no code change mid-pass | 🟡 **GREEN (in-memory) / live pending** | Surface F chained e2e on an in-memory Firestore double + faked Graph/Sheets, 11 tests, no code change mid-pass. **The live BP21↔BP21 golden campaign is the LAST checkpoint — left for Baylor.** |
| 6 | Safety rails live | 🟢 **GREEN** | Reply-safety (validated at all 4 send entrypoints), daily send cap (fail-closed), kill-switch/dry-run outbound-mode lever, dead-letter reason+alert. PR #18. |
| 7 | Next-version isolated (Results/Tour/PDF/Map out of Base build) | 🟡 **GREEN (pending review)** | WS-C flag + tree-shake landed on `codex/ws-c-results-isolation-20260702` (prior session); not re-verified tonight. |
| 8 | Staging env | 🟡 **PARTIAL** | In-memory Firestore harness stood up (Surface F) — the substitute for the JRE-blocked Firebase Emulator. No live staging project (needs Baylor: project + secrets). |
| 9 | Observability (health can't lie) | 🟢 **GREEN (was FAILING) / 1 residual** | Gate 9 was **failing** — health reported healthy while Graph send was fully broken AND while queue counts were unreadable. Both fail-closed now (PR #18). **Residual:** the swallowed-per-item send-failure class (sendMail 403 while reads succeed) needs the send drivers to return per-run op-states; the main.py consumer rail is wired to escalate the instant they do. |
| 10 | Rollback + runbook | 🟢 **GREEN** | `SAFETY-RAILS-AND-ROLLBACK-RUNBOOK.md` (rail operation + reopen/fast-halt) + `deploy/README.md` scheduler cutover/rollback. |

## Bugs found AND fixed this run (ledger)
- **Surface A — broker-language (54):** 20/20 event suites green across 3 passes. Fail-closed Sent-Items continuation guard, paused-thread follow-up block, plus-alias operator drop, company-name greeting guard, rejected-tour-time never confirmed, extraction failures raise `RetryableProcessingError`, deterministic wrong-property flyer guard.
- **Surface A′ — real-AI classification (46 misreads, 30 HIGH):** live gpt-5.2, 271 cases / 303 calls, zero sends. 43 fixes: target-grounded terminal detection, quote-trap retention guards, TI-credit-as-rent, cross-property rent write, wrong-person greeting, mixed-basis opex. Deferred (documented): M26/M36 covered by prompt rules only.
- **Surface B — extraction (7, 6 fixed):** placeholder-VALUE sheet writes, mixed-basis opex, fabricated gross-basis opex, 'under joist' clear-height misfire, cents-per-SF + total-annual-over-area rent extraction.
- **Surface C — dashboard/callables:** backend — every mutating dashboard/debug/admin/device-flow route made verified-token + caller-scoped + input-bounded (cross-tenant disclosure, destructive cross-tenant cleanup, MSAL identity confusion, session-uid cache-wipe). Frontend — **anonymous permanent `deleteSheet`**, anonymous arbitrary-sheet-write `acceptNewProperty`, MSAL mailbox-binding forgery, sheet IDOR, audit-trail spoofing, OpenAI-cost input caps.
- **Surface E — combination decks (4):** confidential client-identity leak false-negative (apposition/representation naming), non-viable-lead-stays-alive gate ordering, 2× row-anchor prefix-leniency (completed sibling absorbing a partial property's reply after a sheet sort).
- **Surface D — state permutations (1 HIGH):** `build_professional_signature_html` leaked unresolved `[NAME]`/`[COMPANY]` tokens into the outbound signature HTML — a placeholder-to-broker-inbox path; now stripped.
- **Surface G — scheduler (2):** fail-open scope default (bare container / Cloud Run Service could process every live user), non-unique lease owner (concurrent double-run). Both caught by the run's own adversarial verify.
- **Surface H — observability (2):** the two "health can lie" paths (send blindness, unreadable-queue sentinel).
- **CodeRabbit triage:** SIGTERM exit-0 masking interrupted runs (#17), follow-up resume-timing unit bug (#16), article-led identity-leak regex gaps + `@odata.nextLink` SSRF host check (#18), `checkRevoked` on onRequest + MSAL-flow staleness reject (#6), and 6 more on #15 (terminal detection masked by size/price token, alternate-tour reorder, ambiguous 2-letter state codes in the flyer guard, column-config `None` hint, fail-loud import).

## Tests / builds
- Rubric branch full suite: **861 tests OK** (1 skip). Backend-contract: 754. Scheduler: 451. Safety-rails: 791. Frontend callables: 117. All `git diff --check` clean, `py_compile`/`node --check` clean, no secrets staged (`.env`/`service-account.json` remain gitignored).

## Branches / commits / PRs (all draft, none merged)
- `codex/prod-v1-rubric-integrity-20260702` @ `48637aa` → **PR #15** (A, A′, B, D, E, F, rubric)
- `codex/backend-contract-hardening-20260703` @ `bccc42c` → **PR #16** (Surface C backend)
- `codex/surface-c-callables-20260704` @ `919bf2a` → **PR #6** (email-admin-ui, Surface C frontend)
- `codex/ws-b-cloudrun-scaffold-20260702` @ `aa7fa83` → **PR #17** (Surface G scheduler)
- `codex/safety-rails-observability-20260704` @ `96e9759` → **PR #18** (Surface H rails + observability)

## CodeRabbit status
All 5 PRs received a full CodeRabbit review (no Critical findings; ~30 Major, mostly quick-wins + test-hygiene). **Every Major was adversarially triaged and either fixed (TDD), verified already-fixed by a later commit, or dispositioned as a false-positive/heavy-lift with a written reason — posted as PR comments.** Because the PRs are drafts, CodeRabbit skips re-review of the post-triage commits; a fresh pass runs automatically when Baylor marks a PR ready-for-review.

## What is still unsafe / residual risk (what a live prod run could still surface)
1. **No live proof.** Everything is against faked Graph/Sheets/Firestore + in-memory doubles. The BP21↔BP21 golden campaign (real send, real inbox, real sheet, real scheduler tick) has NOT run — it is the final checkpoint and is Baylor's to run.
2. **Auth-service MSAL shared-cache identity-mixing (Major, deferred to its own PR).** `auth_service.py` serializes the whole-process MSAL cache under the current uid and `get_accounts()[0]` can pick the wrong identity — a real mailbox-confusion risk on the *login* path. Must land before reopen.
3. **Swallowed-per-item send-failure observability (gate 9 residual).** A `sendMail` 403 while reads succeed is not yet reflected in health; the consumer rail is wired but the send drivers must return op-states.
4. **Scheduler live SIGTERM/atexit timing (gate 3).** Only a real Cloud Run execution can prove the token-cache upload finishes within the grace window.
5. **Stress-matrix coverage (gate 1 caveat).** 101 `featureStressMatrix` cells (rate-limit / concurrency / malformed-data / retry-storm) are not yet fixtured.
6. **Classifier nondeterminism.** A′ confirmations are 2-sample; sub-50% intermittent misreads on *passed* cases are invisible. Code-level fixes (preferred, model-independent) mitigate; prompt-level fixes need re-sweeping on any model bump.

## Next exact gate
**Baylor runs the live BP21↔BP21 golden campaign** (frontend Start scoped to BP21 → deployed scoped scheduler → Firestore/Sheets/Graph readback) with the preflight in the run brief. If it is clean, and the auth-service identity-mixing fix + the send-failure observability follow-on land, proceed to a **staged allowlisted pilot** (kill-switch armed, daily cap low, dead-letter alerting watched), then a full reopen only after the pilot is clean.

---

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
- **Surface B COMPLETE — extraction robustness (gates 1,4).** `b3c4fbf`, 748 tests. Pressure-tested the extraction layer (deterministic + live-OpenAI) across rent/NNN-OPEX-TMI/clear-height/docks/power/metric-SF formats: 7 bugs, 6 fixed (1 non-defect). HIGH: placeholder VALUES (TBD/N-A/pending) could be written into a client sheet as data (now skipped); mixed-basis opex ($/MO alongside annualized rent — the M38 class); fabricated gross-basis opex; 'under joist' clear-height misfiring property_unavailable. 11 new tests, all against a fake Sheets client (zero live writes).
- **Surface E COMPLETE — 8 combination stress decks + cross-feature (gate 2 integrated lane).** 8 decks + `reply_all_privacy_boundary` as REAL chained-interaction integration tests (48 tests); **4 real bugs found + fixed**: (1) `contains_confidential_disclosure` missed apposition/possessive/representation client-identity naming ('our client Acme Logistics', 'representing Northstar') — an **identity-leak false negative** in the last guard before an auto-reply; (2) a combined tour-decline + physical-non-fit stayed alive as a live lead (requirements-mismatch backstop was gated behind the tour-only check — reordered); (3+4) **row-anchor prefix-leniency**: a completed sibling row whose address token-prefixes a partial property absorbed the partial's reply after a sheet sort, and the row-move recovery walk picked the wrong ordinal (both fixed exact-only + ordinal-gated). Fixture map: **9 cells closed** (cross-feature `reply_all_privacy_boundary` + 8 deck cells, distinct provesBehavior each); the 5 remaining cross-feature groups honestly left `needs_fixture` — they span next-gen Results/Tour/Map features intentionally isolated from Base-V1 tonight. 795-suite green, enforcement test green.
- **Surface H COMPLETE — safety rails + observability + rollback runbook (gates 6,9,10).** PR **#18** (`codex/safety-rails-observability-20260704`), 787 tests. **Gate 9 was FAILING and is now fixed:** the health rollup could report healthy while Graph send was fully broken (`graph_state` only from receive-side scans) AND while queue counts were unreadable (`-1` sentinel treated as empty) — both now fail closed. Added: daily send cap (per-user Firestore counter, fail-closed), kill-switch/dry-run outbound-mode lever, auto-reply allowlist parser fix, dead-letter reason+alert visibility. Verified-present rails: misdirection reply-safety, no-hidden-failed-sends, no-duplicate-send. Runbook: `SAFETY-RAILS-AND-ROLLBACK-RUNBOOK.md`. **Honest residual:** the swallowed-per-item send-failure class (sendMail 403 while reads succeed) isn't fully closed — needs the send drivers to return per-run op-states; the main.py consumer rail is wired to escalate the instant they do. Top follow-on before an unattended (non-allowlisted) reopen.
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
