#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' "TEST ENVIRONMENT UNAVAILABLE: install uv to bootstrap requirements.lock"
  exit 3
fi

exec uv run \
  --isolated \
  --no-project \
  --with-requirements "${repo_root}/requirements.lock" \
  python "${repo_root}/scripts/run_test_level.py" "$@"
