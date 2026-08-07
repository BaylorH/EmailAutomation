"""Fail-closed contract for immutable production release provenance."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "docs" / "release-safety" / "production-release-manifest.json"
VALIDATOR_PATH = REPO_ROOT / "scripts" / "verify_release_manifest.py"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-clearance-ci.yml"

BACKEND_PRODUCTION_SHA = "92de8ab5bf841f5faa453de34cc3846bd65611af"
BACKEND_CANDIDATE_SHA = "c3f452b31a34797b629c27e0397c77bf9beecdea"
BACKEND_RECEIPT_SHA = "fb9eaae8e0398135355c636850553f5d54cc7754"
BACKEND_DARK_SHA = "ed64554b5e65fbbdd471364bc76b59209e4801af"
FRONTEND_PRODUCTION_SHA = "52aa66299751893bbf9ec596d7fa84c5a767933d"
FRONTEND_OBSERVED_CANDIDATE_SHA = "45b182d244753769620ab78b4b9dd542a9cb055b"
PRODUCTION_DIGEST = "sha256:8c943805c87783b94361ca0e9fa7eee6fb2aac00e3211f3a2389bcc08c71daa9"
PRODUCTION_REVISION = "process-user-jill-one-202608020520"
DARK_DIGEST = "sha256:ab67d92dbf5983ffe862e87d982d8a17c7bb4ee3486cbd3940135c446cffd608"
DARK_REVISION = "process-user-00058-biz"
WORKFLOW_DATABASE_ID = 327317922


def _read_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class ReleaseManifestTests(unittest.TestCase):
    def _validate(
        self,
        manifest: dict,
        *validator_args: str,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), str(path), *validator_args],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def _assert_rejected(self, manifest: dict) -> None:
        result = self._validate(manifest)
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout)
        self.assertIn("release manifest invalid:", result.stderr)

    def test_manifest_captures_observed_immutable_production_truth(self):
        manifest = _read_manifest()

        self.assertEqual(1, manifest["schemaVersion"])
        self.assertEqual("BLOCKED_PROVENANCE", manifest["workflowState"])
        self.assertEqual("email-automation-cache", manifest["backend"]["projectId"])
        self.assertEqual("us-central1", manifest["backend"]["region"])
        self.assertEqual("process-user", manifest["backend"]["service"])
        self.assertEqual(BACKEND_PRODUCTION_SHA, manifest["backend"]["productionSha"])
        self.assertEqual(BACKEND_CANDIDATE_SHA, manifest["backend"]["candidateSha"])
        self.assertEqual(BACKEND_RECEIPT_SHA, manifest["backend"]["receiptSha"])
        self.assertEqual(PRODUCTION_DIGEST, manifest["backend"]["artifactDigest"])
        self.assertEqual(PRODUCTION_REVISION, manifest["backend"]["deployedRevision"])
        self.assertEqual(100, manifest["backend"]["trafficPercent"])
        self.assertEqual(PRODUCTION_REVISION, manifest["backend"]["rollbackRevision"])
        self.assertEqual(
            {
                "sourceSha": BACKEND_DARK_SHA,
                "artifactDigest": DARK_DIGEST,
                "artifactTag": BACKEND_DARK_SHA[:12],
                "revision": DARK_REVISION,
                "trafficPercent": 0,
                "outboundMode": "paused",
                "coordinatorMode": "disabled",
            },
            manifest["backend"]["observedDarkDeployment"],
        )
        self.assertEqual(
            FRONTEND_PRODUCTION_SHA,
            manifest["frontend"]["productionSha"],
        )
        self.assertEqual(
            FRONTEND_OBSERVED_CANDIDATE_SHA,
            manifest["frontend"]["observedCandidateSha"],
        )
        self.assertEqual("api-00110-cib", manifest["frontend"]["functionRevision"])
        self.assertEqual(
            "BLOCKED_PROVENANCE",
            manifest["frontend"]["functionCommitMapping"],
        )
        self.assertEqual(
            "BLOCKED_PROVENANCE",
            manifest["frontend"]["hostingReleaseId"],
        )
        self.assertEqual(
            "BLOCKED_PROVENANCE",
            manifest["frontend"]["hostingRollbackReleaseId"],
        )
        self.assertEqual(
            "BLOCKED_PROVENANCE",
            manifest["frontend"]["hostingCommitMapping"],
        )
        self.assertRegex(
            manifest["backend"]["deploymentConfigHash"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(
            "sha256:canonical-json(spec,image=IMAGE_DIGEST_BOUND_AT_DEPLOY)",
            manifest["backend"]["deploymentConfigHashAlgorithm"],
        )

    def test_validator_accepts_the_canonical_manifest(self):
        result = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(MANIFEST_PATH)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("release manifest valid:", result.stdout)
        self.assertEqual("", result.stderr)

    def test_validator_rejects_branch_names_where_exact_shas_are_required(self):
        manifest = _read_manifest()
        mutations = (
            ("backend", "productionSha", "main"),
            ("backend", "candidateSha", "codex/release-rails"),
            ("backend", "receiptSha", "HEAD"),
            ("backendDark", "sourceSha", "codex/dark"),
            ("frontend", "productionSha", "main"),
            ("frontend", "observedCandidateSha", "release/frontend"),
        )
        for section, field, value in mutations:
            with self.subTest(field=f"{section}.{field}"):
                mutated = copy.deepcopy(manifest)
                target = (
                    mutated["backend"]["observedDarkDeployment"]
                    if section == "backendDark"
                    else mutated[section]
                )
                target[field] = value
                self._assert_rejected(mutated)

    def test_validator_rejects_candidate_without_matching_successful_exact_sha_ci(self):
        manifest = _read_manifest()
        mutations = (
            ("headSha", "0" * 40),
            ("headSha", "main"),
            ("status", "queued"),
            ("conclusion", "failure"),
            ("runId", 0),
            ("runId", True),
            ("url", "https://example.com/not-a-github-run"),
            (
                "url",
                "https://github.com/another-owner/another-repo/actions/runs/31131399984",
            ),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                mutated = copy.deepcopy(manifest)
                mutated["backend"]["candidateCi"][field] = value
                self._assert_rejected(mutated)

    def test_validator_rejects_receipt_without_matching_successful_exact_sha_ci(self):
        manifest = _read_manifest()
        for field, value in (
            ("headSha", BACKEND_CANDIDATE_SHA),
            ("status", "in_progress"),
            ("conclusion", None),
        ):
            with self.subTest(field=field):
                mutated = copy.deepcopy(manifest)
                mutated["backend"]["receiptCi"][field] = value
                self._assert_rejected(mutated)

    def test_validator_rejects_invalid_production_readback_fields(self):
        manifest = _read_manifest()
        mutations = (
            ("artifactDigest", "latest"),
            ("artifactTag", "production"),
            ("artifactTag", "0" * 12),
            ("deployedRevision", ""),
            ("configHash", "sha256:unknown"),
            ("configHashAlgorithm", "unspecified"),
            ("trafficPercent", -1),
            ("trafficPercent", 101),
            ("trafficPercent", True),
            ("rollbackRevision", ""),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                mutated = copy.deepcopy(manifest)
                mutated["backend"][field] = value
                self._assert_rejected(mutated)

    def test_validator_requires_exactly_100_percent_production_traffic(self):
        manifest = _read_manifest()
        for traffic in (0, 1, 50, 99):
            with self.subTest(traffic=traffic):
                mutated = copy.deepcopy(manifest)
                mutated["backend"]["trafficPercent"] = traffic
                self._assert_rejected(mutated)

    def test_validator_rejects_invalid_dark_deployment_readback(self):
        manifest = _read_manifest()
        mutations = (
            ("artifactDigest", "latest"),
            ("artifactTag", "0" * 12),
            ("revision", ""),
            ("trafficPercent", 1),
            ("trafficPercent", False),
            ("outboundMode", "live"),
            ("coordinatorMode", "enabled"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                mutated = copy.deepcopy(manifest)
                mutated["backend"]["observedDarkDeployment"][field] = value
                self._assert_rejected(mutated)

    def test_validator_rejects_unknown_or_missing_schema_fields(self):
        manifest = _read_manifest()
        mutations = []

        missing_root = copy.deepcopy(manifest)
        missing_root.pop("observedAt")
        mutations.append(missing_root)

        extra_root = copy.deepcopy(manifest)
        extra_root["branch"] = "main"
        mutations.append(extra_root)

        missing_backend = copy.deepcopy(manifest)
        missing_backend["backend"].pop("rollbackRevision")
        mutations.append(missing_backend)

        extra_ci = copy.deepcopy(manifest)
        extra_ci["backend"]["candidateCi"]["ref"] = "main"
        mutations.append(extra_ci)

        extra_frontend = copy.deepcopy(manifest)
        extra_frontend["frontend"]["guessedFunctionSha"] = "0" * 40
        mutations.append(extra_frontend)

        for index, mutated in enumerate(mutations):
            with self.subTest(case=index):
                self._assert_rejected(mutated)

    def test_validator_rejects_invalid_timestamp_state_and_provenance_sentinel(self):
        manifest = _read_manifest()
        mutations = []

        invalid_timestamp = copy.deepcopy(manifest)
        invalid_timestamp["observedAt"] = "2026-08-06"
        mutations.append(invalid_timestamp)

        invalid_state = copy.deepcopy(manifest)
        invalid_state["workflowState"] = "READY"
        mutations.append(invalid_state)

        false_clearance = copy.deepcopy(manifest)
        false_clearance["workflowState"] = "CI_VERIFIED"
        mutations.append(false_clearance)

        branch_mapping = copy.deepcopy(manifest)
        branch_mapping["frontend"]["functionCommitMapping"] = "main"
        mutations.append(branch_mapping)

        guessed_function_mapping = copy.deepcopy(manifest)
        guessed_function_mapping["frontend"]["functionCommitMapping"] = "0" * 40
        mutations.append(guessed_function_mapping)

        guessed_hosting_release = copy.deepcopy(manifest)
        guessed_hosting_release["frontend"]["hostingReleaseId"] = "release-123"
        mutations.append(guessed_hosting_release)

        guessed_hosting_rollback = copy.deepcopy(manifest)
        guessed_hosting_rollback["frontend"]["hostingRollbackReleaseId"] = (
            "release-122"
        )
        mutations.append(guessed_hosting_rollback)

        guessed_hosting_mapping = copy.deepcopy(manifest)
        guessed_hosting_mapping["frontend"]["hostingCommitMapping"] = "0" * 40
        mutations.append(guessed_hosting_mapping)

        for index, mutated in enumerate(mutations):
            with self.subTest(case=index):
                self._assert_rejected(mutated)

    def test_validator_rejects_duplicate_keys_and_nonstandard_json_constants(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cases = {
                "duplicate.json": '{"schemaVersion":1,"schemaVersion":1}\n',
                "nan.json": '{"schemaVersion":NaN}\n',
                "infinity.json": '{"schemaVersion":Infinity}\n',
            }
            for filename, body in cases.items():
                with self.subTest(filename=filename):
                    path = temp_root / filename
                    path.write_text(body, encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, str(VALIDATOR_PATH), str(path)],
                        cwd=REPO_ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual("", result.stdout)

    def test_remote_attestation_verifies_real_github_run_readback(self):
        manifest = _read_manifest()
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$*" in
                      *"{manifest['backend']['candidateCi']['runId']}"*)
                        printf '%s\\n' '{json.dumps({**manifest['backend']['candidateCi'], 'databaseId': manifest['backend']['candidateCi']['runId'], 'workflowName': 'Production Clearance CI', 'workflowDatabaseId': WORKFLOW_DATABASE_ID, 'event': 'push'}, separators=(',', ':'))}'
                        ;;
                      *"{manifest['backend']['receiptCi']['runId']}"*)
                        printf '%s\\n' '{json.dumps({**manifest['backend']['receiptCi'], 'databaseId': manifest['backend']['receiptCi']['runId'], 'workflowName': 'Production Clearance CI', 'workflowDatabaseId': WORKFLOW_DATABASE_ID, 'event': 'push'}, separators=(',', ':'))}'
                        ;;
                      *) exit 44 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            result = self._validate(
                manifest,
                "--verify-github",
                env=env,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("GitHub attestations verified", result.stdout)

    def test_remote_attestation_rejects_a_coherent_but_fabricated_ci_tuple(self):
        manifest = _read_manifest()
        fabricated = copy.deepcopy(manifest)
        fabricated_run_id = 99999999999
        fabricated_sha = "1" * 40
        fabricated["backend"]["candidateSha"] = fabricated_sha
        fabricated["backend"]["candidateCi"] = {
            "runId": fabricated_run_id,
            "url": (
                "https://github.com/BaylorH/EmailAutomation/actions/runs/"
                f"{fabricated_run_id}"
            ),
            "headSha": fabricated_sha,
            "status": "completed",
            "conclusion": "success",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    printf '%s\\n' '{json.dumps({**manifest['backend']['candidateCi'], 'databaseId': manifest['backend']['candidateCi']['runId'], 'workflowName': 'Production Clearance CI', 'workflowDatabaseId': WORKFLOW_DATABASE_ID, 'event': 'push'}, separators=(',', ':'))}'
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            result = self._validate(
                fabricated,
                "--verify-github",
                env=env,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("GitHub CI attestation mismatch", result.stderr)

    def test_remote_attestation_rejects_manual_dispatch_with_matching_head_sha(self):
        manifest = _read_manifest()
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir) / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            candidate = {
                **manifest["backend"]["candidateCi"],
                "databaseId": manifest["backend"]["candidateCi"]["runId"],
                "workflowName": "Production Clearance CI",
                "workflowDatabaseId": WORKFLOW_DATABASE_ID,
                "event": "workflow_dispatch",
            }
            receipt = {
                **manifest["backend"]["receiptCi"],
                "databaseId": manifest["backend"]["receiptCi"]["runId"],
                "workflowName": "Production Clearance CI",
                "workflowDatabaseId": WORKFLOW_DATABASE_ID,
                "event": "push",
            }
            fake_gh.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    case "$*" in
                      *"{candidate['databaseId']}"*) printf '%s\\n' '{json.dumps(candidate, separators=(',', ':'))}' ;;
                      *"{receipt['databaseId']}"*) printf '%s\\n' '{json.dumps(receipt, separators=(',', ':'))}' ;;
                      *) exit 44 ;;
                    esac
                    """
                ),
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            result = self._validate(manifest, "--verify-github", env=env)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("GitHub CI attestation mismatch", result.stderr)

    def test_ci_workflow_checks_out_and_verifies_an_exact_candidate_sha(self):
        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("candidate_sha:", workflow)
        self.assertIn("required: true", workflow)
        self.assertIn("CANDIDATE_SHA:", workflow)
        self.assertIn("github.event.pull_request.head.sha", workflow)
        self.assertIn("ref: ${{ env.CANDIDATE_SHA }}", workflow)
        self.assertIn('git rev-parse HEAD', workflow)
        self.assertIn('^[0-9a-f]{40}$', workflow)
        self.assertIn("scripts/verify_release_manifest.py", workflow)
        self.assertIn("tests.test_release_manifest", workflow)
        self.assertIn("tests.test_process_user_production_deploy_contract", workflow)
        self.assertIn('--expected-candidate-sha "$CANDIDATE_SHA"', workflow)


if __name__ == "__main__":
    unittest.main()
