# SEC-01 L2 Firestore Emulator Evidence

## Result

- Date: `2026-07-24`
- Scenario: `SEC-01`
- Status: `passed`
- Data: `synthetic emulator only`
- Runnable EmailAutomation source commit: `b60c31f6b1ae59c6ef3ac6944ef9094a8c55e34a`
- SEC L2 adapter implementation commit: `7b2f6aa539c440ebcda25d24ebef20e4c7389d3b`
- email-admin-ui rules and emulator-test commit: `d98740b9eab03bf0ef971b26349318d25e1956b5`

The runnable source commit contains both the adapter implementation and the
configured L2 registry profile used by the canonical command.

## Clean-Source L1 Regression

Command:

```bash
./scripts/run_test_level.sh --level L1
```

Exact summary at the runnable source commit:

```text
L1 PASSED tests=2340 failures=0 errors=0 skipped=0
```

Exit code: `0`

The canonical L1 summary format does not expose a duration field. No duration
has been inferred or inserted into that exact line.

## Clean-Source L2 Execution

Command:

```bash
./scripts/run_test_level.sh --level L2
```

`SITESIFT_ADMIN_UI_ROOT` named the clean email-admin-ui worktree used by the
adapter. Its machine-specific value is intentionally omitted.

Exact sanitized summary:

```text
L2 PASSED family=SEC scenario=SEC-01 tests=10 failures=0 errors=0 skipped=0 duration_ms=14559 admin_ui_commit=d98740b9eab03bf0ef971b26349318d25e1956b5
```

Exit code: `0`

## Sanitized Runtime And Dependencies

- Node.js: `25.9.0`
- npm: `11.12.1`
- OpenJDK: `25.0.2`
- Firebase CLI: `14.27.0`
- `@firebase/rules-unit-testing`: `4.0.1`

## Proven

- The complete checked-in Firestore rules file loads in a disposable emulator.
- Intended owner access succeeds while unauthenticated and cross-user access is denied.
- Direct reads, collection queries, creates, updates, and deletes follow the tested authorization matrix.
- Protected `systemHealth`, `actionAudit`, and `outbox` constraints behave as specified.
- The obsolete global clients access is denied and unknown top-level paths remain denied by default.

## Not Proven

- SEC-02 Function identity handling is not proven.
- SEC-03 privacy of every operational writer is not proven.
- Production rules deployment or deployed identity is not proven.
- External provider behavior is not proven.
- Queue behavior is not proven.
- Worker behavior is not proven.
- L3 sandbox behavior is not proven.
- L4 controlled end-to-end behavior is not proven.
- Gate 2 is not proven or authorized.

Gate 2 remains unauthorized.
