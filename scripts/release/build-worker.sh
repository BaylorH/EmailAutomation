#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
default_repo="$(cd "$script_dir/../.." && pwd -P)"
repo="$default_repo"
output=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || {
        printf 'Refusing worker artifact build: --repo needs a path.\n' >&2
        exit 64
      }
      repo="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || {
        printf 'Refusing worker artifact build: --output needs a path.\n' >&2
        exit 64
      }
      output="$2"
      shift 2
      ;;
    *)
      printf 'Refusing worker artifact build: unknown argument %s.\n' "$1" >&2
      exit 64
      ;;
  esac
done

if [[ -z "$output" ]]; then
  printf 'Usage: %s [--repo PATH] --output NEW_DIRECTORY\n' "$0" >&2
  exit 64
fi

repo="$(cd "$repo" && pwd -P)"
if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=all)" ]]; then
  printf 'Refusing worker artifact build: source repository must be clean.\n' >&2
  exit 65
fi
source_commit="$(git -C "$repo" rev-parse HEAD)"
if [[ ! "$source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Refusing worker artifact build: HEAD is not a full commit SHA.\n' >&2
  exit 66
fi
python_bin="${PYTHON_BIN:-python3}"
python_version="$("$python_bin" --version 2>&1)"
if [[ "$python_version" != "Python 3.12.13" ]]; then
  printf 'Refusing worker artifact build: Python 3.12.13 is required; found %s.\n' "$python_version" >&2
  exit 66
fi
if ! mkdir "$output"; then
  printf 'Refusing worker artifact build: output must not exist: %s\n' "$output" >&2
  exit 67
fi
output="$(cd "$output" && pwd -P)"

source_archive="$output/worker-source.tar"
source_git_pathspecs=(
  "."
  ":(exclude,glob)**/venv/**"
  ":(exclude,glob)**/.venv/**"
)
git -C "$repo" archive \
  --format=tar \
  "--output=$source_archive" \
  HEAD \
  -- \
  "${source_git_pathspecs[@]}"
source_git_pathspecs_json="$(
  "$python_bin" - "${source_git_pathspecs[@]}" <<'PY'
import json
import sys

print(json.dumps(sys.argv[1:], separators=(",", ":")))
PY
)"

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

source_archive_sha256="$(sha256_file "$source_archive")"
dockerfile_sha256="$(sha256_file "$repo/Dockerfile")"
lockfile_sha256="$(sha256_file "$repo/requirements.lock")"
base_image="$(
  awk '/^FROM[[:space:]]+/ { print $2; exit }' "$repo/Dockerfile"
)"
if [[ ! "$base_image" =~ @sha256:[0-9a-f]{64}$ ]]; then
  printf 'Refusing worker artifact build: Dockerfile base image is not digest-pinned.\n' >&2
  exit 68
fi

source_build_identity="$(
  "$python_bin" - \
    "$source_commit" \
    "$source_archive_sha256" \
    "$dockerfile_sha256" \
    "$lockfile_sha256" \
    "$base_image" \
    "$source_git_pathspecs_json" <<'PY'
import hashlib
import json
import sys

payload = {
    "commit": sys.argv[1],
    "sourceArchiveSha256": sys.argv[2],
    "dockerfileSha256": sys.argv[3],
    "lockfileSha256": sys.argv[4],
    "baseImage": sys.argv[5],
    "gitPathspecs": json.loads(sys.argv[6]),
}
encoded = json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
print(hashlib.sha256(encoded).hexdigest())
PY
)"
git_version="$(git --version)"
tar_version="$(tar --version | head -n 1)"

"$python_bin" - \
  "$output/worker-release-manifest.json" \
  "$source_commit" \
  "$source_archive_sha256" \
  "$source_build_identity" \
  "$dockerfile_sha256" \
  "$lockfile_sha256" \
  "$base_image" \
  "$source_git_pathspecs_json" \
  "$python_version" \
  "$git_version" \
  "$tar_version" <<'PY'
import json
from pathlib import Path
import sys

(
    manifest_path,
    commit,
    archive_sha,
    build_identity,
    dockerfile_sha,
    lockfile_sha,
    base_image,
    git_pathspecs_json,
    python_version,
    git_version,
    tar_version,
) = sys.argv[1:]

manifest = {
    "schemaVersion": 1,
    "artifactKind": "sitesift-worker-source-rollback",
    "repository": {
        "commit": commit,
        "clean": True,
    },
    "source": {
        "archive": "worker-source.tar",
        "archiveSha256": archive_sha,
        "gitPathspecs": json.loads(git_pathspecs_json),
    },
    "build": {
        "sourceBuildIdentity": build_identity,
        "dockerfileSha256": dockerfile_sha,
        "lockfileSha256": lockfile_sha,
        "baseImage": base_image,
        "command": [
            "docker",
            "build",
            "--build-arg",
            f"SOURCE_COMMIT={commit}",
            "--build-arg",
            f"SOURCE_BUILD_IDENTITY={build_identity}",
            ".",
        ],
    },
    "tools": {
        "python": python_version,
        "git": git_version,
        "tar": tar_version,
    },
    "restoreCommands": {
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
    },
}
Path(manifest_path).write_text(
    json.dumps(manifest, indent=2) + "\n",
    encoding="utf-8",
)
PY

printf 'Built deterministic worker source artifact for %s\n' "$source_commit"
