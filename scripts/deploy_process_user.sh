#!/usr/bin/env bash
set -euo pipefail

ACCOUNT="bp21harrison@gmail.com"
PROJECT="email-automation-cache"
PROJECT_NUMBER="248289505828"
REGION="us-central1"
SERVICE="process-user"
IMAGE_REPOSITORY="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}"
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

mode="dry-run"
case "${1:-}" in
  ""|--dry-run) ;;
  --apply) mode="apply" ;;
  *)
    printf 'Usage: %s [--dry-run|--apply]\n' "$0" >&2
    exit 64
    ;;
esac

if [[ "${GCLOUD_ACCOUNT:-}" != "$ACCOUNT" ]]; then
  printf 'Refusing: GCLOUD_ACCOUNT must be exactly %s.\n' "$ACCOUNT" >&2
  exit 65
fi
export CLOUDSDK_CORE_ACCOUNT="$ACCOUNT"

if [[ -n "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:-}" ]]; then
  printf 'Refusing: gcloud service-account impersonation must be disabled.\n' >&2
  exit 66
fi

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  printf 'Refusing: deployment checkout must be clean.\n' >&2
  exit 67
fi
short_sha="$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
if [[ ! "$short_sha" =~ ^[0-9a-f]{12}$ ]]; then
  printf 'Refusing: git HEAD did not resolve to a 12-character lowercase SHA.\n' >&2
  exit 68
fi
image_tag="${IMAGE_REPOSITORY}:${short_sha}"

build_command=(
  gcloud builds submit
  --account "$ACCOUNT"
  --project "$PROJECT"
  --tag "$image_tag"
  "$REPO_ROOT"
)
digest_command=(
  gcloud artifacts docker images describe "$image_tag"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --format=value\(image_summary.digest\)
)

env_vars="FIREBASE_BUCKET=email-automation-cache.firebasestorage.app,ENFORCE_OPENAI_BUDGET=1,USAGE_MONTHLY_BUDGET_USD=100"
secrets="AZURE_API_APP_ID=AZURE_API_APP_ID:latest,AZURE_API_CLIENT_SECRET=AZURE_API_CLIENT_SECRET:latest,FIREBASE_API_KEY=FIREBASE_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,GOOGLE_OAUTH_CLIENT_ID=GOOGLE_OAUTH_CLIENT_ID:latest,GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest,GOOGLE_REFRESH_TOKEN=GOOGLE_REFRESH_TOKEN:latest"

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

if [[ "$mode" == "dry-run" ]]; then
  printf 'dry-run: zero gcloud commands will execute\n'
  printf 'image tag: %s\n' "$image_tag"
  print_command "${build_command[@]}"
  print_command "${digest_command[@]}"
  printf 'deploy image after digest resolution: %s@sha256:<64-hex-digest>\n' "$image_tag"
  exit 0
fi

configured_impersonation="$(
  gcloud config get-value auth/impersonate_service_account \
    --account "$ACCOUNT" \
    --project "$PROJECT"
)"
if [[ -n "$configured_impersonation" && "$configured_impersonation" != "(unset)" ]]; then
  printf 'Refusing: gcloud auth/impersonate_service_account must be unset.\n' >&2
  exit 69
fi

auth_accounts="$(
  gcloud auth list \
    --account "$ACCOUNT" \
    --project "$PROJECT" \
    "--filter=account=${ACCOUNT}" \
    "--format=value(account)"
)"
auth_count="$(printf '%s\n' "$auth_accounts" | awk 'NF { count++ } END { print count + 0 }')"
if [[ "$auth_count" != "1" || "$auth_accounts" != "$ACCOUNT" ]]; then
  printf 'Refusing: expected exactly one gcloud auth account matching %s.\n' "$ACCOUNT" >&2
  exit 70
fi

project_info="$(
  gcloud projects describe "$PROJECT" \
    --account "$ACCOUNT" \
    "--format=value(projectNumber,lifecycleState)"
)"
IFS=$'\t' read -r actual_project_number lifecycle_state <<< "$project_info"
if [[ "$actual_project_number" != "$PROJECT_NUMBER" || "$lifecycle_state" != "ACTIVE" ]]; then
  printf 'Refusing: expected project %s number %s ACTIVE; got %s.\n' \
    "$PROJECT" "$PROJECT_NUMBER" "$project_info" >&2
  exit 71
fi

"${build_command[@]}"
digest="$("${digest_command[@]}")"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  printf 'Refusing to deploy: Artifact Registry returned invalid digest %q.\n' "$digest" >&2
  exit 72
fi

immutable_image="${image_tag}@${digest}"
deploy_command=(
  gcloud run deploy "$SERVICE"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --image "$immutable_image"
  --command gunicorn
  --args '--bind=:8080,--workers=1,--threads=8,--timeout=0,service:app'
  --service-account "$SERVICE_ACCOUNT"
  --concurrency 1
  --timeout 540
  --min-instances 0
  --max-instances 10
  --no-allow-unauthenticated
  --set-env-vars "$env_vars"
  --set-secrets "$secrets"
  --no-traffic
  --tag release-a
)
"${deploy_command[@]}"

printf 'Created no-traffic Release A revision from immutable image %s.\n' "$immutable_image"
