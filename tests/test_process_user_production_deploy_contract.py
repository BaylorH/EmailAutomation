"""Black-box safety contract for the process-user Release A deployment."""

from pathlib import Path
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tests import test_process_user_tagless_staging_contract as tagless_contract
from tests.test_process_user_tagless_staging_contract import (
    CANDIDATE_REVISION,
    OLD_REVISION,
    REVISION_SUFFIX,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_process_user.sh"
PREFLIGHT_HELPER = REPO_ROOT / "scripts" / "process_user_gcloud_preflight.sh"
DEPLOY_README = REPO_ROOT / "deploy" / "README.md"
GCLOUD_IGNORE = REPO_ROOT / ".gcloudignore"
GIT_IGNORE = REPO_ROOT / ".gitignore"

ACCOUNT = "bp21harrison@gmail.com"
PROJECT = "email-automation-cache"
PROJECT_NUMBER = "248289505828"
REGION = "us-central1"
SERVICE = "process-user"
SHA = "1234567890abcdef1234567890abcdef12345678"
SHORT_SHA = SHA[:12]
TAG = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    f"cloud-run-source-deploy/process-user:{SHORT_SHA}"
)
DIGEST = "sha256:" + "a" * 64
IMAGE = f"{TAG}@{DIGEST}"
CANONICAL_IMAGE = f"{TAG.rsplit(':', 1)[0]}@{DIGEST}"
SERVICE_ACCOUNT = "248289505828-compute@developer.gserviceaccount.com"
ROLLBACK_REVISION = "process-user-lock-0837727b"
ROLLBACK_DIGEST = "sha256:" + "c" * 64
ROLLBACK_IMAGE = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    f"cloud-run-source-deploy/process-user@{ROLLBACK_DIGEST}"
)
RELEASE_REVISION = "process-user-release-a-abc123"

ENV_VARS = (
    "^:^FIREBASE_BUCKET=email-automation-cache.firebasestorage.app:"
    "ENFORCE_OPENAI_BUDGET=1:USAGE_MONTHLY_BUDGET_USD=100:"
    "SITESIFT_AUTO_REPLY_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:"
    "SITESIFT_DAILY_SEND_CAP=20:"
    "SITESIFT_GLOBAL_DAILY_SEND_CAP=20:"
    "SITESIFT_TOUR_ACTION_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:"
    "SITESIFT_OUTBOUND_MODE=live"
)
SECRETS = (
    "AZURE_API_APP_ID=AZURE_API_APP_ID:latest,"
    "AZURE_API_CLIENT_SECRET=AZURE_API_CLIENT_SECRET:latest,"
    "FIREBASE_API_KEY=FIREBASE_API_KEY:latest,"
    "OPENAI_API_KEY=OPENAI_API_KEY:latest,"
    "GOOGLE_OAUTH_CLIENT_ID=GOOGLE_OAUTH_CLIENT_ID:latest,"
    "GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest,"
    "GOOGLE_REFRESH_TOKEN=GOOGLE_REFRESH_TOKEN:latest"
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class DeployScriptContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.gcloud_log = self.tmp / "gcloud.log"
        self.git_log = self.tmp / "git.log"
        self.gcloud_state = self.tmp / "gcloud-state.json"
        self.gcloud_state.write_text(
            json.dumps({"service_describes": 0}),
            encoding="utf-8",
        )

        _write_executable(
            self.bin_dir / "git",
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
                if [ "$1" = "-C" ]; then
                  [ "$2" = "${FAKE_REPO_ROOT:?}" ] || exit 63
                  shift 2
                fi
                case "$*" in
                  "status --porcelain")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      dirty_tracked) printf '%s\\n' ' M app.py' ;;
                      dirty_untracked) printf '%s\\n' '?? local.txt' ;;
                    esac
                    exit 0
                    ;;
                  "rev-parse --short=12 HEAD") printf '%s\\n' "${FAKE_GIT_SHA%????????????????????????????}"; exit 0 ;;
                esac
                exit 64
                """
            ),
        )
        _write_executable(
            self.bin_dir / "gcloud",
            tagless_contract.TaglessStagingContractTests._fake_gcloud_source(),
        )
        _write_executable(
            self.bin_dir / "python3",
            textwrap.dedent(
                """\
                #!/bin/sh
                if [ "$1" = "${FAKE_PHASE1_ROLLOUT_MODULE:?}" ]; then
                  printf '%s\n' '["phase1-prerequisites"]' >> "$FAKE_GCLOUD_LOG"
                  [ "${2:-}" = "--verify-staging-prerequisites" ] || exit 64
                  [ "$FAKE_GCLOUD_SCENARIO" != "prerequisite_failure" ] || exit 78
                  exit 0
                fi
                exec "${REAL_PYTHON3:?}" "$@"
                """
            ),
        )

    def _run(
        self,
        *args: str,
        account: str | None = ACCOUNT,
        scenario: str = "ok",
        cwd: Path = REPO_ROOT,
        impersonation_env: str | None = None,
        auth_override_env: tuple[str, str] | None = None,
    ):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["FAKE_GCLOUD_LOG"] = str(self.gcloud_log)
        env["FAKE_GIT_LOG"] = str(self.git_log)
        env["FAKE_GCLOUD_STATE"] = str(self.gcloud_state)
        env["FAKE_GIT_SHA"] = SHA
        env["FAKE_REPO_ROOT"] = str(REPO_ROOT)
        env["FAKE_GCLOUD_SCENARIO"] = scenario
        env["FAKE_CANDIDATE_REVISION"] = CANDIDATE_REVISION
        env["FAKE_OLD_REVISION"] = OLD_REVISION
        env["FAKE_OTHER_REVISION"] = "process-user-00096-old"
        env["FAKE_CANONICAL_IMAGE"] = CANONICAL_IMAGE
        env["FAKE_PHASE1_ROLLOUT_MODULE"] = str(
            REPO_ROOT / "scripts" / "phase1_rollout.py"
        )
        env["REAL_PYTHON3"] = sys.executable
        if impersonation_env is None:
            env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
        else:
            env["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = impersonation_env
        for name in (
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
            "CLOUDSDK_CORE_ACCOUNT",
            "CLOUDSDK_CORE_PROJECT",
        ):
            env.pop(name, None)
        if auth_override_env is not None:
            env[auth_override_env[0]] = auth_override_env[1]
        if account is None:
            env.pop("GCLOUD_ACCOUNT", None)
        else:
            env["GCLOUD_ACCOUNT"] = account
        return subprocess.run(
            ["bash", str(DEPLOY_SCRIPT), *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _gcloud_calls(self) -> list[list[str]]:
        if not self.gcloud_log.exists():
            return []
        return [json.loads(line) for line in self.gcloud_log.read_text().splitlines()]

    def _git_calls(self) -> list[list[str]]:
        if not self.git_log.exists():
            return []
        return [shlex.split(line) for line in self.git_log.read_text().splitlines()]

    def test_dry_run_is_default_and_makes_zero_gcloud_calls(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertEqual(self._git_calls(), self._git_preflight_calls())
        self.assertIn(TAG, result.stdout)
        self.assertIn("dry-run", result.stdout.lower())

    def test_deploy_script_sources_shared_preflight_helper(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(PREFLIGHT_HELPER.is_file())
        self.assertIn('source "$SCRIPT_DIR/process_user_gcloud_preflight.sh"', script)
        self.assertIn("process_user_gcloud_preflight", script)
        self.assertNotIn("gcloud auth list", script)
        self.assertNotIn("gcloud projects describe", script)

    def test_gcloudignore_excludes_local_state_and_includes_repo_safety_rules(self):
        self.assertTrue(
            GCLOUD_IGNORE.is_file(),
            "an explicit .gcloudignore must make the HEAD-tagged build context reproducible",
        )
        rules = [
            line.strip()
            for line in GCLOUD_IGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and (not line.lstrip().startswith("#") or line.startswith("#!include:"))
        ]
        self.assertEqual(
            [
                ".gcloudignore",
                ".git",
                ".gitignore",
                "#!include:.gitignore",
                ".pytest_cache/",
            ],
            rules,
        )

        gitignore = GIT_IGNORE.read_text(encoding="utf-8").splitlines()
        for required_rule in (
            ".venv/",
            ".env",
            "*credentials*.json",
            "*service-account*.json",
            "*.pem",
            "*.key",
            "msal_token_cache.bin",
            "token_cache.bin",
            "run_production.sh",
            "logs/",
        ):
            self.assertIn(required_rule, gitignore)

        effective_rules = []
        for line in GCLOUD_IGNORE.read_text(encoding="utf-8").splitlines():
            if line.strip() == "#!include:.gitignore":
                effective_rules.extend(gitignore)
            else:
                effective_rules.append(line)

        tracked_python = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()
        required_paths = {
            ".dockerignore",
            "Dockerfile",
            "requirements.lock",
            "service.py",
            "main.py",
            "config.py",
            "scripts/deploy_process_user.sh",
            *(
                path
                for path in tracked_python
                if not path.startswith("tests/")
                and "/venv/" not in path
                and "/.venv/" not in path
            ),
        }
        forbidden_paths = {
            ".env",
            ".env.local",
            ".gcloudignore",
            ".pytest_cache/v/cache/nodeids",
            ".venv/bin/python",
            "local-credentials.json",
            "logs/process-user.log",
            "msal_token_cache.bin",
            "private.key",
            "private.pem",
            "run_production.sh",
            "service-account.json",
            "token_cache.bin",
        }

        with tempfile.TemporaryDirectory() as tempdir:
            sandbox = Path(tempdir)
            subprocess.run(
                ["git", "init", "--quiet"],
                cwd=sandbox,
                text=True,
                capture_output=True,
                check=True,
            )
            (sandbox / ".gitignore").write_text(
                "\n".join(effective_rules) + "\n",
                encoding="utf-8",
            )
            for relative_path in sorted(required_paths | forbidden_paths):
                path = sandbox / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            def is_ignored(relative_path: str) -> bool:
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", "--", relative_path],
                    cwd=sandbox,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertIn(result.returncode, (0, 1), result.stderr)
                return result.returncode == 0

            for relative_path in sorted(required_paths):
                self.assertTrue((REPO_ROOT / relative_path).is_file(), relative_path)
                self.assertFalse(is_ignored(relative_path), relative_path)
            for relative_path in sorted(forbidden_paths):
                self.assertTrue(is_ignored(relative_path), relative_path)

    def test_explicit_dry_run_makes_zero_gcloud_calls(self):
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertEqual(self._git_calls(), self._git_preflight_calls())

    def test_missing_principal_stops_before_git_or_gcloud(self):
        result = self._run("--apply", account=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertFalse(self.git_log.exists())

    def test_wrong_principal_stops_before_git_or_gcloud(self):
        result = self._run("--apply", account="someone@example.com")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertFalse(self.git_log.exists())

    def test_impersonation_environment_stops_before_git_or_gcloud(self):
        result = self._run(
            "--apply",
            impersonation_env="deployer@example.iam.gserviceaccount.com",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertFalse(self.git_log.exists())

    def test_cloud_sdk_auth_environment_overrides_stop_before_git_or_gcloud(self):
        for name in (
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
            "CLOUDSDK_CORE_ACCOUNT",
            "CLOUDSDK_CORE_PROJECT",
        ):
            with self.subTest(name=name):
                result = self._run(
                    "--apply", auth_override_env=(name, "unexpected")
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self._gcloud_calls(), [])
                self.assertFalse(self.git_log.exists())

    def test_configured_impersonation_stops_before_auth_or_mutation(self):
        result = self._run("--apply", scenario="configured_impersonation")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [self._config_call()])

    def test_absolute_script_path_builds_repository_root_not_caller_directory(self):
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        result = self._run("--apply", cwd=foreign)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._gcloud_calls()
        self.assertIn(self._deploy_call(), calls)
        self.assertEqual(
            calls[len(self._apply_gate_calls()) + 3], self._build_call()
        )

    def test_auth_missing_stops_before_project_or_mutation(self):
        result = self._run("--apply", scenario="auth_missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [
            *self._config_calls(),
            [
                "auth",
                "list",
                "--account",
                ACCOUNT,
                "--project",
                PROJECT,
                f"--filter=account={ACCOUNT}",
                "--format=value(account)",
            ]
        ])

    def test_auth_duplicate_stops_before_project_or_mutation(self):
        result = self._run("--apply", scenario="auth_duplicate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self._gcloud_calls()), len(self._config_calls()) + 1)

    def test_wrong_project_number_stops_before_build(self):
        result = self._run("--apply", scenario="project_wrong_number")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), self._preflight_calls())

    def test_inactive_project_stops_before_build(self):
        result = self._run("--apply", scenario="project_inactive")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), self._preflight_calls())

    def test_dirty_tracked_checkout_stops_before_gcloud(self):
        result = self._run("--apply", scenario="dirty_tracked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._git_calls(), [self._git_preflight_calls()[0]])
        self.assertEqual(self._gcloud_calls(), [])

    def test_dirty_untracked_checkout_stops_before_gcloud(self):
        result = self._run("--apply", scenario="dirty_untracked")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._git_calls(), [self._git_preflight_calls()[0]])
        self.assertEqual(self._gcloud_calls(), [])

    def test_empty_digest_stops_before_deploy(self):
        result = self._run("--apply", scenario="empty_digest")
        self.assertNotEqual(result.returncode, 0)
        calls = self._gcloud_calls()
        self.assertEqual(
            calls,
            [
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
                self._build_call(),
                self._digest_call(),
            ],
        )

    def test_invalid_digest_stops_before_deploy(self):
        result = self._run("--apply", scenario="invalid_digest")
        self.assertNotEqual(result.returncode, 0)
        calls = self._gcloud_calls()
        self.assertEqual(len(calls), len(self._apply_gate_calls()) + 5)
        self.assertEqual(calls[-1], self._digest_call())
        self.assertFalse(any(call[:2] == ["run", "deploy"] for call in calls))

    def test_apply_uses_exact_order_and_immutable_digest_deployment(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
                self._build_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
                self._revision_describe_call(),
            ],
        )

    def test_deploy_omits_service_wide_scaling_flags(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = self._find_deploy_call()
        self.assertNotIn("--min", deploy)
        self.assertNotIn("--max", deploy)
        self.assertEqual(deploy[deploy.index("--min-instances") + 1], "0")
        self.assertEqual(deploy[deploy.index("--max-instances") + 1], "10")
        self.assertNotIn("--cpu=1", deploy)
        self.assertNotIn("--memory=1Gi", deploy)

    def test_deploy_updates_config_without_erasing_panic_switch_or_other_secrets(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = self._find_deploy_call()
        self.assertIn("--update-env-vars", deploy)
        self.assertIn("--update-secrets", deploy)
        self.assertNotIn("--set-env-vars", deploy)
        self.assertNotIn("--set-secrets", deploy)

    def test_deploy_explicitly_arms_only_the_internal_release_lane(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = self._find_deploy_call()
        env_vars = deploy[deploy.index("--update-env-vars") + 1]
        self.assertIn("SITESIFT_OUTBOUND_MODE=live", env_vars)
        self.assertIn("SITESIFT_DAILY_SEND_CAP=20", env_vars)
        self.assertIn("SITESIFT_GLOBAL_DAILY_SEND_CAP=20", env_vars)
        self.assertIn(
            "SITESIFT_AUTO_REPLY_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            env_vars,
        )
        self.assertIn(
            "SITESIFT_TOUR_ACTION_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            env_vars,
        )

    def test_every_gcloud_call_binds_explicit_account_and_project(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self._gcloud_calls():
            if call == ["phase1-prerequisites"]:
                continue
            self.assertIn("--account", call)
            self.assertEqual(call[call.index("--account") + 1], ACCOUNT)
            if call[:2] != ["projects", "describe"]:
                self.assertIn("--project", call)
                self.assertEqual(call[call.index("--project") + 1], PROJECT)

    @staticmethod
    def _git_preflight_calls() -> list[list[str]]:
        prefix = ["-C", str(REPO_ROOT)]
        return [
            [*prefix, "status", "--porcelain"],
            [*prefix, "rev-parse", "--short=12", "HEAD"],
        ]

    def _find_deploy_call(self) -> list[str]:
        return next(
            call for call in self._gcloud_calls() if call[:2] == ["run", "deploy"]
        )

    @staticmethod
    def _config_call() -> list[str]:
        return [
            "config", "get-value", "auth/impersonate_service_account",
            "--account", ACCOUNT, "--project", PROJECT,
        ]

    @staticmethod
    def _config_calls() -> list[list[str]]:
        return [
            [
                "config", "get-value", property_name,
                "--account", ACCOUNT, "--project", PROJECT,
            ]
            for property_name in (
                "auth/impersonate_service_account",
                "auth/access_token_file",
                "auth/credential_file_override",
            )
        ]

    @staticmethod
    def _preflight_calls() -> list[list[str]]:
        return [
            *DeployScriptContractTests._config_calls(),
            [
                "auth",
                "list",
                "--account",
                ACCOUNT,
                "--project",
                PROJECT,
                f"--filter=account={ACCOUNT}",
                "--format=value(account)",
            ],
            [
                "projects",
                "describe",
                PROJECT,
                "--account",
                ACCOUNT,
                "--project",
                PROJECT,
                "--format=value(projectNumber,lifecycleState)",
            ],
        ]

    @staticmethod
    def _apply_gate_calls() -> list[list[str]]:
        return [
            *DeployScriptContractTests._preflight_calls(),
            ["phase1-prerequisites"],
        ]

    @staticmethod
    def _build_call() -> list[str]:
        return [
            "builds",
            "submit",
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--tag", TAG,
            str(REPO_ROOT),
        ]

    @staticmethod
    def _service_describe_call() -> list[str]:
        return [
            "run", "services", "describe", SERVICE,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--region", REGION,
            "--format=json",
        ]

    @staticmethod
    def _revision_list_call() -> list[str]:
        return tagless_contract.TaglessStagingContractTests._revision_list_call()

    @staticmethod
    def _baseline_revision_describe_call() -> list[str]:
        return tagless_contract.TaglessStagingContractTests._baseline_revision_describe_call()

    @staticmethod
    def _digest_call() -> list[str]:
        return [
            "artifacts",
            "docker",
            "images",
            "describe",
            TAG,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--format=value(image_summary.digest)",
        ]

    @staticmethod
    def _deploy_call() -> list[str]:
        return [
            "run",
            "deploy",
            SERVICE,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--region", REGION,
            "--image", IMAGE,
            "--command", "gunicorn",
            "--args=--bind=:8080,--workers=1,--threads=8,--max-requests=1,--timeout=0,service:app",
            "--service-account", SERVICE_ACCOUNT,
            "--concurrency", "1",
            "--memory", "2Gi",
            "--timeout", "540",
            "--min-instances", "0",
            "--max-instances", "10",
            "--no-allow-unauthenticated",
            "--update-env-vars", ENV_VARS,
            "--update-secrets", SECRETS,
            "--no-traffic",
            "--revision-suffix", REVISION_SUFFIX,
        ]

    @staticmethod
    def _revision_describe_call() -> list[str]:
        return [
            "run", "revisions", "describe", CANDIDATE_REVISION,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--region", REGION,
            "--format=json",
        ]


class RollbackRunbookContractTests(unittest.TestCase):
    def _extract_runbook(self) -> str:
        readme = DEPLOY_README.read_text(encoding="utf-8")
        heading = "### Prove rollback and guaranteed Release A restoration"
        self.assertEqual(readme.count(heading), 1)
        after_heading = readme.split(heading, 1)[1]
        match = re.search(r"```bash\n(.*?)\n```", after_heading, flags=re.DOTALL)
        self.assertIsNotNone(match, "heading must be followed by an executable bash block")
        return match.group(1)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.log = self.tmp / "gcloud.jsonl"
        self.git_log = self.tmp / "git.jsonl"
        self.state = self.tmp / "state.json"
        self.state.write_text(json.dumps({"current": RELEASE_REVISION}), encoding="utf-8")
        _write_executable(self.bin_dir / "git", self._fake_git_source())
        _write_executable(self.bin_dir / "gcloud", self._fake_gcloud_source())

    def _run(
        self,
        scenario: str = "ok",
        account: str | None = ACCOUNT,
        replace_rollback_placeholders: bool = True,
    ):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["FAKE_GCLOUD_LOG"] = str(self.log)
        env["FAKE_GIT_LOG"] = str(self.git_log)
        env["FAKE_GCLOUD_STATE"] = str(self.state)
        env["FAKE_GCLOUD_SCENARIO"] = scenario
        if scenario == "release_not_live":
            self.state.write_text(
                json.dumps({"current": ROLLBACK_REVISION}),
                encoding="utf-8",
            )
        if account is None:
            env.pop("GCLOUD_ACCOUNT", None)
        else:
            env["GCLOUD_ACCOUNT"] = account
        runbook = self._extract_runbook()
        if replace_rollback_placeholders:
            runbook = runbook.replace(
                'ROLLBACK_REVISION="REPLACE_ME_ROLLBACK_REVISION"',
                f'ROLLBACK_REVISION="{ROLLBACK_REVISION}"',
            ).replace(
                'EXPECTED_ROLLBACK_IMAGE="REPLACE_ME_ROLLBACK_IMAGE@sha256:REPLACE_ME_ROLLBACK_DIGEST"',
                f'EXPECTED_ROLLBACK_IMAGE="{ROLLBACK_IMAGE}"',
            )
        return subprocess.run(
            ["bash", "-c", runbook],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def _git_calls(self) -> list[list[str]]:
        if not self.git_log.exists():
            return []
        return [json.loads(line) for line in self.git_log.read_text().splitlines()]

    def _traffic_targets(self) -> list[str]:
        targets = []
        for call in self._calls():
            if call[:3] == ["run", "services", "update-traffic"]:
                if "--to-revisions" in call:
                    revision_arg = call[call.index("--to-revisions") + 1]
                else:
                    revision_arg = next(arg for arg in call if arg.startswith("--to-revisions="))
                    revision_arg = revision_arg.removeprefix("--to-revisions=")
                targets.append(revision_arg.rsplit("=", 1)[0])
        return targets

    def test_success_rolls_back_then_restores_exact_release_revision(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._git_calls(), [["rev-parse", "--short=12", "HEAD"]])
        self.assertEqual(self._traffic_targets(), [ROLLBACK_REVISION, RELEASE_REVISION])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["current"], RELEASE_REVISION)

    def test_runbook_sources_shared_preflight_without_duplicating_checks(self):
        runbook = self._extract_runbook()
        self.assertIn("scripts/process_user_gcloud_preflight.sh", runbook)
        self.assertIn("process_user_gcloud_preflight", runbook)
        self.assertNotIn("gcloud auth list", runbook)
        self.assertNotIn("gcloud projects describe", runbook)

    def test_unedited_rollback_placeholders_fail_before_gcloud(self):
        result = self._run(replace_rollback_placeholders=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("REPLACE_ME", result.stderr)
        self.assertEqual(self._calls(), [])

    def test_missing_rollback_revision_fails_before_traffic_mutation(self):
        result = self._run(scenario="rollback_revision_missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [])

    def test_mismatched_rollback_image_fails_before_traffic_mutation(self):
        result = self._run(scenario="mismatched_rollback_image")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [])

    def test_rollback_mutation_failure_still_restores_release_a(self):
        result = self._run(scenario="rollback_update_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [ROLLBACK_REVISION, RELEASE_REVISION])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["current"], RELEASE_REVISION)

    def test_rollback_readback_failure_triggers_guaranteed_restoration(self):
        result = self._run(scenario="rollback_readback_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [ROLLBACK_REVISION, RELEASE_REVISION])
        state = json.loads(self.state.read_text())
        self.assertEqual(state["current"], RELEASE_REVISION)

    def test_unprovable_restoration_fails_critically(self):
        result = self._run(scenario="restoration_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CRITICAL", result.stderr)
        self.assertGreaterEqual(self._traffic_targets().count(RELEASE_REVISION), 1)

    def test_missing_principal_makes_zero_gcloud_calls(self):
        result = self._run(account=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._calls(), [])

    def test_invalid_artifact_digest_fails_before_traffic_mutation(self):
        result = self._run(scenario="invalid_release_digest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [])

    def test_mismatched_release_image_fails_before_traffic_mutation(self):
        result = self._run(scenario="mismatched_release_image")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [])

    def test_release_a_must_already_be_live_before_rollback_proof(self):
        result = self._run(scenario="release_not_live")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._traffic_targets(), [])

    def test_every_runbook_gcloud_call_binds_approved_account_and_project(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self._calls():
            self.assertIn("--account", call, call)
            self.assertEqual(call[call.index("--account") + 1], ACCOUNT)
            self.assertIn("--project", call, call)
            self.assertEqual(call[call.index("--project") + 1], PROJECT)

    @staticmethod
    def _fake_git_source() -> str:
        return textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            with Path(os.environ["FAKE_GIT_LOG"]).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")
            if args == ["rev-parse", "--short=12", "HEAD"]:
                print("{SHORT_SHA}")
                raise SystemExit(0)
            raise SystemExit(64)
            """
        )

    @staticmethod
    def _fake_gcloud_source() -> str:
        return textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")

            state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            scenario = os.environ.get("FAKE_GCLOUD_SCENARIO", "ok")

            def save():
                state_path.write_text(json.dumps(state), encoding="utf-8")

            if args[:2] == ["auth", "list"]:
                print("{ACCOUNT}")
                raise SystemExit(0)

            if args[:2] == ["config", "get-value"]:
                if scenario == "configured_impersonation":
                    print("deployer@example.iam.gserviceaccount.com")
                else:
                    print("(unset)")
                raise SystemExit(0)

            if args[:2] == ["projects", "describe"]:
                print("{PROJECT_NUMBER}\\tACTIVE")
                raise SystemExit(0)

            if args[:3] == ["artifacts", "docker", "images"]:
                if scenario == "invalid_release_digest":
                    print("latest")
                else:
                    print("{DIGEST}")
                raise SystemExit(0)

            if args[:3] == ["run", "services", "describe"]:
                current = state["current"]
                if scenario == "rollback_readback_failure" and current == "{ROLLBACK_REVISION}" and not state.get("bad_readback_emitted"):
                    current = "unexpected-revision"
                    state["bad_readback_emitted"] = True
                    save()
                print(json.dumps({{
                    "metadata": {{"annotations": {{"run.googleapis.com/maxScale": "20"}}}},
                    "status": {{"traffic": [
                        {{"revisionName": "{RELEASE_REVISION}", "tag": "release-a"}},
                        {{"revisionName": current, "percent": 100}},
                    ]}},
                }}))
                raise SystemExit(0)

            if args[:3] == ["run", "revisions", "describe"]:
                revision_name = args[3]
                if scenario == "rollback_revision_missing" and revision_name == "{ROLLBACK_REVISION}":
                    raise SystemExit(1)
                if revision_name == "{ROLLBACK_REVISION}":
                    rollback_image = (
                        "{ROLLBACK_IMAGE.rsplit('@', 1)[0]}@sha256:" + "d" * 64
                        if scenario == "mismatched_rollback_image"
                        else "{ROLLBACK_IMAGE}"
                    )
                    print(json.dumps({{
                        "spec": {{"containers": [{{"image": rollback_image}}]}},
                    }}))
                    raise SystemExit(0)
                release_image = (
                    "us-central1-docker.pkg.dev/email-automation-cache/"
                    "cloud-run-source-deploy/process-user@sha256:" + "b" * 64
                    if scenario == "mismatched_release_image"
                    else "{CANONICAL_IMAGE}"
                )
                print(json.dumps({{
                    "metadata": {{"annotations": {{"autoscaling.knative.dev/maxScale": "10"}}}},
                    "spec": {{
                        "containerConcurrency": 1,
                        "containers": [{{"image": release_image}}],
                    }},
                }}))
                raise SystemExit(0)

            if args[:3] == ["run", "services", "update-traffic"]:
                if "--to-revisions" in args:
                    revision_arg = args[args.index("--to-revisions") + 1]
                else:
                    revision_arg = next(arg for arg in args if arg.startswith("--to-revisions="))
                    revision_arg = revision_arg.removeprefix("--to-revisions=")
                target = revision_arg.rsplit("=", 1)[0]
                if target == "{ROLLBACK_REVISION}" and scenario == "rollback_update_failure":
                    raise SystemExit(1)
                if target == "{RELEASE_REVISION}" and scenario == "restoration_failure":
                    raise SystemExit(1)
                state["current"] = target
                save()
                raise SystemExit(0)

            print("unexpected fake gcloud command: " + " ".join(args), file=sys.stderr)
            raise SystemExit(65)
            """
        )


if __name__ == "__main__":
    unittest.main()
