"""Black-box contract for effect-bounded process-user tagless staging.

RUNTIME, and why the sweep calls this a HANG: it does not hang. It passes, and
it is simply slower than the certification sweep's 25s per-module cutoff, so
subprocess.TimeoutExpired is recorded as HANG. Every test here shells out to
bash (fake gcloud/git shims on PATH, no network at all), and the per-process
cost is the whole runtime.

Measured 2026-08-19, three consecutive runs: 53 tests OK in 38.6s, 41.9s,
41.6s. Unlike its sibling this one is never close to the cutoff, so it is
recorded HANG deterministically, in every baseline, while being green.
Raising the sweep cutoff to ~45s -- or budgeting this module separately --
is what would make its real status visible.
"""

from pathlib import Path
import json
import os
import shlex
import stat
import subprocess
import sys
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
OLD_REVISION = "process-user-stage-48c7381cea2a"
OTHER_REVISION = "process-user-00096-old"
TAG = (
    "us-central1-docker.pkg.dev/email-automation-cache/"
    f"cloud-run-source-deploy/process-user:{SHORT_SHA}"
)
DIGEST = "sha256:" + "a" * 64
IMAGE_REPOSITORY = TAG.rsplit(":", 1)[0]
IMAGE = f"{TAG}@{DIGEST}"
CANONICAL_IMAGE = f"{IMAGE_REPOSITORY}@{DIGEST}"
SERVICE_ACCOUNT = "248289505828-compute@developer.gserviceaccount.com"

ENV_VARS = (
    "^:^FIREBASE_BUCKET=email-automation-cache.firebasestorage.app:"
    "ENFORCE_OPENAI_BUDGET=1:USAGE_MONTHLY_BUDGET_USD=100:"
    "SITESIFT_AUTO_REPLY_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:"
    "SITESIFT_DAILY_SEND_CAP=20:"
    "SITESIFT_GLOBAL_DAILY_SEND_CAP=20:"
    "SITESIFT_TOUR_ACTION_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:"
    "SITESIFT_NATIVE_IMAGE_INGESTION=false:"
    "SITESIFT_OUTBOUND_MODE=live:"
    f"SITESIFT_SOURCE_REVISION={SHA}:"
    f"SITESIFT_IMAGE_DIGEST={DIGEST}"
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
    def test_deploy_readme_does_not_describe_the_live_service_as_unapplied(self):
        readme = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split())

        self.assertNotIn("# WS-B — EmailAutomation scheduler → Cloud Run Job (scaffold)", readme)
        self.assertNotIn("**Scaffold only.** Nothing here has been applied", readme)
        self.assertIn("`process-user` Cloud Run service is live", normalized)

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
                  "rev-parse HEAD")
                    case "$FAKE_GCLOUD_SCENARIO" in
                      invalid_sha) printf '%s\\n' 'NOTASHA' ;;
                      *) printf '%s\\n' "$FAKE_GIT_SHA" ;;
                    esac
                    exit 0
                    ;;
                esac
                exit 64
                """
            ),
        )
        _write_executable(self.bin_dir / "gcloud", self._fake_gcloud_source())
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
        tested_image_digest: str | None = CANONICAL_IMAGE,
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
        if tested_image_digest is None:
            env.pop("TESTED_IMAGE_DIGEST", None)
        else:
            env["TESTED_IMAGE_DIGEST"] = tested_image_digest
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
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
                self._revision_describe_call(),
            ],
        )
        self.assertIn("verified", result.stdout.lower())
        self.assertIn("untagged", result.stdout.lower())
        self.assertIn("0%", result.stdout)

    def test_prerequisite_failure_stops_before_service_build_or_deploy(self):
        result = self._run("--apply", scenario="prerequisite_failure")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            self._apply_gate_calls(),
            self._gcloud_calls(),
        )

    def test_deploy_is_digest_pinned_no_traffic_and_has_no_tag(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = next(
            call for call in self._gcloud_calls() if call[:2] == ["run", "deploy"]
        )
        self.assertEqual(deploy, self._deploy_call())
        self.assertEqual(deploy[deploy.index("--image") + 1], CANONICAL_IMAGE)
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
        deploy_source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        for route in ("/process-user", "/process-outbox"):
            self.assertNotIn(route, deploy_source)

    def test_invalid_head_identity_stops_before_gcloud(self):
        result = self._run("--apply", scenario="invalid_sha")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._gcloud_calls(), [])
        self.assertIn("40-character lowercase", result.stderr)

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

    def test_latest_revision_spec_target_is_rejected_before_build(self):
        self._assert_baseline_refused("latest_spec")

    def test_implicit_spec_target_is_rejected_before_build(self):
        self._assert_baseline_refused("implicit_spec_target")

    def test_duplicate_spec_tag_is_rejected_before_build(self):
        self._assert_baseline_refused("spec_duplicate_tag")

    def test_malformed_spec_tag_is_rejected_before_build(self):
        self._assert_baseline_refused("spec_invalid_tag")

    def test_existing_candidate_outside_service_status_is_rejected_before_build(self):
        self._assert_inventory_refused("inventory_candidate_collision")

    def test_revision_inventory_read_failure_is_rejected_before_build(self):
        self._assert_inventory_refused("inventory_read_failure")

    def test_malformed_revision_inventory_is_rejected_before_build(self):
        self._assert_inventory_refused("inventory_malformed")

    def test_duplicate_revision_inventory_is_rejected_before_build(self):
        self._assert_inventory_refused("inventory_duplicate")

    def test_real_auxiliary_tags_are_accepted_and_preserved(self):
        result = self._run("--apply", scenario="ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release-a", result.stdout)
        self.assertEqual(
            self._gcloud_calls()[len(self._apply_gate_calls())],
            self._service_describe_call(),
        )
        preflight_len = len(self._apply_gate_calls())
        self.assertEqual(
            self._gcloud_calls()[preflight_len + 1], self._revision_list_call()
        )
        self.assertEqual(
            self._gcloud_calls()[preflight_len + 2],
            self._baseline_revision_describe_call(),
        )

    def test_baseline_revision_read_failure_is_rejected_before_build(self):
        self._assert_baseline_revision_refused("baseline_revision_read_failure")

    def test_malformed_baseline_revision_is_rejected_before_build(self):
        self._assert_baseline_revision_refused("baseline_revision_malformed")

    def test_candidate_added_environment_entry_is_rejected(self):
        self._assert_revision_refused("candidate_extra_env")

    def test_candidate_changed_inherited_environment_entry_is_rejected(self):
        self._assert_revision_refused("candidate_changed_extra_env")

    def test_candidate_removed_inherited_environment_entry_is_rejected(self):
        self._assert_revision_refused("candidate_missing_extra_env")

    def test_candidate_other_config_drift_is_rejected(self):
        self._assert_revision_refused("candidate_other_config_drift")

    def test_candidate_functional_annotation_drift_is_rejected(self):
        self._assert_revision_refused("candidate_annotation_drift")

    def test_candidate_missing_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_missing_native_gate")

    def test_candidate_true_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_true_native_gate")

    def test_candidate_case_variant_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_case_native_gate")

    def test_candidate_padded_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_padded_native_gate")

    def test_candidate_duplicate_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_duplicate_native_gate")

    def test_candidate_secret_bound_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_secret_native_gate")

    def test_candidate_extra_keyed_native_gate_is_rejected(self):
        self._assert_revision_refused("candidate_extra_key_native_gate")

    def test_baseline_native_gate_is_rejected(self):
        self._assert_revision_refused("baseline_native_gate")

    def test_cloud_sdk_auth_environment_overrides_stop_before_gcloud(self):
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
                self.assertEqual([], self._gcloud_calls())

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

    def test_canonical_spec_traffic_must_not_change(self):
        self._assert_post_readback_refused("spec_mutation")

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
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
                self._digest_call(),
                self._deploy_call(),
            ],
        )

    # -- the script never builds ------------------------------------------
    #
    # The twin has to run the byte-identical artifact production stages. A
    # rebuild from the same commit produces a different digest, so it would
    # certify a different artifact than the one shipped. The already built and
    # tested digest is therefore an INPUT to staging, never something staging
    # can create.

    def test_apply_never_builds_an_image(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [],
            [call for call in self._gcloud_calls() if call[:1] == ["builds"]],
            "staging must offer the already built digest, never rebuild it",
        )
        source = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("builds submit", source)
        self.assertNotIn("docker build", source)

    def test_missing_tested_image_digest_refuses_before_any_cloud_call(self):
        result = self._run("--apply", tested_image_digest=None)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self._gcloud_calls())
        self.assertIn("TESTED_IMAGE_DIGEST", result.stderr)

    def test_dry_run_also_requires_the_tested_digest(self):
        result = self._run("--dry-run", tested_image_digest=None)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self._gcloud_calls())

    def test_mutable_or_malformed_tested_image_reference_is_refused(self):
        for value in (
            TAG,
            f"{IMAGE_REPOSITORY}:latest",
            IMAGE,
            f"{IMAGE_REPOSITORY}@sha256:{'a' * 63}",
            f"{IMAGE_REPOSITORY}@sha256:{'A' * 64}",
            f"{IMAGE_REPOSITORY}@sha512:{'a' * 64}",
            f"{IMAGE_REPOSITORY}@{DIGEST} ",
        ):
            with self.subTest(value=value):
                self.setUp()
                result = self._run("--apply", tested_image_digest=value)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], self._gcloud_calls())

    def test_foreign_repository_digest_is_refused(self):
        result = self._run(
            "--apply",
            tested_image_digest=(
                "us-central1-docker.pkg.dev/attacker-project/"
                f"cloud-run-source-deploy/process-user@{DIGEST}"
            ),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self._gcloud_calls())

    def test_registry_digest_disagreement_stops_before_deploy(self):
        result = self._run("--apply", scenario="digest_mismatch")
        self.assertNotEqual(0, result.returncode)
        calls = self._gcloud_calls()
        self.assertFalse(any(call[:2] == ["run", "deploy"] for call in calls))
        self.assertEqual(calls[-1], self._digest_call())

    def test_deploy_binds_the_release_stamp_to_source_and_image(self):
        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        deploy = next(
            call for call in self._gcloud_calls() if call[:2] == ["run", "deploy"]
        )
        env_argument = deploy[deploy.index("--update-env-vars") + 1]
        self.assertIn(f"SITESIFT_SOURCE_REVISION={SHA}", env_argument)
        self.assertIn(f"SITESIFT_IMAGE_DIGEST={DIGEST}", env_argument)
        self.assertEqual(deploy[deploy.index("--image") + 1], CANONICAL_IMAGE)

    def test_missing_candidate_source_stamp_is_refused(self):
        self._assert_revision_refused("candidate_missing_source_stamp")

    def test_missing_candidate_digest_stamp_is_refused(self):
        self._assert_revision_refused("candidate_missing_digest_stamp")

    def test_forged_candidate_source_stamp_is_refused(self):
        self._assert_revision_refused("candidate_forged_source_stamp")

    def test_forged_candidate_digest_stamp_is_refused(self):
        self._assert_revision_refused("candidate_forged_digest_stamp")

    def test_duplicate_candidate_source_stamp_is_refused(self):
        self._assert_revision_refused("candidate_duplicate_source_stamp")

    def test_secret_bound_candidate_source_stamp_is_refused(self):
        self._assert_revision_refused("candidate_secret_source_stamp")

    def test_baseline_carrying_a_release_stamp_is_refused(self):
        self._assert_revision_refused("baseline_source_stamp")

    def _assert_baseline_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [*self._apply_gate_calls(), self._service_describe_call()],
        )

    def _assert_post_readback_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
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
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
                self._digest_call(),
                self._deploy_call(),
                self._service_describe_call(),
                self._revision_describe_call(),
            ],
        )

    def _assert_inventory_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
            ],
        )

    def _assert_baseline_revision_refused(self, scenario: str) -> None:
        result = self._run("--apply", scenario=scenario)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self._gcloud_calls(),
            [
                *self._apply_gate_calls(),
                self._service_describe_call(),
                self._revision_list_call(),
                self._baseline_revision_describe_call(),
            ],
        )

    @staticmethod
    def _git_preflight_calls() -> list[list[str]]:
        prefix = ["-C", str(REPO_ROOT)]
        return [
            [*prefix, "status", "--porcelain"],
            [*prefix, "rev-parse", "HEAD"],
        ]

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
            *TaglessStagingContractTests._config_calls(),
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
    def _apply_gate_calls() -> list[list[str]]:
        return [
            *TaglessStagingContractTests._preflight_calls(),
            TaglessStagingContractTests._prerequisite_call(),
        ]

    @staticmethod
    def _prerequisite_call() -> list[str]:
        return ["phase1-prerequisites"]

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
        return [
            "run", "revisions", "list",
            "--service", SERVICE,
            "--account", ACCOUNT,
            "--project", PROJECT,
            "--region", REGION,
            "--format=json",
        ]

    @staticmethod
    def _digest_call() -> list[str]:
        return [
            "artifacts", "docker", "images", "describe", CANONICAL_IMAGE,
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
            "--image", CANONICAL_IMAGE,
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
    def _baseline_revision_describe_call() -> list[str]:
        return [
            "run", "revisions", "describe", OLD_REVISION,
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

            scenario = os.environ.get("FAKE_GCLOUD_SCENARIO", "ok")
            state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            candidate = os.environ["FAKE_CANDIDATE_REVISION"]
            old = os.environ["FAKE_OLD_REVISION"]
            other = os.environ["FAKE_OTHER_REVISION"]
            canonical_image = os.environ["FAKE_CANONICAL_IMAGE"]
            source_revision = os.environ["FAKE_GIT_SHA"]
            image_digest = "sha256:" + canonical_image.rsplit("sha256:", 1)[1]

            def save():
                state_path.write_text(json.dumps(state), encoding="utf-8")

            def service_document(post_deploy):
                include_auxiliary_tags = scenario not in {
                    "latest_spec",
                    "implicit_spec_target",
                    "spec_duplicate_tag",
                    "spec_invalid_tag",
                    "spec_mutation",
                }
                spec_traffic = [
                    {"revisionName": old, "percent": 100},
                    {"revisionName": old, "tag": "release-a"},
                ]
                if include_auxiliary_tags:
                    spec_traffic.extend([
                        {"revisionName": other, "tag": "rollback-door"},
                        {"revisionName": "process-user-00095-jil", "tag": "jill-one"},
                        {"revisionName": "process-user-00094-lck", "tag": "lock"},
                    ])
                status_traffic = [dict(item) for item in spec_traffic]
                for item in status_traffic:
                    if item.get("tag"):
                        item["url"] = "https://" + item["tag"] + ".invalid"
                document = {
                    "metadata": {"annotations": {"run.googleapis.com/maxScale": "20"}},
                    "spec": {"traffic": spec_traffic},
                    "status": {
                        "latestCreatedRevisionName": candidate if post_deploy else old,
                        "traffic": status_traffic,
                    },
                }
                if not post_deploy:
                    if scenario == "missing_positive":
                        status_traffic[0].pop("percent")
                    elif scenario == "multiple_positive":
                        status_traffic[0]["percent"] = 50
                        status_traffic.append({"revisionName": other, "percent": 50})
                    elif scenario == "release_a_wrong":
                        status_traffic[1]["revisionName"] = other
                    elif scenario == "release_a_duplicate":
                        status_traffic.append({"revisionName": old, "tag": "release-a"})
                    elif scenario == "candidate_collision":
                        status_traffic.append({"revisionName": candidate, "tag": "staging"})
                    elif scenario == "latest_spec":
                        spec_traffic[0] = {"latestRevision": True, "percent": 100}
                    elif scenario == "implicit_spec_target":
                        spec_traffic[0] = {"percent": 100}
                    elif scenario == "spec_duplicate_tag":
                        spec_traffic.extend([
                            {"revisionName": old, "tag": "lock"},
                            {"revisionName": other, "tag": "lock"},
                        ])
                    elif scenario == "spec_invalid_tag":
                        spec_traffic.append({"revisionName": other, "tag": "Bad_Tag"})
                    return document

                if scenario == "candidate_tagged":
                    status_traffic.append({"revisionName": candidate, "tag": "staging"})
                elif scenario == "candidate_positive":
                    status_traffic[0]["percent"] = 90
                    status_traffic.append({"revisionName": candidate, "percent": 10})
                elif scenario == "old_positive_drift":
                    status_traffic[0] = {"revisionName": other, "percent": 100}
                elif scenario == "old_tag_drift":
                    status_traffic[1]["revisionName"] = other
                elif scenario == "service_config_drift":
                    document["metadata"]["annotations"]["run.googleapis.com/maxScale"] = "21"
                elif scenario == "spec_mutation":
                    spec_traffic.append({"revisionName": other, "tag": "unexpected"})
                elif scenario == "latest_created_missing":
                    document["status"].pop("latestCreatedRevisionName")
                elif scenario == "latest_created_ambiguous":
                    document["status"]["latestCreatedRevisionName"] = other
                return document

            def revision_document(is_baseline=False):
                image = canonical_image
                concurrency = 1
                ready = "True"
                if is_baseline:
                    image = (
                        "us-central1-docker.pkg.dev/email-automation-cache/"
                        "cloud-run-source-deploy/process-user@sha256:" + "c" * 64
                    )
                elif scenario == "wrong_image":
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
                    "SITESIFT_SOURCE_COORDINATOR_MODE": "locked",
                }
                if is_baseline and scenario == "baseline_source_stamp":
                    values["SITESIFT_SOURCE_REVISION"] = source_revision
                if not is_baseline:
                    values["SITESIFT_SOURCE_REVISION"] = source_revision
                    values["SITESIFT_IMAGE_DIGEST"] = image_digest
                    if scenario == "candidate_missing_source_stamp":
                        values.pop("SITESIFT_SOURCE_REVISION")
                    elif scenario == "candidate_missing_digest_stamp":
                        values.pop("SITESIFT_IMAGE_DIGEST")
                    elif scenario == "candidate_forged_source_stamp":
                        values["SITESIFT_SOURCE_REVISION"] = "0" * 40
                    elif scenario == "candidate_forged_digest_stamp":
                        values["SITESIFT_IMAGE_DIGEST"] = "sha256:" + "f" * 64
                if is_baseline and scenario == "baseline_native_gate":
                    values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "false"
                if not is_baseline:
                    values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "false"
                    if scenario == "candidate_missing_native_gate":
                        values.pop("SITESIFT_NATIVE_IMAGE_INGESTION")
                    elif scenario == "candidate_true_native_gate":
                        values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "true"
                    elif scenario == "candidate_case_native_gate":
                        values["SITESIFT_NATIVE_IMAGE_INGESTION"] = "False"
                    elif scenario == "candidate_padded_native_gate":
                        values["SITESIFT_NATIVE_IMAGE_INGESTION"] = " false "
                if not is_baseline and scenario == "candidate_extra_env":
                    values["UNEXPECTED_EXTRA_MODE"] = "1"
                elif not is_baseline and scenario == "candidate_changed_extra_env":
                    values["SITESIFT_SOURCE_COORDINATOR_MODE"] = "changed"
                elif not is_baseline and scenario == "candidate_missing_extra_env":
                    values.pop("SITESIFT_SOURCE_COORDINATOR_MODE")
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
                if not is_baseline and scenario == "candidate_duplicate_source_stamp":
                    env.append({
                        "name": "SITESIFT_SOURCE_REVISION",
                        "value": source_revision,
                    })
                if not is_baseline and scenario == "candidate_secret_source_stamp":
                    stamp = next(
                        entry
                        for entry in env
                        if entry.get("name") == "SITESIFT_SOURCE_REVISION"
                    )
                    stamp.pop("value")
                    stamp["valueFrom"] = {
                        "secretKeyRef": {
                            "name": "SITESIFT_SOURCE_REVISION",
                            "key": "latest",
                        }
                    }
                if not is_baseline and scenario == "candidate_duplicate_native_gate":
                    env.append({
                        "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                        "value": "false",
                    })
                if not is_baseline and scenario in {
                    "candidate_secret_native_gate",
                    "candidate_extra_key_native_gate",
                }:
                    gate = next(
                        entry
                        for entry in env
                        if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
                    )
                    if scenario == "candidate_secret_native_gate":
                        gate.pop("value")
                        gate["valueFrom"] = {
                            "secretKeyRef": {
                                "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                                "key": "latest",
                            }
                        }
                    else:
                        gate["unexpected"] = "rejected"
                document = {
                    "metadata": {
                        "name": old if is_baseline else candidate,
                        "annotations": {
                            "autoscaling.knative.dev/minScale": "0",
                            "autoscaling.knative.dev/maxScale": "10",
                            "run.googleapis.com/operation-id": (
                                "baseline-operation" if is_baseline else "candidate-operation"
                            ),
                            "run.googleapis.com/startup-cpu-boost": "true",
                        },
                        "labels": {
                            "cloud.googleapis.com/location": "us-central1",
                            "serving.knative.dev/configurationGeneration": (
                                "97" if is_baseline else "98"
                            ),
                            **(
                                {"serving.knative.dev/route": "process-user"}
                                if is_baseline else {}
                            ),
                            "serving.knative.dev/service": "process-user",
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
                if not is_baseline and scenario == "candidate_other_config_drift":
                    document["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "2"
                elif not is_baseline and scenario == "candidate_annotation_drift":
                    document["metadata"]["annotations"][
                        "run.googleapis.com/startup-cpu-boost"
                    ] = "false"
                return document

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
            if args[:3] == ["run", "revisions", "list"]:
                if scenario == "inventory_read_failure":
                    raise SystemExit(1)
                if scenario == "inventory_malformed":
                    print("{not-json")
                    raise SystemExit(0)
                revisions = [
                    {"metadata": {"name": old}},
                    {"metadata": {"name": other}},
                    {"metadata": {"name": "process-user-00095-jil"}},
                    {"metadata": {"name": "process-user-00094-lck"}},
                ]
                if scenario == "inventory_candidate_collision":
                    revisions.append({"metadata": {"name": candidate}})
                elif scenario == "inventory_duplicate":
                    revisions.append({"metadata": {"name": old}})
                print(json.dumps(revisions))
                raise SystemExit(0)
            if args[:2] == ["builds", "submit"]:
                raise SystemExit(0)
            if args[:3] == ["artifacts", "docker", "images"]:
                if scenario == "empty_digest":
                    raise SystemExit(0)
                if scenario == "invalid_digest":
                    print("latest")
                    raise SystemExit(0)
                if scenario == "digest_mismatch":
                    print("sha256:" + "b" * 64)
                    raise SystemExit(0)
                if scenario == "registry_read_failure":
                    raise SystemExit(1)
                print("sha256:" + "a" * 64)
                raise SystemExit(0)
            if args[:2] == ["run", "deploy"]:
                if scenario == "deploy_failure":
                    raise SystemExit(1)
                raise SystemExit(0)
            if args[:3] == ["run", "revisions", "describe"]:
                revision_name = args[3]
                if revision_name == old and scenario == "baseline_revision_read_failure":
                    raise SystemExit(1)
                if revision_name == old and scenario == "baseline_revision_malformed":
                    print("{not-json")
                    raise SystemExit(0)
                if revision_name == candidate and scenario == "revision_read_failure":
                    raise SystemExit(1)
                print(json.dumps(revision_document(is_baseline=revision_name == old)))
                raise SystemExit(0)
            print("unexpected fake gcloud command: " + " ".join(args), file=sys.stderr)
            raise SystemExit(65)
            """
        )


if __name__ == "__main__":
    unittest.main()
