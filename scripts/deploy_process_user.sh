#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/process_user_gcloud_preflight.sh"

ACCOUNT="$PROCESS_USER_APPROVED_ACCOUNT"
PROJECT="$PROCESS_USER_PROJECT"
PROJECT_NUMBER="$PROCESS_USER_PROJECT_NUMBER"
REGION="us-central1"
SERVICE="process-user"
IMAGE_REPOSITORY="${REGION}-docker.pkg.dev/${PROJECT}/cloud-run-source-deploy/${SERVICE}"
SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
MANIFEST_PATH="$REPO_ROOT/docs/release-safety/production-release-manifest.json"
MANIFEST_VALIDATOR="$REPO_ROOT/scripts/verify_release_manifest.py"

mode="dry-run"
case "${1:-}" in
  ""|--dry-run) ;;
  --apply) mode="apply" ;;
  *)
    printf 'Usage: %s [--dry-run|--apply]\n' "$0" >&2
    exit 64
    ;;
esac

process_user_gcloud_preflight local

if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
  printf 'Refusing: deployment checkout must be clean.\n' >&2
  exit 67
fi
controller_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
if [[ ! "$controller_sha" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'Refusing: release-controller HEAD did not resolve to an exact lowercase SHA.\n' >&2
  exit 68
fi

python3 "$MANIFEST_VALIDATOR" "$MANIFEST_PATH" >/dev/null
manifest_fields="$({
  python3 -c '
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
backend = manifest["backend"]
print("{}\t{}\t{}".format(
    backend["candidateSha"],
    backend["candidateCi"]["runId"],
    backend["deploymentConfigHash"],
))
' "$MANIFEST_PATH"
})"
IFS=$'\t' read -r candidate_sha candidate_ci_run_id expected_config_hash <<< "$manifest_fields"
short_sha="${candidate_sha:0:12}"
image_tag="${IMAGE_REPOSITORY}:${short_sha}"
expected_revision="${SERVICE}-${short_sha}"

dry_run_build_command=(
  gcloud builds submit
  --account "$ACCOUNT"
  --project "$PROJECT"
  --tag "$image_tag"
  --suppress-logs
  --format=value\(id\)
  "<candidate-archive:${candidate_sha}>"
)
digest_command=(
  gcloud artifacts docker images describe "$image_tag"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --format=value\(image_summary.digest\)
)
service_readback_command=(
  gcloud run services describe "$SERVICE"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  '--format=json(status.latestCreatedRevisionName,status.traffic)'
)
revision_readback_command=(
  gcloud run revisions describe "$expected_revision"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  '--format=json(metadata.name,spec)'
)

env_vars="^:^FIREBASE_BUCKET=email-automation-cache.firebasestorage.app:ENFORCE_OPENAI_BUDGET=1:USAGE_MONTHLY_BUDGET_USD=100:SITESIFT_AUTO_REPLY_ALLOWLIST=NO7lVYVp6BaplKYEfMlWCgBnpdh2:SITESIFT_OUTBOUND_MODE=paused:SITESIFT_SOURCE_COORDINATOR_MODE=disabled"
secrets="AZURE_API_APP_ID=AZURE_API_APP_ID:latest,AZURE_API_CLIENT_SECRET=AZURE_API_CLIENT_SECRET:latest,FIREBASE_API_KEY=FIREBASE_API_KEY:latest,OPENAI_API_KEY=OPENAI_API_KEY:latest,GOOGLE_OAUTH_CLIENT_ID=GOOGLE_OAUTH_CLIENT_ID:latest,GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest,GOOGLE_REFRESH_TOKEN=GOOGLE_REFRESH_TOKEN:latest"

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

if [[ "$mode" == "dry-run" ]]; then
  printf 'dry-run: zero gcloud commands will execute\n'
  printf 'release controller: %s\n' "$controller_sha"
  printf 'manifest candidate: %s\n' "$candidate_sha"
  printf 'candidate CI run: %s\n' "$candidate_ci_run_id"
  printf 'image tag: %s\n' "$image_tag"
  printf 'expected revision: %s\n' "$expected_revision"
  print_command "${service_readback_command[@]}"
  print_command "${dry_run_build_command[@]}"
  print_command "${digest_command[@]}"
  printf 'deploy image after digest resolution: %s@sha256:<64-hex-digest>\n' "$image_tag"
  print_command "${revision_readback_command[@]}"
  exit 0
fi

controller_ci_run_id="${PROCESS_USER_CONTROLLER_CI_RUN_ID:-}"
if [[ ! "$controller_ci_run_id" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Refusing: PROCESS_USER_CONTROLLER_CI_RUN_ID must identify the successful exact-controller CI run.\n' >&2
  exit 69
fi

python3 "$MANIFEST_VALIDATOR" \
  "$MANIFEST_PATH" \
  --expected-candidate-sha "$candidate_sha" \
  --verify-github \
  --controller-sha "$controller_sha" \
  --controller-ci-run-id "$controller_ci_run_id" >/dev/null

if ! git -C "$REPO_ROOT" cat-file -e "${candidate_sha}^{commit}"; then
  printf 'Refusing: manifest candidate is unavailable in the approved repository.\n' >&2
  exit 70
fi

build_root="$(mktemp -d "${TMPDIR:-/tmp}/sitesift-process-user-candidate.XXXXXX")"
cleanup_build_root() {
  rm -rf -- "$build_root"
}
trap cleanup_build_root EXIT
git -C "$REPO_ROOT" archive --format=tar "$candidate_sha" | tar -xf - -C "$build_root"
if [[ ! -f "$build_root/Dockerfile" || ! -f "$build_root/.gcloudignore" ]]; then
  printf 'Refusing: exact candidate archive is missing deployment inputs.\n' >&2
  exit 71
fi
printf 'candidate archive: %s from %s\n' "$build_root" "$candidate_sha"

build_command=(
  gcloud builds submit
  --account "$ACCOUNT"
  --project "$PROJECT"
  --tag "$image_tag"
  --suppress-logs
  --format=value\(id\)
  "$build_root"
)

process_user_gcloud_preflight apply

predeploy_service_json="$("${service_readback_command[@]}")"
rollback_revision="$(
  python3 -c '
import json
import re
import sys

service = json.load(sys.stdin)
traffic = service.get("status", {}).get("traffic")
if not isinstance(traffic, list):
    raise SystemExit("Refusing: service traffic readback is missing.")
live = [row for row in traffic if row.get("percent", 0) > 0]
if len(live) != 1 or live[0].get("percent") != 100:
    raise SystemExit("Refusing: expected exactly one 100-percent rollback revision.")
revision = live[0].get("revisionName")
if not isinstance(revision, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", revision):
    raise SystemExit("Refusing: live rollback revision is invalid.")
print(revision)
' <<< "$predeploy_service_json"
)"

build_id="$("${build_command[@]}")"
if [[ ! "$build_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  printf 'Refusing to deploy: Cloud Build returned invalid build ID %q.\n' "$build_id" >&2
  exit 72
fi
build_readback_command=(
  gcloud builds describe "$build_id"
  --account "$ACCOUNT"
  --project "$PROJECT"
  '--format=json(id,status,results.images)'
)
build_json="$("${build_readback_command[@]}")"
digest="$(
  python3 -c '
import json
import re
import sys

build = json.load(sys.stdin)
expected_id, expected_name = sys.argv[1:]
if build.get("id") != expected_id:
    raise SystemExit("Refusing to deploy: Cloud Build readback returned the wrong build.")
if build.get("status") != "SUCCESS":
    raise SystemExit("Refusing to deploy: exact Cloud Build did not succeed.")
images = build.get("results", {}).get("images")
if not isinstance(images, list) or len(images) != 1:
    raise SystemExit("Refusing to deploy: exact Cloud Build image result is ambiguous.")
image = images[0]
if not isinstance(image, dict) or image.get("name") != expected_name:
    raise SystemExit("Refusing to deploy: exact Cloud Build produced the wrong image tag.")
digest = image.get("digest")
if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
    raise SystemExit("Refusing to deploy: exact Cloud Build returned an invalid digest.")
print(digest)
' "$build_id" "$image_tag" <<< "$build_json"
)"
tag_digest="$("${digest_command[@]}")"
if [[ ! "$tag_digest" =~ ^sha256:[0-9a-f]{64}$ || "$tag_digest" != "$digest" ]]; then
  printf 'Refusing to deploy: image tag does not resolve to the exact Cloud Build digest.\n' >&2
  exit 73
fi

immutable_image="${image_tag}@${digest}"
canonical_image="${IMAGE_REPOSITORY}@${digest}"
deploy_command=(
  gcloud run deploy "$SERVICE"
  --account "$ACCOUNT"
  --project "$PROJECT"
  --region "$REGION"
  --image "$immutable_image"
  --revision-suffix "$short_sha"
  --command gunicorn
  '--args=--bind=:8080,--workers=1,--threads=8,--max-requests=1,--timeout=0,service:app'
  --service-account "$SERVICE_ACCOUNT"
  --concurrency 1
  --memory 2Gi
  --timeout 540
  --min-instances 0
  --max-instances 10
  --no-allow-unauthenticated
  --update-env-vars "$env_vars"
  --update-secrets "$secrets"
  --no-traffic
  --tag release-a
)
"${deploy_command[@]}"

postdeploy_service_json="$("${service_readback_command[@]}")"
traffic_readback="$(
  python3 -c '
import json
import sys

service = json.load(sys.stdin)
expected_revision, rollback_revision = sys.argv[1:]
status = service.get("status", {})
if status.get("latestCreatedRevisionName") != expected_revision:
    raise SystemExit("Postdeploy mismatch: latest created revision is not the expected commit revision.")
traffic = status.get("traffic")
if not isinstance(traffic, list):
    raise SystemExit("Postdeploy mismatch: service traffic readback is missing.")
created = [row for row in traffic if row.get("revisionName") == expected_revision]
if len(created) != 1 or created[0].get("tag") != "release-a":
    raise SystemExit("Postdeploy mismatch: expected revision is not uniquely tagged release-a.")
created_percent = created[0].get("percent", 0)
if created_percent != 0:
    raise SystemExit("Postdeploy mismatch: the new revision received traffic.")
rollback = [row for row in traffic if row.get("revisionName") == rollback_revision]
if len(rollback) != 1 or rollback[0].get("percent") != 100:
    raise SystemExit("Postdeploy mismatch: rollback revision no longer has 100 percent traffic.")
other_live = [
    row for row in traffic
    if row.get("revisionName") != rollback_revision and row.get("percent", 0) > 0
]
if other_live:
    raise SystemExit("Postdeploy mismatch: unexpected revision has positive traffic.")
print("{}\t{}".format(created_percent, rollback[0].get("percent")))
' "$expected_revision" "$rollback_revision" <<< "$postdeploy_service_json"
)"
IFS=$'\t' read -r created_traffic rollback_traffic <<< "$traffic_readback"

revision_json="$("${revision_readback_command[@]}")"
revision_readback="$(
  python3 -c '
import hashlib
import json
import sys

revision = json.load(sys.stdin)
expected_revision, expected_image, expected_config_hash = sys.argv[1:]
if revision.get("metadata", {}).get("name") != expected_revision:
    raise SystemExit("Postdeploy mismatch: revision readback returned the wrong revision.")
spec = revision.get("spec")
if not isinstance(spec, dict):
    raise SystemExit("Postdeploy mismatch: revision spec is missing.")
containers = spec.get("containers")
if not isinstance(containers, list) or len(containers) != 1:
    raise SystemExit("Postdeploy mismatch: expected exactly one revision container.")
container = containers[0]
if container.get("image") != expected_image:
    raise SystemExit("Postdeploy mismatch: revision image digest differs from the built artifact.")
env_rows = container.get("env")
if not isinstance(env_rows, list):
    raise SystemExit("Postdeploy mismatch: revision environment is missing.")
env = {}
for row in env_rows:
    name = row.get("name") if isinstance(row, dict) else None
    if not isinstance(name, str) or name in env:
        raise SystemExit("Postdeploy mismatch: revision environment is malformed or duplicated.")
    env[name] = row.get("value")
outbound = env.get("SITESIFT_OUTBOUND_MODE")
coordinator = env.get("SITESIFT_SOURCE_COORDINATOR_MODE")
if outbound != "paused":
    raise SystemExit("Postdeploy mismatch: outbound mode is not paused.")
if coordinator != "disabled":
    raise SystemExit("Postdeploy mismatch: coordinator mode is not disabled.")
canonical_spec = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
normalized_spec = json.loads(canonical_spec)
for normalized_container in normalized_spec["containers"]:
    normalized_container["image"] = "IMAGE_DIGEST_BOUND_AT_DEPLOY"
canonical_normalized_spec = json.dumps(
    normalized_spec,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
config_hash = "sha256:" + hashlib.sha256(
    canonical_normalized_spec.encode("utf-8")
).hexdigest()
if config_hash != expected_config_hash:
    raise SystemExit(
        "Postdeploy mismatch: configuration hash differs from the approved manifest."
    )
print(f"{outbound}\t{coordinator}\t{config_hash}")
' "$expected_revision" "$canonical_image" "$expected_config_hash" <<< "$revision_json"
)"
IFS=$'\t' read -r outbound_mode coordinator_mode config_hash <<< "$revision_readback"

post_tag_digest="$("${digest_command[@]}")"
if [[ ! "$post_tag_digest" =~ ^sha256:[0-9a-f]{64}$ || "$post_tag_digest" != "$digest" ]]; then
  printf 'Postdeploy mismatch: commit tag no longer resolves to deployed digest.\n' >&2
  exit 74
fi

printf 'revision: %s\n' "$expected_revision"
printf 'cloud build: %s\n' "$build_id"
printf 'image digest: %s\n' "$digest"
printf 'commit tag: %s\n' "$short_sha"
printf 'config hash: %s\n' "$config_hash"
printf 'outbound mode: %s\n' "$outbound_mode"
printf 'coordinator mode: %s\n' "$coordinator_mode"
printf 'traffic percent: %s\n' "$created_traffic"
printf 'rollback revision: %s\n' "$rollback_revision"
printf 'rollback traffic percent: %s\n' "$rollback_traffic"
printf 'Deployment readback verified for immutable image %s.\n' "$immutable_image"
