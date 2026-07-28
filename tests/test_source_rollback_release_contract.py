"""Credential-free contract tests for the worker source rollback artifact."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "release" / "build-worker.sh"
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "release" / "verify-worker.sh"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "release" / "restore-worker.sh"
SCHEMA_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "release-manifest.schema.json"
)
DESCRIPTOR_PATH = (
    REPO_ROOT / "docs" / "release-safety" / "source-rollback-manifest.json"
)
RUNBOOK_PATH = (
    REPO_ROOT
    / "docs"
    / "release-safety"
    / "SAFETY-RAILS-AND-ROLLBACK-RUNBOOK.md"
)


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    subprocess_env = os.environ.copy() if env is None else env.copy()
    subprocess_env["PYTHON_BIN"] = sys.executable
    interpreter_bin = str(Path(sys.executable).resolve().parent)
    subprocess_env["PATH"] = (
        interpreter_bin
        + os.pathsep
        + subprocess_env.get("PATH", "")
    )
    return subprocess.run(
        command,
        cwd=cwd,
        env=subprocess_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class WorkerSourceRollbackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "worker"
        self.output = self.root / "artifact"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "release-test@example.com")
        self._git("config", "user.name", "Release Test")
        _write(
            self.repo / "Dockerfile",
            "FROM python:3.12-slim@sha256:"
            + ("a" * 64)
            + "\nCOPY . .\nENTRYPOINT [\"python\", \"main.py\"]\n",
        )
        _write(self.repo / ".dockerignore", ".git\ntests/\n")
        _write(
            self.repo / "requirements.lock",
            "example==1.0 --hash=sha256:" + ("b" * 64) + "\n",
        )
        _write(self.repo / "main.py", "print('worker')\n")
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = _run(["git", *args], cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _build(self) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "bash",
                str(BUILD_SCRIPT),
                "--repo",
                str(self.repo),
                "--output",
                str(self.output),
            ]
        )

    def _replace_archive_with_member(
        self,
        manifest_path: Path,
        *,
        name: str,
        member_type: bytes,
        linkname: str = "",
    ) -> None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive_path = manifest_path.parent / manifest["source"]["archive"]
        tar_bytes = io.BytesIO()
        with tarfile.open(
            fileobj=tar_bytes,
            mode="w",
            format=tarfile.USTAR_FORMAT,
        ) as archive:
            member = tarfile.TarInfo(name)
            member.type = member_type
            member.linkname = linkname
            member.mode = 0o644
            member.size = 0
            if member_type in (tarfile.CHRTYPE, tarfile.BLKTYPE):
                member.devmajor = 1
                member.devminor = 3
            archive.addfile(member)
        archive_path.write_bytes(tar_bytes.getvalue())

        archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        manifest["source"]["archiveSha256"] = archive_sha
        identity_input = {
            "commit": manifest["repository"]["commit"],
            "sourceArchiveSha256": archive_sha,
            "dockerfileSha256": manifest["build"]["dockerfileSha256"],
            "lockfileSha256": manifest["build"]["lockfileSha256"],
            "baseImage": manifest["build"]["baseImage"],
            "gitPathspecs": manifest["source"]["gitPathspecs"],
        }
        source_build_identity = hashlib.sha256(
            json.dumps(
                identity_input,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest["build"]["sourceBuildIdentity"] = source_build_identity
        manifest["build"]["command"][5] = (
            f"SOURCE_BUILD_IDENTITY={source_build_identity}"
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_build_records_clean_commit_source_and_build_identity(self):
        result = self._build()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(
            (self.output / "worker-release-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["schemaVersion"], 1)
        self.assertEqual(manifest["artifactKind"], "sitesift-worker-source-rollback")
        self.assertEqual(
            manifest["repository"],
            {"commit": self.commit, "clean": True},
        )
        for key in (
            "archiveSha256",
            "sourceBuildIdentity",
            "dockerfileSha256",
            "lockfileSha256",
        ):
            section = (
                manifest["source"]
                if key == "archiveSha256"
                else manifest["build"]
            )
            self.assertRegex(section[key], r"^[0-9a-f]{64}$")
        self.assertRegex(
            manifest["build"]["baseImage"],
            r"^python:3\.12-slim@sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(
            manifest["build"]["command"],
            [
                "docker",
                "build",
                "--build-arg",
                f"SOURCE_COMMIT={self.commit}",
                "--build-arg",
                "SOURCE_BUILD_IDENTITY="
                + manifest["build"]["sourceBuildIdentity"],
                ".",
            ],
        )
        self.assertEqual(
            set(manifest["tools"]),
            {"python", "git", "tar"},
        )
        self.assertEqual(manifest["tools"]["python"], "Python 3.12.13")
        self.assertIn("git", manifest["tools"])
        self.assertIn("tar", manifest["tools"])
        self.assertIn("verify", manifest["restoreCommands"])
        self.assertIn("dryRun", manifest["restoreCommands"])

    def test_build_refuses_dirty_source_before_artifact_creation(self):
        _write(self.repo / "secret-untracked.txt", "secret\n")
        result = self._build()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr.lower(), r"dirty|clean|refus")
        self.assertFalse(
            (self.output / "worker-release-manifest.json").exists()
        )

    def test_build_excludes_tracked_virtualenv_from_restorable_source(self):
        venv_bin = self.repo / "auth_service" / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to("/private/build-host/python")
        _write(
            self.repo / "auth_service" / "venv" / "installed-package.py",
            "host specific\n",
        )
        self._git("add", ".")
        self._git("commit", "-qm", "add tracked build-host virtualenv")

        built = self._build()
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest_path = self.output / "worker-release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["source"]["gitPathspecs"],
            [
                ".",
                ":(exclude,glob)**/venv/**",
                ":(exclude,glob)**/.venv/**",
            ],
        )
        with tarfile.open(self.output / "worker-source.tar", "r:") as archive:
            self.assertFalse(
                any(
                    "/venv/" in f"/{member.name.rstrip('/')}/"
                    or "/.venv/" in f"/{member.name.rstrip('/')}/"
                    for member in archive.getmembers()
                )
            )
        verified = _run(
            ["bash", str(VERIFY_SCRIPT), "--manifest", str(manifest_path)]
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_offline_verify_rejects_tampered_source_archive(self):
        built = self._build()
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest = self.output / "worker-release-manifest.json"
        verified = _run(
            ["bash", str(VERIFY_SCRIPT), "--manifest", str(manifest)]
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("verified", verified.stdout.lower())

        with (self.output / "worker-source.tar").open("ab") as stream:
            stream.write(b"tampered")
        tampered = _run(
            ["bash", str(VERIFY_SCRIPT), "--manifest", str(manifest)]
        )
        self.assertNotEqual(tampered.returncode, 0)
        self.assertRegex(tampered.stderr.lower(), r"mismatch|refus")

    def test_verify_rejects_noncanonical_restore_metadata(self):
        built = self._build()
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest_path = self.output / "worker-release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["restoreCommands"]["apply"] = "gcloud run deploy worker"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        verified = _run(
            ["bash", str(VERIFY_SCRIPT), "--manifest", str(manifest_path)]
        )
        self.assertNotEqual(verified.returncode, 0)
        self.assertRegex(verified.stderr.lower(), r"restore|canonical|refus")

    def test_verify_rejects_absolute_and_traversal_paths_before_access(self):
        for unsafe_path in ("/private/secret.tar", "../../private/secret.tar"):
            with self.subTest(path=unsafe_path):
                output = self.root / ("artifact-" + str(len(unsafe_path)))
                result = _run(
                    [
                        "bash",
                        str(BUILD_SCRIPT),
                        "--repo",
                        str(self.repo),
                        "--output",
                        str(output),
                    ]
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest_path = output / "worker-release-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["source"]["archive"] = unsafe_path
                manifest_path.write_text(
                    json.dumps(manifest, indent=2) + "\n",
                    encoding="utf-8",
                )
                verified = _run(
                    [
                        "bash",
                        str(VERIFY_SCRIPT),
                        "--manifest",
                        str(manifest_path),
                    ]
                )
                self.assertNotEqual(verified.returncode, 0)
                self.assertRegex(
                    verified.stderr.lower(),
                    r"unsafe|relative|path|refus",
                )
                self.assertNotRegex(
                    verified.stderr,
                    r"ENOENT.*private/secret",
                )

    def test_verify_rejects_every_non_regular_archive_member(self):
        cases = (
            ("absolute path", "/private/escape", tarfile.REGTYPE, ""),
            ("traversal path", "../../escape", tarfile.REGTYPE, ""),
            ("symbolic link", "safe-link", tarfile.SYMTYPE, "/private/secret"),
            ("hard link", "safe-hardlink", tarfile.LNKTYPE, "target"),
            ("fifo", "safe-fifo", tarfile.FIFOTYPE, ""),
            ("character device", "safe-device", tarfile.CHRTYPE, ""),
        )
        for index, (label, name, member_type, linkname) in enumerate(cases):
            with self.subTest(member=label):
                output = self.root / f"malicious-artifact-{index}"
                built = _run(
                    [
                        "bash",
                        str(BUILD_SCRIPT),
                        "--repo",
                        str(self.repo),
                        "--output",
                        str(output),
                    ]
                )
                self.assertEqual(built.returncode, 0, built.stderr)
                manifest_path = output / "worker-release-manifest.json"
                self._replace_archive_with_member(
                    manifest_path,
                    name=name,
                    member_type=member_type,
                    linkname=linkname,
                )

                verified = _run(
                    [
                        "bash",
                        str(VERIFY_SCRIPT),
                        "--manifest",
                        str(manifest_path),
                    ]
                )
                self.assertNotEqual(
                    verified.returncode,
                    0,
                    f"{label} unexpectedly verified:\n{verified.stdout}",
                )
                self.assertRegex(
                    verified.stderr.lower(),
                    r"archive|member|unsafe|link|restorable|refus",
                )

    def test_restore_defaults_dry_run_and_apply_never_calls_provider_tools(self):
        built = self._build()
        self.assertEqual(built.returncode, 0, built.stderr)
        manifest = self.output / "worker-release-manifest.json"
        target = self.root / "restored"
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        sentinel = self.root / "provider-called"
        for command in ("gcloud", "docker", "curl", "firebase"):
            path = fake_bin / command
            _write(
                path,
                f"#!/bin/sh\nprintf called > '{sentinel}'\nexit 99\n",
            )
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

        dry_run = _run(
            [
                "bash",
                str(RESTORE_SCRIPT),
                "--manifest",
                str(manifest),
                "--target",
                str(target),
            ],
            env=env,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("dry-run", dry_run.stdout.lower())
        self.assertFalse(target.exists())

        applied = _run(
            [
                "bash",
                str(RESTORE_SCRIPT),
                "--apply",
                "--manifest",
                str(manifest),
                "--target",
                str(target),
            ],
            env=env,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(
            (target / "main.py").read_text(encoding="utf-8"),
            "print('worker')\n",
        )
        self.assertFalse(sentinel.exists())

    def test_schema_descriptor_dockerfile_and_runbook_pin_source_restore(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schemaVersion"]["const"], 1)
        self.assertEqual(
            schema["properties"]["artifactKind"]["const"],
            "sitesift-worker-source-rollback",
        )
        descriptor = json.loads(
            DESCRIPTOR_PATH.read_text(encoding="utf-8")
        )
        self.assertTrue(descriptor["cleanSourceRequired"])
        self.assertEqual(descriptor["defaultRestoreMode"], "dry-run")
        self.assertTrue(descriptor["offlineVerificationRequired"])
        self.assertFalse(descriptor["providerImageRequired"])
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG SOURCE_COMMIT", dockerfile)
        self.assertIn("ARG SOURCE_BUILD_IDENTITY", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8").lower()
        for phrase in (
            "build-worker.sh",
            "verify-worker.sh",
            "restore-worker.sh",
            "dry-run",
            "offline",
            "source archive",
            "docker daemon",
            "no provider",
        ):
            self.assertIn(phrase, runbook)


if __name__ == "__main__":
    unittest.main()
