"""Phase-1 webhook: contract tests for deploy/cloudrun-service.yaml.

The webhook SERVICE reuses the batch JOB's image but overrides the entrypoint to
serve service.py via gunicorn. These tests pin the deploy scaffold statically
(no cloud access, no PyYAML dependency — same line-scan approach as
tests/test_ws_b_cloudrun_job_spec.py):

  * the service serves `service:app` via gunicorn,
  * the request timeout stays <= the per-user lease TTL (a request outliving
    its lease could let a Cloud Tasks retry run the same user concurrently),
  * gunicorn is declared as a dependency so the image can actually serve it,
  * the in-app shared-secret gate (PROCESS_USER_AUTH) is wired,
  * ADC is used (no GOOGLE_APPLICATION_CREDENTIALS key-file env),
  * the batch launch-safety scope trio is NOT carried onto the webhook path.
"""

import os
import re
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.scheduler_lease import DEFAULT_USER_LEASE_TTL_SECONDS


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_YAML_PATH = os.path.join(REPO_ROOT, "deploy", "cloudrun-service.yaml")
REQUIREMENTS_PATH = os.path.join(REPO_ROOT, "requirements.txt")


def _scalar_int(yaml_text: str, key: str) -> int:
    matches = re.findall(
        rf"^\s*{re.escape(key)}:\s*\"?(\d+)\"?\s*(?:#.*)?$",
        yaml_text,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one '{key}:' scalar in {SERVICE_YAML_PATH}, found {len(matches)}"
        )
    return int(matches[0])


class CloudRunServiceSpecContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE_YAML_PATH, "r") as f:
            cls.yaml_text = f.read()
        with open(REQUIREMENTS_PATH, "r") as f:
            cls.requirements = f.read()

    def test_is_a_knative_service(self):
        self.assertIn("kind: Service", self.yaml_text)

    def test_serves_service_app_via_gunicorn(self):
        self.assertRegex(self.yaml_text, r'command:\s*\[\s*"gunicorn"\s*\]')
        self.assertIn("service:app", self.yaml_text)

    def test_gunicorn_is_a_declared_dependency(self):
        active = {
            line.strip()
            for line in self.requirements.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn("gunicorn", active)

    def test_request_timeout_within_user_lease_ttl(self):
        timeout_seconds = _scalar_int(self.yaml_text, "timeoutSeconds")
        self.assertLessEqual(
            timeout_seconds,
            DEFAULT_USER_LEASE_TTL_SECONDS,
            "Service timeoutSeconds must be <= the per-user lease TTL "
            f"({DEFAULT_USER_LEASE_TTL_SECONDS}s); a request outliving its lease "
            "could let a Cloud Tasks retry process the same user concurrently.",
        )

    def test_shared_secret_gate_wired(self):
        self.assertIn("PROCESS_USER_AUTH", self.yaml_text)

    def test_google_application_credentials_not_injected(self):
        self.assertNotRegex(self.yaml_text, r"name:\s*GOOGLE_APPLICATION_CREDENTIALS")

    def test_batch_scope_trio_not_on_webhook_path(self):
        # The per-user webhook never calls resolve_scheduler_user_ids; the batch
        # launch-safety scope must not silently ride along as an ACTIVE env var
        # (an explanatory comment naming it is fine).
        self.assertNotRegex(self.yaml_text, r"name:\s*SITESIFT_DEV_SCOPED_SCHEDULER")
        self.assertNotRegex(self.yaml_text, r"name:\s*SITESIFT_SCHEDULER_TARGET_USER_IDS")


if __name__ == "__main__":
    unittest.main()
