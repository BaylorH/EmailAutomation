#!/usr/bin/env bash
set -euo pipefail

manifest=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      [[ $# -ge 2 ]] || {
        printf 'Refusing worker artifact: --manifest needs a path.\n' >&2
        exit 64
      }
      manifest="$2"
      shift 2
      ;;
    *)
      printf 'Refusing worker artifact: unknown argument %s.\n' "$1" >&2
      exit 64
      ;;
  esac
done
if [[ -z "$manifest" ]]; then
  printf 'Usage: %s --manifest PATH\n' "$0" >&2
  exit 64
fi

python_bin="${PYTHON_BIN:-python3}"
python_version="$("$python_bin" --version 2>&1)"
if [[ "$python_version" != "Python 3.12.13" ]]; then
  printf 'Refusing worker artifact: Python 3.12.13 is required; found %s.\n' "$python_version" >&2
  exit 65
fi
"$python_bin" - "$manifest" <<'PY'
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile


def refuse(message: str) -> None:
    print(f"Refusing worker artifact: {message}", file=sys.stderr)
    raise SystemExit(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        refuse(f"unsafe relative path for {label}")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or "\\" in value
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        refuse(f"unsafe relative path for {label}")
    return value


manifest_path = Path(sys.argv[1]).resolve()
try:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    refuse("manifest is missing or invalid JSON")

if (
    data.get("schemaVersion") != 1
    or data.get("artifactKind") != "sitesift-worker-source-rollback"
):
    refuse("unsupported manifest identity")
repository = data.get("repository")
if (
    not isinstance(repository, dict)
    or repository.get("clean") is not True
    or not re.fullmatch(r"[0-9a-f]{40}", str(repository.get("commit", "")))
):
    refuse("clean source commit identity is invalid")

source = data.get("source")
expected_git_pathspecs = [
    ".",
    ":(exclude,glob)**/venv/**",
    ":(exclude,glob)**/.venv/**",
]
archive_name = safe_relative(
    source.get("archive") if isinstance(source, dict) else None,
    "source archive",
)
if archive_name != "worker-source.tar":
    refuse("source archive must use the canonical relative name")
if source.get("gitPathspecs") != expected_git_pathspecs:
    refuse("source archive Git pathspec identity is invalid")
archive_hash = source.get("archiveSha256")
if not isinstance(archive_hash, str) or not re.fullmatch(
    r"[0-9a-f]{64}", archive_hash
):
    refuse("source archive hash is invalid")
archive_path = manifest_path.parent / archive_name
if not archive_path.is_file():
    refuse("source archive is missing")
if sha256(archive_path) != archive_hash:
    refuse("source archive hash mismatch")

build = data.get("build")
required_hashes = ("sourceBuildIdentity", "dockerfileSha256", "lockfileSha256")
if not isinstance(build, dict) or any(
    not re.fullmatch(r"[0-9a-f]{64}", str(build.get(name, "")))
    for name in required_hashes
):
    refuse("worker build hashes are invalid")
base_image = build.get("baseImage")
if not isinstance(base_image, str) or not re.search(
    r"@sha256:[0-9a-f]{64}$", base_image
):
    refuse("worker base image identity is invalid")
identity_input = {
    "commit": repository["commit"],
    "sourceArchiveSha256": archive_hash,
    "dockerfileSha256": build["dockerfileSha256"],
    "lockfileSha256": build["lockfileSha256"],
    "baseImage": base_image,
    "gitPathspecs": expected_git_pathspecs,
}
expected_identity = hashlib.sha256(
    json.dumps(
        identity_input,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
if build["sourceBuildIdentity"] != expected_identity:
    refuse("worker source build identity mismatch")
expected_command = [
    "docker",
    "build",
    "--build-arg",
    f"SOURCE_COMMIT={repository['commit']}",
    "--build-arg",
    f"SOURCE_BUILD_IDENTITY={expected_identity}",
    ".",
]
if build.get("command") != expected_command:
    refuse("worker build command mismatch")

tools = data.get("tools")
if not isinstance(tools, dict) or set(tools) != {"python", "git", "tar"}:
    refuse("tool identity fields are invalid")
for name in ("python", "git", "tar"):
    if not isinstance(tools.get(name), str):
        refuse(f"tool identity missing: {name}")
if tools["python"] != "Python 3.12.13":
    refuse("Python tool identity mismatch")
commands = data.get("restoreCommands")
expected_commands = {
    "verify": (
        "bash scripts/release/verify-worker.sh "
        "--manifest worker-release-manifest.json"
    ),
    "dryRun": (
        "bash scripts/release/restore-worker.sh "
        "--manifest worker-release-manifest.json "
        "--target <empty-dir>"
    ),
    "apply": (
        "bash scripts/release/restore-worker.sh --apply "
        "--manifest worker-release-manifest.json "
        "--target <empty-dir>"
    ),
}
if commands != expected_commands:
    refuse("canonical restore commands are invalid")

try:
    with tarfile.open(archive_path, "r:") as archive:
        for member in archive.getmembers():
            safe_relative(member.name.rstrip("/"), "archive member")
            if member.issym() or member.islnk():
                refuse("source archive links are not restorable")
            if not (member.isfile() or member.isdir()):
                refuse(
                    "source archive special members are not restorable"
                )
except (tarfile.TarError, OSError):
    refuse("source archive is not a readable tar file")

print(f"Offline worker artifact verified: {manifest_path}")
PY
