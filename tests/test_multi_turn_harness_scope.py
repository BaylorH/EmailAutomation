"""The live multi-turn harness must never be able to run the scheduler unscoped.

The harness used to shell out to ``run_production.sh``, a file that has never existed
in git, so it could not run at all. The tempting repair -- recreate the wrapper around
``python3 main.py`` -- is the dangerous one: the scheduler's legacy default is EVERY
user, and the other mailboxes hold real third-party broker correspondence. A harness
that processes them is the one mistake this project cannot make twice.

So the invocation is asserted here rather than trusted: scope pinned to the single test
account, and any inherited all-user opt-in stripped before the child process sees it.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_TESTS_DIR))
# multi_turn_live_test imports its scenarios as a top-level module.
sys.path.insert(0, _TESTS_DIR)

from tests.multi_turn_live_test import MultiTurnTestRunner, OUTLOOK_USER_ID  # noqa: E402


class HarnessScopeTests(unittest.TestCase):
    def test_pipeline_runs_main_py_directly(self):
        cmd, _env = MultiTurnTestRunner.pipeline_command_and_env({})
        self.assertTrue(cmd[-1].endswith("main.py"), cmd)
        self.assertNotIn(
            "run_production.sh", " ".join(cmd),
            "the harness must not depend on a script that has never existed in git",
        )

    def test_scope_is_pinned_to_the_single_test_user(self):
        _cmd, env = MultiTurnTestRunner.pipeline_command_and_env({})
        self.assertEqual(env.get("SITESIFT_DEV_SCOPED_SCHEDULER"), "1")
        self.assertEqual(env.get("SITESIFT_SCHEDULER_TARGET_USER_IDS"), OUTLOOK_USER_ID)
        self.assertEqual(env.get("SITESIFT_SCHEDULER_ALLOWED_USER_IDS"), OUTLOOK_USER_ID)

    def test_an_inherited_all_user_optin_is_stripped(self):
        """A stray env var in the operator's shell must not widen the run."""
        _cmd, env = MultiTurnTestRunner.pipeline_command_and_env(
            {"SITESIFT_SCHEDULER_ALLOW_ALL_USERS": "1"}
        )
        self.assertNotIn("SITESIFT_SCHEDULER_ALLOW_ALL_USERS", env)

    def test_the_pinned_scope_resolves_to_exactly_one_user(self):
        """The env the harness builds must survive the product's own scope gate."""
        from email_automation.scheduler_scope import resolve_scheduler_user_ids

        _cmd, env = MultiTurnTestRunner.pipeline_command_and_env({})
        preserved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            scope = resolve_scheduler_user_ids(
                [OUTLOOK_USER_ID, "some-other-live-user", "another-live-user"]
            )
        finally:
            os.environ.clear()
            os.environ.update(preserved)
        self.assertEqual(scope.user_ids, [OUTLOOK_USER_ID])


if __name__ == "__main__":
    unittest.main()
