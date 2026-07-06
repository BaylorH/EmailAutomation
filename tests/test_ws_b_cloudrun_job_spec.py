"""WS-B: contract tests for deploy/cloudrun-job.yaml vs the Firestore lease.

Invariant under test (double-send guard): the Cloud Run task timeout must be
<= the scheduler lease TTL. A task still running after the lease TTL expires
holds an EXPIRED lease, so the next Cloud Scheduler trigger acquires it and
two runners execute concurrently — exactly the scenario the lease exists to
prevent. Legacy GitHub Actions never hit this because its concurrency group
cancelled the in-progress run at each 30-min trigger; Cloud Run has no such
cancel, so the yaml must uphold the invariant statically.

No cloud access: the test parses the committed yaml file only. PyYAML is not
a project dependency, so scalar fields are extracted line-by-line (the file
is a flat-scalar spec; every asserted key appears exactly once).
"""

import os
import re
import unittest

from email_automation.scheduler_lease import DEFAULT_TTL_SECONDS


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB_YAML_PATH = os.path.join(REPO_ROOT, "deploy", "cloudrun-job.yaml")


def _scalar_int(yaml_text: str, key: str) -> int:
    """Extract a unique integer scalar `key: <int>` from the yaml text."""
    matches = re.findall(
        rf"^\s*{re.escape(key)}:\s*\"?(\d+)\"?\s*(?:#.*)?$",
        yaml_text,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one '{key}:' scalar in {JOB_YAML_PATH}, found {len(matches)}"
        )
    return int(matches[0])


class CloudRunJobSpecContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(JOB_YAML_PATH, "r") as f:
            cls.yaml_text = f.read()

    def test_task_timeout_does_not_exceed_lease_ttl(self):
        timeout_seconds = _scalar_int(self.yaml_text, "timeoutSeconds")
        self.assertLessEqual(
            timeout_seconds,
            DEFAULT_TTL_SECONDS,
            "Cloud Run task timeoutSeconds must be <= the scheduler lease TTL "
            f"({DEFAULT_TTL_SECONDS}s). A task outliving its lease lets the next "
            "Cloud Scheduler trigger acquire the expired lease and run "
            "concurrently — the double-send scenario the lease prevents.",
        )

    def test_no_automatic_task_retries(self):
        self.assertEqual(
            0,
            _scalar_int(self.yaml_text, "maxRetries"),
            "maxRetries must stay 0: a crashed run must leave the lease to "
            "expire rather than stacking duplicate runners.",
        )

    def test_single_task_single_parallelism(self):
        self.assertEqual(1, _scalar_int(self.yaml_text, "parallelism"))
        self.assertEqual(1, _scalar_int(self.yaml_text, "taskCount"))

    def test_dev_scoped_scheduler_guard_pinned_on(self):
        """The launch-safety scope trio must stay in the job env, dev flag '1'."""
        self.assertIn("SITESIFT_DEV_SCOPED_SCHEDULER", self.yaml_text)
        self.assertIn("SITESIFT_SCHEDULER_TARGET_USER_IDS", self.yaml_text)
        self.assertIn("SITESIFT_SCHEDULER_ALLOWED_USER_IDS", self.yaml_text)
        dev_flag = re.search(
            r"name:\s*SITESIFT_DEV_SCOPED_SCHEDULER\s*\n\s*value:\s*\"?(\w+)\"?",
            self.yaml_text,
        )
        self.assertIsNotNone(dev_flag, "SITESIFT_DEV_SCOPED_SCHEDULER needs an inline value")
        self.assertEqual("1", dev_flag.group(1))

    def test_google_application_credentials_not_injected(self):
        """ADC via the job SA; a key-file env var must never reappear."""
        self.assertNotRegex(self.yaml_text, r"name:\s*GOOGLE_APPLICATION_CREDENTIALS")


if __name__ == "__main__":
    unittest.main()
