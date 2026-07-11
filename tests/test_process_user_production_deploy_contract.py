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


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_process_user.sh"
DEPLOY_README = REPO_ROOT / "deploy" / "README.md"

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
SERVICE_ACCOUNT = "248289505828-compute@developer.gserviceaccount.com"
ROLLBACK_REVISION = "process-user-lock-0837727b"
RELEASE_REVISION = "process-user-release-a-abc123"

ENV_VARS = (
    "FIREBASE_BUCKET=email-automation-cache.firebasestorage.app,"
    "ENFORCE_OPENAI_BUDGET=1,USAGE_MONTHLY_BUDGET_USD=100"
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
            textwrap.dedent(
                """\
                #!/bin/sh
                printf '%s\\n' "$*" >> "$FAKE_GCLOUD_LOG"
                if [ "${CLOUDSDK_CORE_ACCOUNT:-}" != "bp21harrison@gmail.com" ]; then
                  printf '%s\\n' 'gcloud account override is not bound to the approved principal' >&2
                  exit 70
                fi
                case "$1 $2" in
                  "config get-value")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      configured_impersonation) printf '%s\\n' 'deployer@example.iam.gserviceaccount.com' ;;
                      *) printf '%s\\n' '(unset)' ;;
                    esac
                    exit 0
                    ;;
                  "auth list")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      auth_missing) exit 0 ;;
                      auth_duplicate)
                        printf '%s\\n%s\\n' 'bp21harrison@gmail.com' 'bp21harrison@gmail.com'
                        exit 0
                        ;;
                      *) printf '%s\\n' 'bp21harrison@gmail.com'; exit 0 ;;
                    esac
                    ;;
                  "projects describe")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      project_wrong_number) printf '%s\\t%s\\n' '999' 'ACTIVE' ;;
                      project_inactive) printf '%s\\t%s\\n' '248289505828' 'DELETE_REQUESTED' ;;
                      *) printf '%s\\t%s\\n' '248289505828' 'ACTIVE' ;;
                    esac
                    exit 0
                    ;;
                  "builds submit") exit 0 ;;
                  "artifacts docker")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      empty_digest) exit 0 ;;
                      invalid_digest) printf '%s\\n' 'latest' ;;
                      *) printf '%s\\n' "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" ;;
                    esac
                    exit 0
                    ;;
                  "run deploy") exit 0 ;;
                esac
                printf 'unexpected fake gcloud command: %s\\n' "$*" >&2
                exit 65
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
    ):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["FAKE_GCLOUD_LOG"] = str(self.gcloud_log)
        env["FAKE_GIT_LOG"] = str(self.git_log)
        env["FAKE_GIT_SHA"] = SHA
        env["FAKE_REPO_ROOT"] = str(REPO_ROOT)
        env["FAKE_GCLOUD_SCENARIO"] = scenario
        if impersonation_env is None:
            env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
        else:
            env["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = impersonation_env
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
        return [shlex.split(line) for line in self.gcloud_log.read_text().splitlines()]

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

    def test_configured_impersonation_stops_before_auth_or_mutation(self):
        result = self._run("--apply", scenario="configured_impersonation")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [self._config_call()])

    def test_absolute_script_path_builds_repository_root_not_caller_directory(self):
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        result = self._run("--apply", cwd=foreign)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gcloud_calls()[-1], self._deploy_call())
        self.assertEqual(self._gcloud_calls()[-3], self._build_call())

    def test_auth_missing_stops_before_project_or_mutation(self):
        result = self._run("--apply", scenario="auth_missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [
            self._config_call(),
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
        self.assertEqual(len(self._gcloud_calls()), 2)

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
        self.assertEqual(calls[:3], self._preflight_calls())
        self.assertEqual(calls[3], self._build_call())
        self.assertEqual(calls[4], self._digest_call())
        self.assertEqual(len(calls), 5)

    def test_invalid_digest_stops_before_deploy(self):
        result = self._run("--apply", scenario="invalid_digest")
        self.assertNotEqual(result.returncode, 0)
        calls = self._gcloud_calls()
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[-1], self._digest_call())
        self.assertFalse(any(call[:2] == ["run", "deploy"] for call in calls))

    def test_apply_uses_exact_order_and_immutable_digest_deployment(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gcloud_calls(),
            [*self._preflight_calls(), self._build_call(), self._digest_call(), self._deploy_call()],
        )

    def test_deploy_omits_service_wide_scaling_flags(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = self._gcloud_calls()[-1]
        self.assertNotIn("--min", deploy)
        self.assertNotIn("--max", deploy)
        self.assertEqual(deploy[deploy.index("--min-instances") + 1], "0")
        self.assertEqual(deploy[deploy.index("--max-instances") + 1], "10")
        self.assertNotIn("--cpu=1", deploy)
        self.assertNotIn("--memory=1Gi", deploy)

    def test_every_gcloud_call_binds_explicit_account_and_project(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self._gcloud_calls():
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

    @staticmethod
    def _config_call() -> list[str]:
        return [
            "config", "get-value", "auth/impersonate_service_account",
            "--account", ACCOUNT, "--project", PROJECT,
        ]

    @staticmethod
    def _preflight_calls() -> list[list[str]]:
        return [
            DeployScriptContractTests._config_call(),
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
                "--format=value(projectNumber,lifecycleState)",
            ],
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
            "--args", "--bind=:8080,--workers=1,--threads=8,--timeout=0,service:app",
            "--service-account", SERVICE_ACCOUNT,
            "--concurrency", "1",
            "--timeout", "540",
            "--min-instances", "0",
            "--max-instances", "10",
            "--no-allow-unauthenticated",
            "--set-env-vars", ENV_VARS,
            "--set-secrets", SECRETS,
            "--no-traffic",
            "--tag", "release-a",
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

    def _run(self, scenario: str = "ok", account: str | None = ACCOUNT):
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
        return subprocess.run(
            ["bash", "-c", self._extract_runbook()],
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

            if os.environ.get("CLOUDSDK_CORE_ACCOUNT") != "{ACCOUNT}":
                print("gcloud account override is not bound to the approved principal", file=sys.stderr)
                raise SystemExit(70)

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
                release_image = (
                    "{TAG}@sha256:" + "b" * 64
                    if scenario == "mismatched_release_image"
                    else "{IMAGE}"
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
