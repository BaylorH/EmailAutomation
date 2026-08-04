# Production-clearance M0 release-gate evidence

**Recorded:** 2026-08-04
**Deliverable:** both (code gate and production-readiness findings)
**Verdict:** **M0 COMPLETE for the release-gate baseline only.** Production
remains **NO-GO**; M1-M6 remain open (M1-M3 contain B2-B4).

## Immutable candidate and inventory

- Fresh `origin/main` and merge base:
  `9e63704d3584966944814a93594d2be1e4b2fcb0`.
- Frozen pre-M0 candidate:
  `6d79271fc71f145b2c76082ae68ac0edd61cc9b3`.
- The frozen candidate is 118 commits ahead and 0 behind `origin/main`, with
  118 non-merge commits.
- Full main-to-candidate delta: 69 files (30 added, 39 modified, 0 deleted,
  0 renamed, and 0 binary files), 106,967 insertions, and 2,094 deletions.

| Surface | Files | Status | Insertions | Deletions |
|---|---:|---|---:|---:|
| Production backend | 17 | 3 added, 14 modified | 40,905 | 1,246 |
| Deployment/config/runtime scripts | 3 | 1 added, 2 modified | 62 | 20 |
| Tests/fixtures | 42 | 19 added, 23 modified | 63,013 | 828 |
| Docs/evidence | 7 | 7 added, 0 modified | 2,987 | 0 |
| **Total** | **69** | **30 added, 39 modified** | **106,967** | **2,094** |

The 17 production-backend files are:

1. `app.py`
2. `email_automation/ai_processing.py`
3. `email_automation/email.py`
4. `email_automation/firestore_transactions.py`
5. `email_automation/followup.py`
6. `email_automation/messaging.py`
7. `email_automation/operator_replay.py`
8. `email_automation/pending_responses.py`
9. `email_automation/processing.py`
10. `email_automation/send_permits.py`
11. `email_automation/sent_mail_guard.py`
12. `email_automation/sheet_operations.py`
13. `email_automation/sheets.py`
14. `email_automation/source_coordinator.py`
15. `email_automation/system_health.py`
16. `main.py`
17. `scheduler_runner.py`

The three deployment/config/runtime-script files are:

1. `.gcloudignore`
2. `scripts/deploy_process_user.sh`
3. `scripts/production_reset.py`

This inventory was taken from the name-status and numstat diff between the two
immutable SHAs above. Private evidence contents were neither required nor
inspected.

## Later-gate recertification map

The complete 118-commit candidate must be recertified across these explicit
clusters:

- auth;
- scanner/coordinator;
- Graph send, drafts, and attachments;
- Sheets, row, terminal, and opt-out behavior;
- notification and cleanup;
- scheduler, retry, pending, and outbox behavior;
- AI and classifier behavior;
- datastore, rules, and migrations; and
- health and admin behavior.

No rules, index, or migration file changed in this main-to-candidate delta.
That absence is an inventory finding, not proof that the surrounding datastore
or client authority is production-ready; B4 still owns those integration and
cutover checks.

The highest-risk routing points for B2-B4 are intentionally kept compact here:

| File | Later-gate routing examples |
|---|---|
| `email_automation/source_coordinator.py` | `SourceCoordinator`, `advance_scan_cursor_if_source_authority_clear()`, `settle_source_marker_context_if_ready()` |
| `email_automation/send_permits.py` | `issue_pending_graph_send_permit()`, `issue_terminal_graph_send_permit()`, `consume_graph_send_capability()`, `resolve_graph_send_permit()` |
| `email_automation/processing.py` | `_build_terminal_saga()`, `_execute_or_reconcile_terminal_sheet_mutation()`, `send_reply_in_thread()`, `scan_inbox_against_index()` |
| `email_automation/pending_responses.py` | `claim_pending_response_for_send_exact()`, `process_pending_responses()`, `clear_pending_response_exact()` |
| `email_automation/followup.py` | `_claim_followup()`, `check_and_send_followups()`, `_send_followup_email()`, `schedule_followup_for_thread()` |
| `email_automation/sheet_operations.py` | `sync_thread_row_numbers_after_move()`, `sync_thread_row_numbers_after_insert()`, `move_row_below_new_divider_atomic()` |
| `email_automation/messaging.py` | `build_conversation_payload()`, `has_processed()`, `mark_processed()` |
| `app.py` | `verify_firebase_token()`, `api_stop_conversation()`, `api_clear_optout()`, `api_firestore_cleanup()` |
| `email_automation/operator_replay.py` | `ReplayRequest`, `replay_exact_message()` |
| `scripts/production_reset.py` | `wipe_user_data()`, `main()` |

## M0 code milestone

- Local milestone SHA:
  `6dcb319281ce1c2519fec15c6a3ec6d1bf84e15b`.
- Remote branch readback:
  `6dcb319281ce1c2519fec15c6a3ec6d1bf84e15b`.
- Branch: `codex/sitesift-production-clearance-20260804`.
- No PR, `main` change, deployment, runtime-flag change, or campaign occurred.

### Fresh local controller rerun before commit

| Gate | Result | Duration |
|---|---:|---:|
| Campaign-clearance plus auth-isolation gate | 95/95 | 0.408s |
| Complete B1 focused suite | 606/606 | 23.491s |
| Retained M2 suite | 669/669 | 22.073s |
| Changed-Python `py_compile` | pass | exit 0 |
| `pip check` | `No broken requirements found` | exit 0 |
| GitHub Actions YAML syntax parse | pass | exit 0 |
| Diff check | pass | exit 0 |

All three test controllers reported `OK`.

### Remote GitHub Actions gate

- Run: [production-clearance CI 30946405294](https://github.com/BaylorH/EmailAutomation/actions/runs/30946405294)
- Job: `92117366380`
- State: completed, success
- Started: `2026-08-04T20:07:34Z`
- Completed: `2026-08-04T20:09:00Z`
- Runtime: Python 3.12 with hash-locked `requirements.lock`
- Every job step passed, including compile and diff checks.

| Remote gate | Result | Duration |
|---|---:|---:|
| Campaign-clearance plus auth-isolation gate | 95/95 | 0.785s |
| Complete B1 focused suite | 606/606 | 30.223s |
| Retained M2 suite | 669/669 | 24.544s |

## Offline containment and effect boundary

- Credentials were absent, the OpenAI key was empty, the emulator endpoint was
  deliberately unreachable, and provider proxies were blackholed.
- The job defaults outbound mode to `paused`.
- The full-campaign step opts into `live` only after every provider boundary is
  replaced with a fake, and its fixture restores `paused` first during teardown.
- The retained-suite step alone uses `live`, only against fake provider
  boundaries.
- The source coordinator remained disabled.
- The local and remote gates caused no production or provider effects.

## Review record

- Initial quality review found a Python/runtime-lock mismatch and job-wide
  `live` mode. Both were corrected.
- Spec re-review found incorrect fixture `ExitStack` ordering. It was corrected.
- Final spec review of the pre-milestone M0 code/workflow four-file diff:
  **APPROVED**.
- Final quality/operability review of that same four-file diff, before it became
  milestone SHA `6dcb319281ce1c2519fec15c6a3ec6d1bf84e15b`: **APPROVED**.

Those approvals cover the workflow, auth-isolation test, campaign test, and
train-plan diff that formed the M0 code milestone. This evidence packet is
outside that SHA and does not approve itself. Its facts and documentation must
receive separate external review before the packet is committed; this record
makes no claim that that review has passed.

## Release-governance finding

GitHub Actions is enabled with all actions permitted. The `main` branch
protection endpoint returned 404 and the branch was read back as unprotected;
no repository setting was changed.

That is a release-governance risk, not authorization to compensate with a
direct push. There must be no direct `main` push or merge. Before M6, the
release decision must require a protected/check-gated path and explicit review
of the successful candidate checks.

## What this gate does and does not prove

M0 proves that the frozen candidate has a complete main-to-candidate inventory,
that its offline release controllers pass locally, and that the same gates are
operable in credential-free CI at the exact remote SHA. It does not prove
deployed behavior.

The full 118-commit surface still requires B2-B4 recertification plus complaint
and canary gates. B1 evidence covers only its own slice. Production therefore
remains **NO-GO** even though the M0 release-gate baseline is complete.
