"""Black-box contract for effect-bounded process-user tagless staging."""

from pathlib import Path
import json
import os
import shlex
import stat
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_process_user.sh"

ACCOUNT = "bp21harrison@gmail.com"
PROJECT = "email-automation-cache"
REGION = "us-central1"
SERVICE = "process-user"
SHA = "1234567890abcdef1234567890abcdef12345678"
SHORT_SHA = SHA[:12]
REVISION_SUFFIX = f"stage-{SHORT_SHA}"
CANDIDATE_REVISION = f"{SERVICE}-{REVISION_SUFFIX}"
OLD_REVISION = "process-user-00097-yus"
OTHER_REVISION = "process-user-00096-old"
TAG = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    f"cloud-run-source-deploy/process-user:{SHORT_SHA}"
)
DIGEST = "sha256:" + "a" * 64
IMAGE = f"{TAG}@{DIGEST}"
CANONICAL_IMAGE = f"{TAG.rsplit(':', 1)[0]}@{DIGEST}"
SERVICE_ACCOUNT = "248289505828-compute@developer.gserviceaccount.com"

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


class TaglessStagingContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp = Path(self.tempdir.name)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        self.gcloud_log = self.tmp / "gcloud.jsonl"
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
                  "rev-parse --short=12 HEAD")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      invalid_sha) printf '%s\\n' 'ABCDEF123456' ;;
                      *) printf '%s\\n' "${FAKE_GIT_SHA%????????????????????????????}" ;;
                    esac
                    exit 0
                    ;;
                esac
                exit 64
                """
            ),
        )
        _write_executable(self.bin_dir / "gcloud", self._fake_gcloud_source())

    def _run(
        self,
        *args: str,
        account: str | None = ACCOUNT,
        scenario: str = "ok",
        cwd: Path = REPO_ROOT,
        impersonation_env: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
        env["FAKE_OTHER_REVISION"] = OTHER_REVISION
        env["FAKE_CANONICAL_IMAGE"] = CANONICAL_IMAGE
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
        return [json.loads(line) for line in self.gcloud_log.read_text().splitlines()]

    def _git_calls(self) -> list[list[str]]:
        if not self.git_log.exists():
            return []
        return [shlex.split(line) for line in self.git_log.read_text().splitlines()]

    def test_dry_run_is_untagged_and_executes_zero_gcloud_calls(self):
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertEqual(self._git_calls(), self._git_preflight_calls())
        self.assertIn(CANDIDATE_REVISION, result.stdout)
        self.assertIn("untagged", result.stdout.lower())
        self.assertIn("0%", result.stdout)

    def test_default_mode_is_the_same_zero_gcloud_dry_run(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertIn("dry-run", result.stdout.lower())
        self.assertIn(REVISION_SUFFIX, result.stdout)

    def test_apply_uses_exact_safe_order_and_proves_readback(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._preflight_calls(),
                self._service_describe_call(),
                self._build_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
                self._revision_describe_call(),
            ],
        )
        self.assertIn("verified", result.stdout.lower())
        self.assertIn("untagged", result.stdout.lower())
        self.assertIn("0%", result.stdout)

    def test_deploy_is_digest_pinned_no_traffic_and_has_no_tag(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = next(
            call for call in self._gcloud_calls() if call[:2] == ["run", "deploy"]
        )
        self.assertEqual(deploy, self._deploy_call())
        self.assertEqual(deploy[deploy.index("--image") + 1], IMAGE)
        self.assertIn("--no-traffic", deploy)
        self.assertNotIn("--tag", deploy)
        self.assertNotIn("release-a", deploy)
        self.assertEqual(
            deploy[deploy.index("--revision-suffix") + 1],
            REVISION_SUFFIX,
        )

    def test_apply_never_mutates_queue_or_traffic(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self._gcloud_calls()
        self.assertFalse(any(call[:2] == ["tasks", "queues"] for call in calls))
        self.assertFalse(
            any(call[:3] == ["run", "services", "update-traffic"] for call in calls)
        )

    def test_invalid_head_identity_stops_before_gcloud(self):
        result = self._run("--apply", scenario="invalid_sha")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertIn("12-character lowercase", result.stderr)

    def test_missing_positive_baseline_stops_before_build(self):
        self._assert_baseline_refused("missing_positive")

    def test_multiple_positive_baseline_stops_before_build(self):
        self._assert_baseline_refused("multiple_positive")

    def test_release_a_must_map_to_the_sole_positive_baseline(self):
        self._assert_baseline_refused("release_a_wrong")

    def test_duplicate_release_a_baseline_is_ambiguous(self):
        self._assert_baseline_refused("release_a_duplicate")

    def test_existing_candidate_route_collision_stops_before_build(self):
        self._assert_baseline_refused("candidate_collision")

    def test_baseline_read_failure_stops_before_build(self):
        self._assert_baseline_refused("baseline_read_failure")

    def test_candidate_must_remain_untagged(self):
        self._assert_post_readback_refused("candidate_tagged")

    def test_candidate_must_remain_at_zero_percent(self):
        self._assert_post_readback_refused("candidate_positive")

    def test_old_positive_route_must_not_drift(self):
        self._assert_post_readback_refused("old_positive_drift")

    def test_release_a_tag_must_not_drift(self):
        self._assert_post_readback_refused("old_tag_drift")

    def test_service_config_must_not_drift(self):
        self._assert_post_readback_refused("service_config_drift")

    def test_missing_latest_created_revision_is_refused(self):
        self._assert_post_readback_refused("latest_created_missing")

    def test_ambiguous_latest_created_revision_is_refused(self):
        self._assert_post_readback_refused("latest_created_ambiguous")

    def test_post_service_read_failure_is_refused(self):
        self._assert_post_readback_refused("post_service_read_failure")

    def test_wrong_candidate_image_is_refused(self):
        self._assert_revision_refused("wrong_image")

    def test_candidate_config_mismatch_is_refused(self):
        self._assert_revision_refused("config_mismatch")

    def test_candidate_not_ready_is_refused(self):
        self._assert_revision_refused("ready_false")

    def test_candidate_revision_read_failure_is_refused(self):
        self._assert_revision_refused("revision_read_failure")

    def test_deploy_failure_stops_before_readback(self):
        result = self._run("--apply", scenario="deploy_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._preflight_calls(),
                self._service_describe_call(),
                self._build_call(),
                self._digest_call(),
                self._deploy_call(),
            ],
        )

    def _assert_baseline_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [*self._preflight_calls(), self._service_describe_call()],
        )

    def _assert_post_readback_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._preflight_calls(),
                self._service_describe_call(),
                self._build_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
            ],
        )

    def _assert_revision_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._preflight_calls(),
                self._service_describe_call(),
                self._build_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
                self._revision_describe_call(),
            ],
        )

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
            TaglessStagingContractTests._config_call(),
            [
                "auth", "list",
                "--account", ACCOUNT,
                "--project", PROJECT,
                f"--filter=account={ACCOUNT}",
                "--format=value(account)",
            ],
            [
                "projects", "describe", PROJECT,
                "--account", ACCOUNT,
                "--project", PROJECT,
                "--format=value(projectNumber,lifecycleState)",
            ],
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
    def _build_call() -> list[str]:
        return [
            "builds", "submit",
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--tag", TAG,
            str(REPO_ROOT),
        ]

    @staticmethod
    def _digest_call() -> list[str]:
        return [
            "artifacts", "docker", "images", "describe", TAG,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--format=value(image_summary.digest)",
        ]

    @staticmethod
    def _deploy_call() -> list[str]:
        return [
            "run", "deploy", SERVICE,
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

    @staticmethod
    def _fake_gcloud_source() -> str:
        return textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            log_path = Path(os.environ["FAKE_GCLOUD_LOG"])
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")

            if os.environ.get("CLOUDSDK_CORE_ACCOUNT") != "bp21harrison@gmail.com":
                print("gcloud account override is not bound to the approved principal", file=sys.stderr)
                raise SystemExit(70)

            scenario = os.environ.get("FAKE_GCLOUD_SCENARIO", "ok")
            state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            candidate = os.environ["FAKE_CANDIDATE_REVISION"]
            old = os.environ["FAKE_OLD_REVISION"]
            other = os.environ["FAKE_OTHER_REVISION"]
            canonical_image = os.environ["FAKE_CANONICAL_IMAGE"]

            def save():
                state_path.write_text(json.dumps(state), encoding="utf-8")

            def service_document(post_deploy):
                traffic = [
                    {"revisionName": old, "percent": 100},
                    {"revisionName": old, "tag": "release-a", "url": "https://release-a.invalid"},
                ]
                document = {
                    "metadata": {"annotations": {"run.googleapis.com/maxScale": "20"}},
                    "status": {
                        "latestCreatedRevisionName": candidate if post_deploy else old,
                        "traffic": traffic,
                    },
                }
                if not post_deploy:
                    if scenario == "missing_positive":
                        traffic[0].pop("percent")
                    elif scenario == "multiple_positive":
                        traffic[0]["percent"] = 50
                        traffic.append({"revisionName": other, "percent": 50})
                    elif scenario == "release_a_wrong":
                        traffic[1]["revisionName"] = other
                    elif scenario == "release_a_duplicate":
                        traffic.append({"revisionName": old, "tag": "release-a"})
                    elif scenario == "candidate_collision":
                        traffic.append({"revisionName": candidate, "tag": "staging"})
                    return document

                if scenario == "candidate_tagged":
                    traffic.append({"revisionName": candidate, "tag": "staging"})
                elif scenario == "candidate_positive":
                    traffic[0]["percent"] = 90
                    traffic.append({"revisionName": candidate, "percent": 10})
                elif scenario == "old_positive_drift":
                    traffic[0] = {"revisionName": other, "percent": 100}
                elif scenario == "old_tag_drift":
                    traffic[1]["revisionName"] = other
                elif scenario == "service_config_drift":
                    document["metadata"]["annotations"]["run.googleapis.com/maxScale"] = "21"
                elif scenario == "latest_created_missing":
                    document["status"].pop("latestCreatedRevisionName")
                elif scenario == "latest_created_ambiguous":
                    document["status"]["latestCreatedRevisionName"] = other
                return document

            def revision_document():
                image = canonical_image
                concurrency = 1
                ready = "True"
                if scenario == "wrong_image":
                    image = canonical_image.rsplit("sha256:", 1)[0] + "sha256:" + "b" * 64
                elif scenario == "config_mismatch":
                    concurrency = 2
                elif scenario == "ready_false":
                    ready = "False"
                values = {
                    "FIREBASE_BUCKET": "email-automation-cache.firebasestorage.app",
                    "ENFORCE_OPENAI_BUDGET": "1",
                    "USAGE_MONTHLY_BUDGET_USD": "100",
                    "SITESIFT_AUTO_REPLY_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                    "SITESIFT_DAILY_SEND_CAP": "20",
                    "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
                    "SITESIFT_TOUR_ACTION_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                    "SITESIFT_OUTBOUND_MODE": "live",
                }
                secret_names = [
                    "AZURE_API_APP_ID",
                    "AZURE_API_CLIENT_SECRET",
                    "FIREBASE_API_KEY",
                    "OPENAI_API_KEY",
                    "GOOGLE_OAUTH_CLIENT_ID",
                    "GOOGLE_OAUTH_CLIENT_SECRET",
                    "GOOGLE_REFRESH_TOKEN",
                ]
                env = [{"name": name, "value": value} for name, value in values.items()]
                env.extend(
                    {
                        "name": name,
                        "valueFrom": {"secretKeyRef": {"name": name, "key": "latest"}},
                    }
                    for name in secret_names
                )
                return {
                    "metadata": {
                        "name": candidate,
                        "annotations": {
                            "autoscaling.knative.dev/minScale": "0",
                            "autoscaling.knative.dev/maxScale": "10",
                        },
                    },
                    "spec": {
                        "serviceAccountName": "248289505828-compute@developer.gserviceaccount.com",
                        "containerConcurrency": concurrency,
                        "timeoutSeconds": 540,
                        "containers": [{
                            "image": image,
                            "command": ["gunicorn"],
                            "args": [
                                "--bind=:8080",
                                "--workers=1",
                                "--threads=8",
                                "--max-requests=1",
                                "--timeout=0",
                                "service:app",
                            ],
                            "resources": {"limits": {"memory": "2Gi"}},
                            "env": env,
                        }],
                    },
                    "status": {"conditions": [{"type": "Ready", "status": ready}]},
                }

            if args[:2] == ["config", "get-value"]:
                if scenario == "configured_impersonation":
                    print("deployer@example.iam.gserviceaccount.com")
                else:
                    print("(unset)")
                raise SystemExit(0)
            if args[:2] == ["auth", "list"]:
                if scenario == "auth_missing":
                    raise SystemExit(0)
                if scenario == "auth_duplicate":
                    print("bp21harrison@gmail.com")
                    print("bp21harrison@gmail.com")
                    raise SystemExit(0)
                print("bp21harrison@gmail.com")
                raise SystemExit(0)
            if args[:2] == ["projects", "describe"]:
                if scenario == "project_wrong_number":
                    print("999\\tACTIVE")
                elif scenario == "project_inactive":
                    print("248289505828\\tDELETE_REQUESTED")
                else:
                    print("248289505828\\tACTIVE")
                raise SystemExit(0)
            if args[:3] == ["run", "services", "describe"]:
                describe_index = state["service_describes"]
                state["service_describes"] += 1
                save()
                if describe_index == 0 and scenario == "baseline_read_failure":
                    raise SystemExit(1)
                if describe_index == 1 and scenario == "post_service_read_failure":
                    raise SystemExit(1)
                print(json.dumps(service_document(post_deploy=describe_index > 0)))
                raise SystemExit(0)
            if args[:2] == ["builds", "submit"]:
                raise SystemExit(0)
            if args[:3] == ["artifacts", "docker", "images"]:
                if scenario == "empty_digest":
                    raise SystemExit(0)
                if scenario == "invalid_digest":
                    print("latest")
                    raise SystemExit(0)
                print("sha256:" + "a" * 64)
                raise SystemExit(0)
            if args[:2] == ["run", "deploy"]:
                if scenario == "deploy_failure":
                    raise SystemExit(1)
                raise SystemExit(0)
            if args[:3] == ["run", "revisions", "describe"]:
                if scenario == "revision_read_failure":
                    raise SystemExit(1)
                print(json.dumps(revision_document()))
                raise SystemExit(0)
            print("unexpected fake gcloud command: " + " ".join(args), file=sys.stderr)
            raise SystemExit(65)
            """
        )


if __name__ == "__main__":
    unittest.main()
