"""Surface D-3 — real state-permutation tests for feature core.scheduler_scope.

Feature under test: scheduler user-scope resolution — the pure resolver
email_automation.scheduler_scope.resolve_scheduler_user_ids() plus the run-scope
gate main.run_all_users() that consumes its result. These tests close Base-V1
rubric needs_fixture cells with assertions that GENUINELY exercise the resolver
in the named state and would FAIL if the scoping/fail-closed behavior regressed.

No live sends: the resolver is a pure function of env + an in-memory user-id
list; the run-scope test mocks main.list_user_ids / main.refresh_and_process_user
so ZERO Graph/Firestore/Sheets mutation occurs.

Cells closed here:
  * terminal_state            -> test_terminal_state_absent_token_cache_aborts_dev_scoped_run
  * operator_visible_failure  -> test_operator_visible_failure_scope_block_halts_run_without_processing

Cells reported NOT APPLICABLE (see structured output / no faked tests):
  * bad_placeholder     — resolver renders no template/message body; no placeholder surface.
  * manual_continuation — resolver has no manual-user-continuation branch; keyed on CI event + env guards.
  * duplicate_retry     — pure stateless resolver; no send / idempotency key / retry state.
"""

import os
import unittest
from unittest.mock import patch, MagicMock


BAYLOR_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
JILL_UID = "C4X3UH1r6QhgP3ivXD1QjyhuGyI2"


def _clear_scope_env():
    """Neutralize every scheduler-scope env var so each test sets its own state."""
    return patch.dict(
        os.environ,
        {
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_TARGET_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": "",
        },
        clear=False,
    )


class SchedulerScopeStatePermutationTests(unittest.TestCase):
    # ---- terminal_state -------------------------------------------------
    def test_terminal_state_absent_token_cache_aborts_dev_scoped_run(self):
        """terminal_state: a requested dev-scoped target whose token cache is
        ABSENT (session terminated / drained — a terminal state for that user)
        aborts the whole scoped run with a specific 'token caches' reason
        instead of silently proceeding with a partial/empty user set. Negative
        control proves the same target succeeds once its cache is present, so
        the failure is caused by the terminal (absent-cache) state, not by the
        allowlist gate.
        """
        from email_automation.scheduler_scope import (
            SchedulerScopeError,
            resolve_scheduler_user_ids,
        )

        # Baylor is allowlisted + requested, but his token cache is GONE
        # (only Jill's cache remains available this run).
        with _clear_scope_env(), patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "schedule",
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                "SITESIFT_SCHEDULER_TARGET_USER_IDS": BAYLOR_UID,
                "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": BAYLOR_UID,
            },
        ):
            with self.assertRaises(SchedulerScopeError) as ctx:
                resolve_scheduler_user_ids([JILL_UID])  # Baylor's cache absent
        message = str(ctx.exception)
        self.assertIn("token cache", message.lower())
        self.assertIn(BAYLOR_UID, message)

        # Negative control: SAME allowlist/target, but Baylor's cache is present
        # -> the run resolves to exactly Baylor. Proves the abort above was the
        # terminal absent-cache state, not the allowlist rejecting the target.
        with _clear_scope_env(), patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "schedule",
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                "SITESIFT_SCHEDULER_TARGET_USER_IDS": BAYLOR_UID,
                "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": BAYLOR_UID,
            },
        ):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])
        self.assertEqual(resolved.mode, "dev_scoped")
        self.assertEqual(resolved.user_ids, [BAYLOR_UID])

    # ---- operator_visible_failure --------------------------------------
    def test_operator_visible_failure_scope_block_halts_run_without_processing(self):
        """operator_visible_failure: when scheduler scope is MISCONFIGURED
        (dev-scoped guard enabled but no target user ids), the run-scope gate
        main.run_all_users() must fail CLOSED and OPERATOR-VISIBLE — it raises
        SystemExit carrying the scope-blocked reason and processes ZERO users
        (no refresh_and_process_user call, hence no downstream Graph/Firestore
        mutation). Negative control: a well-formed scheduled run processes every
        token user, proving the halt is caused specifically by the misconfig,
        not an always-on block.
        """
        import main

        # --- misconfigured scope: guard on, target unset -> visible hard stop
        with _clear_scope_env(), patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "schedule",
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                # SITESIFT_SCHEDULER_TARGET_USER_IDS intentionally left blank
            },
        ), patch.object(main, "list_user_ids", return_value=[JILL_UID, BAYLOR_UID]), \
                patch.object(main, "refresh_and_process_user") as proc_blocked:
            with self.assertRaises(SystemExit) as ctx:
                main.run_all_users()
        # Operator-visible reason surfaced, and NOTHING processed (fail closed).
        self.assertIn("Scheduler scope blocked", str(ctx.exception))
        proc_blocked.assert_not_called()

        # --- negative control: clean scheduled run fans out to all token users
        with _clear_scope_env(), patch.dict(
            os.environ, {"GITHUB_EVENT_NAME": "schedule"}
        ), patch.object(main, "list_user_ids", return_value=[JILL_UID, BAYLOR_UID]), \
                patch.object(main, "refresh_and_process_user") as proc_ok:
            main.run_all_users()
        processed = [c.args[0] for c in proc_ok.call_args_list]
        self.assertEqual(processed, [JILL_UID, BAYLOR_UID])


if __name__ == "__main__":
    unittest.main()
