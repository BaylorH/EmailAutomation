import os
import unittest
from unittest.mock import patch


BAYLOR_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
JILL_UID = "C4X3UH1r6QhgP3ivXD1QjyhuGyI2"


class SchedulerScopeTests(unittest.TestCase):
    def _clear_scope_env(self):
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

    def test_scheduled_runs_process_all_token_users(self):
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        with self._clear_scope_env(), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

        self.assertEqual(resolved.mode, "all")
        self.assertEqual(resolved.user_ids, [JILL_UID, BAYLOR_UID])

    def test_scheduled_runs_can_be_emergency_scoped_to_baylor(self):
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        with self._clear_scope_env(), patch.dict(
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

    def test_manual_dispatch_requires_dev_scoped_guard(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        with self._clear_scope_env(), patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            with self.assertRaisesRegex(SchedulerScopeError, "dev-scoped"):
                resolve_scheduler_user_ids([BAYLOR_UID])

    def test_manual_dispatch_can_only_process_baylor_by_default(self):
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        with self._clear_scope_env(), patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                "SITESIFT_SCHEDULER_TARGET_USER_IDS": BAYLOR_UID,
            },
        ):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

        self.assertEqual(resolved.mode, "dev_scoped")
        self.assertEqual(resolved.user_ids, [BAYLOR_UID])

    def test_manual_dispatch_rejects_jill_even_if_requested(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        with self._clear_scope_env(), patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                "SITESIFT_SCHEDULER_TARGET_USER_IDS": JILL_UID,
            },
        ):
            with self.assertRaisesRegex(SchedulerScopeError, "not allowed"):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])


if __name__ == "__main__":
    unittest.main()
