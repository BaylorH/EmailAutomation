"""WS-B: cutover & rollback runbook contract (worklist #6) + secret-coverage
delta documentation (worklist #7).

deploy/README.md is the operational runbook for the GHA-cron → Cloud Run Job
migration. Setup/deploy/schedule alone is not enough to operate the cutover:
the doc must also say (a) how to cut over — disable the legacy GitHub Actions
cron so two schedulers never run against the same lease long-term — and
(b) how to roll back — pause the Cloud Scheduler trigger and re-enable the
GHA workflow. These tests are grep-based doc contracts: if someone rewrites
the runbook and drops the rollback story (or the intentional env-var deltas),
the suite goes red instead of the knowledge silently evaporating.
"""

import os
import re
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README_PATH = os.path.join(REPO_ROOT, "deploy", "README.md")
JOB_YAML_PATH = os.path.join(REPO_ROOT, "deploy", "cloudrun-job.yaml")
LEGACY_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "email.yml")


class CutoverRollbackDocContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(README_PATH, "r") as f:
            cls.readme = f.read()

    def test_has_cutover_and_rollback_section(self):
        self.assertRegex(
            self.readme,
            re.compile(r"^##.*[Cc]utover.*[Rr]ollback", re.MULTILINE),
            "deploy/README.md needs a '## Cutover & rollback' section",
        )

    def test_rollback_names_scheduler_pause(self):
        """Rolling back must start by stopping the new trigger."""
        self.assertIn(
            "gcloud scheduler jobs pause",
            self.readme,
            "rollback story must name `gcloud scheduler jobs pause` as the "
            "way to stop the Cloud Run trigger",
        )

    def test_rollback_names_gha_reenable(self):
        """Rolling back must end by re-enabling the legacy GHA cron."""
        self.assertIn(
            "gh workflow enable",
            self.readme,
            "rollback story must name `gh workflow enable` to restore the "
            "legacy GitHub Actions cron",
        )

    def test_cutover_names_gha_disable(self):
        """Cutting over must disable the legacy cron (not delete the file)."""
        self.assertIn(
            "gh workflow disable",
            self.readme,
            "cutover story must name `gh workflow disable` — the workflow "
            "file itself stays in-repo as the behavioral spec / rollback path",
        )

    def test_legacy_workflow_file_still_present(self):
        """The legacy workflow is the behavioral spec AND the rollback target;
        it must not be deleted until parity is proven and rollback retired."""
        self.assertTrue(
            os.path.exists(LEGACY_WORKFLOW_PATH),
            ".github/workflows/email.yml must stay in the repo as reference "
            "and rollback target until Cloud Run parity is proven",
        )


class SecretCoverageDeltaDocTests(unittest.TestCase):
    """Worklist #7 (doc half): the legacy workflow injects CLIENT_ID,
    FIREBASE_SA_KEY and AZURE_TENANT_ID; the Cloud Run job intentionally
    omits them because the scheduler path (main.py's import closure) never
    reads them. That delta must be written down in both deploy docs so a
    future 'parity' pass doesn't cargo-cult them back in as secrets."""

    OMITTED_VARS = ("CLIENT_ID", "FIREBASE_SA_KEY", "AZURE_TENANT_ID")

    @classmethod
    def setUpClass(cls):
        with open(README_PATH, "r") as f:
            cls.readme = f.read()
        with open(JOB_YAML_PATH, "r") as f:
            cls.job_yaml = f.read()

    def test_readme_documents_each_omitted_legacy_var(self):
        """Not just any mention: the var must appear on a line that also says
        'omit' (omitted/omission), i.e. documented AS an intentional omission
        — a stray mention elsewhere must not satisfy this contract."""
        for var in self.OMITTED_VARS:
            with self.subTest(var=var):
                self.assertRegex(
                    self.readme,
                    rf"(?im)^(?=.*{var})(?=.*omit).*$",
                    f"deploy/README.md must document why legacy env {var} is "
                    "intentionally omitted from the Cloud Run job",
                )

    def test_job_yaml_comments_on_omitted_legacy_vars(self):
        """Same 'omit'-context requirement, and it must live in a comment."""
        comment_lines = "\n".join(
            line for line in self.job_yaml.splitlines()
            if line.lstrip().startswith("#")
        )
        for var in self.OMITTED_VARS:
            with self.subTest(var=var):
                self.assertIn(
                    var,
                    comment_lines,
                    f"deploy/cloudrun-job.yaml needs a comment noting {var} "
                    "is intentionally omitted (unused by the scheduler path)",
                )

    def test_job_yaml_never_injects_omitted_vars_as_env(self):
        """They may appear in comments only — never as actual env entries."""
        for var in self.OMITTED_VARS:
            with self.subTest(var=var):
                self.assertNotRegex(
                    self.job_yaml,
                    rf"name:\s*{var}\s*$",
                    f"{var} must not be injected into the job environment",
                )

    def test_readme_documents_concurrency_semantics_flip(self):
        """GHA cancel-in-progress killed the OLD run; the Firestore lease
        skips the NEW run — so a hung task now lives until timeoutSeconds
        instead of ~30 min. Operators must know this before debugging a
        'scheduler skipped' incident. Contract: a line mentioning
        cancel-in-progress in the same breath as the lease/skip flip."""
        self.assertRegex(
            self.readme,
            r"(?is)cancel-in-progress[^\n]*(?:\n[^\n#]*){0,6}(?:skip|lease)",
            "README must document the concurrency-semantics flip: GHA "
            "cancel-in-progress killed the OLD run, the Firestore lease "
            "skips the NEW run (hung task lives until timeoutSeconds)",
        )


if __name__ == "__main__":
    unittest.main()
