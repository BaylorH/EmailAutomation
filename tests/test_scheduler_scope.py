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


class CloudRunSchedulerScopeTests(unittest.TestCase):
    """WS-B: on Cloud Run the scope gate must fail CLOSED.

    Cloud Run Jobs inject CLOUD_RUN_JOB / CLOUD_RUN_EXECUTION. In that runtime
    the legacy default (mode='all' whenever the dev flag is absent) is a live
    all-user footgun: the dev-scope env lives in mutable job config, not a
    git-reviewed workflow file. Processing every user must therefore require
    an explicit opt-in (SITESIFT_SCHEDULER_ALLOW_ALL_USERS='1'); anything else
    raises SchedulerScopeError before any user is touched.
    """

    def _cloud_run_env(self, extra=None):
        env = {
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_TARGET_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "",
            "CLOUD_RUN_JOB": "email-automation-scheduler",
            "CLOUD_RUN_EXECUTION": "email-automation-scheduler-abc12",
        }
        env.update(extra or {})
        return patch.dict(os.environ, env, clear=False)

    def test_cloud_run_without_dev_flag_fails_closed(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        with self._cloud_run_env():
            with self.assertRaisesRegex(SchedulerScopeError, "fail-closed|fails closed|Cloud Run"):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_cloud_run_dev_flag_with_valid_target_is_dev_scoped(self):
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        with self._cloud_run_env(
            {
                "SITESIFT_DEV_SCOPED_SCHEDULER": "1",
                "SITESIFT_SCHEDULER_TARGET_USER_IDS": BAYLOR_UID,
                "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": BAYLOR_UID,
            }
        ):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

        self.assertEqual(resolved.mode, "dev_scoped")
        self.assertEqual(resolved.user_ids, [BAYLOR_UID])

    def test_cloud_run_all_users_requires_explicit_opt_in(self):
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        with self._cloud_run_env({"SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "1"}):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

        self.assertEqual(resolved.mode, "all")
        self.assertEqual(resolved.user_ids, [JILL_UID, BAYLOR_UID])

    def test_cloud_run_mistyped_allow_all_still_fails_closed(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        for bad_value in ("true", "yes", "0", "TRUE"):
            with self.subTest(allow_all=bad_value):
                with self._cloud_run_env({"SITESIFT_SCHEDULER_ALLOW_ALL_USERS": bad_value}):
                    with self.assertRaises(SchedulerScopeError):
                        resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_cloud_run_mistyped_dev_flag_errors_instead_of_silent_all(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        for bad_value in ("true", "0", "", "yes"):
            with self.subTest(dev_flag=bad_value):
                with self._cloud_run_env(
                    {
                        "SITESIFT_DEV_SCOPED_SCHEDULER": bad_value,
                        "SITESIFT_SCHEDULER_TARGET_USER_IDS": BAYLOR_UID,
                    }
                ):
                    with self.assertRaises(SchedulerScopeError):
                        resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_cloud_run_execution_alone_triggers_gate(self):
        """Either injected Cloud Run env var must arm the gate."""
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        with self._cloud_run_env({"CLOUD_RUN_JOB": ""}):
            with self.assertRaises(SchedulerScopeError):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_cloud_run_job_alone_triggers_gate(self):
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        with self._cloud_run_env({"CLOUD_RUN_EXECUTION": ""}):
            with self.assertRaises(SchedulerScopeError):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_non_cloud_run_scheduled_behavior_unchanged(self):
        """Legacy GHA cron path: no Cloud Run env → mode 'all' as before."""
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        env = {
            "GITHUB_EVENT_NAME": "schedule",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_TARGET_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "",
            "CLOUD_RUN_JOB": "",
            "CLOUD_RUN_EXECUTION": "",
        }
        with patch.dict(os.environ, env, clear=False):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

        self.assertEqual(resolved.mode, "all")
        self.assertEqual(resolved.user_ids, [JILL_UID, BAYLOR_UID])

    def test_unknown_runtime_no_github_no_cloudrun_fails_closed(self):
        """Adversarial (verify-agent finding): the image run OUTSIDE GitHub
        Actions and outside a detected Cloud Run Job — a bare `docker run` with
        prod secrets, or a Cloud Run Service before K_SERVICE detection — must
        NOT fall through to the legacy all-user default. It must fail closed."""
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        env = {
            "GITHUB_ACTIONS": "",
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_TARGET_USER_IDS": "",
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "",
            "CLOUD_RUN_JOB": "",
            "CLOUD_RUN_EXECUTION": "",
            "K_SERVICE": "",
            "K_REVISION": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SchedulerScopeError):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_cloud_run_service_k_service_triggers_gate(self):
        """A Cloud Run *Service* (K_SERVICE injected, not the Job vars) must hit
        the same fail-closed gate as a Job."""
        from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids

        env = {
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "",
            "CLOUD_RUN_JOB": "",
            "CLOUD_RUN_EXECUTION": "",
            "K_SERVICE": "email-automation-svc",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(SchedulerScopeError):
                resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])

    def test_unknown_runtime_all_users_opt_in_still_works(self):
        """The explicit opt-in escape hatch still resolves to all-users so a
        deliberate operator run is not blocked."""
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        env = {
            "GITHUB_ACTIONS": "",
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "1",
            "CLOUD_RUN_JOB": "",
            "CLOUD_RUN_EXECUTION": "",
            "K_SERVICE": "",
        }
        with patch.dict(os.environ, env, clear=False):
            resolved = resolve_scheduler_user_ids([JILL_UID, BAYLOR_UID])
        self.assertEqual(resolved.mode, "all")


class SchedulerLeaseOwnerTests(unittest.TestCase):
    """WS-B (verify-agent finding): the lease owner must be globally unique or
    owner-checked release lets one execution free another's lease and both run.
    On Cloud Run the entrypoint is PID 1 and hostname is not unique, so
    hostname:pid can collide on '<host>:1' — prefer CLOUD_RUN_EXECUTION."""

    def test_cloud_run_execution_used_as_owner(self):
        from email_automation.scheduler_lease import _default_owner

        env = {
            "GITHUB_RUN_ID": "",
            "RENDER_INSTANCE_ID": "",
            "CLOUD_RUN_EXECUTION": "email-automation-scheduler-abc12",
            "CLOUD_RUN_TASK_INDEX": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            owner = _default_owner()
        self.assertEqual(owner, "email-automation-scheduler-abc12:0")

    def test_two_tasks_same_execution_get_distinct_owners(self):
        from email_automation.scheduler_lease import _default_owner

        base = {"GITHUB_RUN_ID": "", "RENDER_INSTANCE_ID": "",
                "CLOUD_RUN_EXECUTION": "exec-1"}
        with patch.dict(os.environ, {**base, "CLOUD_RUN_TASK_INDEX": "0"}, clear=False):
            owner_a = _default_owner()
        with patch.dict(os.environ, {**base, "CLOUD_RUN_TASK_INDEX": "1"}, clear=False):
            owner_b = _default_owner()
        self.assertNotEqual(owner_a, owner_b)

    def test_github_run_id_still_preferred(self):
        from email_automation.scheduler_lease import _default_owner

        with patch.dict(os.environ, {"GITHUB_RUN_ID": "gha-999",
                                     "CLOUD_RUN_EXECUTION": "exec-1"}, clear=False):
            self.assertEqual(_default_owner(), "gha-999")


if __name__ == "__main__":
    unittest.main()
