#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
mode="dry-run"
manifest=""
target=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      mode="apply"
      shift
      ;;
    --dry-run)
      mode="dry-run"
      shift
      ;;
    --manifest)
      [[ $# -ge 2 ]] || {
        printf 'Refusing worker restore: --manifest needs a path.\n' >&2
        exit 64
      }
      manifest="$2"
      shift 2
      ;;
    --target)
      [[ $# -ge 2 ]] || {
        printf 'Refusing worker restore: --target needs a path.\n' >&2
        exit 64
      }
      target="$2"
      shift 2
      ;;
    *)
      printf 'Refusing worker restore: unknown argument %s.\n' "$1" >&2
      exit 64
      ;;
  esac
done

if [[ -z "$manifest" || -z "$target" ]]; then
  printf 'Usage: %s [--dry-run|--apply] --manifest PATH --target EMPTY_DIR\n' "$0" >&2
  exit 64
fi

bash "$script_dir/verify-worker.sh" --manifest "$manifest"
artifact_dir="$(cd "$(dirname "$manifest")" && pwd -P)"

if [[ "$mode" == "dry-run" ]]; then
  printf 'dry-run: verified worker source; would restore it into %s\n' "$target"
  printf 'Docker daemon capability is intentionally not assumed or executed.\n'
  exit 0
fi

if [[ -e "$target" ]]; then
  if [[ ! -d "$target" || -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'Refusing worker restore: target must not exist or must be empty: %s\n' "$target" >&2
    exit 65
  fi
fi
mkdir -p "$target"
tar -xf "$artifact_dir/worker-source.tar" -C "$target"
printf 'Restored verified worker source into %s; no provider or Docker command executed.\n' "$target"
